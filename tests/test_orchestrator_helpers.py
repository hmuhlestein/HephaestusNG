"""Tests for autopilot/orchestrator.py — pure utilities + detection functions."""

import pytest
import json
import hashlib
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta, timezone


class TestFileHash:
    def test_deterministic(self, tmp_path):
        from src.autopilot.orchestrator import file_hash
        f = tmp_path / "test.md"
        f.write_text("hello world")
        h1 = file_hash(f)
        h2 = file_hash(f)
        assert h1 == h2

    def test_different_content(self, tmp_path):
        from src.autopilot.orchestrator import file_hash
        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("hello")
        f2.write_text("world")
        assert file_hash(f1) != file_hash(f2)

    def test_length(self, tmp_path):
        from src.autopilot.orchestrator import file_hash
        f = tmp_path / "test.md"
        f.write_text("data")
        assert len(file_hash(f)) == 16


class TestPipelineState:
    def test_to_dict(self):
        from src.autopilot.orchestrator import PipelineState
        s = PipelineState(designs_processed=5, designs_succeeded=3)
        d = s.to_dict()
        assert d["designs_processed"] == 5
        assert d["designs_succeeded"] == 3

    def test_from_dict(self):
        from src.autopilot.orchestrator import PipelineState
        d = {"designs_processed": 10, "current_design": "test.md"}
        s = PipelineState.from_dict(d)
        assert s.designs_processed == 10
        assert s.current_design == "test.md"

    def test_from_dict_empty(self):
        from src.autopilot.orchestrator import PipelineState
        s = PipelineState.from_dict({})
        assert s.designs_processed == 0
        assert s.current_design is None

    def test_roundtrip(self):
        from src.autopilot.orchestrator import PipelineState
        s = PipelineState(designs_processed=7, designs_failed=2, run_id="run-123")
        s2 = PipelineState.from_dict(s.to_dict())
        assert s2.designs_processed == 7
        assert s2.designs_failed == 2
        assert s2.run_id == "run-123"


class TestPersistentPipelineState:
    def test_save_load_clear(self, tmp_path):
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState
        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            pps = PersistentPipelineState()
            state = PipelineState(designs_processed=3, run_id="run-456")
            pps.save(state, {"hash1", "hash2"})

            loaded_state, hashes = pps.load()
            assert loaded_state.designs_processed == 3
            assert loaded_state.run_id == "run-456"
            assert "hash1" in hashes
            assert "hash2" in hashes

            pps.clear()
            assert not pps.state_file.exists()
            assert not pps.processed_file.exists()

    def test_load_empty(self, tmp_path):
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState
        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            pps = PersistentPipelineState()
            state, hashes = pps.load()
            assert isinstance(state, PipelineState)
            assert hashes == set()

    def test_has_incomplete_work_no_file(self, tmp_path):
        from src.autopilot.orchestrator import PersistentPipelineState
        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            pps = PersistentPipelineState()
            assert pps.has_incomplete_work() is False

    def test_has_incomplete_work_no_design(self, tmp_path):
        from src.autopilot.orchestrator import PersistentPipelineState
        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            pps = PersistentPipelineState()
            pps.state_file.write_text(json.dumps({"current_design": None}))
            assert pps.has_incomplete_work() is False

    def test_has_incomplete_work_with_design(self, tmp_path):
        from src.autopilot.orchestrator import PersistentPipelineState
        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            pps = PersistentPipelineState()
            pps.state_file.write_text(json.dumps({"current_design": "test.md"}))
            assert pps.has_incomplete_work() is True

    def test_get_last_run_id(self, tmp_path):
        from src.autopilot.orchestrator import PersistentPipelineState
        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            pps = PersistentPipelineState()
            pps.state_file.write_text(json.dumps({"run_id": "run-789"}))
            assert pps.get_last_run_id() == "run-789"

    def test_get_last_run_id_no_file(self, tmp_path):
        from src.autopilot.orchestrator import PersistentPipelineState
        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            pps = PersistentPipelineState()
            assert pps.get_last_run_id() is None


