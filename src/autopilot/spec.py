"""Hybrid completion gate for the autopilot pipeline (design §9.1).

Combines a machine-checkable spec (hard floors) with the agent's own subjective
judgement to produce the `score` that drives the engine's evaluation points
(goto/retry/continue) after the QA and product-validation phases.

The spec lives at ~/.hephaestus/autopilot/qa_spec.json (per-project) and is also
copied into each worktree's .hephaestus/ so agents can read it. Phase 7/8 agents
emit an OKF report (qa.md / validation.md) which this module
scores against the spec.

Score bands map onto the autopilot evaluation_points thresholds:
    score < 0.3  -> goto architecture  (fundamental problem)
    score < 0.7  -> goto development   (code-level problem)
    score >= 0.7 -> continue           (passes the gate)
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from src.autopilot.okf_markdown import read_okf
from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME

logger = logging.getLogger(__name__)

SPEC_PATH = Path(AUTOPILOT_STATE_DIR) / "qa_spec.json"

DEFAULT_SPEC: Dict[str, Any] = {
    "max_failed_tests": 0,
    "max_critical_issues": 0,
    "required_pass_rate": 100,  # percent of tests that must pass
    "min_requirements_met_rate": 100,  # percent of requirements that must be met
    "max_minor_unmet_requirements": 2,  # PASS_WITH_MINOR_GAPS tolerance, see score_product_validation
}

_WORKFLOWS_DIR = Path(__file__).parent.parent.parent / "config" / "workflows"


def _load_gated_phases() -> Tuple[str, ...]:
    """Phases gated by the hybrid spec (engine evaluation point keys).

    Read from each phase's own YAML file (`spec_gate: true`) instead of a
    hardcoded tuple here -- the two used to be able to drift silently.
    architectural_review/adversarial_review were added to a hardcoded tuple
    only after discovering their workflow.yaml evaluation_points (score<0.3
    -> goto architecture_design, score<0.6 -> goto development) had never
    actually fired: build_phase_output returned {} for any phase not in the
    tuple, so the heuristic evaluator's json.dumps({}) scan found zero
    keywords and fell through to its baseline 0.75 ("pass") every time --
    regardless of how many BLOCKERs a review found. Observed live: an
    adversarial review reporting 6 BLOCKERs still completed with
    action="continue", because the score that would have triggered the
    goto-back-to-development condition was never computed from real
    content. Declaring the gate on the phase's own file (next to its
    `outputs:`/`required_output:` declarations, the same place a phase
    author already looks) removes the second place that has to be kept in
    sync by hand.
    """
    gated = []
    try:
        if not _WORKFLOWS_DIR.exists():
            return ()
        workflow_dirs = sorted(_WORKFLOWS_DIR.iterdir())
    except OSError as e:
        # A filesystem hiccup here must not crash `import src.autopilot.spec`
        # (this module is imported by orchestrator.py and
        # task_completion_service.py) -- degrade to "no gated phases" instead
        # of taking the whole app down at startup.
        logger.error(f"Could not list {_WORKFLOWS_DIR} for spec_gate scan: {e}")
        return ()
    for workflow_dir in workflow_dirs:
        if not workflow_dir.is_dir():
            continue
        for phase_file in sorted(workflow_dir.glob("*.yaml")):
            if phase_file.name == "workflow.yaml":
                continue
            try:
                phase_cfg = yaml.safe_load(phase_file.read_text())
            except Exception as e:
                logger.warning(f"Could not parse {phase_file} while scanning for spec_gate: {e}")
                continue
            if isinstance(phase_cfg, dict) and phase_cfg.get("spec_gate") and phase_cfg.get("name"):
                gated.append(phase_cfg["name"])
    return tuple(gated)


GATED_PHASES = _load_gated_phases()

# Single-file overrides per phase, keyed by phase name — used when a phase's
# real output lives somewhere its own declared `outputs:` list doesn't
# literally spell out (e.g. Phase 0's Feature Architect writes to the
# git-excluded .hephaestus/ dir). Loaded from workflow.yaml's
# `required_output:` block. Every other phase's hard floor is now derived
# directly from its own YAML `outputs:` list (see get_phase_required_files
# below) instead of a hardcoded dict — previously only 4 of ~11 phases had
# any output-artifact enforcement at all, so e.g. adversarial_review and
# security_review could silently skip producing their declared report with
# zero consequence.
DEFAULT_PHASE_OUTPUT_ARTIFACTS: Dict[str, str] = {}

# Runtime-loaded artifacts from workflow.yaml
PHASE_OUTPUT_ARTIFACTS = dict(DEFAULT_PHASE_OUTPUT_ARTIFACTS)

# Matches entries in a phase's declared `outputs:` YAML list that look like a
# real, existence-checkable filename (has an extension, no spaces) as
# opposed to a descriptive, non-file deliverable ("source code in project
# path", "pull request created and merged") that can't be checked this way.
_FILENAME_RE = re.compile(r"^[\w.][\w./\-]*\.[A-Za-z0-9]+$")


def _extract_declared_files(outputs: Any) -> list:
    """Normalise a Phase.outputs DB value (a list, or a JSON-ish/repr-ish
    string of one, depending on how it was written) into a list of real,
    checkable filenames."""
    if isinstance(outputs, str):
        try:
            outputs = json.loads(outputs)
        except Exception:
            try:
                import ast

                outputs = ast.literal_eval(outputs)
            except Exception:
                outputs = [outputs]
    if not isinstance(outputs, list):
        return []
    return [
        o.strip()
        for o in outputs
        if isinstance(o, str) and _FILENAME_RE.match(o.strip())
    ]


def get_phase_required_files(phase: Any, workflow_id: Optional[str] = None) -> list:
    """The list of output files `phase` must produce for its 'done' claim
    to be accepted, derived from its own YAML-declared `outputs:` (stored on
    Phase.outputs), with an optional override from workflow.yaml's
    `required_output:` block (string or list).
    """
    override = load_phase_output_artifacts(workflow_id).get(phase.name)
    if override:
        # Support both string and list values
        if isinstance(override, list):
            return override
        return [override]
    return _extract_declared_files(getattr(phase, "outputs", None))


# A declared output's current documented filename, mapped to the older name
# some phases used before the OKF (docs.md/summary.md/etc.) convention --
# both are accepted so an agent (or a report written before the rename)
# still resolves. Single source of truth: this used to be defined
# byte-identically in two places in src/services/task_completion/
# verification.py (verify_output_artifact and verify_output_survived_commit),
# a duplication that module's own docstring flagged as deliberate pending
# this consolidation (Phase 2 §4.9).
OUTPUT_NAME_ALIASES: Dict[str, str] = {
    "docs.md": "doc_review_report.md",
    "summary.md": "code_summary.md",
    "security.md": "security_report.md",
    "review.md": "architectural_review_report.md",
    "adversarial.md": "adversarial_review_report.md",
    "qa.md": "qa_report.md",
    "validation.md": "product_validation.md",
    "requirements.md": "requirements_analysis.md",
    "scope.md": "scope_review_result.md",
    "forensics.md": "forensics_report.md",
}


def resolve_declared_output_path(
    working_directory: str, phase_name: str, declared_output: str
) -> Optional[Path]:
    """Find where a phase's declared output file actually landed in its
    worktree, trying every sanctioned location and old-name alias in the
    same order verify_output_artifact/verify_output_survived_commit have
    always searched. Returns the first candidate that exists, or None.

    Search order (first match wins, current name checked before its old
    alias at each location):
      1. .hephaestus/<phase_name>/<name> -- the one sanctioned location
         each gated phase's own CRITICAL PATH RULE tells it to write to,
         checked first rather than guessed at.
      2. <working_directory>/<name>
      3. .hephaestus/<name> (flat, no phase subfolder) -- ONLY for a
         non-gated phase (e.g. Phase 0's Feature Architect writes internal
         orchestration artifacts here directly), or a gated phase whose
         own GATE_RESULT_SUBDIR override IS this flat location (none,
         currently -- see that dict's own comment). For every gated phase
         this candidate is skipped: read_okf_report -- the search that
         actually SCORES a report -- only ever checks
         .hephaestus/<phase_name>/ and the worktree root for those phases,
         never flat .hephaestus/, so accepting a flat-.hephaestus/ report
         as "found" here produced exactly the same class of bug as the
         docs/ case below: passes this existence check, then silently
         mis-scores as "no report" at actual gate evaluation (Phase 2
         §4.9, confirmed live: build_phase_output returned
         result_missing=True for a qa.md placed only at flat
         .hephaestus/qa.md, which this function used to accept).
         feature_review was the one gated phase this used to matter for
         (a flat .hephaestus/review.md that also collided in name with
         architectural_review's own review.md) until normalized onto this
         same .hephaestus/<phase_name>/ convention.

    Deliberately does NOT check docs/<name> (Phase 2 §4.9): every gated
    phase's own prompt explicitly forbids writing there ("Write ALL
    reports to Artifacts Path (.hephaestus/) -- NOT the project root, NOT
    ./docs/"), and read_okf_report never accepted it either. Same
    reasoning as candidate 3 above -- rejecting it immediately here, with
    a clear "missing" message while the completing agent still has
    context to fix it, beats a confusing async failure downstream, and
    makes this function agree with what scoring will actually accept.

    Does not check the feature-gallery archive or git history -- those are
    separate fallback layers only one of this function's two callers uses
    (see verify_output_artifact's feature_dir search and
    verify_output_survived_commit's git-history search respectively);
    kept out of this shared function rather than forced to fit both.
    """
    base = Path(working_directory)
    old_name = OUTPUT_NAME_ALIASES.get(declared_output)
    names_to_check = [declared_output] + ([old_name] if old_name else [])
    # See candidate 3's docstring paragraph above -- flat .hephaestus/ is
    # only a real scoring-time location for a non-gated phase, or a gated
    # phase whose GATE_RESULT_SUBDIR override IS that flat location (none
    # currently -- kept for a future phase that might genuinely need it).
    gate_excludes_flat = (
        phase_name in GATED_PHASES
        and GATE_RESULT_SUBDIR.get(phase_name) != CONTEXT_DIR_NAME
    )
    for name in names_to_check:
        candidates = [
            base / CONTEXT_DIR_NAME / phase_name / name,
            base / name,
        ]
        # The exclusion above is about the FLAT location, so test the path
        # this candidate actually produces rather than the phase's gated-ness
        # alone. A declared name that already carries its own subdirectory
        # ("security_review/security.md") makes this candidate
        # .hephaestus/security_review/security.md -- not flat at all, and
        # exactly the file read_okf_report scores -- so excluding it creates
        # the opposite of the bug the exclusion exists to prevent: the report
        # sits in the right place and is reported missing.
        #
        # This is not hypothetical. Phase.outputs is snapshotted into the DB
        # when a workflow is created and never re-read from YAML, while
        # GATED_PHASES is read from YAML at import -- so the moment
        # security_review became a gated phase, every workflow already
        # in flight kept its old "security_review/security.md" declaration
        # and could no longer complete the phase at all. Correcting the YAML
        # to the bare filename fixes new workflows; only this fixes the ones
        # already running.
        if not (gate_excludes_flat and Path(name).parent == Path(".")):
            candidates.append(base / CONTEXT_DIR_NAME / name)
        for candidate in candidates:
            if candidate.exists():
                return candidate
    if phase_name == "feature_review" and declared_output == "feature_review.md":
        # TEMPORARY (Phase 2 §4.9 follow-up) -- see
        # _feature_review_legacy_report's own docstring. An in-flight
        # Phase 0 run started before the normalization may still be
        # writing to the old flat .hephaestus/review.md; without this the
        # existence check would reject it as "missing" before the report
        # ever reaches scoring.
        legacy = base / CONTEXT_DIR_NAME / "review.md"
        if legacy.exists():
            return legacy
    return None


# Per-workflow-definition caches for the two loaders below, keyed by
# Workflow.definition_id. A workflow.yaml's required_output/optional_phases
# don't change during the process's lifetime, so caching is safe -- but it
# must be keyed per definition, not merged into one shared dict/set. The
# previous implementation mutated PHASE_OUTPUT_ARTIFACTS/OPTIONAL_PHASES
# in place (.update() / reassignment) every time ANY workflow was queried,
# so one workflow definition's overrides leaked into every other workflow
# definition's lookups for the rest of the process's life (e.g. two
# unrelated workflow.yaml files declaring a same-named phase with different
# required_output would silently clobber each other, last-loaded-wins).
_PHASE_OUTPUT_ARTIFACTS_CACHE: Dict[str, dict] = {}
_OPTIONAL_PHASES_CACHE: Dict[str, set] = {}
_MAX_REVIEW_RUNS_CACHE: Dict[tuple, Optional[int]] = {}


def load_phase_output_artifacts(workflow_id: Optional[str] = None) -> dict:
    """Load required_output artifacts from workflow.yaml if available.

    Falls back to DEFAULT_PHASE_OUTPUT_ARTIFACTS if workflow_id is None
    or workflow.yaml doesn't have required_output config.
    """
    if workflow_id is None:
        return PHASE_OUTPUT_ARTIFACTS

    try:
        from src.core.database import DatabaseManager, Workflow

        db = DatabaseManager(None)
        session = db.get_session()
        try:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if not wf or not wf.definition_id:
                return PHASE_OUTPUT_ARTIFACTS

            cached = _PHASE_OUTPUT_ARTIFACTS_CACHE.get(wf.definition_id)
            if cached is not None:
                return cached

            # Load workflow definition from YAML
            from src.workflow_registry import _WORKFLOWS_DIR

            wf_dir = _WORKFLOWS_DIR / wf.definition_id
            workflow_yaml = wf_dir / "workflow.yaml"
            merged = dict(DEFAULT_PHASE_OUTPUT_ARTIFACTS)
            if workflow_yaml.exists():
                import yaml

                with open(workflow_yaml) as f:
                    wf_config = yaml.safe_load(f)
                if wf_config and "required_output" in wf_config:
                    merged.update(wf_config["required_output"])
                    logger.info(
                        f"Loaded required_output from workflow.yaml: {merged}"
                    )
            _PHASE_OUTPUT_ARTIFACTS_CACHE[wf.definition_id] = merged
            return merged
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not load required_output from workflow.yaml: {e}")

    return PHASE_OUTPUT_ARTIFACTS


# Optional phases that can fail without blocking the pipeline
OPTIONAL_PHASES = {"forensics_analysis", "git_expert"}


def load_optional_phases(workflow_id: Optional[str] = None) -> set:
    """Load optional_phases from workflow.yaml if available."""
    if workflow_id is None:
        return OPTIONAL_PHASES

    try:
        from src.core.database import DatabaseManager, Workflow

        db = DatabaseManager(None)
        session = db.get_session()
        try:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if not wf or not wf.definition_id:
                return OPTIONAL_PHASES

            cached = _OPTIONAL_PHASES_CACHE.get(wf.definition_id)
            if cached is not None:
                return cached

            from src.workflow_registry import _WORKFLOWS_DIR

            wf_dir = _WORKFLOWS_DIR / wf.definition_id
            workflow_yaml = wf_dir / "workflow.yaml"
            result = OPTIONAL_PHASES
            if workflow_yaml.exists():
                import yaml

                with open(workflow_yaml) as f:
                    wf_config = yaml.safe_load(f)
                if wf_config and "optional_phases" in wf_config:
                    result = set(wf_config["optional_phases"])
                    logger.info(
                        f"Loaded optional_phases from workflow.yaml: {result}"
                    )
            _OPTIONAL_PHASES_CACHE[wf.definition_id] = result
            return result
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not load optional_phases from workflow.yaml: {e}")

    return OPTIONAL_PHASES


def get_max_review_runs(workflow_id: Optional[str], phase_name: str) -> Optional[int]:
    """Opt-in cap on how many times `phase_name` may itself re-run
    (workflow.yaml's eval_point `max_review_runs`) before
    _create_phase_task force-completes it "with caveats" instead of
    spawning yet another review agent.

    Distinct from max_retries, which bounds the GOTO TARGET's retries
    (e.g. development) -- this bounds the REVIEW phase's own re-entry
    count. None (the default for every phase that doesn't set this in
    workflow.yaml) means uncapped, same as today.
    """
    if not workflow_id:
        return None

    try:
        from src.core.database import Workflow, get_db

        with get_db() as session:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if not wf or not wf.definition_id:
                return None

            cache_key = (wf.definition_id, phase_name)
            if cache_key in _MAX_REVIEW_RUNS_CACHE:
                return _MAX_REVIEW_RUNS_CACHE[cache_key]

            from src.workflow_registry import _WORKFLOWS_DIR

            wf_dir = _WORKFLOWS_DIR / wf.definition_id
            workflow_yaml = wf_dir / "workflow.yaml"
            value = None
            if workflow_yaml.exists():
                import yaml

                with open(workflow_yaml) as f:
                    wf_config = yaml.safe_load(f)
                eval_points = (wf_config or {}).get("orchestrator", {}).get(
                    "evaluation_points", []
                )
                for ep in eval_points:
                    if ep.get("after_phase") == phase_name:
                        value = ep.get("max_review_runs")
                        break
            _MAX_REVIEW_RUNS_CACHE[cache_key] = value
            return value
    except Exception as e:
        logger.debug(f"Could not load max_review_runs for {phase_name}: {e}")
        return None


def get_review_findings_history(workflow_id: str, phase_name: str) -> list:
    """Findings recorded from prior runs of `phase_name` within this
    workflow (see record_review_finding), oldest first. Empty list if none
    recorded yet -- the first run, or a phase that doesn't opt into
    max_review_runs.
    """
    from src.core.database import ProjectContext, get_db

    with get_db() as session:
        row = (
            session.query(ProjectContext)
            .filter_by(key=f"review_findings:{workflow_id}:{phase_name}")
            .first()
        )
        return list(row.value) if row and row.value else []


_PHASE_INPUTS_CACHE: Dict[str, dict] = {}


def load_phase_inputs(workflow_id: Optional[str] = None) -> dict:
    """workflow.yaml's `phase_inputs:` block: {phase_name: {required: [...],
    optional: [...]}}.

    Read from disk per definition_id, the same way load_phase_output_artifacts
    reads `required_output:`, and for the same reason: Phase.outputs and
    friends are snapshotted into the DB at workflow-creation time and never
    re-read, so anything declared per-phase reaches only workflows created
    afterwards. Runs already in flight are exactly the ones most likely to be
    missing an input.
    """
    if workflow_id is None:
        return {}
    try:
        from src.core.database import DatabaseManager, Workflow

        session = DatabaseManager(None).get_session()
        try:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if not wf or not wf.definition_id:
                return {}
            cached = _PHASE_INPUTS_CACHE.get(wf.definition_id)
            if cached is not None:
                return cached
            from src.workflow_registry import _WORKFLOWS_DIR

            workflow_yaml = _WORKFLOWS_DIR / wf.definition_id / "workflow.yaml"
            declared = {}
            if workflow_yaml.exists():
                cfg = yaml.safe_load(workflow_yaml.read_text()) or {}
                declared = cfg.get("phase_inputs") or {}
            _PHASE_INPUTS_CACHE[wf.definition_id] = declared
            return declared
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not load phase_inputs from workflow.yaml: {e}")
        return {}


_INPUT_PRODUCERS_CACHE: Dict[str, Dict[str, list]] = {}


def input_producer_phases(workflow_id: Optional[str], filename: str) -> list:
    """Which phases are documented to write `filename`, so a consumer knows
    which .hephaestus/<phase_name>/ subdirectories to look in.

    Built from the workflow definition's OWN declarations -- each phase
    YAML's `outputs:` list plus workflow.yaml's `required_output:` block --
    rather than a second hand-maintained filename->phase table that would
    drift out of sync with them. Read from disk, not the DB, for the reason
    load_phase_inputs documents.

    Deliberately NOT a directory scan of .hephaestus/*/: iterating whatever
    subdirectory happens to contain a same-named file risks picking up a
    stale copy from an earlier retry pass, since filesystem order is not
    "most recent" -- the same trap read_okf_report's docstring calls out.
    """
    if workflow_id is None:
        return []
    try:
        from src.core.database import DatabaseManager, Workflow

        session = DatabaseManager(None).get_session()
        try:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if not wf or not wf.definition_id:
                return []
            cached = _INPUT_PRODUCERS_CACHE.get(wf.definition_id)
            if cached is None:
                from src.workflow_registry import _WORKFLOWS_DIR

                wf_dir = _WORKFLOWS_DIR / wf.definition_id
                cached = {}

                def _record(basename: str, phase: str) -> None:
                    cached.setdefault(basename, [])
                    if phase not in cached[basename]:
                        cached[basename].append(phase)

                for phase_file in sorted(wf_dir.glob("*.yaml")):
                    cfg = yaml.safe_load(phase_file.read_text()) or {}
                    if phase_file.name == "workflow.yaml":
                        for phase, declared in (cfg.get("required_output") or {}).items():
                            entries = declared if isinstance(declared, list) else [declared]
                            for entry in entries:
                                _record(Path(entry).name, phase)
                        continue
                    name = cfg.get("name")
                    if not name:
                        continue
                    for entry in _extract_declared_files(cfg.get("outputs")):
                        _record(Path(entry).name, name)
                _INPUT_PRODUCERS_CACHE[wf.definition_id] = cached
            return list(cached.get(filename, []))
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not resolve producer phases for {filename}: {e}")
        return []


def resolve_phase_input(
    working_directory: Any, filename: str, workflow_id: Optional[str] = None
) -> Optional[Path]:
    """Find an input file one phase produced and another consumes.

    Checks every .hephaestus/<phase_name>/ subdirectory that a phase is
    actually documented to write this filename into, then the flat
    .hephaestus/ location, then the worktree root -- plus each candidate's
    old-name alias, the same table read_okf_report and
    resolve_declared_output_path use.

    Unlike resolve_declared_output_path (which knows which phase it is
    checking, and must NOT accept a gated phase's report from the flat
    location) this is a consumer-side lookup: the reader does not care which
    phase wrote the file or whether that phase was gated, only where it is.
    Accepting the flat location here is therefore correct, not a loosening --
    nothing scores off this result.
    """
    base = Path(working_directory)
    names = [filename]
    old_name = OUTPUT_NAME_ALIASES.get(filename)
    if old_name:
        names.append(old_name)
    producers = input_producer_phases(workflow_id, filename)
    for name in names:
        candidates = [base / CONTEXT_DIR_NAME / producer / name for producer in producers]
        candidates.append(base / CONTEXT_DIR_NAME / name)
        candidates.append(base / name)
        for candidate in candidates:
            if candidate.exists():
                return candidate
    return None


def build_input_manifest(
    workflow_id: Optional[str], phase_name: str, working_directory: Any
) -> str:
    """A concrete, per-run list of which declared inputs actually exist right
    now, for injection into the phase's task description.

    Returns "" when the phase declares no inputs or nothing can be resolved,
    so callers can concatenate unconditionally.

    This is the consumer-side counterpart to verify_output_artifact: outputs
    have been checked for existence at completion for a while, inputs never
    were at dispatch. A phase whose input is missing -- rewound by a goto,
    deleted by consume_gate_artifacts, or never produced because an optional
    phase was skipped -- previously found out by cat-ing a path and getting
    nothing, with no way to tell "not produced this run" from "I guessed the
    path wrong".
    """
    declared = load_phase_inputs(workflow_id).get(phase_name)
    if not declared or not working_directory:
        return ""

    lines = []
    missing_required = []
    for kind in ("required", "optional"):
        for filename in declared.get(kind) or []:
            found = resolve_phase_input(working_directory, filename, workflow_id)
            if found:
                rel = Path(found)
                try:
                    rel = rel.relative_to(Path(working_directory))
                except ValueError:
                    pass
                lines.append(f"  [present]  {filename}  ->  ./{rel}")
            else:
                lines.append(f"  [MISSING]  {filename}  ({kind})")
                if kind == "required":
                    missing_required.append(filename)
    if not lines:
        return ""

    manifest = (
        "\n\nINPUTS AVAILABLE TO YOU THIS RUN (resolved at dispatch, do not "
        "guess these paths):\n" + "\n".join(lines)
    )
    if missing_required:
        manifest += (
            f"\n\nNOTE: {', '.join(missing_required)} "
            f"{'is' if len(missing_required) == 1 else 'are'} normally "
            "available to this phase and absent right now -- most likely the "
            "producing phase was rewound by a goto, or its report was consumed "
            "after a gate decision. Work from what IS present and say plainly "
            "in your report what you could not check. Do NOT go looking for "
            "these files in other feature folders or invent their contents."
        )
    return manifest


def gate_finding_count(phase_name: str, result: Optional[Dict[str, Any]]) -> int:
    """How many unresolved findings a gated phase's own report records.

    There is no shared key: each gated phase's report speaks its own
    vocabulary, and its score_* function above reads accordingly --
    blocker_count for the three BLOCKER-style reviews, failed_tests /
    critical_issues for QA, unmet_requirements for product validation,
    unresolved_count for security review. record_review_finding used to
    read result["blocker_count"] unconditionally, so every phase that
    doesn't use that key recorded 0 findings no matter what it found, and
    the prior-findings block injected into the next run's task description
    (see _create_phase_task) announced "0 finding(s)" above a summary
    describing real ones. Same class of mistake as the one
    synthetic_clean_result's docstring documents: assuming one phase's
    schema is every phase's.

    scope_review is verdict-based with no count at all and correctly
    returns 0 here -- its findings survive via the summary text, which is
    what actually gets read back.
    """
    if not result:
        return 0
    if phase_name == "security_review":
        return int(result.get("unresolved_count") or 0)
    if phase_name == "qa_validation":
        return int(result.get("failed_tests") or 0) + int(
            result.get("critical_issues") or 0
        )
    if phase_name == "product_validation":
        unmet = result.get("unmet_requirements") or []
        return len(unmet) if isinstance(unmet, (list, tuple)) else 0
    return int(result.get("blocker_count") or 0)


def record_review_finding(
    workflow_id: str, phase_name: str, blocker_count: int, summary: str
) -> None:
    """Append one run's findings to this phase's persistent history.

    `blocker_count` is the stored key name (kept as-is: history rows
    already written use it) but the VALUE should come from
    gate_finding_count above, not from result["blocker_count"] -- "blocker"
    is only three of the seven gated phases' vocabulary.

    Called right before consume_gate_artifacts deletes the result files
    those findings were read from -- the NEXT run of this phase is a fresh
    agent session with zero memory of its own (see _create_phase_task),
    so without this every re-run re-reviews from scratch instead of just
    verifying what was already found. Only meaningful for phases that opt
    into max_review_runs; callers should check get_max_review_runs first
    to avoid writing history nothing ever reads.
    """
    from datetime import datetime

    from src.core.database import ProjectContext, get_db

    with get_db() as session:
        key = f"review_findings:{workflow_id}:{phase_name}"
        row = session.query(ProjectContext).filter_by(key=key).first()
        history = list(row.value) if row and row.value else []
        history.append(
            {
                "run_number": len(history) + 1,
                "blocker_count": blocker_count,
                # Bounded -- this gets echoed into a task description, not
                # stored for its own sake.
                "summary": (summary or "")[:500],
                "timestamp": datetime.utcnow().isoformat(),
            }
        )
        if row:
            row.value = history
        else:
            session.add(ProjectContext(key=key, value=history))


# Score anchors for the three bands.
_ARCH = 0.25  # < 0.3  -> goto architecture
_DEV = 0.5  # < 0.7  -> goto development
_PASS_FLOOR = 0.7  # >= 0.7 -> continue


def load_spec(spec_path: Optional[Path] = None) -> Dict[str, Any]:
    """Load the spec, merged over defaults. Tolerates a missing/corrupt file."""
    spec = dict(DEFAULT_SPEC)
    p = spec_path or SPEC_PATH
    try:
        if p.exists():
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                spec.update({k: data[k] for k in DEFAULT_SPEC if k in data})
    except Exception:
        pass
    return spec


def _clamp01(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


def _pass_with_subjective(agent_score: Any) -> float:
    """Floors passed -> 0.7..1.0 blended with the agent's subjective score."""
    return round(_PASS_FLOOR + (1.0 - _PASS_FLOOR) * _clamp01(agent_score, 1.0), 4)


#: Matches pytest's plain-text summary line, e.g. "2 failed, 8 passed in
#: 1.05s" or "3 passed, 1 skipped, 2 xfailed in 0.42s" or "5 error in 0.10s".
#: Deliberately does NOT depend on the pytest-json-report plugin (undeclared
#: anywhere in this repo, and not something we can assume a target project
#: has installed) — this format is emitted by vanilla pytest with zero
#: extra plugins, which is the only thing we can rely on being present.
_PYTEST_SUMMARY_COUNT_RE = re.compile(
    r"(\d+)\s+(failed|passed|error(?:s)?|skipped|xfailed|xpassed)"
)


def _parse_pytest_summary(output: str) -> Optional[Dict[str, int]]:
    """Parse counts out of pytest's final summary line.

    Returns None if no recognizable summary line is found (e.g. pytest
    crashed before producing one, or "no tests ran").
    """
    for line in reversed(output.splitlines()):
        matches = _PYTEST_SUMMARY_COUNT_RE.findall(line)
        if not matches:
            continue
        counts = {"failed": 0, "passed": 0, "error": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}
        for num, label in matches:
            key = "error" if label.startswith("error") else label
            counts[key] = counts.get(key, 0) + int(num)
        return counts
    return None


def run_independent_test_verification(
    working_directory: str,
    timeout_seconds: int = 300,
) -> Optional[Dict[str, Any]]:
    """Run the test suite independently to verify agent-reported QA metrics.

    Enhancement 1 (from docs/LOOP_ENGINEERING_EVALUATION.md):
    Turns the QA gate from 'trust the JSON format' into 'verify against
    reality' by running pytest independently and comparing results.

    Parses pytest's plain-text summary output rather than requiring the
    pytest-json-report plugin, since we can't assume an arbitrary target
    project (or even a Python one) has that plugin installed — this way the
    verification actually runs for any project with vanilla pytest instead
    of silently no-op'ing whenever the plugin is absent.

    Returns:
        Dict with 'failed', 'passed', 'total', 'pass_rate' keys, or None
        if tests couldn't be run (caller should fall back to agent report).
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "python", "-m", "pytest", "-q", "--tb=no",
                # -p no:libtmux: if this subprocess inherits the same Python
                # environment/site-packages as the orchestrator itself (the
                # common case unless the target project has its own venv on
                # PATH), HephaestusNG's own libtmux dependency registers a
                # pytest plugin via entry_points that crashes on newer pytest
                # ("Marks cannot be applied to fixtures") regardless of the
                # target project's own test files — verified reproducible.
                # Disabling it here only affects auto-loaded Hephaestus-side
                # plugins, not anything the target project installed itself.
                "-p", "no:libtmux",
            ],
            cwd=working_directory,
            capture_output=True,
            timeout=timeout_seconds,
            text=True,
        )

        counts = _parse_pytest_summary(result.stdout)
        if counts is None:
            logger.warning(
                f"[INDEPENDENT_TEST] Could not parse pytest output "
                f"(exit code: {result.returncode}); falling back to agent report"
            )
            return None

        failed = counts["failed"] + counts["error"]
        passed = counts["passed"] + counts["xpassed"]
        total = failed + passed + counts["skipped"] + counts["xfailed"]
        pass_rate = (passed / total * 100.0) if total > 0 else 0.0

        logger.info(
            f"[INDEPENDENT_TEST] Verification complete: "
            f"{passed}/{total} passed ({pass_rate:.1f}%), "
            f"{failed} failed"
        )

        return {
            "failed": failed,
            "passed": passed,
            "total": total,
            "pass_rate": round(pass_rate, 1),
            "source": "independent_verification",
        }

    except subprocess.TimeoutExpired:
        logger.warning(
            f"[INDEPENDENT_TEST] Test suite timed out after {timeout_seconds}s"
        )
    except FileNotFoundError:
        logger.warning(
            "[INDEPENDENT_TEST] pytest not available in working directory"
        )
    except Exception as e:
        logger.warning(f"[INDEPENDENT_TEST] Unexpected error: {e}")

    return None


def verify_qa_against_independent(
    agent_result: Dict[str, Any],
    independent_result: Dict[str, Any],
) -> Tuple[bool, str]:
    """Compare agent-reported QA metrics against independent test run.

    Returns:
        (is_consistent, discrepancy_message)
    """
    agent_failed = int(
        agent_result.get("failed_tests")
        or agent_result.get("tests_failed")
        or 0
    )
    agent_pass_rate = float(agent_result.get("pass_rate", 0.0))

    ind_failed = independent_result.get("failed", 0)
    ind_pass_rate = independent_result.get("pass_rate", 0.0)

    discrepancies = []

    # Check if agent claims 0 failures but independent run found failures
    if agent_failed == 0 and ind_failed > 0:
        discrepancies.append(
            f"Agent reported 0 failed tests but independent run found {ind_failed} failures"
        )

    # Check if pass rates diverge significantly (>5%)
    if abs(agent_pass_rate - ind_pass_rate) > 5.0:
        discrepancies.append(
            f"Pass rate divergence: agent={agent_pass_rate:.1f}%, "
            f"independent={ind_pass_rate:.1f}%"
        )

    if discrepancies:
        msg = "; ".join(discrepancies)
        logger.warning(f"[QA_VERIFICATION] Discrepancy detected: {msg}")
        return False, msg

    logger.info("[QA_VERIFICATION] Agent report consistent with independent run")
    return True, ""


def score_scope_review(
    result: Optional[Dict[str, Any]],
) -> Tuple[float, Dict[str, Any]]:
    """Score a scope.md. Binary: PASS=1.0, FAIL=0.2, missing=0.4.

    Trusts the agent's verdict field directly -- out_of_scope/missing are
    listed for transparency but do not themselves force a FAIL. scope_review.yaml
    explicitly tells the agent to PASS with benign items still listed (an
    implementation detail the design doc implies, e.g.) and reserve FAIL for
    real scope drift. Requiring both lists empty here used to override that
    judgment call: an agent that correctly assessed benign-only items and
    wrote verdict=PASS still scored 0.2 (FAIL) because a list had entries,
    forcing an unwinnable goto loop back to product_requirements whenever a
    design doc had any historical inconsistency (e.g. its own schema comment
    omitting a value its own code example uses) -- product_requirements
    regenerates the same reasonable inference, scope_review re-approves it,
    the scorer re-fails it, forever. Observed live: 162 gotos and all 3
    arbitration attempts burned on exactly this pattern before the workflow
    gave up.

    Accepts both the canonical flat schema and the nested schema agents sometimes
    write ({"scope_review": {"verdict": ...}, "out_of_scope_items": [...], ...}).
    """
    if not result:
        return 0.4, {
            "gate": "scope_review",
            "reason": "no scope.md found",
            "result_missing": True,
        }

    # Normalise: agents sometimes write {"scope_review": {"verdict": ...}} instead
    # of the flat {"verdict": ...} schema specified in scope_review.yaml.
    flat = result
    if (
        "verdict" not in result
        and "scope_review" in result
        and isinstance(result["scope_review"], dict)
    ):
        flat = result["scope_review"]

    verdict = str(flat.get("verdict", "")).strip().upper()

    # Accept verdict from analysis_summary if still missing (another common variant)
    if not verdict:
        summary = result.get("analysis_summary") or {}
        scope_drift = summary.get("scope_drift_detected")
        if scope_drift is False:
            verdict = "PASS"
        elif scope_drift is True:
            verdict = "FAIL"

    # out_of_scope / missing: try multiple key names agents use
    out_of_scope = (
        result.get("out_of_scope")
        or flat.get("out_of_scope")
        or result.get("out_of_scope_items")
        or []
    )
    missing = (
        result.get("missing")
        or flat.get("missing")
        or result.get("missing_items")
        or []
    )
    meta = {
        "gate": "scope_review",
        "verdict": verdict,
        "out_of_scope_count": len(out_of_scope),
        "missing_count": len(missing),
    }
    if verdict == "PASS":
        return 1.0, {**meta, "band": "pass"}
    # Same handoff mechanism as score_architectural_review/score_adversarial_review's
    # report_text quoting (see _fire_phase_transition's feedback extraction):
    # without a real "reason" here, product_requirements only ever sees the
    # static workflow.yaml condition text ("Scope drift detected...") on a
    # goto, not which items actually drifted.
    reason_parts = []
    if out_of_scope:
        reason_parts.append(f"Out of scope: {out_of_scope}")
    if missing:
        reason_parts.append(f"Missing from requirements: {missing}")
    if not reason_parts:
        # verdict says FAIL (or scope_drift_detected=True) but the agent
        # didn't populate itemized out_of_scope/missing in any schema
        # variant this reads (e.g. a nested "analysis": {...} shape with
        # aggregate counts instead of itemized lists) -- fall back to the
        # agent's own free-text summary rather than leaving development
        # staring at a dangling "Scope drift detected — " with nothing
        # after it and no way to know what to actually fix.
        summary_text = flat.get("summary") or result.get("summary")
        reason_parts.append(
            summary_text or "no specific out-of-scope/missing items reported by the agent"
        )
    return 0.2, {
        **meta,
        "band": "requirements",
        "out_of_scope": out_of_scope,
        "missing": missing,
        "reason": "Scope drift detected — " + "; ".join(reason_parts),
    }


def score_qa(
    result: Optional[Dict[str, Any]],
    spec: Dict[str, Any],
    working_directory: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score a structured QA result against the spec (hard floors + judgement).

    Expected result keys (all optional, scored defensively):
        failed_tests, passed_tests, total_tests, pass_rate (0-100),
        critical_issues, requirements_met, requirements_total, agent_score (0-1).

    Enhancement 1: If working_directory is provided, runs an independent test
    verification and compares against the agent's self-reported metrics.
    """
    if not result:
        return _DEV, {
            "gate": "qa",
            "reason": "no qa.md found",
            "result_missing": True,
        }

    spec = spec or DEFAULT_SPEC
    failed = int(result.get("failed_tests") or result.get("tests_failed") or 0)
    passed = int(result.get("passed_tests") or result.get("tests_passed") or 0)
    total = int(
        result.get("total_tests") or result.get("tests_run") or (passed + failed) or 0
    )
    critical = int(result.get("critical_issues", 0) or 0)

    pass_rate = result.get("pass_rate")
    if pass_rate is None:
        pass_rate = (passed / total * 100.0) if total > 0 else 0.0
    pass_rate = float(pass_rate)

    req_total = int(result.get("requirements_total", 0) or 0)
    req_met = int(result.get("requirements_met", req_total) or 0)
    req_rate = (req_met / req_total * 100.0) if req_total > 0 else 100.0

    # Enhancement 1: Independent test verification
    independent_verification = None
    verification_discrepancy = ""
    if working_directory:
        independent_result = run_independent_test_verification(working_directory)
        if independent_result:
            independent_verification = independent_result
            is_consistent, discrepancy = verify_qa_against_independent(
                result, independent_result
            )
            if not is_consistent:
                verification_discrepancy = discrepancy
                ind_failed = independent_result.get("failed", 0)
                ind_total = independent_result.get("total", 0)
                if total == 0 and ind_total > 0:
                    # agent's qa.md didn't populate the documented
                    # top-level failed_tests/passed_tests/total_tests/pass_rate
                    # fields at all (e.g. it wrote its own nested report shape
                    # instead of the prompt's "EXACTLY this schema" — a real
                    # xiaomi/mimo-v2.5 failure mode observed in smoke testing).
                    # Reading that as "0 tests, 0% pass rate" is worse than
                    # useless: it fails a genuinely passing QA run and sends
                    # the whole feature back to development, wasting an
                    # entire review cycle. The independent run is ground
                    # truth here regardless of direction, not just when it's
                    # worse than the agent's claim.
                    logger.warning(
                        f"[QA_GATE] Agent report has no usable test counts "
                        f"(total=0) — adopting independent verification "
                        f"wholesale: {ind_total} tests, {independent_result.get('pass_rate', 0)}% pass rate"
                    )
                    failed = ind_failed
                    passed = independent_result.get("passed", passed)
                    total = ind_total
                    pass_rate = independent_result.get("pass_rate", pass_rate)
                elif ind_failed > failed:
                    # Use the independent (worse) metrics if agent claims better results
                    logger.warning(
                        f"[QA_GATE] Overriding agent metrics with independent results: "
                        f"failed_tests {failed} -> {ind_failed}"
                    )
                    failed = ind_failed
                    passed = independent_result.get("passed", passed)
                    total = independent_result.get("total", total)
                    pass_rate = independent_result.get("pass_rate", pass_rate)

    violations = []
    if critical > spec.get("max_critical_issues", 0):
        violations.append(
            f"critical_issues={critical} > {spec.get('max_critical_issues', 0)}"
        )
    if failed > spec.get("max_failed_tests", 0):
        violations.append(f"failed_tests={failed} > {spec.get('max_failed_tests', 0)}")
    if pass_rate < spec.get("required_pass_rate", 100):
        violations.append(
            f"pass_rate={pass_rate:.0f}% < {spec.get('required_pass_rate', 100)}%"
        )
    if req_rate < spec.get("min_requirements_met_rate", 100):
        violations.append(
            f"requirements_met={req_rate:.0f}% < {spec.get('min_requirements_met_rate', 100)}%"
        )

    meta = {
        "gate": "qa",
        "violations": violations,
        "pass_rate": round(pass_rate, 1),
        "failed_tests": failed,
        "critical_issues": critical,
        "requirements_met_rate": round(req_rate, 1),
    }

    # Include verification metadata
    if independent_verification:
        meta["independent_verification"] = independent_verification
    if verification_discrepancy:
        meta["verification_discrepancy"] = verification_discrepancy

    # Critical issues are treated as fundamental (architecture); other floor
    # breaches are code-level (development); otherwise pass + subjective blend.
    # Same handoff mechanism as score_architectural_review/score_adversarial_review's
    # report_text quoting (see _fire_phase_transition's feedback extraction):
    # without a real "reason" here, development only ever sees the static
    # workflow.yaml condition text ("QA failed, returning to development")
    # on a goto, not which specific violations (already computed above) failed.
    #
    # failed_test_names (if the agent populated it) names the ACTUAL failing
    # tests -- without this, a goto only carried a bare count
    # ("failed_tests=1 > 0"), leaving development to guess which test and
    # whether touching it was even in scope. Observed live: a single
    # pre-existing/stale test failure bounced the pipeline between
    # qa_validation and development for 4+ cycles because development never
    # knew which test to fix or that fixing a pre-existing failure (not
    # newly broken by this feature) was acceptable -- the gate requires
    # 100% pass rate regardless of whose fault a failure is.
    failed_test_names = result.get("failed_test_names")
    test_detail = f" Failing tests: {failed_test_names}." if failed_test_names else ""
    permission = (
        " You may fix ANY failing test blocking this gate, including "
        "pre-existing/stale ones unrelated to this feature's own changes -- "
        "the gate requires 100% pass rate regardless of whose change broke it."
        if failed > 0
        else ""
    )
    if critical > spec.get("max_critical_issues", 0):
        return _ARCH, {
            **meta,
            "band": "architecture",
            "reason": f"QA critical issues: {violations}.{test_detail}",
        }
    if violations:
        return _DEV, {
            **meta,
            "band": "development",
            "reason": f"QA violations: {violations}.{test_detail}{permission}",
        }
    return _pass_with_subjective(result.get("agent_score", 1.0)), {
        **meta,
        "band": "pass",
    }


def score_product_validation(
    result: Optional[Dict[str, Any]],
    spec: Dict[str, Any],
    report_text: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score a structured product-validation result (verdict + unmet reqs + floors).

    Expected keys: verdict ("PASS"|"PASS_WITH_MINOR_GAPS"|"NEEDS_WORK"|"ARCHITECTURE"),
        unmet_requirements (list), agent_score (0-1).
    """
    if not result:
        # The agent may have written the markdown report but failed (or
        # forgot) to also emit the structured JSON -- don't discard real
        # findings just because the JSON is missing. Same fail-safe pattern
        # as score_adversarial_review/score_architectural_review/
        # score_feature_review: always the failing band, report text is
        # context for the developer, never a route to a pass.
        reason = (
            f"no validation.md frontmatter found, but a report was "
            f"written:\n\n{report_text}"
            if report_text
            else "no validation.md found"
        )
        return _DEV, {
            "gate": "product",
            "reason": reason,
            "result_missing": True,
        }

    verdict = str(result.get("verdict", "")).strip().upper()
    unmet = result.get("unmet_requirements") or []
    if not isinstance(unmet, list):
        unmet = [unmet]
    meta = {"gate": "product", "verdict": verdict, "unmet_count": len(unmet)}

    # Architecture-level signal: explicit verdict or wording.
    if verdict in ("ARCHITECTURE", "ARCH") or "ARCHITECT" in verdict:
        return _ARCH, {**meta, "band": "architecture"}

    # PASS_WITH_MINOR_GAPS: the agent explicitly judged unmet requirements
    # as non-blocking. Accept the verdict only for an EXACT match (not any
    # string containing "PASS" and "MINOR") and only up to
    # max_minor_unmet_requirements -- past that cap, fall through to the
    # hard floor below like any other unmet-requirements case. This is a
    # documented verdict value (product_validation.yaml's schema), not an
    # emergent one the scorer just happens to tolerate.
    max_minor = spec.get("max_minor_unmet_requirements", DEFAULT_SPEC["max_minor_unmet_requirements"])
    if verdict == "PASS_WITH_MINOR_GAPS" and len(unmet) <= max_minor:
        return _PASS_FLOOR, {
            **meta,
            "band": "pass",
            "reason": f"PASS_WITH_MINOR_GAPS accepted: {len(unmet)} unmet requirement(s), within cap of {max_minor}",
        }

    # Hard floor: a PASS verdict cannot stand if requirements are unmet.
    # Same handoff mechanism as score_architectural_review/score_adversarial_review's
    # report_text quoting (see _fire_phase_transition's feedback extraction):
    # quote the actual unmet_requirements list, not just "unmet requirements
    # override verdict" -- development otherwise gets sent back with no idea
    # which requirements were unmet.
    if unmet:
        return _DEV, {
            **meta,
            "band": "development",
            "reason": f"Unmet requirements override verdict: {unmet}",
        }

    if verdict in ("NEEDS_WORK", "FAIL", "NEEDS WORK"):
        return _DEV, {
            **meta,
            "band": "development",
            "reason": f"Product validation verdict: {verdict}",
        }

    if verdict == "PASS":
        return _pass_with_subjective(result.get("agent_score", 1.0)), {
            **meta,
            "band": "pass",
        }

    # Unknown/empty verdict with no unmet reqs — treat conservatively as code-level.
    return _DEV, {**meta, "band": "development", "reason": "unrecognized verdict"}


def score_adversarial_review(
    result: Optional[Dict[str, Any]],
    report_text: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score an adversarial.md by BLOCKER/WARNING/NIT counts.

    adversarial_review.yaml's report classifies findings as BLOCKER (process
    death, silent data corruption, unrecoverable state), WARNING (fails under
    real load/edge conditions), or NIT (style/minor). Any BLOCKER lands below
    the workflow.yaml `score < 0.6` threshold so the engine goes back to
    development to fix it instead of silently continuing — this is the exact
    gate that was previously dead (see GATED_PHASES comment above): scoring
    always fell through to a fixed 0.75 baseline because no phase_output was
    ever built for this phase, regardless of how many BLOCKERs were found.

    No distinct signal currently exists to tell "needs a development fix"
    apart from "needs an architectural redesign" (workflow.yaml's `score <
    0.3 -> architecture_design` band), so any BLOCKER routes to development
    rather than architecture_design -- a known limitation, not a silent gap.
    """
    if not result:
        # The agent may have written the markdown report but failed (or
        # forgot) to also emit the structured JSON -- don't discard real
        # findings just because the JSON is missing.
        reason = (
            f"no adversarial.md frontmatter found, but a report was "
            f"written:\n\n{report_text}"
            if report_text
            else "no adversarial.md found"
        )
        return 0.4, {
            "gate": "adversarial_review",
            "reason": reason,
            "result_missing": True,
        }

    blockers = int(result.get("blocker_count") or 0)
    warnings = int(result.get("warning_count") or 0)

    if blockers > 0:
        # Send the full report, not just a count or an extracted snippet --
        # a developer agent with no other context needs the actual attack
        # vectors, failure sequences, and recommended fixes the reviewer
        # wrote, not a summary that strips them back out.
        reason = (
            f"{blockers} BLOCKER(s) found in adversarial review:\n\n{report_text}"
            if report_text
            else f"{blockers} BLOCKER(s) found — returning to development"
        )
        return 0.4, {
            "gate": "adversarial_review",
            "band": "development",
            "blocker_count": blockers,
            "warning_count": warnings,
            "reason": reason,
        }
    if warnings > 0:
        reason = (
            f"{warnings} WARNING(s) found in adversarial review:\n\n{report_text}"
            if report_text
            else f"{warnings} WARNING(s) found — returning to development"
        )
        return 0.5, {
            "gate": "adversarial_review",
            "band": "development",
            "warning_count": warnings,
            "reason": reason,
        }
    return 0.9, {"gate": "adversarial_review", "band": "pass", "reason": "clean"}


def score_design_review(
    result: Optional[Dict[str, Any]],
    report_text: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score a challenge.md by BLOCKER/WARNING/NIT counts.

    design_review.yaml's report classifies findings as BLOCKER
    (requirement unimplemented, real race condition or data-consistency
    gap baked into the design, secrets mishandling, unresolved TBD), WARNING
    (unspecified error propagation, scope mismatch), or NIT (minor). Unlike
    score_adversarial_review/score_architectural_review (where only a
    BLOCKER routes back, a WARNING-only report still passes), ANY finding
    here routes back to architecture_design: development hasn't run yet, so
    there's no other phase for a WARNING to be deferred to, and looping
    architecture_design again is far cheaper than discovering the same gap
    after code has been built on top of it.
    """
    if not result:
        # The agent may have written the markdown report but failed (or
        # forgot) to also emit the structured JSON -- don't discard real
        # findings just because the JSON is missing.
        reason = (
            f"no challenge.md frontmatter found, but a report was "
            f"written:\n\n{report_text}"
            if report_text
            else "no challenge.md found"
        )
        return 0.4, {
            "gate": "design_review",
            "reason": reason,
            "result_missing": True,
        }

    blockers = int(result.get("blocker_count") or 0)
    warnings = int(result.get("warning_count") or 0)

    if blockers > 0:
        reason = (
            f"{blockers} BLOCKER(s) found in design review:\n\n{report_text}"
            if report_text
            else f"{blockers} BLOCKER(s) found — returning to architecture_design"
        )
        return 0.4, {
            "gate": "design_review",
            "band": "architecture_design",
            "blocker_count": blockers,
            "warning_count": warnings,
            "reason": reason,
        }
    if warnings > 0:
        reason = (
            f"{warnings} WARNING(s) found in design review:\n\n{report_text}"
            if report_text
            else f"{warnings} WARNING(s) found — returning to architecture_design"
        )
        return 0.5, {
            "gate": "design_review",
            "band": "architecture_design",
            "warning_count": warnings,
            "reason": reason,
        }
    return 0.9, {"gate": "design_review", "band": "pass", "reason": "clean"}


def score_architectural_review(
    result: Optional[Dict[str, Any]],
    report_text: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score an review.md by BLOCKER/FIX/DEFER counts.

    architectural_review.yaml's report classifies findings as BLOCKER
    (architecture violated), FIX (design deviation), or DEFER. Same dead-gate
    bug and same fix as score_adversarial_review above — see GATED_PHASES
    comment. Any BLOCKER routes to development (workflow.yaml's `score < 0.6`
    band), same known limitation re: the `score < 0.3` architecture_design
    band as noted there.
    """
    if not result:
        # The agent may have written the markdown report but failed (or
        # forgot) to also emit the structured JSON -- don't discard real
        # findings just because the JSON is missing.
        reason = (
            f"no review.md frontmatter found, but a report was "
            f"written:\n\n{report_text}"
            if report_text
            else "no review.md found"
        )
        return 0.4, {
            "gate": "architectural_review",
            "reason": reason,
            "result_missing": True,
        }

    blockers = int(result.get("blocker_count") or 0)
    fixes = int(result.get("fix_count") or 0)

    if blockers > 0:
        reason = (
            f"{blockers} BLOCKER(s) found in architectural review:\n\n{report_text}"
            if report_text
            else f"{blockers} BLOCKER(s) found — returning to development"
        )
        return 0.4, {
            "gate": "architectural_review",
            "band": "development",
            "blocker_count": blockers,
            "fix_count": fixes,
            "reason": reason,
        }
    if fixes > 0:
        reason = (
            f"{fixes} FIX item(s) found in architectural review:\n\n{report_text}"
            if report_text
            else f"{fixes} FIX item(s) found — returning to development"
        )
        return 0.5, {
            "gate": "architectural_review",
            "band": "development",
            "fix_count": fixes,
            "reason": reason,
        }
    return 0.9, {"gate": "architectural_review", "band": "pass", "reason": "clean"}


def score_security_review(
    result: Optional[Dict[str, Any]],
    report_text: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score a security.md by unresolved critical/high vulnerability count.

    security_review.yaml's mandate is unusual among the review phases: it
    FIXES critical and high vulnerabilities itself rather than only
    reporting them ("Critical and high vulnerabilities FIXED in the code"),
    and tickets the medium/low ones. So the gate input is not "how many did
    you find" -- a review that found ten and fixed ten is a clean pass --
    it's `unresolved_count`: the critical/high findings still live in the
    code when the agent marked itself done.

    Same dead-gate bug and same fix as score_adversarial_review/
    score_architectural_review (see the GATED_PHASES comment above), just
    caught later: security_review carried a full set of workflow.yaml
    conditions (score < 0.3 -> architecture_design, score < 0.7 ->
    development) but never declared `spec_gate: true`, so
    build_phase_output returned {} for it and the heuristic evaluator's
    fixed 0.75 baseline continued past the gate every single time --
    regardless of what the review found or failed to fix. Of the two
    phases left behind by that earlier fix, this is the one where it
    mattered: a security review reporting unfixed critical vulnerabilities
    advanced straight to QA.

    As with those two, no distinct signal exists to separate "needs a
    development fix" from "needs an architectural redesign"
    (workflow.yaml's `score < 0.3` band), so anything unresolved routes to
    development -- a known limitation, not a silent gap.
    """
    if not result:
        # Same rationale as score_adversarial_review's: the agent may have
        # written the report but failed to emit the structured frontmatter
        # -- don't discard real findings over a missing header.
        reason = (
            f"no security.md frontmatter found, but a report was "
            f"written:\n\n{report_text}"
            if report_text
            else "no security.md found"
        )
        return 0.4, {
            "gate": "security_review",
            "reason": reason,
            "result_missing": True,
        }

    unresolved = int(result.get("unresolved_count") or 0)
    criticals = int(result.get("critical_count") or 0)
    highs = int(result.get("high_count") or 0)

    if unresolved > 0:
        # Send the full report, not a count: a developer agent with no
        # other context needs the actual file:line references and
        # remediation the reviewer wrote.
        reason = (
            f"{unresolved} unresolved critical/high vulnerability(ies) left "
            f"in the code by security review:\n\n{report_text}"
            if report_text
            else f"{unresolved} unresolved critical/high vulnerability(ies) "
            "— returning to development"
        )
        return 0.4, {
            "gate": "security_review",
            "band": "development",
            "unresolved_count": unresolved,
            "critical_count": criticals,
            "high_count": highs,
            "reason": reason,
        }
    return 0.9, {
        "gate": "security_review",
        "band": "pass",
        "reason": (
            f"clean — {criticals} critical / {highs} high found, all fixed"
            if criticals or highs
            else "clean"
        ),
    }


def score_feature_review(
    result: Optional[Dict[str, Any]],
    report_text: Optional[str] = None,
) -> Tuple[float, Dict[str, Any]]:
    """Score a feature_review.md by BLOCKER/FIX/DEFER counts.

    02_feature_review.yaml's report classifies findings as BLOCKER (feature
    decomposition contradicts or omits part of the design), FIX (scope
    imprecision, ownership overlap), or DEFER. Unlike
    score_architectural_review/score_adversarial_review (where a FIX-only
    report proceeds and only a BLOCKER routes back), ANY finding here --
    BLOCKER or FIX -- routes back to Feature Architect: Phase 0 runs once,
    before any per-feature pipeline exists, so there's no later phase that
    will independently catch an unaddressed FIX the way development/QA/
    doc_review do downstream in the main pipeline. Only DEFER-only or a
    clean report passes.
    """
    if not result:
        # The agent may have written the markdown report but failed (or
        # forgot) to also emit the structured JSON -- don't discard real
        # findings just because the JSON is missing.
        reason = (
            f"no feature_review.md frontmatter found, but a report was "
            f"written:\n\n{report_text}"
            if report_text
            else "no feature_review.md found"
        )
        return 0.4, {
            "gate": "feature_review",
            "reason": reason,
            "result_missing": True,
        }

    blockers = int(result.get("blocker_count") or 0)
    fixes = int(result.get("fix_count") or 0)

    if blockers > 0 or fixes > 0:
        findings = f"{blockers} BLOCKER(s), {fixes} FIX item(s)"
        reason = (
            f"{findings} found in feature review:\n\n{report_text}"
            if report_text
            else f"{findings} found — returning to Feature Architect"
        )
        return 0.1, {
            "gate": "feature_review",
            "band": "feature_architect",
            "blocker_count": blockers,
            "fix_count": fixes,
            "reason": reason,
        }
    return 0.9, {"gate": "feature_review", "band": "pass", "reason": "clean"}


def read_okf_report(
    working_directory: Any,
    filename: str,
    subdir: Optional[str] = None,
    phase_name: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read a phase's OKF report (frontmatter + body) an agent wrote.

    Agents write to ./.hephaestus/ (git-excluded, never merged to main).
    Does NOT iterate worktrees (too slow for per-turn calls).

    subdir: if given, this phase's real (sole, not a fallback) output
    location is working_directory/subdir/filename (or its old-name alias,
    see below). Reads ONLY that one location; does not try other
    directories.

    phase_name: each gated phase's task description tells it the ONE
    sanctioned subdirectory of .hephaestus/ to write to --
    .hephaestus/<phase_name>/, see each gated phase's CRITICAL PATH RULE
    -- so this is checked first, not guessed at: iterating every
    subdirectory that happens to contain a same-named file risks picking
    up a stale file left behind by an earlier retry/goto pass of this
    same phase, since filesystem iteration order isn't "most recent."
    The project root remains as a fallback location for older agent
    behavior.

    At each location, `filename`'s old-name alias (OUTPUT_NAME_ALIASES,
    the same table resolve_declared_output_path checks) is tried too --
    a small, fixed lookup, not a directory scan, so it doesn't reintroduce
    the stale-file risk above; without it, a report written under its old
    name passed verify_output_artifact's existence check (which does
    resolve aliases) but silently scored as "no report" here, since the
    two functions used to disagree on this (Phase 2 §4.9).

    Returns (frontmatter, body) -- either or both None if the file is
    missing or has no parseable frontmatter block (see okf_markdown.read_okf).
    """
    base = Path(working_directory)
    old_name = OUTPUT_NAME_ALIASES.get(filename)
    names = [filename] + ([old_name] if old_name else [])
    if subdir is not None:
        candidates = [base / subdir / name for name in names]
    else:
        candidates = []
        for name in names:
            if phase_name:
                candidates.append(base / ".hephaestus" / phase_name / name)
            candidates.append(base / name)
    for candidate in candidates:
        if candidate.exists():
            parsed = read_okf(candidate)
            return parsed if parsed else (None, None)
    return None, None


# The single OKF report file each gated phase's score is computed from --
# mirrors the filename build_phase_output reads below (keep in sync). Used
# by consume_gate_artifacts when a gate's goto decision fires, and by
# verify_gate_result_schema's output-schema floor.
GATE_RESULT_ARTIFACTS: Dict[str, Tuple[str, ...]] = {
    "scope_review": ("scope.md",),
    "design_review": ("challenge.md",),
    "architectural_review": ("review.md",),
    "adversarial_review": ("adversarial.md",),
    "security_review": ("security.md",),
    "qa_validation": ("qa.md",),
    "product_validation": ("validation.md",),
    "feature_review": ("feature_review.md",),
}

# Override for a gated phase whose result lives somewhere other than
# .hephaestus/<phase_name>/<file> or <root>/<file> -- currently unused.
# feature_review used to be the one exception (a flat .hephaestus/review.md,
# colliding in name with architectural_review's own review.md) until
# normalized onto the same .hephaestus/<phase_name>/ convention and
# feature_review.md's unique filename (Phase 2 §4.9 follow-up). Kept as
# infrastructure for read_okf_report's/consume_gate_artifacts's subdir
# parameter, in case a future gated phase genuinely needs a non-standard
# location -- not speculative, just not deleting a general mechanism for
# the sake of the one entry that used it.
GATE_RESULT_SUBDIR: Dict[str, str] = {}


def synthetic_clean_result(phase_name: str, run_count: int) -> Dict[str, Any]:
    """Build a synthetic "clean pass" result in the schema THIS phase's own
    scorer actually reads, for _cap_out_review_phase's "stop re-reviewing,
    mark done with caveats" path.

    Each gated phase has a different scorer reading different keys --
    score_architectural_review/score_adversarial_review read blocker_count,
    but score_qa reads pass_rate/failed_tests/requirements_met_rate,
    score_product_validation reads verdict/unmet_requirements, and
    score_scope_review reads verdict. A single blocker_count-only shape
    written for every phase is a real bug, not a harmless default: handed
    to score_qa it reads as total_tests=0 -> pass_rate=0.0%, i.e. the
    WORST possible score -- the opposite of the clean pass this function
    exists to produce. Observed live: qa_validation's cap-out wrote
    {"blocker_count": 0}, scored as a 0% pass rate, and immediately
    goto'd back to development -- burning through max_total_gotos faster
    than not capping at all would have.
    """
    # type: matches validate_gate_result_schema's expected `type` for this
    # phase (GATE_RESULT_TYPE_OVERRIDE, the same source of truth) -- without
    # it this synthetic result would fail the very check a real agent's
    # report has to pass.
    base = {"type": expected_gate_result_type(phase_name), "capped": True, "capped_after_runs": run_count}
    if phase_name == "qa_validation":
        return {
            **base,
            "passed_tests": 1,
            "failed_tests": 0,
            "total_tests": 1,
            "pass_rate": 100.0,
            "critical_issues": 0,
            "requirements_met": 1,
            "requirements_total": 1,
        }
    if phase_name == "product_validation":
        return {**base, "verdict": "PASS", "unmet_requirements": []}
    if phase_name == "scope_review":
        return {**base, "verdict": "PASS"}
    if phase_name == "security_review":
        return {**base, "unresolved_count": 0, "critical_count": 0, "high_count": 0}
    # design_review, architectural_review, adversarial_review, feature_review: blocker-count schema.
    return {**base, "blocker_count": 0}


def consume_gate_artifacts(phase_name: str, working_directory: Any) -> list:
    """Delete a gated phase's result artifacts after its goto decision has
    been acted on (the findings are already threaded into the corrective
    task's description), so a later re-run of the phase MUST produce fresh
    ones.

    Without this, the stale files satisfy both the output-existence floor
    (update_task_status only checks the declared report EXISTS, not that
    it's fresh) and the gate's own scorer -- observed live: development
    genuinely fixed all 4 BLOCKERs, but every adversarial_review re-run
    re-scored the pre-fix result.json (blocker_count=4) and sent the
    pipeline back to development again, in a loop, burning one goto per
    cycle until max_total_gotos would have force-failed the workflow.

    The files are safe to delete: they're the agent's own gate-result
    report, regenerated fresh on the phase's next run -- not a record
    anything else depends on keeping. .hephaestus/ is git-excluded (never
    committed), so there's no history copy either way; that's fine, since
    forcing regeneration is the whole point of deleting them.

    Returns the paths actually deleted.
    """
    deleted = []
    base = Path(working_directory)
    subdir = GATE_RESULT_SUBDIR.get(phase_name)
    for filename in GATE_RESULT_ARTIFACTS.get(phase_name, ()):
        # .hephaestus/<phase_name>/ is the one sanctioned subdirectory name
        # this phase's own CRITICAL PATH RULE tells it to use, checked
        # first -- not a search: iterating every subdirectory of
        # .hephaestus/ risked deleting a DIFFERENT feature's (or an earlier
        # retry pass's) still-needed result file. Must mirror
        # read_okf_report's candidate order exactly, including its old-name
        # alias -- that's what actually decides which file the gate reads,
        # so a stale file this function doesn't know to delete resurrects
        # the exact stale-result loop this function exists to prevent.
        old_name = OUTPUT_NAME_ALIASES.get(filename)
        names = [filename] + ([old_name] if old_name else [])
        if subdir:
            candidates = [base / subdir / name for name in names]
        else:
            candidates = []
            for name in names:
                candidates.append(base / ".hephaestus" / phase_name / name)
                candidates.append(base / name)
        if phase_name == "feature_review":
            # TEMPORARY (Phase 2 §4.9 follow-up) -- see
            # _feature_review_legacy_report's own docstring. Must also be
            # cleaned up here, not just read as a fallback: an unconsumed
            # stale legacy file would otherwise keep resurrecting the
            # exact goto-loop bug this function exists to prevent, across
            # every retry of an in-flight run still writing to it.
            candidates.append(base / CONTEXT_DIR_NAME / "review.md")
        for candidate in candidates:
            if candidate.exists():
                try:
                    candidate.unlink()
                    deleted.append(str(candidate))
                except OSError as e:
                    logger.warning(
                        f"[SPEC-GATE] Could not remove consumed gate artifact "
                        f"{candidate}: {e}"
                    )
    if deleted:
        logger.info(
            f"[SPEC-GATE] {phase_name}: consumed gate artifacts after goto "
            f"(re-run must regenerate them): {deleted}"
        )
    return deleted


# Top-level keys each gated phase's structured JSON result must have AT
# LEAST ONE of, for its score_* function below to read real signal instead
# of an optimistic silent default. Missing every one of these means the
# agent almost certainly wrote an incompatible custom shape -- observed
# live: a QA agent wrote {"overall_status": ..., "test_results": {"main_
# suite": {"total": ..., "passed": ...}}, "requirements_compliance": {...}}
# instead of the documented flat schema. score_qa's own field reads
# (result.get("failed_tests"), result.get("critical_issues"), etc.) all
# silently defaulted to "everything passed" -- including critical_issues
# and requirements_met, which nothing else independently re-verifies the
# way the pytest re-run catches a wrong pass/fail count. Each tuple lists
# every variant the corresponding score_* function actually accepts (see
# scope_review's documented nested-schema fallback), not just the primary
# documented key, so this doesn't reject a shape the scorer already
# tolerates.
GATE_RESULT_REQUIRED_KEYS: Dict[str, Tuple[str, ...]] = {
    "scope_review": ("verdict", "scope_review", "analysis_summary"),
    "design_review": ("blocker_count",),
    "architectural_review": ("blocker_count",),
    "adversarial_review": ("blocker_count",),
    "security_review": ("unresolved_count", "critical_count", "high_count"),
    "qa_validation": ("failed_tests", "passed_tests", "critical_issues"),
    "product_validation": ("verdict",),
    "feature_review": ("blocker_count",),
}

# The documented frontmatter `type:` value for each gated phase, per its
# own workflow.yaml prompt -- six of seven document the bare phase name
# (e.g. qa_validation.yaml's own written example: `type: qa_validation`),
# but feature_review.yaml documents `type: feature_review_result`, an
# inconsistency baked into the prompts themselves, not a typo in one
# call site. Single source of truth for both validate_gate_result_schema
# (below) and synthetic_clean_result -- before this, they disagreed with
# each other (synthetic_clean_result unconditionally appended "_result",
# validate_gate_result_schema unconditionally used the bare name), so
# _cap_out_review_phase's own synthetic "clean pass" would have failed
# this exact check for every phase except feature_review had anything
# ever re-validated it -- the same type:-field-mismatch bug class Phase 2
# §4.9 was written to close (see docs/AUTOPILOT_REFACTOR_PLAN.md §4.9).
GATE_RESULT_TYPE_OVERRIDE: Dict[str, str] = {
    "feature_review": "feature_review_result",
    # security_review.yaml documented `type: security_review_report` long
    # before it became a gated phase (its report predates the spec_gate
    # mechanism), and other consumers -- verify_output_artifact's ash-scan
    # check, the archived feature record -- already read files written with
    # it. Override rather than rewriting the prompt's documented type, which
    # is exactly the prompt-level inconsistency this table exists to absorb.
    "security_review": "security_review_report",
}


def expected_gate_result_type(phase_name: str) -> str:
    """The documented frontmatter `type:` value a gated phase's report
    must declare -- see GATE_RESULT_TYPE_OVERRIDE for why this isn't
    always just the bare phase name."""
    return GATE_RESULT_TYPE_OVERRIDE.get(phase_name, phase_name)


def validate_gate_result_schema(
    phase_name: str, result: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Check a gated phase's OKF frontmatter declares the right `type` and
    has at least one of its required top-level keys actually present (not
    just defaulted).

    Returns an error message describing the gap if the schema looks wrong,
    else None. A missing/unparseable report is a separate, already-handled
    case -- each score_* function treats result=None as its own
    "result_missing" band -- so this only fires once a report exists but
    doesn't resemble the documented schema at all.

    `type` is OKF's one always-required field (see okf_markdown.py's module
    docstring) and is checked first: an agent that writes the wrong shape
    almost always gets `type` wrong too (it's the first line of the
    documented example), so this catches the "incompatible schema" case
    more directly than key-presence alone ever could.
    """
    required = GATE_RESULT_REQUIRED_KEYS.get(phase_name)
    if not required or result is None:
        return None
    expected_type = expected_gate_result_type(phase_name)
    actual_type = result.get("type")
    if actual_type != expected_type:
        return (
            f"{phase_name}'s report has frontmatter type: {actual_type!r}, "
            f"expected type: {expected_type!r} -- this looks like an "
            f"incompatible or missing frontmatter block, not the one "
            f"documented in {phase_name}.yaml. Rewrite it in the exact "
            "documented shape so the pipeline gate can read your findings."
        )
    if any(key in result for key in required):
        return None
    return (
        f"{phase_name}'s report has the right type but none of the "
        f"required fields {required} -- this looks like an incompatible "
        f"schema, not the one documented in {phase_name}.yaml. Rewrite it "
        "in the exact documented shape so the pipeline gate can read your "
        "findings (silently defaulting missing fields would mask real "
        "issues, e.g. blocker/critical-issue counts reading as 0 when they "
        "weren't actually checked)."
    )


# TEMPORARY (Phase 2 §4.9 follow-up) -- feature_review's report moved from
# flat .hephaestus/review.md to .hephaestus/feature_review/feature_review.md
# so it stops colliding in name with architectural_review's own review.md
# and matches every other gated phase's .hephaestus/<phase_name>/
# convention. A Phase 0 run already in flight when this change lands may
# still be writing to the old location, so it's checked as a fallback
# wherever feature_review's report gets read or cleaned up.
#
# DELETE THIS FUNCTION AND ITS CALLERS' FALLBACK BRANCHES once no run
# started before the normalization can still be active (Phase 0 runs are
# one-shot and short-lived, so this should be safe to remove soon --
# there's deliberately no long-term compatibility guarantee here).
def _feature_review_legacy_report(
    working_directory: Any,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    return read_okf_report(working_directory, "review.md", subdir=CONTEXT_DIR_NAME)


def build_phase_output(
    phase_name: str,
    working_directory: Any,
    spec: Optional[Dict[str, Any]] = None,
    skip_independent_verification: bool = False,
) -> Dict[str, Any]:
    """Build the engine phase_output (carrying `score`) for a gated phase.

    Returns {} for non-gated phases so the engine's default evaluation applies.
    For gated phases, returns {"score": float, "spec_gate": {...}} — the engine's
    heuristic evaluator honours an explicit `score`, so this drives goto/retry/
    continue against the configured thresholds.
    """
    if phase_name not in GATED_PHASES:
        return {}

    spec = spec if spec is not None else load_spec()

    if phase_name == "scope_review":
        result, _ = read_okf_report(
            working_directory, "scope.md", phase_name=phase_name
        )
        score, meta = score_scope_review(result)
    elif phase_name == "design_review":
        result, report_text = read_okf_report(
            working_directory, "challenge.md", phase_name=phase_name
        )
        score, meta = score_design_review(result, report_text=report_text)
    elif phase_name == "architectural_review":
        result, report_text = read_okf_report(
            working_directory, "review.md", phase_name=phase_name
        )
        score, meta = score_architectural_review(result, report_text=report_text)
    elif phase_name == "adversarial_review":
        result, report_text = read_okf_report(
            working_directory, "adversarial.md", phase_name=phase_name
        )
        score, meta = score_adversarial_review(result, report_text=report_text)
    elif phase_name == "security_review":
        result, report_text = read_okf_report(
            working_directory, "security.md", phase_name=phase_name
        )
        score, meta = score_security_review(result, report_text=report_text)
    elif phase_name == "qa_validation":
        result, _ = read_okf_report(working_directory, "qa.md", phase_name=phase_name)
        # Enhancement 1: Pass working_directory for independent test verification
        wd = None if skip_independent_verification else working_directory
        score, meta = score_qa(result, spec, working_directory=wd)
    elif phase_name == "feature_review":
        # .hephaestus/feature_review/, not docs/ -- the same convention
        # every other gated phase uses (Phase 2 §4.9 follow-up).
        result, report_text = read_okf_report(
            working_directory, "feature_review.md", phase_name=phase_name
        )
        if result is None:
            result, report_text = _feature_review_legacy_report(working_directory)
        score, meta = score_feature_review(result, report_text=report_text)
    else:  # product_validation
        result, report_text = read_okf_report(
            working_directory, "validation.md", phase_name=phase_name
        )
        score, meta = score_product_validation(result, spec, report_text=report_text)

    return {"score": score, "spec_gate": meta}
