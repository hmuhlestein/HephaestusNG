"""Load autopilot phase config from YAML and assemble Phase objects."""
from pathlib import Path

import yaml

from src.sdk.models import LaunchParameter, LaunchTemplate, Phase, WorkflowConfig

_WORKFLOWS_PATH = Path(__file__).parent.parent.parent / "config" / "workflows"


def load_autopilot_config(workflow_name: str = "autopilot", config_dir: Path = None) -> dict:
    base = config_dir or _WORKFLOWS_PATH / workflow_name
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


def load_launch_template(cfg: dict, phase_1_task_prompt: str) -> LaunchTemplate:
    lt = cfg["launch_template"]
    params = [LaunchParameter(**p) for p in lt["parameters"]]
    return LaunchTemplate(parameters=params, phase_1_task_prompt=phase_1_task_prompt)