class TestDetectHardError:
    def test_crashed_agent(self):
        from src.autopilot.orchestrator import detect_hard_error
        agents = [{"id": "a1", "status": "error"}]
        found, msg = detect_hard_error(agents, [])
        assert found is True
        assert "Crashed" in msg

    def test_critical_failure(self):
        from src.autopilot.orchestrator import detect_hard_error
        agents = [{"id": "a1", "status": "active"}]
        tasks = [{"id": "t1", "priority": "critical", "description": "fix auth"}]
        found, msg = detect_hard_error(agents, tasks)
        assert found is True
        assert "Critical" in msg

    def test_architectural_failure(self):
        from src.autopilot.orchestrator import detect_hard_error
        agents = []
        tasks = [{"id": "t1", "description": "Architectural issue found"}]
        found, msg = detect_hard_error(agents, tasks)
        assert found is True

    def test_no_error(self):
        from src.autopilot.orchestrator import detect_hard_error
        agents = [{"id": "a1", "status": "active"}]
        tasks = [{"id": "t1", "priority": "low", "description": "minor fix"}]
        found, msg = detect_hard_error(agents, tasks)
        assert found is False

    def test_workflow_filter(self):
        from src.autopilot.orchestrator import detect_hard_error
        agents = []
        tasks = [
            {"id": "t1", "priority": "critical", "description": "fix", "workflow_id": "wf-other"},
        ]
        found, msg = detect_hard_error(agents, tasks, workflow_id="wf-1")
        assert found is False


class TestDetectImpasse:
    def test_no_agents_pending_tasks(self):
        from src.autopilot.orchestrator import detect_impasse
        found, msg = detect_impasse([], [{"id": "t1"}], [], elapsed_seconds=400)
        assert found is True
        assert "No active agents" in msg

    def test_grace_period(self):
        from src.autopilot.orchestrator import detect_impasse
        found, msg = detect_impasse([], [{"id": "t1"}], [], elapsed_seconds=100)
        assert found is False

    def test_stuck_task(self):
        from src.autopilot.orchestrator import detect_impasse
        started = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        agents = [{"id": "a1", "status": "active"}]
        tasks = [{"id": "t1", "started_at": started}]
        found, msg = detect_impasse(agents, [], tasks)
        assert found is True
        assert "stuck" in msg.lower()

    def test_no_impasse(self):
        from src.autopilot.orchestrator import detect_impasse
        agents = [{"id": "a1", "status": "active"}]
        found, msg = detect_impasse(agents, [{"id": "t1"}], [], elapsed_seconds=100)
        assert found is False


class TestDetectArchitecturalIssue:
    def test_finds_issue(self, tmp_path):
        from src.autopilot.orchestrator import detect_architectural_issue
        report = tmp_path / "report.md"
        report.write_text("This has a major architectural issue that needs redesign")
        found, msg = detect_architectural_issue([str(report)])
        assert found is True
        assert "architectural issue" in msg.lower()

    def test_no_issue(self, tmp_path):
        from src.autopilot.orchestrator import detect_architectural_issue
        report = tmp_path / "report.md"
        report.write_text("Everything looks good, tests passing")
        found, msg = detect_architectural_issue([str(report)])
        assert found is False

    def test_missing_file(self):
        from src.autopilot.orchestrator import detect_architectural_issue
        found, msg = detect_architectural_issue(["/nonexistent/file.md"])
        assert found is False

    def test_empty_list(self):
        from src.autopilot.orchestrator import detect_architectural_issue
        found, msg = detect_architectural_issue([])
        assert found is False


