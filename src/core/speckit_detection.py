"""Read-only Spec Kit (specs/<NNN>-<name>/) discovery. No DB writes.

Shared by CLI pre-flight, the dashboard detection route, and the
voluntary readiness-check command -- one implementation, three callers,
per the feature's Awareness Model (detection must never diverge by
caller). REQ-01/REQ-02/REQ-11/REQ-12/REQ-18.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from src.core.repo_resolution import get_project_repos

logger = logging.getLogger(__name__)

_DIR_RE = re.compile(r"^(\d+)-(.+)$")
_NEEDS_CLARIFICATION_RE = re.compile(r"\[NEEDS CLARIFICATION:([^\]]*)\]")
_SPEC_KIT_OPTIONAL_FILES = ("plan.md", "tasks.md", "data-model.md", "research.md", "quickstart.md")
_SPEC_KIT_OPTIONAL_DIRS = ("contracts", "checklists")


@dataclass(frozen=True)
class SpecKitFeature:
    """One specs/<NNN>-<name>/ directory. Immutable snapshot of a single
    detection pass -- never cached across calls, since a user may run
    /speckit.plan between two detections in the same session."""

    dir_path: Path
    number: str
    slug: str
    repo_id: str
    repo_label: str
    has_plan: bool
    has_tasks: bool
    extra_files: List[str] = field(default_factory=list)

    @property
    def dir_name(self) -> str:
        """The exact specs/ directory name, e.g. '001-checkout-flow' --
        what --feature accepts verbatim and what CLI error messages list."""
        return self.dir_path.name


@dataclass(frozen=True)
class ReadinessIssue:
    """Not an exception -- a plain, immutable report row. Readiness
    checking never raises for an incomplete spec; incompleteness IS the
    expected report content (FR-011: advisory only, never a gate)."""

    kind: str
    detail: str


class NoSpecKitFeatureError(Exception):
    """No detected feature matches the given selector. .selector holds
    the offending --feature/--design-doc argument for the caller's error
    message."""

    def __init__(self, selector: str):
        self.selector = selector
        super().__init__(f"No Spec Kit feature matches {selector!r}")


class AmbiguousSpecKitFeatureError(Exception):
    """More than one candidate matches; caller MUST require a more
    specific selector (--feature, or --feature + --repo) rather than
    picking one -- NFR-03's 'never silently resolved' guarantee lives
    here, at the one chokepoint both the CLI and the dashboard route
    call through."""

    def __init__(self, candidates: List[SpecKitFeature]):
        self.candidates = candidates
        super().__init__(f"{len(candidates)} Spec Kit features match — repo/feature selector required")


def find_speckit_features(db: Session, project_id: str) -> List[SpecKitFeature]:
    """Scan every ProjectRepo of project_id for specs/*/spec.md. Returns
    all matches across all repos, stably ordered (repo_label, then
    number) for deterministic CLI/dashboard listings.

    ERROR ISOLATION: a filesystem error scanning ONE repo is caught and
    logged as a WARNING internally, and that repo is simply skipped for
    this call -- it never aborts the scan of the other repos, and it
    never raises out of this function for filesystem-level problems. This
    is all-or-nothing per repo: a repo's partial results (e.g. an error
    raised after some but not all of its features were found) are
    discarded entirely rather than silently returned as if complete.
    """
    features: List[SpecKitFeature] = []
    for repo in get_project_repos(db, project_id):
        repo_features: List[SpecKitFeature] = []
        try:
            specs_dir = Path(repo.path) / "specs"
            if not specs_dir.is_dir():
                continue
            for spec_file in specs_dir.glob("*/spec.md"):
                feature_dir = spec_file.parent
                match = _DIR_RE.match(feature_dir.name)
                if not match:
                    continue
                number, slug = match.group(1), match.group(2)
                extra_files = [name for name in _SPEC_KIT_OPTIONAL_FILES if (feature_dir / name).exists()]
                extra_files += [name for name in _SPEC_KIT_OPTIONAL_DIRS if (feature_dir / name).is_dir()]
                repo_features.append(
                    SpecKitFeature(
                        dir_path=feature_dir,
                        number=number,
                        slug=slug,
                        repo_id=str(repo.id),
                        repo_label=str(repo.label),
                        has_plan="plan.md" in extra_files,
                        has_tasks="tasks.md" in extra_files,
                        extra_files=extra_files,
                    )
                )
        except OSError:
            logger.warning(f"[SPECKIT-DETECTION] failed scanning repo {repo.label!r} ({repo.path!r}) for Spec Kit features", exc_info=True)
            continue

        features.extend(repo_features)

    features.sort(key=lambda f: (f.repo_label, int(f.number), f.number))
    return features


def select_speckit_feature(
    features: List[SpecKitFeature],
    feature_arg: Optional[str],
    repo_label: Optional[str],
) -> SpecKitFeature:
    """Resolve --feature/--repo against a detection list. Pure function,
    no I/O -- testable without a DB or filesystem.

    Raises:
        NoSpecKitFeatureError: features is empty, or feature_arg matches nothing.
        AmbiguousSpecKitFeatureError: feature_arg is None and len(features) > 1,
            OR feature_arg matches >1 repo and repo_label is None (FR-023).
    """
    if not features:
        raise NoSpecKitFeatureError(feature_arg or "<none>")

    if feature_arg is None:
        if len(features) > 1:
            raise AmbiguousSpecKitFeatureError(features)
        return features[0]

    candidates = [f for f in features if f.dir_name == feature_arg or f.number == feature_arg]
    if not candidates:
        raise NoSpecKitFeatureError(feature_arg)

    if repo_label is not None:
        candidates = [f for f in candidates if f.repo_label == repo_label]
        if not candidates:
            raise NoSpecKitFeatureError(feature_arg)

    if len(candidates) > 1:
        raise AmbiguousSpecKitFeatureError(candidates)

    return candidates[0]


def check_readiness(feature: SpecKitFeature) -> List[ReadinessIssue]:
    """Missing-file check (plan.md absent -> ReadinessIssue) + a
    [NEEDS CLARIFICATION: ...] regex scan of spec.md. Never raises for
    normal incompleteness -- an empty list means "fully ready."

    Raises:
        OSError: spec.md exists per detection but became unreadable
            between find_speckit_features and this call -- including
            spec.md having been replaced by content that is not valid
            UTF-8, which is surfaced as OSError (not UnicodeDecodeError)
            so callers have exactly one exception type to catch.
    """
    issues: List[ReadinessIssue] = []

    if not feature.has_plan:
        issues.append(ReadinessIssue(kind="missing_file", detail="plan.md missing"))

    spec_path = feature.dir_path / "spec.md"
    try:
        spec_text = spec_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        logger.warning(f"[SPECKIT-DETECTION] {spec_path} is not valid UTF-8", exc_info=True)
        raise OSError(f"{spec_path} is not valid UTF-8") from e

    for match in _NEEDS_CLARIFICATION_RE.finditer(spec_text):
        issues.append(ReadinessIssue(kind="needs_clarification", detail=match.group(0)))

    return issues
