"""Load autopilot phase config from YAML and assemble Phase objects."""
import logging
from pathlib import Path

import yaml

from src.sdk.models import LaunchParameter, LaunchTemplate, Phase, WorkflowConfig

logger = logging.getLogger(__name__)

_WORKFLOWS_PATH = Path(__file__).parent.parent.parent / "config" / "workflows"


def load_autopilot_config(workflow_name: str = "autopilot", config_dir: Path = None) -> dict:
    base = config_dir or _WORKFLOWS_PATH / workflow_name

    # Validate YAML configs before loading
    try:
        from src.workflow_engine.config_validator import validate_single_workflow
        errors = validate_single_workflow(base)
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
            # Raise on errors (not warnings) to fail fast on broken configs
            real_errors = [e for e in errors if e["severity"] == "error"]
            if real_errors:
                raise ValueError(
                    f"Workflow '{workflow_name}' has {len(real_errors)} config error(s):\n"
                    + "\n".join(err_msgs)
                )
    except ImportError:
        logger.debug("config_validator not available, skipping validation")
    except Exception as e:
        logger.warning(f"Config validation failed for '{workflow_name}': {e}")

    # Load shared config
    cfg = yaml.safe_load((base / "_workflow.yaml").read_text())
    # Load per-phase files
    phases = []
    for p in sorted(base.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        phases.append(yaml.safe_load(p.read_text()))
    phases.sort(key=lambda x: x["id"])
    cfg["phases"] = phases
    return cfg


def build_phase(
    phase_cfg: dict,
    default_model: str,
    default_thinking: str,
) -> Phase:
    return Phase(
        id=phase_cfg["id"],
        name=phase_cfg["name"],
        description=phase_cfg.get("description", ""),
        done_definitions=phase_cfg.get("done_definitions", []),
        additional_notes=phase_cfg.get("additional_notes", ""),
        thinking_level=phase_cfg.get("thinking_level", default_thinking),
        cli_model=phase_cfg.get("model", default_model),
        working_directory=phase_cfg.get("working_directory"),
        outputs=phase_cfg.get("outputs", []),
        next_steps=phase_cfg.get("next_steps", []),
    )


def load_workflow_config(cfg: dict) -> WorkflowConfig:
    wf = cfg["workflow"]
    board = wf["board"]
    return WorkflowConfig(
        has_result=True,
        result_criteria=wf["result_criteria"],
        on_result_found=wf["on_result_found"],
        enable_tickets=wf.get("enable_tickets", True),
        board_config={
            "columns": board["columns"],
            "ticket_types": board["ticket_types"],
            "default_ticket_type": board["default_ticket_type"],
            "initial_status": board["initial_status"],
            "auto_assign": board["auto_assign"],
            "require_comments_on_status_change": board["require_comments_on_status_change"],
            "allow_reopen": board["allow_reopen"],
            "track_time": board["track_time"],
        },
    )


def load_launch_template(cfg: dict) -> LaunchTemplate:
    lt = cfg["launch_template"]
    params = [LaunchParameter(**p) for p in lt["parameters"]]
    prompt = lt.get("phase_1_task_prompt", "")
    return LaunchTemplate(parameters=params, phase_1_task_prompt=prompt)


def build_phase_list(cfg: dict) -> list:
    """Return Phase objects in execution order from cfg."""
    phases_by_id = {
        pc["id"]: build_phase(pc, cfg.get("default_model", "xiaomi/mimo-v2.5"), cfg.get("default_thinking_level", "low"))
        for pc in cfg["phases"]
    }
    order = cfg.get("execution_order") or sorted(phases_by_id)
    return [phases_by_id[i] for i in order]