class TestScanDesignQueue:
    def test_scans_md_files(self, tmp_path):
        from src.autopilot.orchestrator import scan_design_queue
        (tmp_path / "design_a.md").write_text("# Design A")
        (tmp_path / "design_b.md").write_text("# Design B")
        designs = scan_design_queue(tmp_path, set())
        assert len(designs) == 2

    def test_skips_processed(self, tmp_path):
        from src.autopilot.orchestrator import scan_design_queue, file_hash
        f = tmp_path / "design.md"
        f.write_text("# Design")
        h = file_hash(f)
        designs = scan_design_queue(tmp_path, {h})
        assert len(designs) == 0

    def test_nonexistent_dir(self):
        from src.autopilot.orchestrator import scan_design_queue
        designs = scan_design_queue(Path("/nonexistent"), set())
        assert designs == []

    def test_skips_directories(self, tmp_path):
        from src.autopilot.orchestrator import scan_design_queue
        (tmp_path / "subdir.md").mkdir()
        designs = scan_design_queue(tmp_path, set())
        assert len(designs) == 0

    def test_queue_order(self, tmp_path):
        from src.autopilot.orchestrator import scan_design_queue
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        (tmp_path / ".queue_order.json").write_text(json.dumps(["b.md", "a.md"]))
        designs = scan_design_queue(tmp_path, set())
        assert designs[0].path.name == "b.md"
        assert designs[1].path.name == "a.md"


class TestPickNextDesign:
    def test_returns_none_empty(self, tmp_path):
        from src.autopilot.orchestrator import pick_next_design, OrchestratorLogger
        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger)
        assert result is None

    def test_picks_first(self, tmp_path):
        from src.autopilot.orchestrator import pick_next_design, OrchestratorLogger
        logger = OrchestratorLogger(tmp_path)
        (tmp_path / "design.md").write_text("# Design")
        result = pick_next_design(tmp_path, set(), logger)
        assert result is not None
        assert result.name == "Design"


class TestCreateFeatureFolder:
    def test_creates_folder(self, tmp_path):
        from src.autopilot.orchestrator import create_feature_folder, OrchestratorLogger
        logger = OrchestratorLogger(tmp_path)
        folder = create_feature_folder(tmp_path, "test_feature", logger)
        assert folder.exists()
        assert folder.is_dir()
        assert "test_feature" in folder.name


class TestCopyDesignDocument:
    def test_copies_file(self, tmp_path):
        from src.autopilot.orchestrator import copy_design_document, DesignEntry
        src = tmp_path / "source.md"
        src.write_text("# Design doc")
        dest_folder = tmp_path / "feature"
        dest_folder.mkdir()
        entry = DesignEntry(path=src, name="Test", content_hash="abc")
        result = copy_design_document(entry, dest_folder)
        assert result.exists()
        assert result.read_text() == "# Design doc"


class TestReportPath:
    def test_returns_path(self, tmp_path):
        from src.autopilot.orchestrator import _report_path
        result = _report_path(tmp_path, "report.md")
        assert result == tmp_path / "report.md"


class TestCollectReportSummaries:
    def test_collects_reports(self, tmp_path):
        from src.autopilot.orchestrator import collect_report_summaries
        # Reports are at project_path level, not in subdirectory
        (tmp_path / "qa_report.md").write_text("# QA Report\nAll tests passed")
        (tmp_path / "architecture.md").write_text("# Architecture")
        result = collect_report_summaries(tmp_path)
        assert "qa" in result
        assert "All tests passed" in result["qa"]
        assert "architecture" in result

    def test_empty_dir(self, tmp_path):
        from src.autopilot.orchestrator import collect_report_summaries
        result = collect_report_summaries(tmp_path)
        # All report files not found
        assert all("not found" in v for v in result.values())
        assert len(result) == 8


class TestCollectFilesCreated:
    def test_collects_files(self, tmp_path):
        from src.autopilot.orchestrator import collect_files_created
        # Files at project_path level
        (tmp_path / "code.py").write_text("print('hello')")
        (tmp_path / "test.py").write_text("assert True")
        (tmp_path / "style.css").write_text("body {}")
        result = collect_files_created(tmp_path)
        assert len(result) == 3

    def test_skips_venv(self, tmp_path):
        from src.autopilot.orchestrator import collect_files_created
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "pkg.py").write_text("x = 1")
        (tmp_path / "main.py").write_text("x = 2")
        result = collect_files_created(tmp_path)
        assert len(result) == 1
        assert "main.py" in result[0]


