"""Tests for autopilot/orchestrator.py — pure utilities + detection functions."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest


@pytest.fixture
def orch_db_env(tmp_path, monkeypatch):
    """Real sqlite DB for functions converted to direct DB access (H-2
    fix) — get_tasks/get_agents/peek_agent_output no longer call api_get,
    so tests seed data via HEPHAESTUS_TEST_DB instead of mocking HTTP."""
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


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
    """PersistentPipelineState is backed by ProjectContext (a generic
    key-value table) instead of JSON files under AUTOPILOT_STATE_DIR --
    files were a second, non-transactional source of truth that could
    drift from the DB's actual workflow state (see incident: tasks/agents/
    workflows got wiped in one DB transaction, but these files didn't move,
    and kept pointing at a dead workflow_id)."""

    def test_save_load_clear(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

        pps = PersistentPipelineState()
        state = PipelineState(designs_processed=3, run_id="run-456")
        pps.save(state, {"hash1", "hash2"})

        loaded_state, hashes = pps.load()
        assert loaded_state.designs_processed == 3
        assert loaded_state.run_id == "run-456"
        assert "hash1" in hashes
        assert "hash2" in hashes

        pps.clear()
        cleared_state, cleared_hashes = pps.load()
        assert cleared_state.designs_processed == 0
        assert cleared_hashes == set()

    def test_load_empty(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

        pps = PersistentPipelineState()
        state, hashes = pps.load()
        assert isinstance(state, PipelineState)
        assert hashes == set()

    def test_has_incomplete_work_no_file(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState

        pps = PersistentPipelineState()
        assert pps.has_incomplete_work() is False

    def test_has_incomplete_work_no_design(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(current_design=None), set())
        assert pps.has_incomplete_work() is False

    def test_has_incomplete_work_with_design(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(current_design="test.md"), set())
        assert pps.has_incomplete_work() is True

    def test_get_last_run_id(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(run_id="run-789"), set())
        assert pps.get_last_run_id() == "run-789"

    def test_get_last_run_id_no_file(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState

        pps = PersistentPipelineState()
        assert pps.get_last_run_id() is None

    def test_has_incomplete_work_tolerates_null_queue_status(self, orch_db_env):
        """Regression: `.get('queue_status', {})` only applies its default
        when the key is ABSENT, not when the stored value is explicitly
        null. Without the `or {}` guard, a stored `queue_status: null`
        makes the next `.get('status')` call raise AttributeError on None
        instead of being treated as 'no incomplete work'."""
        from src.autopilot.orchestrator import PersistentPipelineState, _set_project_context
        from src.core.database import get_db

        pps = PersistentPipelineState()
        with get_db() as db:
            _set_project_context(
                db, pps.STATE_KEY, {"current_design": None, "queue_status": None}
            )

        assert pps.has_incomplete_work() is False

    def test_save_survives_first_write_error_and_still_saves_state(self, orch_db_env):
        """save() now does two separate transactions (processed_designs,
        then state) instead of one shared transaction, specifically so a
        failure in one doesn't silently also lose the other -- verify state
        still saves even if the processed-designs write is broken."""
        from unittest.mock import patch

        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

        pps = PersistentPipelineState()
        with patch(
            "src.autopilot.orchestrator._set_project_context",
            side_effect=[RuntimeError("boom"), None],
        ) as mock_set:
            pps.save(PipelineState(run_id="run-999"), {"h1"})
            assert mock_set.call_count == 2

    def test_remove_processed_hash_does_not_touch_state(self, orch_db_env):
        """Regression: a caller that only wants to un-mark one processed
        design (e.g. re-adding a deleted design) must not go through
        load()+save() -- that round trip re-persists the ENTIRE pipeline
        state read at load()-time, silently clobbering any newer state a
        concurrently-running pipeline had already written in between."""
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(designs_processed=5, run_id="run-1"), {"h1", "h2"})

        pps.remove_processed_hash("h1")

        state, hashes = pps.load()
        assert hashes == {"h2"}
        # State untouched by the hash removal.
        assert state.designs_processed == 5
        assert state.run_id == "run-1"

    def test_remove_processed_hash_missing_is_safe(self, orch_db_env):
        from src.autopilot.orchestrator import PersistentPipelineState

        pps = PersistentPipelineState()
        pps.remove_processed_hash("does-not-exist")  # should not raise
        _, hashes = pps.load()
        assert hashes == set()


class TestSetProjectContextUpsert:
    """_set_project_context uses an atomic INSERT ... ON CONFLICT DO UPDATE
    instead of a read-then-write (SELECT then add-or-update), which had a
    real TOCTOU window: two callers writing the same key for the first time
    could both see no existing row and both attempt to insert, raising
    IntegrityError on ProjectContext.key's unique constraint."""

    def test_set_then_set_again_updates_in_place(self, orch_db_env):
        from src.autopilot.orchestrator import _get_project_context, _set_project_context
        from src.core.database import ProjectContext, get_db

        with get_db() as db:
            _set_project_context(db, "some-key", {"a": 1})
        with get_db() as db:
            _set_project_context(db, "some-key", {"a": 2})
        with get_db() as db:
            assert _get_project_context(db, "some-key") == {"a": 2}

        with get_db() as db:
            assert db.query(ProjectContext).filter_by(key="some-key").count() == 1

    def test_concurrent_first_write_to_same_key_does_not_raise(self, orch_db_env):
        """Simulates two callers racing to create the same key for the
        first time -- both see no existing row (this test skips straight to
        calling _set_project_context twice with no row in between, since
        the atomic upsert makes true thread-interleaving unnecessary to
        prove: an INSERT ... ON CONFLICT DO UPDATE statement can't raise
        IntegrityError on its own conflict target no matter how many times
        it's called)."""
        from src.autopilot.orchestrator import _get_project_context, _set_project_context
        from src.core.database import get_db

        with get_db() as db:
            _set_project_context(db, "race-key", "first")
        with get_db() as db:
            _set_project_context(db, "race-key", "second")  # must not raise

        with get_db() as db:
            assert _get_project_context(db, "race-key") == "second"


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
            {
                "id": "t1",
                "priority": "critical",
                "description": "fix",
                "workflow_id": "wf-other",
            },
        ]
        found, msg = detect_hard_error(agents, tasks, workflow_id="wf-1")
        assert found is False


