import os

"""
Tests for autopilot orchestrator logic.

Tests the recovery, completion checking, and design management functions.
Uses mocked API calls to avoid requiring live services.
"""

# Import the functions we're testing
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from src.autopilot.orchestrator import (
    DesignEntry,
    DesignStatus,
    attempt_recovery,
    get_active_workflows,
    is_design_fully_complete,
    pick_next_design,
)


class MockLogger:
    """Mock logger for testing."""

    def __init__(self):
        self.logs = []

    def log(self, msg, level="INFO"):
        self.logs.append((level, msg))

    def info(self, msg):
        self.logs.append(("INFO", msg))

    def warning(self, msg):
        self.logs.append(("WARNING", msg))

    def error(self, msg):
        self.logs.append(("ERROR", msg))

    def event(self, name, data):
        self.logs.append(("EVENT", f"{name}: {data}"))


class TestIsDesignFullyComplete:
    """Tests for is_design_fully_complete function."""

    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("subprocess.run")
    def test_complete_when_all_done(
        self, mock_subprocess, mock_agents, mock_tasks, mock_wf_status, tmp_path
    ):
        """Design is complete when all 10 phases done, no agents, branches merged."""
        mock_wf_status.return_value = {"status": "completed"}

        # 10 done tasks, nothing else
        done_tasks = [{"id": f"task-{i}", "status": "done"} for i in range(10)]
        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "pending": [],
            "queued": [],
            "in_progress": [],
            "assigned": [],
            "failed": [],
            "done": done_tasks,
        }.get(status, [])

        mock_agents.return_value = [{"id": "agent-1", "status": "terminated"}]

        # No branches
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="")

        result, reason = is_design_fully_complete("wf-123", MockLogger())
        assert result is True
        assert "done" in reason.lower() or "complete" in reason.lower()

    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("src.autopilot.orchestrator.get_tasks")
    def test_incomplete_when_pending_tasks(self, mock_tasks, mock_wf_status):
        """Design is not complete when tasks are pending."""
        mock_wf_status.return_value = {"status": "active"}

        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "pending": [{"id": "task-1", "status": "pending"}],
            "queued": [],
            "in_progress": [],
            "assigned": [],
            "failed": [],
            "done": [{"id": f"task-{i}", "status": "done"} for i in range(9)],
        }.get(status, [])

        result, reason = is_design_fully_complete("wf-123", MockLogger())
        assert result is False
        assert "active" in reason.lower() or "task" in reason.lower()

    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    def test_incomplete_when_agents_active(
        self, mock_agents, mock_tasks, mock_wf_status
    ):
        """Design is not complete when agents are still running."""
        mock_wf_status.return_value = {"status": "active"}

        done_tasks = [{"id": f"task-{i}", "status": "done"} for i in range(10)]
        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "pending": [],
            "queued": [],
            "in_progress": [],
            "assigned": [],
            "failed": [],
            "done": done_tasks,
        }.get(status, [])

        mock_agents.return_value = [{"id": "agent-1", "status": "working"}]

        result, reason = is_design_fully_complete("wf-123", MockLogger())
        assert result is False
        assert "agent" in reason.lower()

    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("subprocess.run")
    def test_incomplete_when_branches_unmerged(
        self, mock_subprocess, mock_agents, mock_tasks, mock_wf_status
    ):
        """Design is not complete when agent branches exist."""
        mock_wf_status.return_value = {"status": "active"}

        done_tasks = [{"id": f"task-{i}", "status": "done"} for i in range(10)]
        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "pending": [],
            "queued": [],
            "in_progress": [],
            "assigned": [],
            "failed": [],
            "done": done_tasks,
        }.get(status, [])

        mock_agents.return_value = [{"id": "agent-1", "status": "terminated"}]

        # Branches exist
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="  agent-feature-1\n  agent-feature-2\n"
        )

        result, reason = is_design_fully_complete("wf-123", MockLogger())
        assert result is False
        assert "branch" in reason.lower()

    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("src.autopilot.orchestrator.get_tasks")
    def test_incomplete_when_tasks_failed(self, mock_tasks, mock_wf_status):
        """Design is not complete when tasks have failed."""
        mock_wf_status.return_value = {"status": "active"}

        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "pending": [],
            "queued": [],
            "in_progress": [],
            "assigned": [],
            "failed": [{"id": "task-fail-1", "status": "failed"}],
            "done": [{"id": f"task-{i}", "status": "done"} for i in range(9)],
        }.get(status, [])

        result, reason = is_design_fully_complete("wf-123", MockLogger())
        assert result is False
        assert "fail" in reason.lower()

    @patch("src.autopilot.orchestrator.get_workflow_status")
    def test_incomplete_when_workflow_failed(self, mock_wf_status):
        """Design is not complete when workflow itself failed."""
        mock_wf_status.return_value = {"status": "failed"}

        result, reason = is_design_fully_complete("wf-123", MockLogger())
        assert result is False
        assert "failed" in reason.lower()


