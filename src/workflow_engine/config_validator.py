"""Workflow YAML config validator.

Validates all workflow YAML configurations at startup to catch errors
before the pipeline runs. Checks structural integrity, required fields,
cross-references between phases and orchestrator config, and naming
consistency.

Usage:
    from src.workflow_engine.config_validator import validate_all_workflows
    errors = validate_all_workflows()
    if errors:
        for e in errors:
            print(f"  [{e['severity']}] {e['file']}: {e['message']}")
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# ── Schema definitions ────────────────────────────────────────────

# Required top-level keys in workflow.yaml
WORKFLOW_REQUIRED_KEYS = {"orchestrator", "workflow", "launch_template"}

# Required keys inside orchestrator section
ORCHESTRATOR_REQUIRED_KEYS = {"type", "max_phase_retries", "evaluation_points"}

# Required keys inside workflow section
WORKFLOW_SECTION_REQUIRED_KEYS = {"result_criteria", "on_result_found"}

# Required keys inside launch_template section
LAUNCH_TEMPLATE_REQUIRED_KEYS = {"parameters"}

# Required keys for each launch parameter
LAUNCH_PARAM_REQUIRED_KEYS = {"name", "label", "type", "required"}

# Required keys in a phase YAML file (order matters for error reporting)
PHASE_REQUIRED_KEYS = ("id", "name", "description")

# Required keys in each orchestrator evaluation_point
EVAL_POINT_REQUIRED_KEYS = {"after_phase", "evaluator", "conditions"}

# Required keys in each evaluation condition
CONDITION_REQUIRED_KEYS = {"if", "action"}

# Valid action types for evaluation conditions
VALID_CONDITION_ACTIONS = {"continue", "goto", "retry"}

# Valid orchestrator types
VALID_ORCHESTRATOR_TYPES = {"evaluating", "sequential", "parallel"}


def _err(
    file: str,
    message: str,
    severity: str = "error",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a validation error dict."""
    entry: Dict[str, Any] = {
        "file": file,
        "message": message,
        "severity": severity,
    }
    if path:
        entry["path"] = path
    return entry


def validate_phase_file(
    phase_data: dict,
    filename: str,
    seen_ids: set,
    seen_names: set,
) -> List[Dict[str, Any]]:
    """Validate a single phase YAML file.

    Args:
        phase_data: Parsed YAML dict for the phase.
        filename: Source filename for error messages.
        seen_ids: Set of phase IDs already seen (mutated on success).
        seen_names: Set of phase names already seen (mutated on success).

    Returns:
        List of error dicts (empty if valid).
    """
    errors = []

    # Required keys
    for key in PHASE_REQUIRED_KEYS:
        if key not in phase_data:
            errors.append(_err(filename, f"Missing required key: {key}"))
            return errors  # Can't validate further without required keys

    phase_id = phase_data["id"]
    phase_name = phase_data["name"]

    # ID must be an integer
    if not isinstance(phase_id, int):
        errors.append(_err(filename, f"Phase id must be an integer, got {type(phase_id).__name__}: {phase_id}"))
        return errors

    # ID uniqueness
    if phase_id in seen_ids:
        errors.append(_err(filename, f"Duplicate phase id: {phase_id}"))
    else:
        seen_ids.add(phase_id)

    # Name uniqueness
    if phase_name in seen_names:
        errors.append(_err(filename, f"Duplicate phase name: '{phase_name}'"))
    else:
        seen_names.add(phase_name)

    # Name must be a non-empty string
    if not isinstance(phase_name, str) or not phase_name.strip():
        errors.append(_err(filename, "Phase name must be a non-empty string"))

    # Description must be a non-empty string
    description = phase_data.get("description", "")
    if not isinstance(description, str) or not description.strip():
        errors.append(_err(filename, "Phase description must be a non-empty string"))

    # done_definitions should be a list if present
    done_defs = phase_data.get("done_definitions")
    if done_defs is not None and not isinstance(done_defs, list):
        errors.append(_err(filename, f"done_definitions must be a list, got {type(done_defs).__name__}"))

    # thinking_level validation if present
    thinking = phase_data.get("thinking_level")
    if thinking is not None and thinking not in ("low", "medium", "high"):
        errors.append(
            _err(filename, f"Invalid thinking_level: '{thinking}' (must be low, medium, or high)")
        )

    return errors