class TestDetectImpasse:
    def test_no_agents_pending_tasks(self):
        from src.autopilot.orchestrator import detect_impasse

        found, msg = detect_impasse([], [{"id": "t1"}], [], elapsed_seconds=700)
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
        from src.autopilot.orchestrator import file_hash, scan_design_queue

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

        # The function looks for order file at queue_dir.parent.parent / .hephaestus / .queue_order.json
        # So we need to set up the directory structure accordingly
        project_root = tmp_path.parent.parent
        hephaestus_dir = project_root / ".hephaestus"
        hephaestus_dir.mkdir(exist_ok=True)

        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        (hephaestus_dir / ".queue_order.json").write_text(json.dumps(["b.md", "a.md"]))
        designs = scan_design_queue(tmp_path, set())
        assert designs[0].path.name == "b.md"
        assert designs[1].path.name == "a.md"


class TestPickNextDesign:
    def test_returns_none_empty(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, pick_next_design

        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger)
        assert result is None

    def test_picks_first(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, pick_next_design

        logger = OrchestratorLogger(tmp_path)
        (tmp_path / "design.md").write_text("# Design")
        result = pick_next_design(tmp_path, set(), logger)
        assert result is not None
        assert result.name == "Design"

    def test_file_scan_fallback_creates_missing_design_row(
        self, tmp_path, orch_db_env
    ):
        """A design picked via the file-scan fallback (no matching
        AutopilotDesign row for the active project -- e.g. the project was
        just auto-created and has no design rows yet) must get a real DB
        row created, not silently return db_id=None. Regression: that None
        used to reach _create_feature_records and crash with 'NOT NULL
        constraint failed: features.design_id' right after Phase 0
        completed (observed live during smoke testing)."""
        from src.autopilot.orchestrator import OrchestratorLogger, pick_next_design
        from src.core.database import AutopilotProject

        session = orch_db_env.get_session()
        session.add(
            AutopilotProject(
                id="proj-new123",
                name="myproject",
                base_dir=str(tmp_path),
                is_active=True,
            )
        )
        session.commit()
        session.close()

        (tmp_path / "design.md").write_text("# Design")
        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger)

        assert result is not None
        assert result.db_id is not None

        from src.core.database import AutopilotDesign, get_db

        with get_db() as db:
            row = db.query(AutopilotDesign).filter_by(id=result.db_id).first()
            assert row is not None
            assert row.project_id == "proj-new123"
            assert row.filename == "design.md"

    def test_file_scan_fallback_reuses_existing_design_row(
        self, tmp_path, orch_db_env
    ):
        """Regression: must not create a duplicate row on every poll cycle
        once one already exists for this file."""
        from src.autopilot.orchestrator import OrchestratorLogger, pick_next_design
        from src.core.database import AutopilotDesign, AutopilotProject

        session = orch_db_env.get_session()
        session.add(
            AutopilotProject(
                id="proj-new456",
                name="myproject",
                base_dir=str(tmp_path),
                is_active=True,
            )
        )
        session.add(
            AutopilotDesign(
                id="des-existing789",
                project_id="proj-new456",
                filename="design.md",
                name="Design",
                status="pending",
            )
        )
        session.commit()
        session.close()

        (tmp_path / "design.md").write_text("# Design")
        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger)

        assert result.db_id == "des-existing789"

        from src.core.database import get_db

        with get_db() as db:
            matches = (
                db.query(AutopilotDesign)
                .filter_by(project_id="proj-new456", filename="design.md")
                .all()
            )
            assert len(matches) == 1


