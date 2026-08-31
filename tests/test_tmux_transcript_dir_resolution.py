"""_resolve_tmux_transcript_dir used to trust agent.working_directory
unconditionally -- once a feature's worktree is cleaned up (deleting
working_directory and everything in it), that path no longer has any
transcript, but the function returned it anyway. get_agent_output then
fell all the way through to a much shorter fallback (e.g. the
termination-time capture-pane snapshot) instead of the full transcript
that _cleanup_worktree/_archive_feature_docs deliberately archive
elsewhere first. Now it verifies the transcript is actually present
before trusting each candidate directory.
"""

from unittest.mock import MagicMock

import pytest

from src.agents.output_capture import AgentOutputCapture
from src.core.database import AutopilotProject, DatabaseManager, Task, Workflow


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_task_workflow_project(db_manager, project_base_dir, workflow_working_dir=None):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="Test", base_dir=str(project_base_dir)))
        session.add(
            Workflow(
                id="wf-1",
                name="Test Workflow",
                phases_folder_path="config/workflows/autopilot",
                definition_id="autopilot",
                status="active",
                project_id="proj-1",
                working_directory=workflow_working_dir,
            )
        )
        session.add(
            Task(
                id="task-1",
                raw_description="d",
                done_definition="d",
                status="done",
                workflow_id="wf-1",
                assigned_agent_id="agent-1",
            )
        )


def _make_agent(working_directory=None, tmux_session_name="agent_test", status="terminated"):
    agent = MagicMock()
    agent.id = "agent-1"
    agent.tmux_session_name = tmux_session_name
    agent.status = status
    agent.current_task_id = None  # cleared on termination, same as real agents
    agent.working_directory = working_directory
    return agent


