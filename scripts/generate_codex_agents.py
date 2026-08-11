#!/usr/bin/env python3
"""Generate Codex custom-agent TOML files from autopilot phase definitions."""

import json
from pathlib import Path

import yaml


def generate_codex_agent(phase_cfg: dict) -> str:
    """Generate one Codex custom-agent configuration for a workflow phase."""
    name = phase_cfg["name"]
    phase_num = phase_cfg["id"]
    agent_name = f"hephaestus-{name.replace('_', '-')}"
    role_title = name.replace("_", " ").title()
    description = phase_cfg.get("description", "").strip()
    first_line = description.splitlines()[0].strip() if description else role_title

    instructions = f"""You are the Hephaestus {role_title} agent (Phase {phase_num} of 13).

{description}

FILE PLACEMENT: deliverables go in the project's normal source tree. Any
scratch/exploratory output that isn't part of the deliverable goes under
.hephaestus/scratch/ — never the project root. Stuck on something unrelated
to your task? Don't write reports about it — work around it or fail the
task with a reason.

When your work is complete, call complete_my_task with status="done" and a
summary. If you cannot proceed, call it with status="failed" and explain why.
"""

    return "\n".join(
        (
            f"name = {json.dumps(agent_name)}",
            f"description = {json.dumps(f'Hephaestus Phase {phase_num}: {role_title} — {first_line}')}",
            "sandbox_mode = \"workspace-write\"",
            'developer_instructions = """',
            instructions.rstrip(),
            '"""',
            "",
        )
    )


def main():
    project_root = Path(__file__).parent.parent
    workflow_dir = project_root / "config" / "workflows" / "autopilot"
    output_dir = project_root / "agents" / "codex"
    phases = []

    for path in sorted(workflow_dir.glob("*.yaml")):
        if path.name != "workflow.yaml":
            phases.append(yaml.safe_load(path.read_text()))
    phases.sort(key=lambda phase: phase["id"])
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(phases)} phases in YAML")
    for phase in phases:
        filename = f"hephaestus-{phase['name'].replace('_', '-')}.toml"
        (output_dir / filename).write_text(generate_codex_agent(phase))
        print(f"  Generated: {filename}")

    print(f"\nGenerated {len(phases)} Codex agents in {output_dir}")
    print("Run install.sh to install them into ~/.codex/agents/")


if __name__ == "__main__":
    main()
