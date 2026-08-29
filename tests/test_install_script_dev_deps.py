"""Regression for ticket-db4720ed: diff-cover was declared in pyproject.toml's
dev dependency group but scripts/install.sh --dev installs a separate,
hardcoded pip list that never included it -- every agent's actual venv
comes from install.sh, not from pyproject.toml directly, so diff-cover was
silently absent everywhere done_definitions call for it. Also, the
"already installed" guard only checked for `pytest`, so a venv missing
just diff-cover would never re-trigger the install step.
"""

from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def _dev_deps_block() -> str:
    content = INSTALL_SH.read_text(encoding="utf-8")
    start = content.index('if [ "$DEV_MODE" = true ]; then')
    end = content.index("\nfi", start)
    return content[start:end]


def test_dev_deps_pip_install_includes_diff_cover():
    block = _dev_deps_block()
    assert "diff-cover" in block, "install.sh --dev must pip install diff-cover, not just declare it in pyproject.toml"


def test_dev_deps_already_installed_guard_checks_diff_cover_too():
    block = _dev_deps_block()
    guard_line = block.splitlines()[1]
    assert "diff_cover" in guard_line, "the already-installed guard must check for diff_cover alongside pytest -- otherwise a venv with pytest but not diff_cover skips reinstalling forever"
