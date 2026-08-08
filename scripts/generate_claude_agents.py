#!/usr/bin/env python3
"""
Generate Claude Code subagent files from the autopilot YAML config.

Reads config/workflows/autopilot/*.yaml (per-phase files) and generates one
Claude Code subagent .md file per phase in the agents/claude/ directory.
Mirrors generate_pi_agents.py so both CLIs are launched via their own
officially supported named-agent mechanism (pi: agent file read directly by
PiAgent; Claude Code: `claude --agent <name>`, which requires the name to
already be a discovered subagent under ~/.claude/agents/).
"""

import sys
from pathlib import Path

import yaml

# Add project root to path so we can import phase modules for descriptions
_project_root = Path(__file__).parent.parent
sys.path.insert(0, str(_project_root))


def generate_claude_agent(phase_cfg: dict) -> str:
    """Generate a Claude Code subagent markdown file from YAML phase config.

    No model: field in the frontmatter -- ClaudeCodeAgent.get_launch_command
    always passes --model resolved from Phase.cli_model/config at launch
    time, so a model: field here would only be able to fall out of sync with
    it (see generate_pi_agents.py, which hit exactly this bug for pi).

    No tools: field either -- Hephaestus launches every Claude Code agent
    with --dangerously-skip-permissions, which already grants full tool
    access; restricting tools: here would only fight that, not add safety.
    """

    name = phase_cfg["name"]
    phase_num = phase_cfg["id"]
    agent_name = f"hephaestus-{name.replace('_', '-')}"

    role_title = name.replace("_", " ").title()

    # Description from YAML (strip trailing newline from block scalar)
    description = phase_cfg.get("description", "").strip()
    first_line = description.splitlines()[0].strip() if description else role_title

    # Identity-only body: who this agent is and how to signal completion.
    # The full phase instructions arrive in the task prompt via the
    # follow-up tmux message (AgentPromptBuilder), not this file.
    identity = f"""You are the Hephaestus {role_title} agent (Phase {phase_num} of 10).

{description}

FILE PLACEMENT: deliverables go in the project's normal source tree. Any
scratch/exploratory output that isn't part of the deliverable goes under
.hephaestus/scratch/ — never the project root. Stuck on something unrelated
to your task? Don't write reports about it — work around it or fail the
task with a reason.

When your work is complete, call your update_task_status tool with
status="done" and a summary. If you cannot proceed, call it with
status="failed" and explain why.
"""

    agent_md = f"""---
name: {agent_name}
description: "Hephaestus Phase {phase_num}: {role_title} — {first_line}"
---

{identity}"""

    return agent_md


def main():
    """Main function to generate Claude Code subagents from YAML config."""
    project_root = Path(__file__).parent.parent
    workflow_dir = project_root / "config" / "workflows" / "autopilot"
    output_dir = project_root / "agents" / "claude"

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
        agent_content = generate_claude_agent(phase_cfg)

        name = phase_cfg["name"]
        agent_filename = f"hephaestus-{name.replace('_', '-')}.md"
        agent_path = output_dir / agent_filename
        agent_path.write_text(agent_content)

        print(f"  Generated: {agent_filename}")

    print(f"\nGenerated {len(phases)} Claude Code agents in {output_dir}")
    print("Run install.sh to install them into ~/.claude/agents/")


if __name__ == "__main__":
    main()