def validate_workflow_yaml(
    cfg: dict,
    filename: str,
    phase_names: set,
) -> List[Dict[str, Any]]:
    """Validate the workflow.yaml configuration.

    Args:
        cfg: Parsed YAML dict for workflow.yaml.
        filename: Source filename for error messages.
        phase_names: Set of valid phase names from phase files.

    Returns:
        List of error dicts (empty if valid).
    """
    errors = []

    # ── Top-level required keys ───────────────────────────────────
    for key in WORKFLOW_REQUIRED_KEYS:
        if key not in cfg:
            errors.append(_err(filename, f"Missing required top-level key: '{key}'"))
    if errors:
        return errors  # Can't validate sections if top-level is broken

    # ── Orchestrator section ──────────────────────────────────────
    orch = cfg["orchestrator"]
    if not isinstance(orch, dict):
        errors.append(_err(filename, "orchestrator must be a dict"))
        return errors

    for key in ORCHESTRATOR_REQUIRED_KEYS:
        if key not in orch:
            errors.append(_err(filename, f"orchestrator missing required key: '{key}'", path="orchestrator"))

    # Validate orchestrator type
    orch_type = orch.get("type")
    if orch_type and orch_type not in VALID_ORCHESTRATOR_TYPES:
        errors.append(
            _err(
                filename,
                f"Invalid orchestrator type: '{orch_type}' (must be one of {sorted(VALID_ORCHESTRATOR_TYPES)})",
                path="orchestrator.type",
            )
        )

    # Validate max_phase_retries
    max_retries = orch.get("max_phase_retries")
    if max_retries is not None and (not isinstance(max_retries, int) or max_retries < 0):
        errors.append(
            _err(filename, f"orchestrator.max_phase_retries must be a non-negative integer, got {max_retries}",
                 path="orchestrator.max_phase_retries")
        )

    # Validate max_total_gotos
    max_gotos = orch.get("max_total_gotos")
    if max_gotos is not None and (not isinstance(max_gotos, int) or max_gotos < 0):
        errors.append(
            _err(filename, f"orchestrator.max_total_gotos must be a non-negative integer, got {max_gotos}",
                 path="orchestrator.max_total_gotos")
        )

    # ── Evaluation points ─────────────────────────────────────────
    eval_points = orch.get("evaluation_points", [])
    if not isinstance(eval_points, list):
        errors.append(_err(filename, "orchestrator.evaluation_points must be a list", path="orchestrator.evaluation_points"))
    else:
        seen_after_phases = set()
        for i, ep in enumerate(eval_points):
            ep_path = f"orchestrator.evaluation_points[{i}]"
            if not isinstance(ep, dict):
                errors.append(_err(filename, f"evaluation_point[{i}] must be a dict", path=ep_path))
                continue

            for key in EVAL_POINT_REQUIRED_KEYS:
                if key not in ep:
                    errors.append(_err(filename, f"evaluation_point[{i}] missing required key: '{key}'", path=ep_path))

            # after_phase must reference a valid phase
            after_phase = ep.get("after_phase")
            if after_phase is not None:
                if after_phase not in phase_names:
                    errors.append(
                        _err(
                            filename,
                            f"evaluation_point[{i}].after_phase references unknown phase: '{after_phase}' "
                            f"(valid: {sorted(phase_names)})",
                            path=f"{ep_path}.after_phase",
                        )
                    )
                if after_phase in seen_after_phases:
                    errors.append(
                        _err(
                            filename,
                            f"evaluation_point[{i}].after_phase duplicates '{after_phase}' (each phase can have at most one evaluation point)",
                            path=f"{ep_path}.after_phase",
                        )
                    )
                seen_after_phases.add(after_phase)

            # Validate conditions
            conditions = ep.get("conditions", [])
            if not isinstance(conditions, list):
                errors.append(
                    _err(filename, f"evaluation_point[{i}].conditions must be a list", path=f"{ep_path}.conditions")
                )
            else:
                for j, cond in enumerate(conditions):
                    cond_path = f"{ep_path}.conditions[{j}]"
                    if not isinstance(cond, dict):
                        errors.append(_err(filename, f"condition[{j}] must be a dict", path=cond_path))
                        continue

                    for key in CONDITION_REQUIRED_KEYS:
                        if key not in cond:
                            errors.append(
                                _err(filename, f"condition[{j}] missing required key: '{key}'", path=cond_path)
                            )

                    action = cond.get("action")
                    if action and action not in VALID_CONDITION_ACTIONS:
                        errors.append(
                            _err(
                                filename,
                                f"condition[{j}].action invalid: '{action}' (must be one of {sorted(VALID_CONDITION_ACTIONS)})",
                                path=f"{cond_path}.action",
                            )
                        )

                    # goto targets must reference valid phases
                    target = cond.get("target")
                    if target and target not in phase_names:
                        errors.append(
                            _err(
                                filename,
                                f"condition[{j}].target references unknown phase: '{target}'",
                                path=f"{cond_path}.target",
                            )
                        )

    # ── Workflow section ──────────────────────────────────────────
    wf = cfg.get("workflow")
    if not isinstance(wf, dict):
        errors.append(_err(filename, "workflow must be a dict"))
    else:
        for key in WORKFLOW_SECTION_REQUIRED_KEYS:
            if key not in wf:
                errors.append(_err(filename, f"workflow missing required key: '{key}'", path="workflow"))

        # Validate board columns if present
        board = wf.get("board")
        if board:
            columns = board.get("columns")
            if columns is not None:
                if not isinstance(columns, list):
                    errors.append(_err(filename, "workflow.board.columns must be a list", path="workflow.board.columns"))
                else:
                    col_ids = set()
                    for k, col in enumerate(columns):
                        if not isinstance(col, dict):
                            errors.append(
                                _err(filename, f"board.columns[{k}] must be a dict", path=f"workflow.board.columns[{k}]")
                            )
                            continue
                        for req in ("id", "name", "order"):
                            if req not in col:
                                errors.append(
                                    _err(
                                        filename,
                                        f"board.columns[{k}] missing required key: '{req}'",
                                        path=f"workflow.board.columns[{k}]",
                                    )
                                )
                        cid = col.get("id")
                        if cid:
                            if cid in col_ids:
                                errors.append(
                                    _err(filename, f"Duplicate board column id: '{cid}'", path=f"workflow.board.columns[{k}]")
                                )
                            col_ids.add(cid)

    # ── Launch template section ───────────────────────────────────
    lt = cfg.get("launch_template")
    if not isinstance(lt, dict):
        errors.append(_err(filename, "launch_template must be a dict"))
    else:
        params = lt.get("parameters")
        if params is not None:
            if not isinstance(params, list):
                errors.append(_err(filename, "launch_template.parameters must be a list", path="launch_template.parameters"))
            else:
                param_names = set()
                for k, param in enumerate(params):
                    param_path = f"launch_template.parameters[{k}]"
                    if not isinstance(param, dict):
                        errors.append(_err(filename, f"parameters[{k}] must be a dict", path=param_path))
                        continue
                    for req in LAUNCH_PARAM_REQUIRED_KEYS:
                        if req not in param:
                            errors.append(
                                _err(filename, f"parameters[{k}] missing required key: '{req}'", path=param_path)
                            )
                    pname = param.get("name")
                    if pname:
                        if pname in param_names:
                            errors.append(
                                _err(filename, f"Duplicate parameter name: '{pname}'", path=param_path)
                            )
                        param_names.add(pname)

    return errors


