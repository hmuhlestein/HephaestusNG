"""Reconciliation between worktree directories on disk and AgentBranch rows.

Everything cleanup_all_stale_branches did before this was DB-driven: it walked
AgentBranch rows and git branches. Nothing reached a worktree *directory* that
had no row. create_agent_worktree does its git work first and writes the record
afterwards, so a failure in that window leaks a directory no sweep can ever
find -- observed live: seven orphans accumulated under .worktrees/, all with no
agent_worktrees row, and had to be removed by hand.

The reverse drift is real too: rows marked "active" whose directory is gone.
Those are reconciled only when the path is under *this* repo's worktree base --
agent_worktrees is shared across every project the installation has run, and a
path missing from here says nothing about another project's worktree.
"""

import subprocess
from pathlib import Path

import pytest


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real git repo with a worktree base, plus a WorktreeManager on it."""
    from src.core.database import DatabaseManager

    main = tmp_path / "repo"
    main.mkdir()
    _git(main, "init", "-b", "main")
    _git(main, "config", "user.email", "t@t")
    _git(main, "config", "user.name", "t")
    (main / "f.txt").write_text("x")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()

    from src.core.worktree_manager import WorktreeManager

    wm = WorktreeManager(db_manager=db)
    monkeypatch.setattr(type(wm), "worktree_base", property(lambda self: main / ".worktrees"))
    monkeypatch.setattr(wm, "main_repo", __import__("git").Repo(str(main)))
    wm.config.base_branch = "main"
    return wm, main, db


def _make_orphan(main, name, *, dirty=False, extra_commit=False):
    """A real git worktree with no AgentBranch row."""
    wt = main / ".worktrees" / name
    wt.parent.mkdir(exist_ok=True)
    _git(main, "worktree", "add", "-b", name.replace("wt_", "agent-"), str(wt))
    if extra_commit:
        (wt / "new.txt").write_text("work")
        _git(wt, "add", "-A")
        _git(wt, "commit", "-m", "unmerged work")
    if dirty:
        (wt / "scratch.txt").write_text("uncommitted")
    return wt


def test_reclaims_a_clean_fully_merged_orphan(repo):
    wm, main, db = repo
    wt = _make_orphan(main, "wt_clean")
    assert wt.is_dir()

    with db.session_scope() as session:
        reclaimed, preserved, rows = wm._reconcile_worktrees_with_db(session)

    assert reclaimed == 1, "a clean, fully-merged orphan should be reclaimed"
    assert preserved == 0
    assert not wt.exists()


def test_preserves_an_orphan_with_uncommitted_work(repo):
    """Abort-and-preserve: an orphan we cannot explain is exactly the case
    where destroying work is unrecoverable."""
    wm, main, db = repo
    wt = _make_orphan(main, "wt_dirty", dirty=True)

    with db.session_scope() as session:
        reclaimed, preserved, rows = wm._reconcile_worktrees_with_db(session)

    assert reclaimed == 0
    assert preserved == 1
    assert wt.is_dir(), "uncommitted work must survive reconciliation"
    assert (wt / "scratch.txt").exists()


def test_preserves_an_orphan_holding_unmerged_commits(repo):
    wm, main, db = repo
    wt = _make_orphan(main, "wt_unmerged", extra_commit=True)

    with db.session_scope() as session:
        reclaimed, preserved, rows = wm._reconcile_worktrees_with_db(session)

    assert reclaimed == 0
    assert preserved == 1
    assert (wt / "new.txt").exists(), "unmerged commits must survive"


def test_leaves_tracked_worktrees_alone(repo):
    """A directory WITH an AgentBranch row is not an orphan."""
    from src.core.database import AgentBranch

    wm, main, db = repo
    wt = _make_orphan(main, "wt_tracked")
    with db.session_scope() as session:
        session.add(
            AgentBranch(
                agent_id="agent-1",
                branch_name="agent-tracked",
                worktree_path=str(wt),
                parent_commit_sha="0" * 40,
                base_commit_sha="0" * 40,
                merge_status="active",
            )
        )
    with db.session_scope() as session:
        reclaimed, preserved, rows = wm._reconcile_worktrees_with_db(session)

    assert (reclaimed, preserved) == (0, 0)
    assert wt.is_dir()


def test_marks_active_row_cleaned_when_directory_is_gone(repo):
    from src.core.database import AgentBranch

    wm, main, db = repo
    ghost = main / ".worktrees" / "wt_ghost"
    with db.session_scope() as session:
        session.add(
            AgentBranch(
                agent_id="agent-ghost",
                branch_name="agent-ghost",
                worktree_path=str(ghost),
                parent_commit_sha="0" * 40,
                base_commit_sha="0" * 40,
                merge_status="active",
            )
        )

    with db.session_scope() as session:
        _, _, rows = wm._reconcile_worktrees_with_db(session)
    assert rows == 1

    with db.session_scope() as session:
        row = session.query(AgentBranch).filter_by(agent_id="agent-ghost").first()
        assert row.merge_status == "cleaned"


def test_does_not_touch_another_projects_rows(repo):
    """agent_worktrees is global; cleanup_all_stale_branches sees one repo.

    Measured on the live database: 56 of 176 rows were "active" with a missing
    directory, every one belonging to a different project. Reconciling those
    from here would be judging a repo this sweep cannot see.
    """
    from src.core.database import AgentBranch

    wm, main, db = repo
    with db.session_scope() as session:
        session.add(
            AgentBranch(
                agent_id="agent-other",
                branch_name="agent-other",
                worktree_path="/some/other/project/.worktrees/wt_other",
                parent_commit_sha="0" * 40,
                base_commit_sha="0" * 40,
                merge_status="active",
            )
        )

    with db.session_scope() as session:
        _, _, rows = wm._reconcile_worktrees_with_db(session)
    assert rows == 0, "another project's row must be left alone"

    with db.session_scope() as session:
        row = session.query(AgentBranch).filter_by(agent_id="agent-other").first()
        assert row.merge_status == "active"