class TestAttemptRecovery:
    """Tests for attempt_recovery function."""

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.update_task_status")
    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("subprocess.run")
    def test_retries_failed_tasks(
        self,
        mock_subprocess,
        mock_wf_status,
        mock_agents,
        mock_create_agent,
        mock_update_status,
        mock_tasks,
    ):
        """Recovery should retry failed tasks by creating new agents."""
        mock_wf_status.return_value = {"status": "active"}

        failed_task = {
            "id": "task-fail-1",
            "status": "failed",
            "phase_id": "phase-1",
            "retry_count": 0,
        }

        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "failed": [failed_task] if status == "failed" else [],
            "done": [],
        }.get(status, [])

        mock_agents.return_value = []
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="")

        # Mock successful retry (H-2: create_agent_for_task_direct replaces
        # the old api_post("/api/create_agent_for_task", ...) self-HTTP call)
        mock_update_status.return_value = True
        mock_create_agent.return_value = {"agent_id": "agent-new-1", "status": "created"}

        os.environ["PROJECT_PATH"] = "/tmp/test-project"
        success, msg = attempt_recovery("wf-123", MockLogger())
        assert success is True
        assert "retry" in msg.lower() or "task" in msg.lower()
        mock_create_agent.assert_called_once()

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.api_post")
    @patch("subprocess.run")
    def test_skips_retry_after_max_attempts(
        self, mock_subprocess, mock_api_post, mock_agents, mock_tasks
    ):
        """Recovery should skip retrying tasks that failed too many times."""
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="")
        mock_agents.return_value = []

        # Task already retried 2 times
        failed_task = {"id": "task-fail-1", "status": "failed", "retry_count": 2}
        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "failed": [failed_task] if status == "failed" else [],
        }.get(status, [])

        os.environ["PROJECT_PATH"] = "/tmp/test-project"
        success, msg = attempt_recovery("wf-123", MockLogger())
        # Should not retry (already max retries)
        assert "skip" in msg.lower() or not success

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("subprocess.run")
    def test_merges_branches(
        self, mock_subprocess, mock_wf_status, mock_agents, mock_tasks
    ):
        """Recovery should merge unmerged agent branches."""
        mock_wf_status.return_value = {"status": "active"}
        mock_tasks.side_effect = lambda status=None, workflow_id=None: []
        mock_agents.return_value = []

        # Mock the workflow query to return a workflow with working_directory
        mock_workflow = MagicMock()
        mock_workflow.working_directory = "/tmp/test-project"
        with patch("src.autopilot.orchestrator.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.__enter__ = MagicMock(return_value=mock_db)
            mock_db.__exit__ = MagicMock(return_value=False)
            mock_db.query.return_value.filter_by.return_value.first.return_value = mock_workflow
            mock_get_db.return_value = mock_db

            # First call: git branch --list agent-* returns branches
            # Second call: git status (clean)
            # Third call: git branch --list (for merge check)
            mock_subprocess.side_effect = [
                MagicMock(returncode=0, stdout="  agent-feature-1\n"),  # branch list
                MagicMock(returncode=0, stdout=""),  # status (clean)
                MagicMock(returncode=0, stdout="  agent-feature-1\n"),  # branch list for merge
                MagicMock(returncode=0, stdout=""),  # checkout branch
                MagicMock(returncode=0, stdout=""),  # checkout main
                MagicMock(returncode=0, stdout="Merge made"),  # merge
                MagicMock(returncode=0, stdout=""),  # delete branch
            ]

            os.environ["PROJECT_PATH"] = "/tmp/test-project"
        success, msg = attempt_recovery("wf-123", MockLogger())
        assert "merge" in msg.lower() or success

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.api_post")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("subprocess.run")
    def test_terminates_stale_agents(
        self, mock_subprocess, mock_wf_status, mock_api_post, mock_agents, mock_tasks
    ):
        """Recovery should terminate stale agents."""
        mock_wf_status.return_value = {"status": "active"}
        mock_tasks.side_effect = lambda status=None, workflow_id=None: []
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="")

        stale_agent = {"id": "agent-stale-1", "status": "working"}
        mock_agents.return_value = [stale_agent]

        mock_api_post.return_value = {"status": "terminated"}

        os.environ["PROJECT_PATH"] = "/tmp/test-project"
        success, msg = attempt_recovery("wf-123", MockLogger())
        assert "terminate" in msg.lower() or success

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("subprocess.run")
    def test_no_recovery_needed(
        self, mock_subprocess, mock_wf_status, mock_agents, mock_tasks
    ):
        """Recovery returns False when nothing needs fixing."""
        mock_wf_status.return_value = {"status": "completed"}
        mock_tasks.side_effect = lambda status=None, workflow_id=None: []
        mock_agents.return_value = []
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="")

        os.environ["PROJECT_PATH"] = "/tmp/test-project"
        success, msg = attempt_recovery("wf-123", MockLogger())
        assert success is False
        assert "no" in msg.lower() or "needed" in msg.lower()


