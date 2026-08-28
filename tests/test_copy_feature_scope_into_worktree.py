"""Regression: _run_one_feature used to set launch_params["feature_scope"]
unconditionally, even when the scope.md copy silently no-op'd because the
source didn't exist -- pointing the agent at a promised primary input
(product_requirements.yaml's phase_1_task_prompt: "if feature_scope is
provided, read it first") that was never actually delivered, with nothing
logging the gap. _copy_feature_scope_into_worktree is the extracted
helper that closes this: it only returns a destination path when the
copy actually landed, so the caller can correctly omit feature_scope from
launch_params otherwise."""

from unittest.mock import Mock

from src.autopilot.orchestrator.pipeline import _copy_feature_scope_into_worktree


def test_copies_and_returns_dest_when_scope_exists(tmp_path):
    designs_folder = tmp_path / "designs"
    (designs_folder / "features" / "auth").mkdir(parents=True)
    (designs_folder / "features" / "auth" / "scope.md").write_text("# Auth scope\n")

    wt_heph = tmp_path / "worktree" / ".hephaestus"
    wt_heph.mkdir(parents=True)
    logger = Mock()

    result = _copy_feature_scope_into_worktree(designs_folder, "auth", wt_heph, logger)

    assert result == wt_heph / "features" / "auth" / "scope.md"
    assert result.read_text() == "# Auth scope\n"
    logger.warning.assert_not_called()


def test_returns_none_and_logs_when_scope_missing(tmp_path):
    designs_folder = tmp_path / "designs"
    designs_folder.mkdir()  # no features/ subdir at all

    wt_heph = tmp_path / "worktree" / ".hephaestus"
    wt_heph.mkdir(parents=True)
    logger = Mock()

    result = _copy_feature_scope_into_worktree(designs_folder, "auth", wt_heph, logger)

    assert result is None
    assert not (wt_heph / "features" / "auth" / "scope.md").exists()
    logger.warning.assert_called_once()
    assert "auth" in logger.warning.call_args[0][0]
