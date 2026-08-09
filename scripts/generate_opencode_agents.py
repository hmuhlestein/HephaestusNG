#!/usr/bin/env python3
"""
Generate OpenCode subagent files from the autopilot YAML config.

Reads config/workflows/autopilot/*.yaml (per-phase files) and generates one
OpenCode subagent .md file per phase in the agents/opencode/ directory.

OpenCode stores agents in its database (opencode.db), not discovered from
files.  Currently the --agent flag on `opencode run` only works with
database-registered agents.  This script generates the files for
documentation and future use; Hephaestus currently passes the full system
prompt as the initial message for OpenCode agents (see
OpenCodeAgent.get_launch_command in cli_interface.py).
"""

import sys
from pathlib import Path

import yaml

_project_root = Path(__file__).parent.parent
sys.path.insert(0, str(_project_root))


def generate_opencode_agent(phase_cfg: dict) -> str:
    """Generate an OpenCode subagent markdown file from YAML phase config."""

    name = phase_cfg["name"]
    phase_num = phase_cfg["id"]
    agent_name = f"hephaestus-{name.replace('_', '-')}"

    role_title = name.replace("_", " ").title()
    description = phase_cfg.get("description", "").strip()
    first_line = description.splitlines()[0].strip() if description else role_title

    body = f"""You are the Hephaestus {role_title} agent (Phase {phase_num} of 14).

{description}

FILE PLACEMENT: deliverables go in the project's normal source tree. Any
scratch/exploratory output that isn't part of the deliverable goes under
.hephaestus/scratch/ — never the project root. Stuck on something unrelated
to your task? Don't write reports about it — work around it or fail the
task with a reason.

When your work is complete, call complete_my_task with
status="done" and a summary. If you cannot proceed, call it with
status="failed" and explain why.
"""

    return f"""---
description: "Hephaestus Phase {phase_num}: {role_title} — {first_line}"
mode: subagent
permission:
  bash: allow
  read: allow
  edit: allow
  glob: allow
  grep: allow
  task: allow
  todowrite: allow
  webfetch: allow
  websearch: allow
---
{body}"""


def main():
    project_root = Path(__file__).parent.parent
    workflow_dir = project_root / "config" / "workflows" / "autopilot"
    output_dir = project_root / "agents" / "opencode"

    phases = []
    for p in sorted(workflow_dir.glob("*.yaml")):
        if p.name == "workflow.yaml":
            continue
        with open(p) as f:
            phases.append(yaml.safe_load(f))
    phases.sort(key=lambda x: x["id"])

    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(phases)} phases in YAML")

    for phase_cfg in phases:
        agent_content = generate_opencode_agent(phase_cfg)
        name = phase_cfg["name"]
        agent_filename = f"hephaestus-{name.replace('_', '-')}.md"
        agent_path = output_dir / agent_filename
        agent_path.write_text(agent_content)
        print(f"  Generated: {agent_filename}")

    print(f"\nGenerated {len(phases)} OpenCode agents in {output_dir}")
    print("NOTE: OpenCode stores agents in its database, not files.")
    print("These files are for documentation and future import.")


if __name__ == "__main__":
    main()