def validate_all_workflows(
    config_dir: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Validate all workflow YAML configs under config/workflows/.

    This is the main entry point. It scans for workflow directories,
    validates each one, and returns all errors found.

    Args:
        config_dir: Override config directory (default: config/workflows/)

    Returns:
        List of error dicts. Empty list means all configs are valid.
    """
    if config_dir is None:
        config_dir = Path(__file__).parent.parent.parent / "config" / "workflows"

    all_errors = []

    if not config_dir.exists():
        all_errors.append(_err(str(config_dir), "Config directory does not exist", severity="warning"))
        return all_errors

    # Find workflow directories (dirs containing workflow.yaml)
    workflow_dirs = sorted(
        d for d in config_dir.iterdir()
        if d.is_dir() and (d / "workflow.yaml").exists()
    )

    if not workflow_dirs:
        all_errors.append(_err(str(config_dir), "No workflow directories found", severity="warning"))
        return all_errors

    for wf_dir in workflow_dirs:
        wf_name = wf_dir.name
        wf_errors = validate_single_workflow(wf_dir)
        all_errors.extend(wf_errors)

        if not wf_errors:
            logger.info(f"[config_validator] Workflow '{wf_name}' OK")
        else:
            err_count = sum(1 for e in wf_errors if e["severity"] == "error")
            warn_count = sum(1 for e in wf_errors if e["severity"] == "warning")
            logger.warning(f"[config_validator] Workflow '{wf_name}': {err_count} error(s), {warn_count} warning(s)")

    return all_errors


def validate_single_workflow(wf_dir: Path) -> List[Dict[str, Any]]:
    """Validate a single workflow directory.

    Args:
        wf_dir: Path to the workflow directory (e.g. config/workflows/autopilot/).

    Returns:
        List of error dicts (empty if valid).
    """
    errors = []
    wf_name = wf_dir.name

    # ── Load and validate each phase file ─────────────────────────
    phase_files = sorted(wf_dir.glob("*.yaml"))
    phase_files = [f for f in phase_files if f.name != "workflow.yaml"]

    if not phase_files:
        errors.append(_err(wf_name, "No phase YAML files found", severity="warning"))

    seen_ids: set = set()
    seen_names: set = set()
    phase_names: set = set()

    for pf in phase_files:
        fname = f"{wf_name}/{pf.name}"
        try:
            phase_data = yaml.safe_load(pf.read_text())
        except yaml.YAMLError as e:
            errors.append(_err(fname, f"YAML parse error: {e}"))
            continue

        if not isinstance(phase_data, dict):
            errors.append(_err(fname, f"Phase file must contain a YAML mapping, got {type(phase_data).__name__}"))
            continue

        phase_errors = validate_phase_file(phase_data, fname, seen_ids, seen_names)
        errors.extend(phase_errors)

        if "name" in phase_data:
            phase_names.add(phase_data["name"])

    # ── Load and validate workflow.yaml ──────────────────────────
    workflow_yaml_path = wf_dir / "workflow.yaml"
    try:
        wf_cfg = yaml.safe_load(workflow_yaml_path.read_text())
    except FileNotFoundError:
        errors.append(_err(f"{wf_name}/workflow.yaml", "File not found"))
        return errors
    except yaml.YAMLError as e:
        errors.append(_err(f"{wf_name}/workflow.yaml", f"YAML parse error: {e}"))
        return errors

    if not isinstance(wf_cfg, dict):
        errors.append(_err(f"{wf_name}/workflow.yaml", "Workflow config must be a YAML mapping"))
        return errors

    wf_errors = validate_workflow_yaml(wf_cfg, f"{wf_name}/workflow.yaml", phase_names)
    errors.extend(wf_errors)

    return errors