class TestCreateFeatureFolder:
    def test_creates_folder(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, create_feature_folder

        logger = OrchestratorLogger(tmp_path)
        folder = create_feature_folder(tmp_path, "test_feature", logger)
        assert folder.exists()
        assert folder.is_dir()
        assert "test_feature" in folder.name


class TestCopyDesignDocument:
    def test_copies_file(self, tmp_path):
        from src.autopilot.orchestrator import DesignEntry, copy_design_document

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
    def _make_task(self, db, task_id, status="pending", workflow_id="wf-1"):
        from src.core.database import Task

        with db.session_scope() as session:
            session.add(
                Task(
                    id=task_id,
                    raw_description="do work",
                    done_definition="done",
                    status=status,
                    workflow_id=workflow_id,
                )
            )

    def test_returns_list(self, orch_db_env):
        from src.autopilot.orchestrator import get_tasks

        self._make_task(orch_db_env, "t1", status="done")
        result = get_tasks()
        assert len(result) == 1

    def test_returns_empty_on_none(self, orch_db_env):
        from src.autopilot.orchestrator import get_tasks

        result = get_tasks()
        assert result == []

    def test_unwraps_dict(self, orch_db_env):
        from src.autopilot.orchestrator import get_tasks

        self._make_task(orch_db_env, "t1")
        result = get_tasks()
        assert len(result) == 1
        assert result[0]["id"] == "t1"

    def test_with_params(self, orch_db_env):
        from src.autopilot.orchestrator import get_tasks

        self._make_task(orch_db_env, "t1", status="done", workflow_id="wf-1")
        self._make_task(orch_db_env, "t2", status="pending", workflow_id="wf-1")
        self._make_task(orch_db_env, "t3", status="done", workflow_id="wf-2")

        result = get_tasks(status="done", workflow_id="wf-1")
        assert len(result) == 1
        assert result[0]["id"] == "t1"

    def test_includes_retry_count(self, orch_db_env):
        """Regression: get_tasks previously omitted retry_count entirely, so
        attempt_recovery's task.get("retry_count", 0) always silently
        defaulted to 0 -- the "stop after 2 retries" guard never engaged and
        a permanently-broken task retried forever."""
        from src.autopilot.orchestrator import get_tasks, increment_task_retry_count

        self._make_task(orch_db_env, "t1")
        assert get_tasks()[0]["retry_count"] == 0

        increment_task_retry_count("t1")
        increment_task_retry_count("t1")
        result = get_tasks()
        assert result[0]["retry_count"] == 2


class TestPromptHumanDismissed:
    def test_dismissed_request_auto_continues_without_crashing(self, tmp_path):
        """Regression: the "dismissed" branch called
        logger.info(message, "WARN") -- OrchestratorLogger.info takes only
        (self, message), so this raised TypeError every time a pending
        human-input request got deleted (e.g. via the UI's designs/reload
        endpoint) instead of answered. Observed live: this crashed
        _run_one_feature's whole try block, which then hit the finally
        block and deleted the feature's worktree while its workflow was
        still legitimately active (just waiting on a stuck-task
        diagnostic agent), not because the feature had actually failed.
        """
        import threading
        import time as time_mod

        from src.autopilot.orchestrator import OrchestratorLogger, prompt_human

        with patch("src.autopilot.orchestrator.AUTOPILOT_STATE_DIR", str(tmp_path)):
            logger = OrchestratorLogger(tmp_path / "logs")
            result = {}

            def _run():
                result["choice"] = prompt_human("test stuck reason", logger, timeout=5)

            t = threading.Thread(target=_run)
            t.start()

            # Wait for prompt_human to create its request file, then delete
            # it to simulate the UI dismissing the request.
            for _ in range(50):
                request_files = list(tmp_path.glob("input_request_*.json"))
                if request_files:
                    request_files[0].unlink()
                    break
                time_mod.sleep(0.1)

            t.join(timeout=10)
            assert not t.is_alive(), "prompt_human should have returned promptly"
            assert result.get("choice") == "c"


class TestIncrementTaskRetryCount:
    def test_persists_across_calls(self, orch_db_env):
        from src.autopilot.orchestrator import increment_task_retry_count
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(
                    id="t1",
                    raw_description="do work",
                    done_definition="done",
                    status="failed",
                )
            )

        assert increment_task_retry_count("t1") == 1
        assert increment_task_retry_count("t1") == 2
        assert increment_task_retry_count("t1") == 3

    def test_missing_task_returns_zero(self, orch_db_env):
        from src.autopilot.orchestrator import increment_task_retry_count

        assert increment_task_retry_count("does-not-exist") == 0


class TestCleanStaleAssignedTasks:
    """Regression: marking a stale task (assigned to a terminated agent)
    failed used to unconditionally overwrite failure_reason with a generic
    "agent terminated" message -- clobbering a specific reason
    update_task_status had already recorded (e.g. a missing output
    artifact), which the retry path then has no way to surface to the next
    agent."""

    def _make_workflow_task_agent(self, db, task_status="in_progress", failure_reason=None):
        from src.core.database import Agent, Task, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Agent(id="agent-1", system_prompt="p", status="terminated", cli_type="pi")
            )
            session.add(
                Task(
                    id="task-1",
                    workflow_id="wf-1",
                    raw_description="r",
                    done_definition="d",
                    status=task_status,
                    assigned_agent_id="agent-1",
                    failure_reason=failure_reason,
                )
            )

    def test_preserves_existing_specific_reason(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _clean_stale_assigned_tasks,
        )
        from src.core.database import Task

        self._make_workflow_task_agent(
            orch_db_env, failure_reason="Missing output artifact: docs/report.md"
        )

        _clean_stale_assigned_tasks("wf-1", OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert task.failure_reason == "Missing output artifact: docs/report.md"

    def test_falls_back_to_generic_reason_when_none_recorded(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _clean_stale_assigned_tasks,
        )
        from src.core.database import Task

        self._make_workflow_task_agent(orch_db_env, failure_reason=None)

        _clean_stale_assigned_tasks("wf-1", OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert "terminated unexpectedly" in task.failure_reason


class TestFailWorkflowDirect:
    """Regression: the backend-startup stale-workflow cleanup used
    complete_workflow_direct unconditionally for any workflow still "active"
    after a restart -- even one abandoned mid-run with most phases
    unfinished, mislabeling it "completed" and corrupting downstream status
    derivation. fail_workflow_direct gives that path a way to mark it
    accurately instead."""

    def test_marks_failed(self, orch_db_env):
        from src.autopilot.orchestrator import fail_workflow_direct
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                )
            )

        assert fail_workflow_direct("wf-1") is True
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"

    def test_missing_workflow_returns_false(self, orch_db_env):
        from src.autopilot.orchestrator import fail_workflow_direct

        assert fail_workflow_direct("does-not-exist") is False


