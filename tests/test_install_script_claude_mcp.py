"""Regression (external evaluation finding, §2.2): a claude-only install
(no pi) never registered Hephaestus's MCP server for Claude Code at all.
cli_interface.py's --mcp-config flag construction only fires when
~/.config/mcp/mcp.json exists, and only the Pi branch of install.sh ever
wrote that file -- so every claude-launched agent had zero heph_* tools
available, complete_my_task included, and no phase could ever finish.

Fixed by giving Claude Code its own MCP registration block, using the
CLI's own native `claude mcp add` (mirroring the pre-existing Codex CLI
block, which already used `codex mcp add` for the identical reason)
rather than depending on Pi's file or Pi being installed at all.
"""

from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def _claude_mcp_block() -> str:
    content = INSTALL_SH.read_text(encoding="utf-8")
    start = content.index('header "Claude Code MCP Configuration"')
    end = content.index('header "Claude Code Agents"', start)
    return content[start:end]


def test_claude_mcp_block_exists_before_agent_installation():
    """Sanity check that the section markers this test relies on are
    actually present -- _claude_mcp_block() raises ValueError otherwise,
    which would make every other test in this file fail with a confusing
    traceback instead of a clear message."""
    block = _claude_mcp_block()
    assert block


def test_claude_mcp_gate_checks_claude_not_pi():
    """The whole point of this fix: registration must depend on whether
    claude is installed, never on whether pi is."""
    block = _claude_mcp_block()
    assert "command -v claude" in block
    assert "command -v pi" not in block


def test_claude_mcp_uses_native_add_command():
    block = _claude_mcp_block()
    assert "claude mcp add" in block
    assert "claude mcp get heph" in block


def test_claude_mcp_registers_at_user_scope():
    """Must be user-scoped, not project/local-scoped -- Hephaestus
    launches claude from a different cwd per agent (each feature's own
    worktree under .worktrees/), not from a single fixed project
    directory a local-scoped registration would be tied to."""
    block = _claude_mcp_block()
    assert "-s user" in block


def test_claude_mcp_points_at_this_repos_mcp_client():
    block = _claude_mcp_block()
    assert "mcp/mcp_client.py" in block
    assert "$VENV_DIR/bin/python" in block


def test_claude_mcp_is_idempotent_and_updates_stale_config():
    """Matches the Codex block's shape: re-running the installer with an
    already-correct registration must not blindly remove+re-add every
    time, but a registration pointing at a stale/wrong path must be
    detected and corrected."""
    block = _claude_mcp_block()
    assert "already configured" in block.lower()
    assert "claude mcp remove heph" in block


def test_claude_mcp_uses_a_lock_dir_against_concurrent_installers():
    """Matches the Codex block's own concurrency guard -- two installer
    runs racing on the same shared ~/.claude.json could otherwise
    corrupt each other's writes."""
    block = _claude_mcp_block()
    assert "mkdir" in block
    assert "lock" in block.lower()


def test_install_sh_is_valid_bash():
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
