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


def generate_pi_agent(phase_cfg: dict) -> str:
    """Generate pi agent markdown from YAML phase config.

    No model: field in the frontmatter -- cli_interface.py's PiAgent reads
    it from there and, when present, lets it silently override the model
    actually resolved from Phase.cli_model/config at launch time. That
    caused every phase's agent to keep launching on a stale model after a
    config-level model switch, until these files were manually
    regenerated. Omitting the field here means there's nothing to fall out
    of sync -- the launch-time resolution is the only source of truth.
    """

    name = phase_cfg["name"]
    phase_num = phase_cfg["id"]
    agent_name = f"hephaestus-{name.replace('_', '-')}"

    mcp_tools = [
        "mcp:heph/save_memory",
        "mcp:heph/search_memory",
        "mcp:heph/update_task_status",
        "mcp:heph/create_task",
        "mcp:heph/get_task_status",
        # Ticket tools: only development/qa_validation/security_review's
        # additional_notes actually instruct agents to use these (create+file
        # for QA/security_review, check+resolve for development), but the
        # tools: allowlist here is shared across every generated agent file --
        # granting access to all phases is simpler than threading a per-phase
        # tool list through this generator, and unused tool access is harmless
        # (a phase whose prompt never mentions tickets just never calls them).
        "mcp:heph/create_ticket",
        "mcp:heph/get_tickets",
        "mcp:heph/search_tickets",
        "mcp:heph/resolve_ticket",
        "mcp:heph/change_ticket_status",
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

FILE PLACEMENT: deliverables go in the project's normal source tree. Any
scratch/exploratory output that isn't part of the deliverable goes under
.hephaestus/scratch/ — never the project root. Stuck on something unrelated
to your task? Don't write reports about it — work around it or fail the
task with a reason.

When your work is complete, call:
  mcp__hephaestus__update_task_status(task_id=<id>, status="done", summary="...")
If you cannot proceed, call it with status="failed".
"""

    agent_md = f"""---
name: {agent_name}
description: "Hephaestus Phase {phase_num}: {role_title} — {first_line}"
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
        agent_content = generate_pi_agent(phase_cfg)

        name = phase_cfg["name"]
        agent_filename = f"hephaestus-{name.replace('_', '-')}.md"
        agent_path = output_dir / agent_filename
        agent_path.write_text(agent_content)

        print(f"  Generated: {agent_filename}")

    print(f"\nGenerated {len(phases)} pi agents in {output_dir}")
    print("Run install.sh to install them into ~/.pi/agent/agents/")


if __name__ == "__main__":
    main()