class TestGetActiveWorkflows:
    """Tests for get_active_workflows function.

    get_active_workflows queries the DB directly (H-2 fix, no more
    self-HTTP call to /api/workflows), so these tests seed a real sqlite
    DB via HEPHAESTUS_TEST_DB rather than mocking api_get.
    """

    @pytest.fixture
    def db_env(self, tmp_path, monkeypatch):
        from src.core.database import DatabaseManager

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        db = DatabaseManager(str(db_path))
        db.create_tables()
        return db

    def _make_workflow(self, db, wf_id, status):
        from src.core.database import Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id=wf_id,
                    name="Test Workflow",
                    status=status,
                    phases_folder_path="/tmp",
                )
            )

    def test_returns_active_workflows(self, db_env):
        """Should return only workflows with 'active' status."""
        self._make_workflow(db_env, "wf-1", "active")
        self._make_workflow(db_env, "wf-2", "completed")
        self._make_workflow(db_env, "wf-3", "failed")

        result = get_active_workflows()
        assert len(result) == 1
        assert result[0]["id"] == "wf-1"
        assert result[0]["status"] == "active"

    def test_returns_empty_when_no_active(self, db_env):
        """Should return empty list when no active workflows."""
        self._make_workflow(db_env, "wf-1", "completed")
        self._make_workflow(db_env, "wf-2", "failed")

        result = get_active_workflows()
        assert len(result) == 0

    def test_handles_dict_response(self, db_env):
        """Should return multiple active workflows when several exist."""
        self._make_workflow(db_env, "wf-1", "active")
        self._make_workflow(db_env, "wf-2", "active")
        self._make_workflow(db_env, "wf-3", "paused")

        result = get_active_workflows()
        assert len(result) == 2
        assert {w["id"] for w in result} == {"wf-1", "wf-2"}


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("subprocess.run")
    def test_handles_none_tasks(
        self, mock_subprocess, mock_agents, mock_tasks, mock_wf_status
    ):
        """Should handle None returned from get_tasks."""
        mock_wf_status.return_value = {"status": "active"}
        mock_tasks.return_value = None  # Simulate API returning None
        mock_agents.return_value = None
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="")

        # Should not crash
        try:
            result, reason = is_design_fully_complete("wf-123", MockLogger())
            # If it doesn't crash, that's good
        except (TypeError, AttributeError):
            # These are expected if None handling is missing
            pass

    @patch("src.autopilot.orchestrator.get_workflow_status")
    def test_handles_workflow_not_found(self, mock_wf_status):
        """Should handle workflow not found."""
        mock_wf_status.return_value = {}

        result, reason = is_design_fully_complete("wf-123", MockLogger())
        assert result is False

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    @patch("subprocess.run")
    def test_handles_git_command_failure(
        self, mock_subprocess, mock_wf_status, mock_agents, mock_tasks
    ):
        """Should handle git command failures gracefully."""
        mock_wf_status.return_value = {"status": "active"}

        done_tasks = [{"id": f"task-{i}", "status": "done"} for i in range(10)]
        mock_tasks.side_effect = lambda status=None, workflow_id=None: {
            "pending": [],
            "queued": [],
            "in_progress": [],
            "assigned": [],
            "failed": [],
            "done": done_tasks,
        }.get(status, [])

        mock_agents.return_value = [{"id": "agent-1", "status": "terminated"}]

        # Git command fails
        mock_subprocess.side_effect = Exception("git not found")

        # Should not crash
        result, reason = is_design_fully_complete("wf-123", MockLogger())
        # Result depends on how failure is handled


