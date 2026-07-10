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
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from src.core.constants import AUTOPILOT_STATE_DIR

logger = logging.getLogger(__name__)

SPEC_PATH = Path(AUTOPILOT_STATE_DIR) / "qa_spec.json"

DEFAULT_SPEC: Dict[str, Any] = {
    "max_failed_tests": 0,
    "max_critical_issues": 0,
    "required_pass_rate": 100,  # percent of tests that must pass
    "min_requirements_met_rate": 100,  # percent of requirements that must be met
}

# Phases gated by the hybrid spec (engine evaluation point keys).
#
# architectural_review/adversarial_review added after discovering their
# workflow.yaml evaluation_points (score<0.3 -> goto architecture_design,
# score<0.6 -> goto development) had never actually fired: build_phase_output
# returned {} for any phase not in this tuple, so the heuristic evaluator's
# json.dumps({}) scan found zero keywords and fell through to its baseline
# 0.75 ("pass") every time -- regardless of how many BLOCKERs a review found.
# Observed live: an adversarial review reporting 6 BLOCKERs still completed
# with action="continue", because the score that would have triggered the
# goto-back-to-development condition was never computed from real content.
GATED_PHASES = (
    "scope_review",
    "architectural_review",
    "adversarial_review",
    "qa_validation",
    "product_validation",
)

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
    Phase.outputs), with an optional single-file override from
    workflow.yaml's `required_output:` block.
    """
    override = load_phase_output_artifacts(workflow_id).get(phase.name)
    if override:
        return [override]
    return _extract_declared_files(getattr(phase, "outputs", None))


def load_phase_output_artifacts(workflow_id: Optional[str] = None) -> dict:
    """Load required_output artifacts from workflow.yaml if available.

    Falls back to DEFAULT_PHASE_OUTPUT_ARTIFACTS if workflow_id is None
    or workflow.yaml doesn't have required_output config.
    """
    global PHASE_OUTPUT_ARTIFACTS
    if workflow_id is None:
        return PHASE_OUTPUT_ARTIFACTS

    try:
        from src.core.database import DatabaseManager, Workflow

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
                        logger.info(
                            f"Loaded required_output from workflow.yaml: {PHASE_OUTPUT_ARTIFACTS}"
                        )
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
        from src.core.database import DatabaseManager, Workflow

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
                        logger.info(
                            f"Loaded optional_phases from workflow.yaml: {OPTIONAL_PHASES}"
                        )
        finally:
            session.close()
    except Exception as e:
        logger.debug(f"Could not load optional_phases from workflow.yaml: {e}")

    return OPTIONAL_PHASES


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
    """Score a scope_review_result.json. Binary: PASS=1.0, FAIL=0.2, missing=0.4.

    Accepts both the canonical flat schema and the nested schema agents sometimes
    write ({"scope_review": {"verdict": ...}, "out_of_scope_items": [...], ...}).
    """
    if not result:
        return 0.4, {
            "gate": "scope_review",
            "reason": "no scope_review_result.json found",
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
    if verdict == "PASS" and not out_of_scope and not missing:
        return 1.0, {**meta, "band": "pass"}
    return 0.2, {
        **meta,
        "band": "requirements",
        "out_of_scope": out_of_scope,
        "missing": missing,
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
            "reason": "no qa_result.json found",
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
                    # agent's qa_result.json didn't populate the documented
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
    if critical > spec.get("max_critical_issues", 0):
        return _ARCH, {**meta, "band": "architecture"}
    if violations:
        return _DEV, {**meta, "band": "development"}
    return _pass_with_subjective(result.get("agent_score", 1.0)), {
        **meta,
        "band": "pass",
    }


def score_product_validation(
    result: Optional[Dict[str, Any]], spec: Dict[str, Any]
) -> Tuple[float, Dict[str, Any]]:
    """Score a structured product-validation result (verdict + unmet reqs + floors).

    Expected keys: verdict ("PASS"|"NEEDS_WORK"|"ARCHITECTURE"),
        unmet_requirements (list), agent_score (0-1).
    """
    if not result:
        return _DEV, {
            "gate": "product",
            "reason": "no product_validation.json found",
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

    # Hard floor: a PASS verdict cannot stand if requirements are unmet.
    if unmet:
        return _DEV, {
            **meta,
            "band": "development",
            "reason": "unmet requirements override verdict",
        }

    if verdict in ("NEEDS_WORK", "FAIL", "NEEDS WORK"):
        return _DEV, {**meta, "band": "development"}

    if verdict == "PASS":
        return _pass_with_subjective(result.get("agent_score", 1.0)), {
            **meta,
            "band": "pass",
        }

    # Unknown/empty verdict with no unmet reqs — treat conservatively as code-level.
    return _DEV, {**meta, "band": "development", "reason": "unrecognized verdict"}


def score_adversarial_review(
    result: Optional[Dict[str, Any]],
) -> Tuple[float, Dict[str, Any]]:
    """Score an adversarial_review_result.json by BLOCKER/WARNING/NIT counts.

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
        return 0.4, {
            "gate": "adversarial_review",
            "reason": "no adversarial_review_result.json found",
            "result_missing": True,
        }

    blockers = int(result.get("blocker_count") or 0)
    warnings = int(result.get("warning_count") or 0)

    if blockers > 0:
        return 0.4, {
            "gate": "adversarial_review",
            "band": "development",
            "blocker_count": blockers,
            "warning_count": warnings,
            "reason": f"{blockers} BLOCKER(s) found — returning to development",
        }
    if warnings > 0:
        return 0.7, {
            "gate": "adversarial_review",
            "band": "pass",
            "warning_count": warnings,
            "reason": f"no BLOCKERs, {warnings} WARNING(s) — proceeding",
        }
    return 0.9, {"gate": "adversarial_review", "band": "pass", "reason": "clean"}


