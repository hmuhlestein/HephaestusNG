#!/usr/bin/env python3
"""
Generate pi agent files from the autopilot YAML config.

Reads config/workflows/autopilot/workflow.yaml and per-phase YAMLs,
generates pi agent .md files in the agents/pi/ directory.
"""

import sys
from pathlib import Path

import yaml

# Add project root to path so we can import phase modules for descriptions
_project_root = Path(__file__).parent.parent
sys.path.insert(0, str(_project_root))


def generate_pi_agent(phase_cfg: dict, default_model: str) -> str:
    """Generate pi agent markdown from YAML phase config."""

    name = phase_cfg["name"]
    phase_num = phase_cfg["id"]
    agent_name = f"hephaestus-{name.replace('_', '-')}"

    model_slug = phase_cfg.get("model", default_model)
    model = f"openrouter/{model_slug}"

    mcp_tools = [
        "mcp:hephaestus/save_memory",
        "mcp:hephaestus/search_memory",
        "mcp:hephaestus/update_task_status",
        "mcp:hephaestus/create_task",
        "mcp:hephaestus/get_task_status",
    ]

    tools_str = "read, write, edit, bash, grep, find, ls, " + ", ".join(mcp_tools)
    role_title = name.replace("_", " ").title()

    # Description from YAML (strip trailing newline from block scalar)
    description = phase_cfg.get("description", "").strip()
    first_line = description.splitlines()[0].strip() if description else role_title

    # Identity-only body: who this agent is and how to signal completion.
    # The full phase instructions arrive in the task prompt via PhaseContext.
    identity = f"""You are the Hephaestus {role_title} agent (Phase {phase_num} of 10).

{description}

When your work is complete, call:
  mcp__hephaestus__update_task_status(task_id=<id>, status="done", summary="...")
If you cannot proceed, call it with status="failed".
"""

    agent_md = f"""---
name: {agent_name}
description: "Hephaestus Phase {phase_num}: {role_title} — {first_line}"
model: {model}
tools: {tools_str}
systemPromptMode: replace
inheritProjectContext: true
inheritSkills: false
---

{identity}"""

    return agent_md


def main():
    """Main function to generate pi agents from YAML config."""
    project_root = Path(__file__).parent.parent
    workflow_dir = project_root / "config" / "workflows" / "autopilot"
    output_dir = project_root / "agents" / "pi"

    # Load shared config for default_model
    with open(workflow_dir / "workflow.yaml") as f:
        shared_cfg = yaml.safe_load(f)

    default_model = shared_cfg.get("default_model", "xiaomi/mimo-v2.5")

    # Load per-phase files (skip workflow.yaml)
    phases = []
    for p in sorted(workflow_dir.glob("*.yaml")):
        if p.name == "workflow.yaml":
            continue
        with open(p) as f:
            phases.append(yaml.safe_load(f))
    phases.sort(key=lambda x: x["id"])

    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(phases)} phases in YAML")

    for phase_cfg in phases:
        agent_content = generate_pi_agent(phase_cfg, default_model)

        name = phase_cfg["name"]
        agent_filename = f"hephaestus-{name.replace('_', '-')}.md"
        agent_path = output_dir / agent_filename
        agent_path.write_text(agent_content)

        print(f"  Generated: {agent_filename}")

    print(f"\nGenerated {len(phases)} pi agents in {output_dir}")
    print("Run install.sh to install them into ~/.pi/agent/agents/")


if __name__ == "__main__":
    main()
