import subprocess
import sys
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def test_generated_codex_agents_are_valid_custom_agent_files():
    script = PROJECT_ROOT / "scripts" / "generate_codex_agents.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    agents = sorted((PROJECT_ROOT / "agents" / "codex").glob("*.toml"))
    assert len(agents) == 14

    for agent_path in agents:
        with agent_path.open("rb") as file:
            agent = tomllib.load(file)
        assert agent["name"] == agent_path.stem
        assert agent["description"].startswith("Hephaestus Phase ")
        assert agent["sandbox_mode"] == "workspace-write"
        assert "call complete_my_task" in agent["developer_instructions"]