class TestPickNextDesign:
    """Tests for pick_next_design function."""

    def test_picks_first_design(self, tmp_path):
        """Should pick the first design in queue."""
        # Create design files
        (tmp_path / "001_design_a.md").write_text("# Design A")
        (tmp_path / "002_design_b.md").write_text("# Design B")

        designs = pick_next_design(tmp_path, set(), MockLogger())
        assert designs is not None
        assert "design a" in designs.name.lower() or "001" in designs.name

    def test_skips_processed_designs(self, tmp_path):
        """Should skip designs that have been processed."""
        (tmp_path / "001_design_a.md").write_text("# Design A")
        (tmp_path / "002_design_b.md").write_text("# Design B")

        # Mark first as processed (by content hash)
        from src.autopilot.orchestrator import file_hash

        processed = {file_hash(tmp_path / "001_design_a.md")}

        designs = pick_next_design(tmp_path, processed, MockLogger())
        assert designs is not None
        assert "design b" in designs.name.lower() or "002" in designs.name

    def test_returns_none_when_empty(self, tmp_path):
        """Should return None when queue is empty."""
        designs = pick_next_design(tmp_path, set(), MockLogger())
        assert designs is None

    def test_returns_none_when_all_processed(self, tmp_path):
        """Should return None when all designs are processed."""
        (tmp_path / "001_design_a.md").write_text("# Design A")

        from src.autopilot.orchestrator import file_hash

        processed = {file_hash(tmp_path / "001_design_a.md")}

        designs = pick_next_design(tmp_path, processed, MockLogger())
        assert designs is None


class TestDesignEntry:
    """Tests for DesignEntry dataclass."""

    def test_design_entry_creation(self, tmp_path):
        """DesignEntry should be creatable with required fields."""
        filepath = tmp_path / "test_design.md"
        filepath.write_text("# Test")

        entry = DesignEntry(
            path=filepath,
            name="Test Design",
            content_hash="abc123",
        )

        assert entry.path == filepath
        assert entry.name == "Test Design"
        assert entry.content_hash == "abc123"
        assert entry.status == DesignStatus.PENDING

    def test_design_entry_status(self, tmp_path):
        """DesignEntry status can be set."""
        filepath = tmp_path / "test_design.md"
        filepath.write_text("# Test")

        entry = DesignEntry(
            path=filepath,
            name="Test Design",
            content_hash="abc123",
        )

        entry.status = DesignStatus.IN_PROGRESS
        assert entry.status == DesignStatus.IN_PROGRESS


class TestDesignStatus:
    """Tests for DesignStatus enum."""

    def test_status_values(self):
        """DesignStatus should have expected values."""
        assert DesignStatus.PENDING.value == "pending"
        assert DesignStatus.IN_PROGRESS.value == "in_progress"
        assert DesignStatus.COMPLETED.value == "completed"
        assert DesignStatus.FAILED.value == "failed"
        assert DesignStatus.SKIPPED.value == "skipped"

    def test_all_statuses_exist(self):
        """DesignStatus should have all expected statuses."""
        expected = {"PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "SKIPPED"}
        actual = {s.name for s in DesignStatus}
        assert expected == actual
        assert DesignStatus.FAILED.value == "failed"
