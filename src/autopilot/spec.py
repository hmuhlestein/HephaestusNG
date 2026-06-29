"""Hybrid completion gate for the autopilot pipeline (design §9.1).

Combines a machine-checkable spec (hard floors) with the agent's own subjective
judgement to produce the `score` that drives the engine's evaluation points
(goto/retry/continue) after the QA and product-validation phases.

The spec lives at ~/.hephaestus/autopilot/qa_spec.json (per-project) and is also
copied into each worktree's .hephaestus/ so agents can read it. Phase 7/8 agents
emit structured JSON (qa_result.json / product_validation.json) which this module
scores against the spec.

Score bands map onto the autopilot evaluation_points thresholds:
    score < 0.3  -> goto architecture  (fundamental problem)
    score < 0.7  -> goto development   (code-level problem)
    score >= 0.7 -> continue           (passes the gate)
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.core.constants import AUTOPILOT_STATE_DIR

SPEC_PATH = Path(AUTOPILOT_STATE_DIR) / "qa_spec.json"

DEFAULT_SPEC: Dict[str, Any] = {
    "max_failed_tests": 0,
    "max_critical_issues": 0,
    "required_pass_rate": 100,        # percent of tests that must pass
    "min_requirements_met_rate": 100,  # percent of requirements that must be met
}

# Phases gated by the hybrid spec (engine evaluation point keys).
GATED_PHASES = ("scope_review", "qa_validation", "product_validation")

# Declared output artifacts per phase — used as completion hard floors.
# If a phase declares an output, update_task_status rejects 'done' when
# the artifact is missing (catches hallucinated completions at the source).
# Default artifacts (can be overridden by workflow.yaml required_output config)
DEFAULT_PHASE_OUTPUT_ARTIFACTS = {
    "architecture_design": "architecture.md",
    "scope_review": "scope_review_result.json",
    "qa_validation": "qa_result.json",
    "product_validation": "product_validation.json",
}

# Runtime-loaded artifacts from workflow.yaml
PHASE_OUTPUT_ARTIFACTS = dict(DEFAULT_PHASE_OUTPUT_ARTIFACTS)


def load_phase_output_artifacts(workflow_id: Optional[str] = None) -> dict:
    """Load required_output artifacts from workflow.yaml if available.
    
    Falls back to DEFAULT_PHASE_OUTPUT_ARTIFACTS if workflow_id is None
    or workflow.yaml doesn't have required_output config.
    """
    global PHASE_OUTPUT_ARTIFACTS
    if workflow_id is None:
        return PHASE_OUTPUT_ARTIFACTS
    
    try:
        from src.core.database import Workflow, DatabaseManager
        db = DatabaseManager()
        session = db.get_session()
        try:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if wf and wf.definition_id:
                # Load workflow definition from YAML
                from src.workflow_registry import _WORKFLOWS_DIR
                wf_dir = _WORKFLOWS_DIR / wf.definition_id
                workflow_yaml = wf_dir / "workflow.yaml"
                if workflow_yaml.exists():
                    import yaml
                    with open(workflow_yaml) as f:
                        wf_config = yaml.safe_load(f)
                    if wf_config and "required_output" in wf_config:
                        PHASE_OUTPUT_ARTIFACTS.update(wf_config["required_output"])
                        logger.info(f"Loaded required_output from workflow.yaml: {PHASE_OUTPUT_ARTIFACTS}")
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not load required_output from workflow.yaml: {e}")
    
    return PHASE_OUTPUT_ARTIFACTS


# Optional phases that can fail without blocking the pipeline
OPTIONAL_PHASES = {"forensics_analysis", "git_commit_push"}


def load_optional_phases(workflow_id: Optional[str] = None) -> set:
    """Load optional_phases from workflow.yaml if available."""
    global OPTIONAL_PHASES
    if workflow_id is None:
        return OPTIONAL_PHASES
    
    try:
        from src.core.database import Workflow, DatabaseManager
        db = DatabaseManager()
        session = db.get_session()
        try:
            wf = session.query(Workflow).filter_by(id=workflow_id).first()
            if wf and wf.definition_id:
                from src.workflow_registry import _WORKFLOWS_DIR
                wf_dir = _WORKFLOWS_DIR / wf.definition_id
                workflow_yaml = wf_dir / "workflow.yaml"
                if workflow_yaml.exists():
                    import yaml
                    with open(workflow_yaml) as f:
                        wf_config = yaml.safe_load(f)
                    if wf_config and "optional_phases" in wf_config:
                        OPTIONAL_PHASES = set(wf_config["optional_phases"])
                        logger.info(f"Loaded optional_phases from workflow.yaml: {OPTIONAL_PHASES}")
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not load optional_phases from workflow.yaml: {e}")
    
    return OPTIONAL_PHASES

# Score anchors for the three bands.
_ARCH = 0.25       # < 0.3  -> goto architecture
_DEV = 0.5         # < 0.7  -> goto development
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


def score_scope_review(result: Optional[Dict[str, Any]]) -> Tuple[float, Dict[str, Any]]:
    """Score a scope_review_result.json. Binary: PASS=1.0, FAIL=0.2, missing=0.4.

    Accepts both the canonical flat schema and the nested schema agents sometimes
    write ({"scope_review": {"verdict": ...}, "out_of_scope_items": [...], ...}).
    """
    if not result:
        return 0.4, {"gate": "scope_review", "reason": "no scope_review_result.json found", "result_missing": True}

    # Normalise: agents sometimes write {"scope_review": {"verdict": ...}} instead
    # of the flat {"verdict": ...} schema specified in scope_review.yaml.
    flat = result
    if "verdict" not in result and "scope_review" in result and isinstance(result["scope_review"], dict):
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
    if verdict == "PASS" and not out_of_scope and not missing:
        return 1.0, {**meta, "band": "pass"}
    return 0.2, {**meta, "band": "requirements", "out_of_scope": out_of_scope, "missing": missing}


def score_qa(result: Optional[Dict[str, Any]], spec: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Score a structured QA result against the spec (hard floors + judgement).

    Expected result keys (all optional, scored defensively):
        failed_tests, passed_tests, total_tests, pass_rate (0-100),
        critical_issues, requirements_met, requirements_total, agent_score (0-1).
    """
    if not result:
        return _DEV, {"gate": "qa", "reason": "no qa_result.json found", "result_missing": True}

    spec = spec or DEFAULT_SPEC
    failed = int(result.get("failed_tests") or result.get("tests_failed") or 0)
    passed = int(result.get("passed_tests") or result.get("tests_passed") or 0)
    total = int(result.get("total_tests") or result.get("tests_run") or (passed + failed) or 0)
    critical = int(result.get("critical_issues", 0) or 0)

    pass_rate = result.get("pass_rate")
    if pass_rate is None:
        pass_rate = (passed / total * 100.0) if total > 0 else 0.0
    pass_rate = float(pass_rate)

    req_total = int(result.get("requirements_total", 0) or 0)
    req_met = int(result.get("requirements_met", req_total) or 0)
    req_rate = (req_met / req_total * 100.0) if req_total > 0 else 100.0

    violations = []
    if critical > spec.get("max_critical_issues", 0):
        violations.append(f"critical_issues={critical} > {spec.get('max_critical_issues', 0)}")
    if failed > spec.get("max_failed_tests", 0):
        violations.append(f"failed_tests={failed} > {spec.get('max_failed_tests', 0)}")
    if pass_rate < spec.get("required_pass_rate", 100):
        violations.append(f"pass_rate={pass_rate:.0f}% < {spec.get('required_pass_rate', 100)}%")
    if req_rate < spec.get("min_requirements_met_rate", 100):
        violations.append(f"requirements_met={req_rate:.0f}% < {spec.get('min_requirements_met_rate', 100)}%")

    meta = {
        "gate": "qa", "violations": violations, "pass_rate": round(pass_rate, 1),
        "failed_tests": failed, "critical_issues": critical, "requirements_met_rate": round(req_rate, 1),
    }

    # Critical issues are treated as fundamental (architecture); other floor
    # breaches are code-level (development); otherwise pass + subjective blend.
    if critical > spec.get("max_critical_issues", 0):
        return _ARCH, {**meta, "band": "architecture"}
    if violations:
        return _DEV, {**meta, "band": "development"}
    return _pass_with_subjective(result.get("agent_score", 1.0)), {**meta, "band": "pass"}


