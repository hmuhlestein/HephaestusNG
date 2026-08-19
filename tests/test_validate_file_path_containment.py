"""Containment tests for validate_file_path (Phase 3 Tier 2 item 15).

The item's complaint was that the traversal check "rejects a path only if it
contains the literal substring '..', never resolving it" -- so an absolute path
needing no traversal at all, e.g. /etc/passwd, sailed through.

Banning absolute paths is not the fix. The server resolves and opens this path
in its own process while the agent that produced it runs in a worktree, so a
relative path would resolve against the wrong directory -- absolute IS the
contract. Worse, the failure would have been invisible: the endpoint tests mock
ResultService.create_result, so the validator never runs there and the suite
stays green while production breaks.

Containment is the fix that matches the threat. These tests pin both halves:
arbitrary system files are refused, and the two legitimate locations (the repo
and its worktrees, and the system temp dir) keep working.
"""

import os
import tempfile
from pathlib import Path

import pytest

from src.services.validation_helpers import validate_file_path


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/etc/shadow",
        "/root/.ssh/id_rsa",
        "/usr/bin/python3",
    ],
)
def test_rejects_absolute_paths_outside_allowed_roots(path):
    """No traversal sequence needed -- this is the gap item 15 names."""
    with pytest.raises(ValueError, match="outside allowed directories"):
        validate_file_path(path)


def test_rejects_traversal_escaping_to_a_system_file():
    with pytest.raises(ValueError):
        validate_file_path("../../../../etc/passwd")


def test_allows_a_file_in_the_system_temp_dir():
    """Where every current caller's result file actually lives."""
    with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as f:
        p = f.name
    try:
        validate_file_path(p)
    finally:
        os.unlink(p)


def test_allows_a_file_inside_the_repo():
    validate_file_path(str(Path.cwd() / "docs" / "report.md"))


def test_allows_a_worktree_path():
    """Worktrees are the other location the docstring named as load-bearing."""
    validate_file_path(str(Path.cwd() / ".worktrees" / "wt_abc" / "out.md"))


def test_explicit_allowed_root_still_narrows(tmp_path):
    """A caller that HAS a real root can still pass one, and it wins."""
    inside = tmp_path / "ok.md"
    validate_file_path(str(inside), allowed_root=str(tmp_path))

    with pytest.raises(ValueError, match="outside allowed directories"):
        validate_file_path(str(tmp_path / "ok.md"), allowed_root=str(tmp_path / "sub"))


def test_segment_check_does_not_false_positive_on_dots_in_a_filename():
    """The pre-existing improvement this builds on: '..' as a path segment,
    not as a substring of the raw string."""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "notes..final.md"
        p.write_text("x")
        validate_file_path(str(p))


def test_a_cwd_launched_server_does_not_void_the_guard(monkeypatch):
    """config.main_repo_path/project_root both default to Path.cwd().

    Launch the server from "/" or the home directory and every allowed root
    becomes an ancestor of everything readable -- the containment check would
    still "pass" while enforcing nothing. Those roots are dropped instead.
    """
    from pathlib import Path

    import src.services.validation_helpers as vh

    class _Cfg:
        main_repo_path = Path("/")
        project_root = Path.home()
        worktree_base_path = None

    monkeypatch.setattr(
        "src.core.simple_config.get_config", lambda *a, **k: _Cfg(), raising=False
    )

    roots = vh._default_allowed_roots()
    assert Path("/") not in roots, "filesystem root must never be a containment root"
    assert Path.home().resolve() not in roots, "home dir must never be one either"

    # And the guard still bites: a system file is refused even though the
    # (rejected) roots would have contained it.
    with pytest.raises(ValueError, match="outside allowed directories"):
        vh.validate_file_path("/etc/passwd")


@pytest.mark.parametrize(
    "root,expected",
    [
        ("/", True),
        (str(Path.home()), True),
        (str(Path.home().parent), True),
        ("/opt/app", False),
    ],
)
def test_breadth_check_classifies_roots(root, expected):
    from src.services.validation_helpers import _too_broad_to_contain

    assert _too_broad_to_contain(Path(root).resolve()) is expected