class TestResumeStuckWorkflowTasks:
    """Regression: a design that had features created (Phase 0 complete) but
    got stopped mid-feature-pipeline previously had no way to continue --
    _run_one_feature always started a brand new workflow for every feature,
    discarding whatever phases had already completed. _resume_stuck_workflow_
    tasks un-pauses the workflow and restarts exactly the tasks left stuck
    (mirrors autopilot_api.py's resume_feature endpoint, but sync since this
    runs from the orchestrator's own background thread)."""

    def _make_workflow(self, db, wf_id, status):
        from src.core.database import Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(id=wf_id, name="t", phases_folder_path="/tmp", status=status)
            )

    def _make_task(self, db, task_id, wf_id, status, agent_id=None, phase_id=None):
        from src.core.database import Task

        with db.session_scope() as session:
            session.add(
                Task(
                    id=task_id,
                    raw_description="r",
                    done_definition="d",
                    status=status,
                    workflow_id=wf_id,
                    phase_id=phase_id,
                    assigned_agent_id=agent_id,
                )
            )

    def _make_agent(self, db, agent_id, status):
        from src.core.database import Agent

        with db.session_scope() as session:
            session.add(Agent(id=agent_id, system_prompt="p", status=status, cli_type="pi"))

    def test_missing_workflow_returns_zero(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks

        result = _resume_stuck_workflow_tasks("does-not-exist", OrchestratorLogger(tmp_path))
        assert result == 0

    def test_unpauses_workflow(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks
        from src.core.database import Workflow

        self._make_workflow(orch_db_env, "wf-1", "paused")

        _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_skips_user_paused_workflow(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: same class of bug _try_auto_resume_paused_workflow
        was fixed for -- this fires whenever the design/feature queue loop
        cycles back to a workflow it already has an id for, which can
        include one the user deliberately paused. Must not silently
        un-pause and restart work on it."""
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks
        from src.core.database import Workflow

        self._make_workflow(orch_db_env, "wf-1", "paused")
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.paused_by = "user"
        self._make_task(orch_db_env, "task-failed", "wf-1", "failed")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 0
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_restarts_failed_and_blocked_tasks(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks
        from src.core.database import Task

        mock_create_agent.return_value = {"agent_id": "new-agent"}
        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_task(orch_db_env, "task-failed", "wf-1", "failed")
        self._make_task(orch_db_env, "task-blocked", "wf-1", "blocked")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 2
        assert mock_create_agent.call_count == 2
        with orch_db_env.session_scope() as session:
            for task_id in ("task-failed", "task-blocked"):
                task = session.query(Task).filter_by(id=task_id).first()
                assert task.status == "in_progress"
                assert task.assigned_agent_id == "new-agent"
                assert task.failure_reason is None

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_restarts_task_whose_agent_was_terminated(self, mock_create_agent, orch_db_env, tmp_path):
        """A task can be stuck 'in_progress' pointing at an agent that was
        already terminated (e.g. the service stop killed it) -- resume must
        detect this and restart it, not just plain 'failed'/'blocked'."""
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks

        mock_create_agent.return_value = {"agent_id": "new-agent"}
        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_agent(orch_db_env, "dead-agent", "terminated")
        self._make_task(orch_db_env, "task-1", "wf-1", "in_progress", agent_id="dead-agent")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 1
        mock_create_agent.assert_called_once()

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_does_not_restart_task_with_live_agent(self, mock_create_agent, orch_db_env, tmp_path):
        """A task genuinely still being worked by a live agent must be left
        alone -- resume is for stuck work, not active work."""
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks
        from src.core.database import Task

        self._make_workflow(orch_db_env, "wf-1", "active")
        self._make_agent(orch_db_env, "live-agent", "working")
        self._make_task(orch_db_env, "task-1", "wf-1", "in_progress", agent_id="live-agent")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 0
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.assigned_agent_id == "live-agent"


class TestCreateAgentForTaskDirectAppStateGuard:
    """Regression: get_app_state() was called BEFORE create_agent_for_task_
    direct's own try/except, so when app state isn't initialized it raised
    RuntimeError straight out of the function instead of the documented
    "returns None on failure" contract. Every caller (self-heal task
    creation, resume, and the new corrective-negotiation retries) relies on
    that contract to fail gracefully rather than crash the whole pipeline
    run. Hit live while testing manually outside the running server."""

    def test_app_state_not_initialized_returns_none_not_raises(self, orch_db_env):
        from src.autopilot.orchestrator import create_agent_for_task_direct
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="task-1", raw_description="r", done_definition="d", status="pending")
            )

        with patch(
            "src.core.app_context.get_app_state",
            side_effect=RuntimeError("App state not initialized"),
        ):
            result = create_agent_for_task_direct("task-1", "wf-1", "phase-1")

        assert result is None


class TestCreateCorrectiveTask:
    """Regression: a phase's output failing validation used to discard the
    whole run outright. _create_corrective_task instead asks the same
    worktree's agent to fix the specific problem, reopening the phase/
    workflow the engine had already marked complete."""

    def _seed_workflow_and_phase(self, db, wf_status="completed", phase_status="completed"):
        from src.core.database import Phase, PhaseExecution, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status=wf_status)
            )
            session.add(
                Phase(
                    id="phase-1",
                    workflow_id="wf-1",
                    order=1,
                    name="Feature Architect",
                    description="d",
                    done_definitions=["features.json valid"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-1", phase_id="phase-1", workflow_execution_id="wf-1",
                    status=phase_status,
                )
            )

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_creates_task_with_feedback_and_reopens_phase(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _create_corrective_task
        from src.core.database import PhaseExecution, Task, Workflow

        self._seed_workflow_and_phase(orch_db_env)
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        task_id = _create_corrective_task(
            "wf-1", "phase-1", "Feature Architect", "got 6, expected 1-5",
            OrchestratorLogger(tmp_path),
        )

        assert task_id is not None
        mock_create_agent.assert_called_once()
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"
            task = session.query(Task).filter_by(id=task_id).first()
            assert "got 6, expected 1-5" in task.enriched_description
            assert task.status == "in_progress"
            assert task.assigned_agent_id == "new-agent"

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_reopening_resets_stale_task_creation_claim(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: like _create_phase_task, this reopens a phase the
        engine already marked complete -- but until fixed it never reset
        task_creation_claimed_at. A phase visited earlier in the pipeline
        carries a claim already consumed by that prior cycle; leaving it
        set means _case_in_progress_complete's claim guard would see the
        stale value once the corrective task finishes and skip evaluating
        the transition forever, even though the work is done."""
        from datetime import datetime

        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _claim_phase_task_creation,
            _create_corrective_task,
        )
        from src.core.database import PhaseExecution

        self._seed_workflow_and_phase(orch_db_env)
        with orch_db_env.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.task_creation_claimed_at = datetime(2020, 1, 1)
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        _create_corrective_task(
            "wf-1", "phase-1", "Feature Architect", "got 6, expected 1-5",
            OrchestratorLogger(tmp_path),
        )

        with orch_db_env.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            assert _claim_phase_task_creation(session, "phase-1") is True

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_missing_workflow_returns_none(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _create_corrective_task

        result = _create_corrective_task(
            "does-not-exist", "phase-1", "Feature Architect", "bad output",
            OrchestratorLogger(tmp_path),
        )

        assert result is None
        mock_create_agent.assert_not_called()

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_skips_user_paused_workflow(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: same class of bug _try_auto_resume_paused_workflow
        was fixed for, but worse here -- unguarded, this would both
        reactivate the workflow AND immediately spawn a live agent against
        it, silently resuming real work on something the user explicitly
        paused."""
        from src.autopilot.orchestrator import OrchestratorLogger, _create_corrective_task
        from src.core.database import Workflow

        self._seed_workflow_and_phase(orch_db_env, wf_status="paused")
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.paused_by = "user"

        result = _create_corrective_task(
            "wf-1", "phase-1", "Feature Architect", "got 6, expected 1-5",
            OrchestratorLogger(tmp_path),
        )

        assert result is None
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct", return_value=None)
    def test_agent_creation_failure_marks_task_failed(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _create_corrective_task
        from src.core.database import Task

        self._seed_workflow_and_phase(orch_db_env)

        task_id = _create_corrective_task(
            "wf-1", "phase-1", "Feature Architect", "bad output",
            OrchestratorLogger(tmp_path),
        )

        assert task_id is None
        with orch_db_env.session_scope() as session:
            tasks = session.query(Task).filter_by(workflow_id="wf-1").all()
            assert len(tasks) == 1
            assert tasks[0].status == "failed"


class TestWaitForTaskTerminal:
    def test_returns_done_status(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _wait_for_task_terminal
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="t-1", raw_description="r", done_definition="d", status="done")
            )

        result = _wait_for_task_terminal("t-1", timeout_seconds=5, logger=OrchestratorLogger(tmp_path))
        assert result == "done"

    def test_returns_failed_status(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _wait_for_task_terminal
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="t-1", raw_description="r", done_definition="d", status="failed")
            )

        result = _wait_for_task_terminal("t-1", timeout_seconds=5, logger=OrchestratorLogger(tmp_path))
        assert result == "failed"

    def test_times_out_on_non_terminal_status(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _wait_for_task_terminal
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="t-1", raw_description="r", done_definition="d", status="in_progress")
            )

        with patch("src.autopilot.orchestrator.POLL_INTERVAL", 0.01):
            result = _wait_for_task_terminal("t-1", timeout_seconds=0.05, logger=OrchestratorLogger(tmp_path))
        assert result == "timeout"

    def test_stop_requested_returns_interrupted(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _wait_for_task_terminal
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="t-1", raw_description="r", done_definition="d", status="in_progress")
            )

        with patch("src.autopilot.orchestrator._should_stop", return_value=True):
            result = _wait_for_task_terminal("t-1", timeout_seconds=5, logger=OrchestratorLogger(tmp_path))
        assert result == "interrupted"


class TestNegotiateValidationFix:
    """The end-to-end negotiation loop: on a validation failure, ask the
    agent to fix it, re-validate, retry up to max_attempts, else give up."""

    def test_success_on_first_attempt(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _negotiate_validation_fix

        output_path = tmp_path / "features.json"

        def fake_create_task(*a, **k):
            # Simulate the agent fixing the file before task completes.
            output_path.write_text('{"features": [1, 2]}')
            return "task-1"

        def validate_fn(parsed):
            if len(parsed["features"]) > 5:
                raise ValueError("too many")

        with patch(
            "src.autopilot.orchestrator._create_corrective_task", side_effect=fake_create_task
        ), patch(
            "src.autopilot.orchestrator._wait_for_task_terminal", return_value="done"
        ):
            success, result = _negotiate_validation_fix(
                "wf-1", "phase-1", "Feature Architect", output_path, validate_fn,
                "got 6, expected 1-5", OrchestratorLogger(tmp_path),
            )

        assert success is True
        assert result == {"features": [1, 2]}

    def test_gives_up_after_max_attempts_still_invalid(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _negotiate_validation_fix

        output_path = tmp_path / "features.json"
        output_path.write_text('{"features": [1, 2, 3, 4, 5, 6]}')  # never fixed

        def validate_fn(parsed):
            if len(parsed["features"]) > 5:
                raise ValueError("too many")

        with patch(
            "src.autopilot.orchestrator._create_corrective_task", return_value="task-1"
        ), patch(
            "src.autopilot.orchestrator._wait_for_task_terminal", return_value="done"
        ) as mock_wait:
            success, result = _negotiate_validation_fix(
                "wf-1", "phase-1", "Feature Architect", output_path, validate_fn,
                "too many", OrchestratorLogger(tmp_path), max_attempts=2,
            )

        assert success is False
        assert result is None
        assert mock_wait.call_count == 2  # exhausted both attempts

    def test_gives_up_immediately_if_task_creation_fails(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _negotiate_validation_fix

        output_path = tmp_path / "features.json"

        with patch(
            "src.autopilot.orchestrator._create_corrective_task", return_value=None
        ):
            success, result = _negotiate_validation_fix(
                "wf-1", "phase-1", "Feature Architect", output_path, lambda x: None,
                "bad output", OrchestratorLogger(tmp_path),
            )

        assert success is False
        assert result is None

    def test_gives_up_if_corrective_task_fails(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _negotiate_validation_fix

        output_path = tmp_path / "features.json"

        with patch(
            "src.autopilot.orchestrator._create_corrective_task", return_value="task-1"
        ), patch(
            "src.autopilot.orchestrator._wait_for_task_terminal", return_value="failed"
        ):
            success, result = _negotiate_validation_fix(
                "wf-1", "phase-1", "Feature Architect", output_path, lambda x: None,
                "bad output", OrchestratorLogger(tmp_path),
            )

        assert success is False
        assert result is None


class TestGetAgents:
    def _make_agent(self, db, agent_id, status="working"):
        from src.core.database import Agent

        with db.session_scope() as session:
            session.add(Agent(id=agent_id, system_prompt="test", status=status, cli_type="pi"))

    def _make_task(self, db, task_id, workflow_id, assigned_agent_id):
        from src.core.database import Task

        with db.session_scope() as session:
            session.add(
                Task(
                    id=task_id,
                    raw_description="do work",
                    done_definition="done",
                    status="in_progress",
                    workflow_id=workflow_id,
                    assigned_agent_id=assigned_agent_id,
                )
            )

    def test_returns_all(self, orch_db_env):
        from src.autopilot.orchestrator import get_agents

        self._make_agent(orch_db_env, "a1")
        result = get_agents()
        assert len(result) == 1

    def test_filters_by_workflow(self, orch_db_env):
        from src.autopilot.orchestrator import get_agents

        self._make_agent(orch_db_env, "a1")
        self._make_agent(orch_db_env, "a2")
        self._make_task(orch_db_env, "t1", "wf-1", "a1")

        result = get_agents(workflow_id="wf-1")
        assert len(result) == 1
        assert result[0]["id"] == "a1"

    def test_returns_empty_on_none(self, orch_db_env):
        from src.autopilot.orchestrator import get_agents

        result = get_agents()
        assert result == []


class TestPeekAgentOutput:
    def test_returns_output(self, orch_db_env):
        from src.autopilot.orchestrator import peek_agent_output
        from src.core.database import Agent

        with orch_db_env.session_scope() as session:
            session.add(
                Agent(
                    id="a1",
                    system_prompt="test",
                    status="working",
                    cli_type="pi",
                    tmux_session_name="agent-a1",
                )
            )

        with patch("libtmux.Server") as mock_server_cls:
            mock_pane = Mock()
            mock_pane.cmd.return_value.stdout = ["Building..."]
            mock_window = Mock()
            mock_window.attached_pane = mock_pane
            mock_session = Mock()
            mock_session.attached_window = mock_window
            mock_server_cls.return_value.sessions.get.return_value = mock_session

            result = peek_agent_output("a1")
        assert result == "Building..."

    def test_returns_empty_on_none(self, orch_db_env):
        from src.autopilot.orchestrator import peek_agent_output
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

    def test_save_state(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, PipelineState

        logger = OrchestratorLogger(tmp_path)
        state = PipelineState(designs_processed=5)
        logger.save_state(state)
        state_file = tmp_path / "state.json"
        assert state_file.exists()



class TestSweepStrayFiles:
    def test_moves_root_files(self, tmp_path):
        from unittest.mock import patch

        from src.autopilot.orchestrator import OrchestratorLogger, _sweep_stray_files

        logger = OrchestratorLogger(tmp_path / "logs")
        feature = tmp_path / "feature"
        feature.mkdir()
        docs = feature / "docs"
        docs.mkdir()

        # Create stray file in project root (use a file from _SWEEP_REPORT_NAMES)
        stray = tmp_path / "review_report.md"
        stray.write_text("# Review Report")

        with patch("src.autopilot.orchestrator.SWEEP_ENABLED", True):
            _sweep_stray_files(tmp_path, feature, docs, logger)
        assert not stray.exists()
        assert (docs / "review_report.md").exists()

    def test_skips_root_files(self, tmp_path):
        from unittest.mock import patch

        from src.autopilot.orchestrator import OrchestratorLogger, _sweep_stray_files

        logger = OrchestratorLogger(tmp_path / "logs")
        feature = tmp_path / "feature"
        feature.mkdir()
        docs = feature / "docs"
        docs.mkdir()

        # Create files that should be skipped
        (tmp_path / "README.md").write_text("skip me")
        (tmp_path / "requirements.txt").write_text("skip")
        (tmp_path / ".env").write_text("skip")
        (tmp_path / "run.py").write_text("skip")

        with patch("src.autopilot.orchestrator.SWEEP_ENABLED", True):
            _sweep_stray_files(tmp_path, feature, docs, logger)
        assert (tmp_path / "README.md").exists()
        assert (tmp_path / "run.py").exists()

    def test_moves_feature_files(self, tmp_path):
        from unittest.mock import patch

        from src.autopilot.orchestrator import OrchestratorLogger, _sweep_stray_files

        logger = OrchestratorLogger(tmp_path / "logs")
        feature = tmp_path / "feature"
        feature.mkdir()
        docs = feature / "docs"
        docs.mkdir()

        stray = feature / "security_report.md"
        stray.write_text("# Security Report")

        with patch("src.autopilot.orchestrator.SWEEP_ENABLED", True):
            _sweep_stray_files(tmp_path, feature, docs, logger)
        assert not stray.exists()
        assert (docs / "security_report.md").exists()

    def test_moves_stray_dirs(self, tmp_path):
        from unittest.mock import patch

        from src.autopilot.orchestrator import OrchestratorLogger, _sweep_stray_files

        logger = OrchestratorLogger(tmp_path / "logs")
        feature = tmp_path / "feature"
        feature.mkdir()
        docs = feature / "docs"
        docs.mkdir()

        # Create report file directly in project root (not in a subdirectory)
        stray = tmp_path / "qa_result.json"
        stray.write_text("{}")

        with patch("src.autopilot.orchestrator.SWEEP_ENABLED", True):
            _sweep_stray_files(tmp_path, feature, docs, logger)
        assert not stray.exists()
        assert (docs / "qa_result.json").exists()

    def test_copies_report_docs(self, tmp_path):
        from unittest.mock import patch

        from src.autopilot.orchestrator import OrchestratorLogger, _sweep_stray_files

        logger = OrchestratorLogger(tmp_path / "logs")
        feature = tmp_path / "feature"
        feature.mkdir()
        docs = feature / "docs"
        docs.mkdir()

        # Create report in project docs/ dir
        proj_docs = tmp_path / "docs"
        proj_docs.mkdir()
        (proj_docs / "qa_result.json").write_text("{}")

        with patch("src.autopilot.orchestrator.SWEEP_ENABLED", True):
            _sweep_stray_files(tmp_path, feature, docs, logger)
        assert (docs / "qa_result.json").exists()

class TestCheckApiCredits:
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    def test_no_credits_issue(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator import check_api_credits

        mock_agents.return_value = [{"status": "working", "error": ""}]
        mock_tasks.return_value = []
        found, msg = check_api_credits()
        assert found is False

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    def test_agent_credit_error(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator import check_api_credits

        mock_agents.return_value = [
            {"id": "a1", "status": "error", "error": "insufficient credits"}
        ]
        mock_tasks.return_value = []
        found, msg = check_api_credits()
        assert found is True
        assert "credit" in msg.lower()

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    def test_task_credit_error(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator import check_api_credits

        mock_agents.return_value = []
        mock_tasks.return_value = [{"id": "t1", "error": "rate limit exceeded"}]
        found, msg = check_api_credits()
        assert found is True

    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_agents")
    def test_agent_output_log_credit(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator import check_api_credits

        mock_agents.return_value = [
            {
                "id": "a1",
                "status": "working",
                "error": "",
                "output_log": "quota exceeded",
            }
        ]
        mock_tasks.return_value = []
        found, msg = check_api_credits()
        assert found is True


class TestIsDesignFullyComplete:
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    def test_incomplete_workflow_status(self, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            is_design_fully_complete,
        )

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "unknown"}
        mock_tasks.return_value = []
        mock_agents.return_value = []
        result, msg = is_design_fully_complete("wf-1", logger)
        assert result is False
        assert "Workflow status" in msg

    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    def test_incomplete_has_active_tasks(self, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            is_design_fully_complete,
        )

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "active"}
        mock_tasks.side_effect = [
            [{"id": "t1"}],  # pending
            [],  # queued
            [],  # in_progress
            [],  # assigned
            [],  # failed
            [
                {"id": "t1"},
                {"id": "t2"},
                {"id": "t3"},
                {"id": "t4"},
                {"id": "t5"},
                {"id": "t6"},
                {"id": "t7"},
                {"id": "t8"},
                {"id": "t9"},
            ],  # done (only 9)
        ]
        mock_agents.return_value = []
        result, msg = is_design_fully_complete("wf-1", logger)
        assert result is False
        assert "active" in msg.lower()

    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    def test_incomplete_has_failed_tasks(self, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            is_design_fully_complete,
        )

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "active"}
        mock_tasks.side_effect = [
            [],  # pending
            [],  # queued
            [],  # in_progress
            [],  # assigned
            [{"id": "t1"}],  # failed
            [
                {"id": "t1"},
                {"id": "t2"},
                {"id": "t3"},
                {"id": "t4"},
                {"id": "t5"},
                {"id": "t6"},
                {"id": "t7"},
                {"id": "t8"},
                {"id": "t9"},
                {"id": "t10"},
            ],  # done (10)
        ]
        mock_agents.return_value = []
        result, msg = is_design_fully_complete("wf-1", logger)
        assert result is False
        assert "failed" in msg.lower()

    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    @patch("src.autopilot.orchestrator.get_workflow_status")
    def test_incomplete_has_active_agents(self, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            is_design_fully_complete,
        )

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "active"}
        mock_tasks.side_effect = [
            [],
            [],
            [],
            [],
            [],
            [
                {"id": "t1"},
                {"id": "t2"},
                {"id": "t3"},
                {"id": "t4"},
                {"id": "t5"},
                {"id": "t6"},
                {"id": "t7"},
                {"id": "t8"},
                {"id": "t9"},
                {"id": "t10"},
            ],  # done (10)
        ]
        mock_agents.return_value = [{"id": "a1", "status": "working"}]
        result, msg = is_design_fully_complete("wf-1", logger)
        assert result is False
        assert "agent" in msg.lower()


class TestAttemptRecovery:
    @patch("src.autopilot.orchestrator.get_db")
    @patch("src.autopilot.orchestrator.api_post")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    def test_no_recovery_needed(self, mock_tasks, mock_agents, mock_post, mock_get_db):
        from src.autopilot.orchestrator import OrchestratorLogger, attempt_recovery

        # Mock get_db to avoid database queries
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_get_db.return_value = mock_db

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_tasks.return_value = []
        mock_agents.return_value = []
        success, msg = attempt_recovery("wf-1", logger)
        assert success is False
        assert "No recovery" in msg

    @patch("src.autopilot.orchestrator.get_db")
    @patch("src.autopilot.orchestrator.update_task_status")
    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    def test_retries_failed_tasks(
        self, mock_tasks, mock_agents, mock_create_agent, mock_update_status, mock_get_db
    ):
        from src.autopilot.orchestrator import OrchestratorLogger, attempt_recovery

        # Mock get_db to avoid database queries
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_get_db.return_value = mock_db

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_tasks.side_effect = [
            [{"id": "t1", "retry_count": 0, "phase_id": "p1"}],  # failed tasks
        ]
        mock_agents.return_value = []
        mock_update_status.return_value = True
        # H-2: create_agent_for_task_direct replaces the old api_post self-HTTP call
        mock_create_agent.return_value = {"agent_id": "a1", "status": "created"}
        success, msg = attempt_recovery("wf-1", logger)
        assert success is True
        assert "retried" in msg.lower()

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    @patch("src.autopilot.orchestrator.get_agents")
    def test_retry_count_persists_and_eventually_stops(
        self, mock_agents, mock_create_agent, orch_db_env
    ):
        """Regression (real DB, no get_db mocking): a task whose retry
        always fails (e.g. its worktree was deleted out from under it) must
        stop retrying after 2 attempts, not loop forever. Before this fix,
        retry_count was never persisted, so this ran every ~60s indefinitely
        in production."""
        from src.autopilot.orchestrator import OrchestratorLogger, attempt_recovery
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(
                    id="t1",
                    raw_description="do work",
                    done_definition="done",
                    status="failed",
                    workflow_id="wf-1",
                    phase_id="p1",
                )
            )

        def _permanently_fails(task_id, workflow_id, phase_id=None):
            # Mirrors real AgentManager.create_agent_for_task's own cleanup
            # path, which re-marks the task "failed" on every failed
            # attempt (see manager.py's except block) -- without this the
            # task would incorrectly sit at "pending" between retries.
            with orch_db_env.session_scope() as session:
                t = session.query(Task).filter_by(id=task_id).first()
                t.status = "failed"
            return None

        mock_agents.return_value = []
        mock_create_agent.side_effect = _permanently_fails

        logger = OrchestratorLogger(Path("/tmp/logs"))
        attempt_recovery("wf-1", logger)
        attempt_recovery("wf-1", logger)
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="t1").first()
            assert task.retry_count == 2

        # Third call must skip retrying entirely -- retry_count already at cap
        calls_before = mock_create_agent.call_count
        attempt_recovery("wf-1", logger)
        assert mock_create_agent.call_count == calls_before
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="t1").first()
            assert task.retry_count == 2  # unchanged -- never even attempted

    @patch("src.autopilot.orchestrator.get_db")
    @patch("src.autopilot.orchestrator.api_post")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    def test_skips_max_retries(self, mock_tasks, mock_agents, mock_post, mock_get_db):
        from src.autopilot.orchestrator import OrchestratorLogger, attempt_recovery

        # Mock get_db to avoid database queries
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_get_db.return_value = mock_db

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_tasks.side_effect = [
            [{"id": "t1", "retry_count": 2, "phase_id": "p1"}],  # already retried 2x
        ]
        mock_agents.return_value = []
        success, msg = attempt_recovery("wf-1", logger)
        assert success is False

    @patch("src.autopilot.orchestrator.terminate_agent_direct")
    @patch("src.core.database.get_db")
    @patch("src.autopilot.orchestrator.get_db")
    @patch("src.autopilot.orchestrator.api_post")
    @patch("src.autopilot.orchestrator.get_agents")
    @patch("src.autopilot.orchestrator.get_tasks")
    def test_terminates_stale_agents(
        self, mock_tasks, mock_agents, mock_post, mock_get_db,
        mock_core_get_db, mock_terminate, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger, attempt_recovery

        # Mock get_db to avoid database queries
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_get_db.return_value = mock_db
        mock_core_get_db.return_value = mock_db

        # Mock terminate_agent_direct to succeed
        mock_terminate.return_value = True

        # Set PROJECT_PATH so code doesn't return early
        import os
        os.environ["PROJECT_PATH"] = str(tmp_path)

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_tasks.return_value = []
        mock_agents.return_value = [{"id": "a1", "status": "working"}]
        mock_post.return_value = {}
        success, msg = attempt_recovery("wf-1", logger)
        assert success is True
        assert "terminated" in msg.lower()


class TestUpdateOrchestratorMaxGotos:
    @patch("src.core.database.get_db")
    def test_updates_config(self, mock_get_db):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _update_orchestrator_max_gotos,
        )

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_defn = Mock(orchestrator_config={"max_total_gotos": 5})
        mock_db = Mock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = mock_defn
        mock_get_db.return_value.__enter__ = Mock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = Mock(return_value=False)
        _update_orchestrator_max_gotos(15, logger)
        assert mock_defn.orchestrator_config["max_total_gotos"] == 15
        mock_db.commit.assert_called()

    @patch("src.core.database.get_db")
    def test_handles_no_defn(self, mock_get_db):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _update_orchestrator_max_gotos,
        )

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_db = Mock()
        mock_db.query.return_value.filter_by.return_value.first.return_value = None
        mock_get_db.return_value.__enter__ = Mock(return_value=mock_db)
        mock_get_db.return_value.__exit__ = Mock(return_value=False)
        _update_orchestrator_max_gotos(10, logger)
        # Should not raise

    @patch("src.core.database.get_db")
    def test_handles_exception(self, mock_get_db):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _update_orchestrator_max_gotos,
        )

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_get_db.return_value.__enter__ = Mock(side_effect=Exception("DB error"))
        mock_get_db.return_value.__exit__ = Mock(return_value=False)
        _update_orchestrator_max_gotos(10, logger)
        # Should not raise


class TestShouldStop:
    def test_no_event(self):
        import src.autopilot.orchestrator as mod
        from src.autopilot.orchestrator import _should_stop

        old = mod._service_stop_event if hasattr(mod, "_service_stop_event") else None
        mod._service_stop_event = None
        try:
            assert _should_stop() is False
        finally:
            if old is not None:
                mod._service_stop_event = old

    def test_event_not_set(self):
        import threading

        import src.autopilot.orchestrator as mod
        from src.autopilot.orchestrator import _should_stop

        event = threading.Event()
        old = getattr(mod, "_service_stop_event", None)
        mod._service_stop_event = event
        try:
            assert _should_stop() is False
        finally:
            if old is not None:
                mod._service_stop_event = old

    def test_event_set(self):
        import threading

        import src.autopilot.orchestrator as mod
        from src.autopilot.orchestrator import _should_stop

        event = threading.Event()
        event.set()
        old = getattr(mod, "_service_stop_event", None)
        mod._service_stop_event = event
        try:
            assert _should_stop() is True
        finally:
            if old is not None:
                mod._service_stop_event = old


class TestGetLitellmConfig:
    def test_reads_env(self):
        import os

        from src.autopilot.orchestrator import get_litellm_config

        old_url = os.environ.get("LITELLM_PROXY_URL")
        os.environ["LITELLM_PROXY_URL"] = "http://localhost:4000"
        try:
            config = get_litellm_config()
            assert config["url"] == "http://localhost:4000"
        finally:
            if old_url is not None:
                os.environ["LITELLM_PROXY_URL"] = old_url
            elif "LITELLM_PROXY_URL" in os.environ:
                del os.environ["LITELLM_PROXY_URL"]

    def test_defaults(self):
        from src.autopilot.orchestrator import get_litellm_config

        config = get_litellm_config()
        assert config["url"] == ""
        assert config["api_key"] == ""
        assert config["cost_tracking"] is False