def score_product_validation(result: Optional[Dict[str, Any]], spec: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Score a structured product-validation result (verdict + unmet reqs + floors).

    Expected keys: verdict ("PASS"|"NEEDS_WORK"|"ARCHITECTURE"),
        unmet_requirements (list), agent_score (0-1).
    """
    if not result:
        return _DEV, {"gate": "product", "reason": "no product_validation.json found", "result_missing": True}

    verdict = str(result.get("verdict", "")).strip().upper()
    unmet = result.get("unmet_requirements") or []
    if not isinstance(unmet, list):
        unmet = [unmet]
    meta = {"gate": "product", "verdict": verdict, "unmet_count": len(unmet)}

    # Architecture-level signal: explicit verdict or wording.
    if verdict in ("ARCHITECTURE", "ARCH") or "ARCHITECT" in verdict:
        return _ARCH, {**meta, "band": "architecture"}

    # Hard floor: a PASS verdict cannot stand if requirements are unmet.
    if unmet:
        return _DEV, {**meta, "band": "development", "reason": "unmet requirements override verdict"}

    if verdict in ("NEEDS_WORK", "FAIL", "NEEDS WORK"):
        return _DEV, {**meta, "band": "development"}

    if verdict == "PASS":
        return _pass_with_subjective(result.get("agent_score", 1.0)), {**meta, "band": "pass"}

    # Unknown/empty verdict with no unmet reqs — treat conservatively as code-level.
    return _DEV, {**meta, "band": "development", "reason": "unrecognized verdict"}


def read_result(working_directory: Any, filename: str) -> Optional[Dict[str, Any]]:
    """Read a structured result file an agent wrote.

    Agents write to ./docs/ (merged from their worktree to <project>/docs/);
    fall back to the project root. Does NOT iterate worktrees (too slow for
    per-turn calls — the merge should bring files to <project>/docs/).
    """
    base = Path(working_directory)
    for candidate in (base / "docs" / filename, base / filename):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except Exception:
                return None
    return None


def build_phase_output(
    phase_name: str,
    working_directory: Any,
    spec: Optional[Dict[str, Any]] = None,
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
        result = read_result(working_directory, "scope_review_result.json")
        score, meta = score_scope_review(result)
    elif phase_name == "qa_validation":
        result = read_result(working_directory, "qa_result.json")
        score, meta = score_qa(result, spec)
    else:  # product_validation
        result = read_result(working_directory, "product_validation.json")
        score, meta = score_product_validation(result, spec)

    return {"score": score, "spec_gate": meta}
