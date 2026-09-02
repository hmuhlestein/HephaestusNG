"""Spec Kit feature detection and selection (REQ-01..REQ-15, REQ-21..23).

Detects `specs/<NNN>-<name>/` directories (Spec Kit's own convention) as an
alternative input to a hand-written spec.md. Discovery/selection here is
pure and DB-free where possible: discover_speckit_features needs a Session
to resolve repo attribution; resolve_feature_selection does not.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_NEEDS_CLARIFICATION_RE = re.compile(r"\[NEEDS CLARIFICATION:\s*(.*?)\]", re.IGNORECASE | re.DOTALL)

_SIBLING_FILE_NAMES = ("data-model.md", "research.md", "quickstart.md")
_SIBLING_DIR_NAMES = ("contracts", "checklists")


@dataclass
class SpecKitFeature:
    """One detected specs/<NNN>-<name>/ directory, analogous to DesignEntry."""

    dir_path: Path
    number: str
    slug: str
    repo_id: Optional[str]
    repo_label: Optional[str]
    spec_path: Path
    plan_path: Optional[Path] = None
    tasks_path: Optional[Path] = None
    extra_files: List[Path] = field(default_factory=list)

    @property
    def dir_name(self) -> str:
        """The exact specs/ directory name, e.g. '001-checkout-flow' --
        what --feature accepts verbatim and what log/error messages list."""
        return self.dir_path.name


@dataclass
class Candidate:
    """One option resolve_feature_selection could not pick between.
    Exactly one of feature/is_design_md is meaningful."""

    feature: Optional[SpecKitFeature]
    is_design_md: bool = False

    def label(self) -> str:
        if self.is_design_md or self.feature is None:
            return "spec.md"
        return f"{self.feature.number}-{self.feature.slug}"


class SpecKitSelectionError(Exception):
    """Raised when CLI/API input can't unambiguously select one feature."""

    def __init__(self, code: str, message: str, candidates: List[Candidate]):
        self.code = code  # "NOT_FOUND" | "MULTIPLE_FEATURES" | "AMBIGUOUS_REPO" | "BOTH_INPUTS_PRESENT"
        self.message = message
        self.candidates = candidates
        super().__init__(message)


@dataclass
class ReadinessReport:
    feature: SpecKitFeature
    needs_clarification: List[str] = field(default_factory=list)
    missing_files: List[str] = field(default_factory=list)


_DIR_NAME_RE = re.compile(r"^(\d+)-(.+)$")


def _scan_one_repo(specs_root: Path, repo_id: Optional[str], repo_label: Optional[str]) -> List[SpecKitFeature]:
    """Scan one repo's specs/ dir for feature directories. Never raises --
    a missing or unreadable specs/ dir yields [] (NFR-07)."""
    features: List[SpecKitFeature] = []
    try:
        if not specs_root.is_dir():
            return []
        specs_root_resolved = specs_root.resolve()
        for entry in sorted(specs_root.iterdir()):
            if not entry.is_dir():
                continue
            # Security: entry.is_dir() follows symlinks. A top-level
            # symlink under specs/ (e.g. specs/999-x -> /etc or -> ~/.ssh)
            # would otherwise be enumerated as a legitimate SpecKitFeature
            # and later copied wholesale into the agent's worktree by
            # _copy_design_content's shutil.copytree, exposing out-of-tree
            # content to phase prompts (ticket-84a86e68). Reject any entry
            # that is itself a symlink, or whose resolved real path escapes
            # specs_root, before it's ever treated as a candidate feature.
            if entry.is_symlink():
                logger.warning(f"[SPECKIT] skipping symlinked specs/ entry (not a real feature directory): {entry}")
                continue
            try:
                entry_resolved = entry.resolve()
            except OSError as e:
                logger.warning(f"[SPECKIT] failed to resolve {entry}: {e}")
                continue
            if entry_resolved.parent != specs_root_resolved:
                logger.warning(f"[SPECKIT] skipping specs/ entry outside specs root: {entry} -> {entry_resolved}")
                continue
            match = _DIR_NAME_RE.match(entry.name)
            if not match:
                continue
            spec_path = entry / "spec.md"
            if not spec_path.is_file():
                continue
            plan_path = entry / "plan.md"
            tasks_path = entry / "tasks.md"
            extra_files: List[Path] = []
            for name in _SIBLING_FILE_NAMES:
                candidate = entry / name
                if candidate.is_file():
                    extra_files.append(candidate)
            for name in _SIBLING_DIR_NAMES:
                candidate = entry / name
                if candidate.is_dir():
                    extra_files.append(candidate)
            features.append(
                SpecKitFeature(
                    dir_path=entry,
                    number=match.group(1),
                    slug=match.group(2),
                    repo_id=repo_id,
                    repo_label=repo_label,
                    spec_path=spec_path,
                    plan_path=plan_path if plan_path.is_file() else None,
                    tasks_path=tasks_path if tasks_path.is_file() else None,
                    extra_files=extra_files,
                )
            )
    except OSError as e:
        logger.warning(f"[SPECKIT] failed to scan {specs_root}: {e}")
        return []
    return features


def speckit_feature_dir_for_path(spec_path: Path) -> Optional[Path]:
    """If spec_path IS a Spec Kit feature's spec.md (basename "spec.md",
    parent dir matching Spec Kit's <NNN>-<name> convention), return that
    parent dir; else None. Lets any code path that only has a plain file
    path on hand (e.g. an AutopilotDesign.file_path from the existing
    manual file-browser flow, REQ-03) recognize a Spec Kit selection
    without needing its own SpecKitFeature/discovery machinery."""
    if spec_path.name != "spec.md":
        return None
    if not _DIR_NAME_RE.match(spec_path.parent.name):
        return None
    return spec_path.parent


