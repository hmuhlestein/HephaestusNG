"""Regression for the architectural-review BLOCKER: run_single_workflow's
shared-worktree design-doc copy called _copy_design_content with no import
of that name at all. A NameError there was caught by the enclosing broad
`except Exception`, which unconditionally discarded a just-created isolated
worktree back to project_path -- exactly the "autopilot mutates the shared
working tree directly" failure mode worktree isolation exists to prevent.

Extracted into _setup_shared_design_worktree so this exact sequence is
directly unit-testable, mirroring test_speckit_pipeline_worktree_wiring.py's
coverage of the sibling Phase-0/per-feature copy call sites."""

from unittest.mock import MagicMock, patch

from src.autopilot.orchestrator.pipeline import _setup_shared_design_worktree


def test_new_worktree_is_not_discarded_back_to_project_path(tmp_path):
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()
    expected_worktree_path = worktree_base / "wt_feature-my-design"

    design_doc = tmp_path / "design.md"
    design_doc.write_text("# Design")

    with (
        patch("src.core.simple_config.get_config"),
        patch("src.core.database.DatabaseManager"),
        patch("src.core.worktree_manager.WorktreeManager") as mock_wt_mgr_cls,
    ):
        mock_wt_mgr = mock_wt_mgr_cls.return_value
        mock_wt_mgr.worktree_base = worktree_base
        mock_wt_mgr.main_repo.git.branch = MagicMock()
        mock_wt_mgr.main_repo.git.worktree = MagicMock()

        design_worktree_path, design_branch_name, db_manager = _setup_shared_design_worktree(
            project_path=str(project_path),
            launch_params={"design_document": str(design_doc)},
            design_name="my design",
            logger=MagicMock(),
        )

    # The BLOCKER: an unimported _copy_design_content raised NameError here,
    # caught by the broad except, resetting design_worktree_path back to
    # project_path -- discarding the freshly created isolated worktree.
    assert design_worktree_path == str(expected_worktree_path)
    assert design_worktree_path != str(project_path)
    assert design_branch_name == "feature/my-design"
    assert db_manager is not None

    # The design document must actually have been copied into the worktree.
    copied = expected_worktree_path / ".hephaestus" / "spec.md"
    assert copied.read_text() == "# Design"


def test_project_path_already_a_worktree_is_used_directly():
    nested_path = "/repo/.worktrees/wt_1"

    with (
        patch("src.core.simple_config.get_config"),
        patch("src.core.database.DatabaseManager"),
        patch("src.core.worktree_manager.WorktreeManager") as mock_wt_mgr_cls,
    ):
        design_worktree_path, design_branch_name, db_manager = _setup_shared_design_worktree(
            project_path=nested_path,
            launch_params=None,
            design_name="",
            logger=MagicMock(),
        )

    assert design_worktree_path == nested_path
    assert design_branch_name is None
    assert db_manager is not None
    # No feature-branch/worktree creation calls in this path -- the
    # no-nested-worktrees guard short-circuits before those.
    mock_wt_mgr_cls.return_value.main_repo.git.worktree.assert_not_called()


def test_db_manager_still_returned_when_worktree_creation_fails(tmp_path):
    """db (now db_manager) is created before the worktree-creation branch
    below it and is NOT reset in the except block -- if worktree creation
    itself fails partway through, run_single_workflow's later final-merge
    call must still get the already-created DatabaseManager instance
    instead of a silently reintroduced None (which would just mean a
    second DatabaseManager gets created there -- harmless but wasteful,
    and worth pinning explicitly since it's easy to regress silently)."""
    project_path = tmp_path / "project"
    project_path.mkdir()
    worktree_base = tmp_path / "worktrees"
    worktree_base.mkdir()

    with (
        patch("src.core.simple_config.get_config"),
        patch("src.core.database.DatabaseManager") as mock_db_cls,
        patch("src.core.worktree_manager.WorktreeManager") as mock_wt_mgr_cls,
    ):
        mock_wt_mgr = mock_wt_mgr_cls.return_value
        mock_wt_mgr.worktree_base = worktree_base
        mock_wt_mgr.main_repo.git.branch = MagicMock()
        mock_wt_mgr.main_repo.git.worktree = MagicMock(side_effect=RuntimeError("git worktree add failed"))

        design_worktree_path, design_branch_name, db_manager = _setup_shared_design_worktree(
            project_path=str(project_path),
            launch_params={},
            design_name="my design",
            logger=MagicMock(),
        )

    # Falls back to project_path (pre-existing behavior for any failure)...
    assert design_worktree_path == str(project_path)
    assert design_branch_name is None
    # ...but db_manager, created before the failure, is NOT discarded.
    assert db_manager is mock_db_cls.return_value