class TestResolveTmuxTranscriptDir:
    def test_uses_working_directory_when_transcript_present(self, db_manager, tmp_path):
        wt = tmp_path / "worktree"
        tmux_dir = wt / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / "agent_test.clean.log").write_text("still here\n")
        _seed_task_workflow_project(db_manager, tmp_path / "project")

        agent = _make_agent(working_directory=str(wt))
        cap = AgentOutputCapture(db_manager, tmux_server=None)

        assert cap._resolve_tmux_transcript_dir(agent) == tmux_dir

    def test_falls_back_to_project_root_when_worktree_deleted(self, db_manager, tmp_path):
        # working_directory points at a worktree that no longer exists at
        # all (the normal post-cleanup state) -- but _cleanup_worktree
        # already archived its tmux logs to the project root first.
        deleted_wt = tmp_path / "worktree_that_was_removed"
        project_base = tmp_path / "project"
        project_tmux = project_base / ".hephaestus" / "tmux"
        project_tmux.mkdir(parents=True)
        (project_tmux / "agent_test.clean.log").write_text("archived at project root\n")
        _seed_task_workflow_project(db_manager, project_base)

        agent = _make_agent(working_directory=str(deleted_wt))
        cap = AgentOutputCapture(db_manager, tmux_server=None)

        assert cap._resolve_tmux_transcript_dir(agent) == project_tmux

    def test_falls_back_to_feature_archive_when_project_root_also_missing(self, db_manager, tmp_path):
        # Neither the worktree nor a project-root copy has it -- only the
        # permanent per-feature archive phase_manager.py's
        # _archive_feature_docs writes to.
        deleted_wt = tmp_path / "worktree_that_was_removed"
        project_base = tmp_path / "project"
        feature_tmux = project_base / ".hephaestus" / "features" / "20260101_000000_my_feature" / "tmux"
        feature_tmux.mkdir(parents=True)
        (feature_tmux / "agent_test.clean.log").write_text("archived under the feature folder\n")
        _seed_task_workflow_project(db_manager, project_base)

        agent = _make_agent(working_directory=str(deleted_wt))
        cap = AgentOutputCapture(db_manager, tmux_server=None)

        assert cap._resolve_tmux_transcript_dir(agent) == feature_tmux

    def test_returns_none_when_transcript_exists_nowhere(self, db_manager, tmp_path):
        deleted_wt = tmp_path / "worktree_that_was_removed"
        _seed_task_workflow_project(db_manager, tmp_path / "project")

        agent = _make_agent(working_directory=str(deleted_wt))
        cap = AgentOutputCapture(db_manager, tmux_server=None)

        assert cap._resolve_tmux_transcript_dir(agent) is None

    def test_falls_back_to_nested_subdirectory_repo(self, db_manager, tmp_path):
        """Regression (reported live: git_expert 03b6ac3e showed 1500+
        uncollapsed blank lines): AutopilotProject.base_dir can be a
        monorepo umbrella directory (e.g. a "parent" project containing
        independently git-managed "front-end"/"back-end" subdirectories,
        each with its own .git, .hephaestus/, and .worktrees/) rather than
        the actual repo root itself. An agent's worktree in that layout
        lives under the SUBDIRECTORY's .worktrees/, and
        _archive_tmux_transcripts (worktree_removal.py) correctly archives
        to that subdirectory's own .hephaestus/tmux/ -- but the old
        base_dir-only search never looked there, silently returning None
        and forcing a fallback to the unfiltered, non-deduplicated
        .clean.log read in _get_terminated_agent_output instead of the
        correctly Spacing-pass-collapsed raw transcript."""
        deleted_wt = tmp_path / "worktree_that_was_removed"
        project_base = tmp_path / "parent"
        sub_repo_tmux = project_base / "front-end" / ".hephaestus" / "tmux"
        sub_repo_tmux.mkdir(parents=True)
        (sub_repo_tmux / "agent_test.transcript.log").write_text("archived under the sub-repo\n")
        _seed_task_workflow_project(db_manager, project_base)

        agent = _make_agent(working_directory=str(deleted_wt))
        cap = AgentOutputCapture(db_manager, tmux_server=None)

        assert cap._resolve_tmux_transcript_dir(agent) == sub_repo_tmux

    def test_falls_back_to_nested_subdirectory_worktree(self, db_manager, tmp_path):
        """Same monorepo layout, but the agent's own worktree (still
        present, not yet cleaned up) is what needs to be found -- under
        the sub-repo's .worktrees/, not base_dir's."""
        project_base = tmp_path / "parent"
        sub_repo_wt_tmux = (
            project_base / "front-end" / ".worktrees" / "wt_feature" / ".hephaestus" / "tmux"
        )
        sub_repo_wt_tmux.mkdir(parents=True)
        (sub_repo_wt_tmux / "agent_test.transcript.log").write_text("live in the sub-repo worktree\n")
        _seed_task_workflow_project(db_manager, project_base)

        deleted_wt = tmp_path / "worktree_that_was_removed"
        agent = _make_agent(working_directory=str(deleted_wt))
        cap = AgentOutputCapture(db_manager, tmux_server=None)

        assert cap._resolve_tmux_transcript_dir(agent) == sub_repo_wt_tmux

    def test_falls_back_to_feature_archive_under_nested_subdirectory(self, db_manager, tmp_path):
        """Same monorepo layout, for the last-resort feature-archive path."""
        project_base = tmp_path / "parent"
        feature_tmux = (
            project_base / "front-end" / ".hephaestus" / "features"
            / "20260101_000000_my_feature" / "tmux"
        )
        feature_tmux.mkdir(parents=True)
        (feature_tmux / "agent_test.clean.log").write_text("archived under the sub-repo's feature folder\n")
        _seed_task_workflow_project(db_manager, project_base)

        deleted_wt = tmp_path / "worktree_that_was_removed"
        agent = _make_agent(working_directory=str(deleted_wt))
        cap = AgentOutputCapture(db_manager, tmux_server=None)

        assert cap._resolve_tmux_transcript_dir(agent) == feature_tmux