def _sort_key(feature: SpecKitFeature):
    return (feature.repo_label or "", int(feature.number) if feature.number.isdigit() else feature.number)


def discover_speckit_features(db: Session, project_id: str, project_base_dir: str) -> List[SpecKitFeature]:
    """Scan specs/ under every ProjectRepo of the project (REQ-02), plus
    project_base_dir itself -- always when no ProjectRepo rows exist yet,
    and additionally for a multi-repo project whose workspace root is a
    genuinely separate directory from every registered repo (a shared/
    cross-cutting feature spec that doesn't belong to just one child repo,
    e.g. `specify init` run at the workspace root instead of inside a
    specific repo). Skipped when the workspace root already IS one of the
    registered repos (the traditional single-repo case), which would
    otherwise double-count that repo's own features. Returns features
    sorted by (repo_label, number). Never raises (NFR-07)."""
    from src.core.repo_resolution import get_project_repos

    features: List[SpecKitFeature] = []
    repos = get_project_repos(db, project_id)
    for repo in repos:
        features.extend(_scan_one_repo(Path(repo.path) / "specs", str(repo.id), str(repo.label)))

    base_dir_resolved = Path(project_base_dir).resolve()
    already_covered = any(Path(repo.path).resolve() == base_dir_resolved for repo in repos)
    if not already_covered:
        features.extend(_scan_one_repo(base_dir_resolved / "specs", None, None))

    return sorted(features, key=_sort_key)


def discover_speckit_features_unregistered(project_base_dir: str) -> List[SpecKitFeature]:
    """Fallback for an unregistered project (no AutopilotProject/ProjectRepo
    rows yet). Scans ONLY project_base_dir/specs/ -- single-repo semantics.
    repo_id/repo_label are always None on the returned features."""
    features = _scan_one_repo(Path(project_base_dir) / "specs", None, None)
    return sorted(features, key=_sort_key)


def _match_feature_arg(features: List[SpecKitFeature], feature_arg: str) -> List[SpecKitFeature]:
    """Full-name match, or exact zero-padded numeric-prefix match (REQ-11,
    Gotcha #6 -- must not partial-match, "001" must not match "0012-y")."""
    exact = [f for f in features if f"{f.number}-{f.slug}" == feature_arg]
    if exact:
        return exact
    return [f for f in features if f.number == feature_arg]


def resolve_feature_selection(
    features: List[SpecKitFeature],
    feature_arg: Optional[str],
    repo_arg: Optional[str],
    design_md_present: bool,
) -> SpecKitFeature:
    """Single choke point for REQ-10/11/12/13's selection rules. Raises
    SpecKitSelectionError (never returns None) for every ambiguous case."""
    if feature_arg is not None:
        matches = _match_feature_arg(features, feature_arg)
        if not matches:
            raise SpecKitSelectionError("NOT_FOUND", f"No Spec Kit feature matches --feature {feature_arg!r}", [])

        distinct_repos = {m.repo_id for m in matches}
        if len(distinct_repos) > 1 and repo_arg is None:
            raise SpecKitSelectionError(
                "AMBIGUOUS_REPO",
                f"--feature {feature_arg!r} matches features in multiple repos; pass --repo to disambiguate",
                [Candidate(feature=m) for m in matches],
            )

        if repo_arg is not None:
            matches = [m for m in matches if m.repo_label == repo_arg]
            if not matches:
                raise SpecKitSelectionError(
                    "NOT_FOUND", f"No Spec Kit feature matches --feature {feature_arg!r} --repo {repo_arg!r}", []
                )

        return matches[0]

    if not features:
        if not design_md_present:
            raise SpecKitSelectionError("NOT_FOUND", "No Spec Kit feature or spec.md found", [])
        raise SpecKitSelectionError("NOT_FOUND", "No Spec Kit feature found (spec.md exists -- use that path directly)", [])

    if len(features) >= 2:
        candidates = [Candidate(feature=f) for f in features]
        if design_md_present:
            candidates.append(Candidate(feature=None, is_design_md=True))
        raise SpecKitSelectionError(
            "MULTIPLE_FEATURES", "Multiple Spec Kit features found; pass --feature to select one", candidates
        )

    if design_md_present:
        raise SpecKitSelectionError(
            "BOTH_INPUTS_PRESENT",
            "Both a Spec Kit feature and spec.md are present; pass --feature or a design document path to select one",
            [Candidate(feature=features[0]), Candidate(feature=None, is_design_md=True)],
        )

    return features[0]


def check_feature_readiness(feature: SpecKitFeature) -> ReadinessReport:
    """REQ-15: voluntary check, zero effect on start(). Parses spec.md for
    [NEEDS CLARIFICATION: ...] markers (best-effort per NFR-07) and checks
    plan.md/tasks.md presence."""
    needs_clarification: List[str] = []
    try:
        content = feature.spec_path.read_text(encoding="utf-8")
        needs_clarification = [m.strip() for m in _NEEDS_CLARIFICATION_RE.findall(content)]
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError is a ValueError subclass, not an OSError one --
        # a plain `except OSError` here never actually caught non-UTF-8
        # spec.md content despite this function's own "best-effort, never
        # raise" contract (NFR-07); found via a test exercising exactly
        # this case.
        logger.warning(f"[SPECKIT] failed to read {feature.spec_path} for readiness check: {e}")

    missing_files: List[str] = []
    if feature.plan_path is None:
        missing_files.append("plan.md")
    if feature.tasks_path is None:
        missing_files.append("tasks.md")

    return ReadinessReport(feature=feature, needs_clarification=needs_clarification, missing_files=missing_files)