def score_architectural_review(
    result: Optional[Dict[str, Any]],
) -> Tuple[float, Dict[str, Any]]:
    """Score an architectural_review_result.json by BLOCKER/FIX/DEFER counts.

    architectural_review.yaml's report classifies findings as BLOCKER
    (architecture violated), FIX (design deviation), or DEFER. Same dead-gate
    bug and same fix as score_adversarial_review above — see GATED_PHASES
    comment. Any BLOCKER routes to development (workflow.yaml's `score < 0.6`
    band), same known limitation re: the `score < 0.3` architecture_design
    band as noted there.
    """
    if not result:
        return 0.4, {
            "gate": "architectural_review",
            "reason": "no architectural_review_result.json found",
            "result_missing": True,
        }

    blockers = int(result.get("blocker_count") or 0)
    fixes = int(result.get("fix_count") or 0)

    if blockers > 0:
        return 0.4, {
            "gate": "architectural_review",
            "band": "development",
            "blocker_count": blockers,
            "fix_count": fixes,
            "reason": f"{blockers} BLOCKER(s) found — returning to development",
        }
    if fixes > 0:
        return 0.7, {
            "gate": "architectural_review",
            "band": "pass",
            "fix_count": fixes,
            "reason": f"no BLOCKERs, {fixes} FIX item(s) — proceeding",
        }
    return 0.9, {"gate": "architectural_review", "band": "pass", "reason": "clean"}


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
    elif phase_name == "architectural_review":
        result = read_result(working_directory, "architectural_review_result.json")
        score, meta = score_architectural_review(result)
    elif phase_name == "adversarial_review":
        result = read_result(working_directory, "adversarial_review_result.json")
        score, meta = score_adversarial_review(result)
    elif phase_name == "qa_validation":
        result = read_result(working_directory, "qa_result.json")
        # Enhancement 1: Pass working_directory for independent test verification
        score, meta = score_qa(result, spec, working_directory=working_directory)
    else:  # product_validation
        result = read_result(working_directory, "product_validation.json")
        score, meta = score_product_validation(result, spec)

    return {"score": score, "spec_gate": meta}
