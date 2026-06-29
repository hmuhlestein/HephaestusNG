"""Generic workflow YAML loader.

Loads workflow definitions from a directory containing:
  - workflow.yaml: shared config (model, thinking level, orchestrator, board, etc.)
  - <name>.yaml: one file per phase (anything that isn't workflow.yaml)

This is the canonical loader used by all workflows. It replaces the old
autopilot-specific src/autopilot/phase_loader.py.
"""

import logging
from pathlib import Path

import yaml

from src.sdk.models import (
    LaunchParameter,
    LaunchTemplate,
    Phase,
    ValidationCriteria,
    WorkflowConfig,
    WorkflowDefinition,
)

logger = logging.getLogger(__name__)


def load_workflow_from_dir(workflow_dir: Path) -> dict:
    """Load raw config dict from a workflow directory.

    Validates the directory with config_validator first, then reads workflow.yaml
    for shared config and all other *.yaml files as phase definitions.

    Args:
        workflow_dir: Path to directory containing workflow.yaml + phase files.

    Returns:
        Merged config dict with cfg["phases"] = list of phase dicts sorted by id.

    Raises:
        ValueError: If the directory contains config errors.
    """
    # Validate YAML configs before loading
    try:
        from src.workflow_engine.config_validator import validate_single_workflow

        errors = validate_single_workflow(workflow_dir)
        if errors:
            err_msgs = []
            for e in errors:
                severity = e["severity"]
                msg = f"[{severity.upper()}] {e['file']}: {e['message']}"
                err_msgs.append(msg)
                if severity == "error":
                    logger.error(msg)
                else:
                    logger.warning(msg)
            # Raise on errors (not warnings)
            real_errors = [e for e in errors if e["severity"] == "error"]
            if real_errors:
                raise ValueError(
                    f"Workflow '{workflow_dir.name}' has {len(real_errors)} config error(s):\n"
                    + "\n".join(err_msgs)
                )
    except ImportError:
        logger.debug("config_validator not available, skipping validation")
    except ValueError:
        raise
    except Exception as e:
        logger.warning(f"Config validation failed for '{workflow_dir.name}': {e}")

    # Load shared config from workflow.yaml
    workflow_yaml = workflow_dir / "workflow.yaml"
    if not workflow_yaml.exists():
        raise ValueError(f"workflow.yaml not found in {workflow_dir}")

    cfg = yaml.safe_load(workflow_yaml.read_text())

    # Load per-phase files (all *.yaml except workflow.yaml)
    phases = []
    for p in sorted(workflow_dir.glob("*.yaml")):
        if p.name == "workflow.yaml":
            continue
        phases.append(yaml.safe_load(p.read_text()))
    phases.sort(key=lambda x: x["id"])
    cfg["phases"] = phases
    return cfg


def build_phase(phase_cfg: dict, default_model: str, default_thinking: str) -> Phase:
    """Build a Phase (sdk) from a phase config dict.

    Args:
        phase_cfg: Parsed YAML dict for a single phase.
        default_model: Default model string inherited from workflow.yaml.
        default_thinking: Default thinking_level string inherited from workflow.yaml.

    Returns:
        Phase dataclass instance.
    """
    # Parse optional validation block
    validation = None
    if "validation" in phase_cfg:
        v = phase_cfg["validation"]
        if isinstance(v, dict):
            validation = ValidationCriteria(
                enabled=v.get("enabled", True),
                criteria=v.get("criteria", []),
            )

    return Phase(
        id=phase_cfg["id"],
        name=phase_cfg["name"],
        description=phase_cfg.get("description", ""),
        done_definitions=phase_cfg.get("done_definitions", []),
        additional_notes=phase_cfg.get("additional_notes", ""),
        thinking_level=phase_cfg.get("thinking_level", default_thinking),
        cli_model=phase_cfg.get("model") or phase_cfg.get("cli_model") or default_model,
        working_directory=phase_cfg.get("working_directory", "."),
        outputs=phase_cfg.get("outputs", []),
        next_steps=phase_cfg.get("next_steps", []),
        validation=validation,
    )


def build_phase_list(cfg: dict) -> list:
    """Return Phase objects in execution order from cfg.

    Args:
        cfg: Config dict as returned by load_workflow_from_dir().

    Returns:
        List of Phase objects ordered by execution_order (or sorted by id).
    """
    phases_by_id = {
        pc["id"]: build_phase(
            pc,
            cfg.get("default_model", "xiaomi/mimo-v2.5"),
            cfg.get("default_thinking_level", "low"),
        )
        for pc in cfg["phases"]
    }
    order = cfg.get("execution_order") or sorted(phases_by_id)
    return [phases_by_id[i] for i in order]


def load_workflow_config(cfg: dict) -> WorkflowConfig:
    """Build WorkflowConfig from cfg["workflow"].

    Args:
        cfg: Config dict as returned by load_workflow_from_dir().

    Returns:
        WorkflowConfig instance.
    """
    wf = cfg.get("workflow", {})
    board = wf.get("board", {})
    return WorkflowConfig(
        has_result=wf.get("has_result", False),
        result_criteria=wf.get("result_criteria"),
        on_result_found=wf.get("on_result_found", "do_nothing"),
        enable_tickets=wf.get("enable_tickets", False),
        board_config={
            "columns": board.get("columns", []),
            "ticket_types": board.get("ticket_types", ["task"]),
            "default_ticket_type": board.get("default_ticket_type", "task"),
            "initial_status": board.get("initial_status", "backlog"),
            "auto_assign": board.get("auto_assign", False),
            "require_comments_on_status_change": board.get(
                "require_comments_on_status_change", False
            ),
            "allow_reopen": board.get("allow_reopen", True),
            "track_time": board.get("track_time", False),
        }
        if board
        else None,
    )


def load_launch_template(cfg: dict) -> LaunchTemplate:
    """Build LaunchTemplate from cfg["launch_template"].

    Args:
        cfg: Config dict as returned by load_workflow_from_dir().

    Returns:
        LaunchTemplate instance, or None if the section is absent.
    """
    lt = cfg.get("launch_template")
    if not lt:
        return None
    params = [LaunchParameter(**p) for p in lt.get("parameters", [])]
    prompt = lt.get("phase_1_task_prompt", "")
    return LaunchTemplate(parameters=params, phase_1_task_prompt=prompt)


def load_full_workflow_definition(
    workflow_dir: Path, workflow_id: str = None
) -> WorkflowDefinition:
    """One-shot: load a workflow directory and return a ready WorkflowDefinition (sdk).

    Args:
        workflow_dir: Path to directory containing workflow.yaml + phase files.
        workflow_id: Identifier for the workflow. Defaults to the directory name.

    Returns:
        WorkflowDefinition instance populated with phases, config, and launch template.
    """
    cfg = load_workflow_from_dir(workflow_dir)
    wf_id = workflow_id or workflow_dir.name
    name = cfg.get("name", wf_id.replace("_", " ").replace("-", " ").title())

    return WorkflowDefinition(
        id=wf_id,
        name=name,
        phases=build_phase_list(cfg),
        config=load_workflow_config(cfg),
        description=cfg.get("description", ""),
        launch_template=load_launch_template(cfg),
        orchestrator_config=cfg.get("orchestrator"),
    )
