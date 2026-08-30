"""The one git-repo rule every entry point shares.

POST /projects, POST /autopilot/start, project activation,
AutopilotService.start(), `heph project add` and `heph autopilot start` each
used to carry their own copy. They had drifted: the two service-layer copies
knew a multi-repo workspace root deliberately need not be a repository itself
(git resolves through registered ProjectRepo rows -- see repo_resolution), and
the newer route-layer checks did not, so a legitimate multi-repo project was
refused at the door.
"""

import subprocess

import pytest

from src.core.repo_resolution import git_repo_error


def _git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def test_a_repository_is_accepted(tmp_path):
    assert git_repo_error(_git_init(tmp_path / "repo")) is None


def test_a_linked_worktree_is_accepted(tmp_path):
    """.git is a FILE in a linked worktree or submodule checkout, so the check
    has to be .exists(), not .is_dir()."""
    fake_worktree = tmp_path / "wt"
    fake_worktree.mkdir()
    (fake_worktree / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")

    assert git_repo_error(fake_worktree) is None


def test_a_plain_directory_is_refused_with_an_actionable_message(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    message = git_repo_error(plain)
    assert "not a git repository" in message
    assert "git init" in message


def test_a_missing_path_is_refused(tmp_path):
    assert "Not a directory" in git_repo_error(tmp_path / "nope")


def test_a_workspace_root_needs_the_allowance(tmp_path):
    """Creating a project is the one caller that runs before any repo can be
    registered, so a child repository is its only evidence of a workspace."""
    workspace = tmp_path / "workspace"
    _git_init(workspace / "front-end")
    (workspace / "back-end").mkdir()

    assert git_repo_error(workspace) is not None
    assert git_repo_error(workspace, allow_workspace_root=True) is None


def test_a_directory_of_non_repos_is_still_refused(tmp_path):
    workspace = tmp_path / "empty-workspace"
    (workspace / "a").mkdir(parents=True)
    (workspace / "b").mkdir()

    assert git_repo_error(workspace, allow_workspace_root=True) is not None


def test_registered_project_repos_exempt_a_non_repo_root(tmp_path, monkeypatch):
    """The multi-repo case the service layer already honored: with repos
    registered, the workspace root itself never needs to be a repository."""
    from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo

    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db = DatabaseManager(db_path)
    db.create_tables()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    child = _git_init(workspace / "front-end")

    with db.session_scope() as session:
        session.add(
            AutopilotProject(id="proj-multi", name="multi", base_dir=str(workspace))
        )
        session.add(
            ProjectRepo(
                id="repo-1",
                project_id="proj-multi",
                label="front-end",
                path=str(child),
                is_primary=True,
            )
        )

    assert git_repo_error(workspace) is not None
    assert git_repo_error(workspace, project_id="proj-multi") is None


def test_a_project_without_repos_is_not_exempted(tmp_path, monkeypatch):
    from src.core.database import AutopilotProject, DatabaseManager

    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db = DatabaseManager(db_path)
    db.create_tables()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with db.session_scope() as session:
        session.add(
            AutopilotProject(id="proj-bare", name="bare", base_dir=str(workspace))
        )

    assert git_repo_error(workspace, project_id="proj-bare") is not None


@pytest.mark.parametrize(
    "module,name",
    [
        ("src.mcp.autopilot.project_routes", "_validate_base_dir"),
        ("src.mcp.autopilot.project_routes", "_apply_active_project"),
        ("src.mcp.autopilot.project_repo_routes", "_validate_repo_path"),
        ("src.mcp.autopilot.control_routes", "start_pipeline"),
        ("src.autopilot.service", "AutopilotService"),
        ("src.cli.commands.project", "_create_offline"),
        ("src.cli.commands.autopilot", "start_pipeline"),
    ],
)
def test_every_entry_point_routes_through_the_shared_rule(module, name):
    """Pins the consolidation: the drift existed because nothing asserted the
    doors shared one rule."""
    import importlib
    import inspect

    mod = importlib.import_module(module)
    assert "git_repo_error" in inspect.getsource(getattr(mod, name)), name
