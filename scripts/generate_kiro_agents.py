#!/usr/bin/env python3
"""Generate Kiro CLI custom-agent JSON files from autopilot phase definitions.

Mirrors generate_codex_agents.py / generate_claude_agents.py so every CLI is
launched via its own officially supported named-agent mechanism. Kiro
discovers agents from ~/.kiro/agents/*.json and launches one with
`kiro-cli chat --agent <name>` (see KiroAgent.get_launch_command).

Each agent JSON embeds the phase identity inline in the `prompt` field
(verified: `kiro-cli agent validate` accepts an inline prompt string, so no
external file:// pointer is needed) and registers the heph MCP server inline
so a Kiro-launched agent has the heph_* tools — complete_my_task included —
regardless of which project/worktree it runs in. The MCP server command/args
are resolved at generation time from the running interpreter and this repo's
mcp/mcp_client.py, matching how install.sh resolves them for the other CLIs.
"""

import json
import sys
from pathlib import Path

import yaml

_project_root = Path(__file__).parent.parent


def _mcp_server_config() -> dict:
    """heph MCP server entry for a generated Kiro agent.

    Prefers the repo's own venv python so the server runs with Hephaestus's
    installed dependencies; falls back to the running interpreter. The script
    path is this repo's mcp/mcp_client.py (absolute, so it resolves from any
    working directory a Kiro agent launches in)."""
    venv_python = _project_root / ".venv" / "bin" / "python"
    python_path = str(venv_python) if venv_python.exists() else sys.executable
    mcp_script = str(_project_root / "mcp" / "mcp_client.py")
    return {"command": python_path, "args": [mcp_script]}


def generate_kiro_agent(phase_cfg: dict, total_phases: int, mcp_server: dict) -> str:
    """Generate one Kiro CLI custom-agent JSON for a workflow phase.

    No model field: KiroAgent.get_launch_command always passes --model
    resolved from Phase.cli_model/config at launch time, so a model here
    could only fall out of sync with it (the exact bug the pi/claude
    generators call out). tools:["*"] grants full tool access, consistent
    with launching under --trust-all-tools; restricting it here would only
    fight that flag, not add safety.
    """
    name = phase_cfg["name"]
    phase_num = phase_cfg["id"]
    agent_name = f"hephaestus-{name.replace('_', '-')}"
    role_title = name.replace("_", " ").title()
    description = phase_cfg.get("description", "").strip()
    first_line = description.splitlines()[0].strip() if description else role_title

    identity = f"""You are the Hephaestus {role_title} agent (Phase {phase_num} of {total_phases}).

{description}

FILE PLACEMENT: deliverables go in the project's normal source tree. Any
scratch/exploratory output that isn't part of the deliverable goes under
.hephaestus/scratch/ — never the project root. Stuck on something unrelated
to your task? Don't write reports about it — work around it or fail the
task with a reason.

When your work is complete, call complete_my_task with status="done" and a
summary. If you cannot proceed, call it with status="failed" and explain why."""

    agent = {
        "name": agent_name,
        "description": f"Hephaestus Phase {phase_num}: {role_title} — {first_line}",
        "prompt": identity,
        "tools": ["*"],
        "useLegacyMcpJson": False,
        "mcpServers": {"heph": mcp_server},
    }
    return json.dumps(agent, indent=2) + "\n"


def main():
    workflow_dir = _project_root / "config" / "workflows" / "autopilot"
    output_dir = _project_root / "agents" / "kiro"

    phases = []
    for path in sorted(workflow_dir.glob("*.yaml")):
        if path.name != "workflow.yaml":
            phases.append(yaml.safe_load(path.read_text()))
    phases.sort(key=lambda phase: phase["id"])

    output_dir.mkdir(parents=True, exist_ok=True)

    # Remove existing generated agents first -- a phase that gets renamed or
    # merged into another otherwise leaves its stale .json behind forever,
    # since the loop below only writes the current phase set's filenames.
    for stale in output_dir.glob("hephaestus-*.json"):
        stale.unlink()

    mcp_server = _mcp_server_config()

    print(f"Found {len(phases)} phases in YAML")
    for phase in phases:
        filename = f"hephaestus-{phase['name'].replace('_', '-')}.json"
        (output_dir / filename).write_text(
            generate_kiro_agent(phase, len(phases), mcp_server)
        )
        print(f"  Generated: {filename}")

    print(f"\nGenerated {len(phases)} Kiro agents in {output_dir}")
    print("Run install.sh to install them into ~/.kiro/agents/")


if __name__ == "__main__":
    main()
