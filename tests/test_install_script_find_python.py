"""Regression (external evaluation finding, §4): find_python() in
install.sh tried literal python3.12/python3.11 names BEFORE python3
itself. On a host whose `python3` correctly resolves to a valid, newer
interpreter (via pyenv/homebrew/a venv shim) but has no binary literally
named "python3.12" or "python3.11" on PATH, the old loop tried two
nonexistent names first and could still end up on an unrelated, too-old
system python3/python, or fail to find Python at all -- despite a
perfectly good interpreter being right there. Also reconciles the
Python version floor stated three different ways across the repo
(README said 3.12+, check_setup_macos.py said 3.10+, install.sh/
pyproject.toml actually enforce 3.11+) down to the one real floor.
"""

from pathlib import Path

INSTALL_SH = Path(__file__).resolve().parent.parent / "scripts" / "install.sh"


def _find_python_block() -> str:
    content = INSTALL_SH.read_text(encoding="utf-8")
    start = content.index("find_python() {")
    end = content.index("\n}", start)
    return content[start:end]


def test_python3_is_tried_before_literal_minor_version_names():
    """python3 must be the FIRST candidate -- it's what pyenv/homebrew/a
    venv actually expose as the correct default on a normal setup."""
    block = _find_python_block()
    for_line = next(line for line in block.splitlines() if line.strip().startswith("for cmd in"))
    candidates = for_line.split("for cmd in", 1)[1].split(";")[0].split()
    assert candidates[0] == "python3", f"python3 must be tried first, got order: {candidates}"


def test_candidate_list_has_a_buffer_of_future_minor_versions():
    """A hardcoded list that only covers today's latest Python versions
    silently stops finding anything the moment a host's only valid
    interpreter is newer than whatever was hardcoded when this script
    was last touched."""
    block = _find_python_block()
    assert "python3.13" in block
    assert "python3.14" in block


def test_still_checks_actual_reported_version_not_just_existence():
    """The whole point of PYTHON_MIN_VERSION -- a candidate merely
    existing on PATH isn't enough, it must actually satisfy the floor."""
    block = _find_python_block()
    assert "PYTHON_MIN_VERSION" in block


def test_install_sh_is_valid_bash():
    import subprocess

    result = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_python_min_version_matches_pyproject_floor():
    """install.sh's PYTHON_MIN_VERSION and pyproject.toml's own
    `python = "^3.X"` constraint must agree -- this is the single
    authoritative floor every other doc (README, check_setup_macos.py)
    should be restating, not independently guessing at."""
    install_content = INSTALL_SH.read_text(encoding="utf-8")
    min_version_line = next(
        line for line in install_content.splitlines() if line.startswith("PYTHON_MIN_VERSION=")
    )
    min_version = min_version_line.split("=", 1)[1].strip('"')

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    pyproject_content = pyproject.read_text(encoding="utf-8")
    python_constraint_line = next(
        line for line in pyproject_content.splitlines() if line.strip().startswith("python =")
    )
    # e.g. python = "^3.11" -> "311"
    import re

    m = re.search(r"\^3\.(\d+)", python_constraint_line)
    assert m, f"could not parse pyproject.toml's python constraint: {python_constraint_line!r}"
    pyproject_min = f"3{m.group(1)}"

    assert min_version == pyproject_min, (
        f"install.sh's PYTHON_MIN_VERSION={min_version!r} disagrees with "
        f"pyproject.toml's python constraint (implies {pyproject_min!r})"
    )