class TestGetTasks:
    @patch("src.autopilot.orchestrator.api_get")
    def test_returns_list(self, mock_get):
        from src.autopilot.orchestrator import get_tasks
        mock_get.return_value = [{"id": "t1", "status": "done"}]
        result = get_tasks()
        assert len(result) == 1

    @patch("src.autopilot.orchestrator.api_get")
    def test_returns_empty_on_none(self, mock_get):
        from src.autopilot.orchestrator import get_tasks
        mock_get.return_value = None
        result = get_tasks()
        assert result == []

    @patch("src.autopilot.orchestrator.api_get")
    def test_unwraps_dict(self, mock_get):
        from src.autopilot.orchestrator import get_tasks
        mock_get.return_value = {"tasks": [{"id": "t1"}]}
        result = get_tasks()
        assert len(result) == 1

    @patch("src.autopilot.orchestrator.api_get")
    def test_with_params(self, mock_get):
        from src.autopilot.orchestrator import get_tasks
        mock_get.return_value = []
        get_tasks(status="done", workflow_id="wf-1")
        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        assert "status=done" in call_url
        assert "workflow_id=wf-1" in call_url


class TestGetAgents:
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.api_get")
    def test_returns_all(self, mock_get, mock_tasks):
        from src.autopilot.orchestrator import get_agents
        mock_get.return_value = [{"id": "a1", "status": "active"}]
        result = get_agents()
        assert len(result) == 1

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.api_get")
    def test_filters_by_workflow(self, mock_get, mock_tasks):
        from src.autopilot.orchestrator import get_agents
        mock_get.return_value = [
            {"id": "a1", "status": "active"},
            {"id": "a2", "status": "active"},
        ]
        mock_tasks.return_value = [
            {"assigned_agent_id": "a1", "workflow_id": "wf-1"},
        ]
        result = get_agents(workflow_id="wf-1")
        assert len(result) == 1
        assert result[0]["id"] == "a1"

    @patch("src.autopilot.orchestrator.api_get")
    def test_returns_empty_on_none(self, mock_get):
        from src.autopilot.orchestrator import get_agents
        mock_get.return_value = None
        result = get_agents()
        assert result == []


class TestPeekAgentOutput:
    @patch("src.autopilot.orchestrator.api_get")
    def test_returns_output(self, mock_get):
        from src.autopilot.orchestrator import peek_agent_output
        mock_get.return_value = {"output": "Building..."}
        result = peek_agent_output("a1")
        assert result == "Building..."

    @patch("src.autopilot.orchestrator.api_get")
    def test_returns_empty_on_none(self, mock_get):
        from src.autopilot.orchestrator import peek_agent_output
        mock_get.return_value = None
        result = peek_agent_output("a1")
        assert result == ""


class TestGetTaskProgress:
    @patch("src.autopilot.orchestrator.get_tasks")
    def test_counts(self, mock_tasks):
        from src.autopilot.orchestrator import get_task_progress
        mock_tasks.side_effect = [
            [{"assigned_agent_id": "a1"}, {"assigned_agent_id": "a1"}],  # done
            [{"assigned_agent_id": "a1"}],  # in_progress
        ]
        result = get_task_progress("a1")
        assert result["done"] == 2
        assert result["in_progress"] == 1


class TestOrchestratorLogger:
    def test_log_creates_file(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        logger = OrchestratorLogger(tmp_path)
        logger.log("Test message")
        # Log file should be created
        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) > 0

    def test_event(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        logger = OrchestratorLogger(tmp_path)
        logger.event("test_event", {"key": "value"})
        # Should not raise
