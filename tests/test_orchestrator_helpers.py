"""Tests for autopilot/orchestrator.py — pure utilities + detection functions."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, patch

import pytest

from src.autopilot.orchestrator.state import FeatureRunStatus


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
        from src.autopilot.orchestrator.engine_client import file_hash

        f = tmp_path / "test.md"
        f.write_text("hello world")
        h1 = file_hash(f)
        h2 = file_hash(f)
        assert h1 == h2

    def test_different_content(self, tmp_path):
        from src.autopilot.orchestrator.engine_client import file_hash

        f1 = tmp_path / "a.md"
        f2 = tmp_path / "b.md"
        f1.write_text("hello")
        f2.write_text("world")
        assert file_hash(f1) != file_hash(f2)

    def test_length(self, tmp_path):
        from src.autopilot.orchestrator.engine_client import file_hash

        f = tmp_path / "test.md"
        f.write_text("data")
        assert len(file_hash(f)) == 16


class TestSelfHealTimeoutOrdering:
    """Regression: two independent self-heal escalations can race on the
    same stuck workflow -- _escalate_stale_active_workflows (kills a
    workflow with zero agent/task activity after
    STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS consecutive scans) and the
    stale task_creation_claimed_at clearing inside _case_in_progress_complete
    (CLAIM_STALE_TIMEOUT_SECONDS). _advance_phases refuses to touch a
    workflow once its status is "failed" (its own first check), so if the
    "kill it" escalation is faster than the "repair its claim" escalation,
    a workflow whose only real problem was a stuck claim gets killed before
    the claim-clearing fix ever gets a chance to run -- permanently, since
    nothing revives a "failed" workflow except the design's own limited
    retry budget. Observed live: a workflow got killed and re-killed this
    way until the design's retry budget was fully exhausted. The claim
    timeout must stay strictly shorter than the workflow-abandonment
    timeout so the targeted repair always gets a chance to fire first."""

    def test_claim_timeout_is_shorter_than_workflow_abandonment_timeout(self):
        from src.autopilot.orchestrator import DESIGN_QUEUE_SCAN_INTERVAL
        from src.autopilot.orchestrator.phase_transitions import CLAIM_STALE_TIMEOUT_SECONDS
        from src.autopilot.orchestrator.policy import STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS

        workflow_abandonment_timeout = (
            STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS * DESIGN_QUEUE_SCAN_INTERVAL
        )
        assert CLAIM_STALE_TIMEOUT_SECONDS < workflow_abandonment_timeout


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
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

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
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        state, hashes = pps.load()
        assert isinstance(state, PipelineState)
        assert hashes == set()

    def test_has_incomplete_work_no_file(self, orch_db_env):
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        assert pps.has_incomplete_work() is False

    def test_has_incomplete_work_no_design(self, orch_db_env):
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(current_design=None), set())
        assert pps.has_incomplete_work() is False

    def test_has_incomplete_work_with_design(self, orch_db_env):
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(current_design="test.md"), set())
        assert pps.has_incomplete_work() is True

    def test_get_last_run_id(self, orch_db_env):
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(run_id="run-789"), set())
        assert pps.get_last_run_id() == "run-789"

    def test_get_last_run_id_no_file(self, orch_db_env):
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        assert pps.get_last_run_id() is None

    def test_has_incomplete_work_tolerates_null_queue_status(self, orch_db_env):
        """Regression: `.get('queue_status', {})` only applies its default
        when the key is ABSENT, not when the stored value is explicitly
        null. Without the `or {}` guard, a stored `queue_status: null`
        makes the next `.get('status')` call raise AttributeError on None
        instead of being treated as 'no incomplete work'."""
        from src.autopilot.orchestrator.state import PersistentPipelineState, _set_project_context
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

        from src.autopilot.orchestrator import PipelineState

        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        with patch(
            "src.autopilot.orchestrator.state._set_project_context",
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
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(designs_processed=5, run_id="run-1"), {"h1", "h2"})

        pps.remove_processed_hash("h1")

        state, hashes = pps.load()
        assert hashes == {"h2"}
        # State untouched by the hash removal.
        assert state.designs_processed == 5
        assert state.run_id == "run-1"

    def test_remove_processed_hash_missing_is_safe(self, orch_db_env):
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        pps.remove_processed_hash("does-not-exist")  # should not raise
        _, hashes = pps.load()
        assert hashes == set()

    def test_save_state_only_does_not_touch_processed_hashes(self, orch_db_env):
        """Regression: run_single_workflow/run_continuous_pipeline call this
        for an early mid-run checkpoint (current_design/current_workflow_id
        become known well before run_single_design returns, but were
        previously only ever persisted afterward -- see save_state_only's
        docstring for the live symptom this caused: the status endpoint
        showing the previous, already-finished run for a new run's entire
        duration). It must only ever touch the state key, never
        processed_hashes -- unlike save(), it's called an unknown number of
        times per design run, and clobbering processed_hashes with a stale
        in-memory set on one of those calls would un-mark already-completed
        designs."""
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps = PersistentPipelineState()
        pps.save(PipelineState(designs_processed=2, run_id="run-1"), {"h1", "h2"})

        pps.save_state_only(PipelineState(current_workflow_id="wf-new", run_id="run-1"))

        state, hashes = pps.load()
        assert state.current_workflow_id == "wf-new"
        assert hashes == {"h1", "h2"}

    def test_namespaced_per_project(self, orch_db_env):
        """Regression: PersistentPipelineState used to store current_
        workflow_id/current_feature_folder/processed_hashes under bare,
        unnamespaced ProjectContext keys shared by EVERY project -- two
        concurrent run_continuous_pipeline loops (one per project) would
        clobber each other's processed-design tracking and resume pointer.
        Two instances for two different project_ids must see only their
        own state."""
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState

        pps_a = PersistentPipelineState(project_id="proj-a")
        pps_b = PersistentPipelineState(project_id="proj-b")

        pps_a.save(PipelineState(designs_processed=3, run_id="run-a"), {"hash-a"})
        pps_b.save(PipelineState(designs_processed=9, run_id="run-b"), {"hash-b"})

        state_a, hashes_a = pps_a.load()
        state_b, hashes_b = pps_b.load()

        assert state_a.designs_processed == 3
        assert state_a.run_id == "run-a"
        assert hashes_a == {"hash-a"}

        assert state_b.designs_processed == 9
        assert state_b.run_id == "run-b"
        assert hashes_b == {"hash-b"}

        # Clearing project A must not touch project B's state.
        pps_a.clear()
        cleared_a, _ = pps_a.load()
        still_there_b, _ = pps_b.load()
        assert cleared_a.designs_processed == 0
        assert still_there_b.designs_processed == 9

    def test_migrates_legacy_state_on_first_load(self, orch_db_env):
        """Sotto's currently-running pipeline (pre-multi-project) persisted
        its pipeline state and processed hashes under the old bare keys.
        The first project_id-aware load() must migrate them onto the
        namespaced keys in place, or a design already processed under the
        legacy key would appear un-processed and get reprocessed."""
        from src.autopilot.orchestrator import PipelineState
        from src.autopilot.orchestrator.state import PersistentPipelineState
        from src.core.database import ProjectContext, get_db

        legacy = PersistentPipelineState()  # project_id=None -> legacy keys
        legacy.save(PipelineState(designs_processed=7, run_id="run-legacy"), {"h1", "h2"})

        pps = PersistentPipelineState(project_id="proj-sotto")
        state, hashes = pps.load()

        assert state.designs_processed == 7
        assert state.run_id == "run-legacy"
        assert hashes == {"h1", "h2"}

        with get_db() as db:
            assert (
                db.query(ProjectContext)
                .filter_by(key=PersistentPipelineState.STATE_KEY_LEGACY)
                .first()
                is None
            )
            assert (
                db.query(ProjectContext)
                .filter_by(key=PersistentPipelineState.PROCESSED_KEY_LEGACY)
                .first()
                is None
            )
            assert (
                db.query(ProjectContext).filter_by(key=pps.STATE_KEY).first()
                is not None
            )

        # Idempotent: a second load() after migration behaves like any
        # other project's state, no re-migration/duplication.
        state_again, hashes_again = pps.load()
        assert state_again.designs_processed == 7
        assert hashes_again == {"h1", "h2"}


class TestSetProjectContextUpsert:
    """_set_project_context uses an atomic INSERT ... ON CONFLICT DO UPDATE
    instead of a read-then-write (SELECT then add-or-update), which had a
    real TOCTOU window: two callers writing the same key for the first time
    could both see no existing row and both attempt to insert, raising
    IntegrityError on ProjectContext.key's unique constraint."""

    def test_set_then_set_again_updates_in_place(self, orch_db_env):
        from src.autopilot.orchestrator.state import _get_project_context, _set_project_context
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
        from src.autopilot.orchestrator.state import _get_project_context, _set_project_context
        from src.core.database import get_db

        with get_db() as db:
            _set_project_context(db, "race-key", "first")
        with get_db() as db:
            _set_project_context(db, "race-key", "second")  # must not raise

        with get_db() as db:
            assert _get_project_context(db, "race-key") == "second"


class TestDetectHardError:
    def test_crashed_agent(self):
        from src.autopilot.orchestrator.policy import detect_hard_error

        agents = [{"id": "a1", "status": "error"}]
        found, msg = detect_hard_error(agents, [])
        assert found is True
        assert "Crashed" in msg

    def test_critical_failure(self):
        from src.autopilot.orchestrator.policy import detect_hard_error

        agents = [{"id": "a1", "status": "active"}]
        tasks = [{"id": "t1", "priority": "critical", "description": "fix auth"}]
        found, msg = detect_hard_error(agents, tasks)
        assert found is True
        assert "Critical" in msg

    def test_architectural_failure(self):
        from src.autopilot.orchestrator.policy import detect_hard_error

        agents = []
        tasks = [{"id": "t1", "description": "Architectural issue found"}]
        found, msg = detect_hard_error(agents, tasks)
        assert found is True

    def test_no_error(self):
        from src.autopilot.orchestrator.policy import detect_hard_error

        agents = [{"id": "a1", "status": "active"}]
        tasks = [{"id": "t1", "priority": "low", "description": "minor fix"}]
        found, msg = detect_hard_error(agents, tasks)
        assert found is False

    def test_workflow_filter(self):
        from src.autopilot.orchestrator.policy import detect_hard_error

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
        from src.autopilot.orchestrator.policy import detect_impasse

        found, msg = detect_impasse([], [{"id": "t1"}], [], elapsed_seconds=700)
        assert found is True
        assert "No active agents" in msg

    def test_grace_period(self):
        from src.autopilot.orchestrator.policy import detect_impasse

        found, msg = detect_impasse([], [{"id": "t1"}], [], elapsed_seconds=100)
        assert found is False

    def test_stuck_task(self):
        from src.autopilot.orchestrator.policy import detect_impasse

        started = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
        agents = [{"id": "a1", "status": "active"}]
        tasks = [{"id": "t1", "started_at": started}]
        found, msg = detect_impasse(agents, [], tasks)
        assert found is True
        assert "stuck" in msg.lower()

    def test_no_impasse(self):
        from src.autopilot.orchestrator.policy import detect_impasse

        agents = [{"id": "a1", "status": "active"}]
        found, msg = detect_impasse(agents, [{"id": "t1"}], [], elapsed_seconds=100)
        assert found is False


class TestDetectArchitecturalIssue:
    def test_finds_issue(self, tmp_path):
        from src.autopilot.orchestrator.policy import detect_architectural_issue

        report = tmp_path / "report.md"
        report.write_text("This has a major architectural issue that needs redesign")
        found, msg = detect_architectural_issue([str(report)])
        assert found is True
        assert "architectural issue" in msg.lower()

    def test_no_issue(self, tmp_path):
        from src.autopilot.orchestrator.policy import detect_architectural_issue

        report = tmp_path / "report.md"
        report.write_text("Everything looks good, tests passing")
        found, msg = detect_architectural_issue([str(report)])
        assert found is False

    def test_missing_file(self):
        from src.autopilot.orchestrator.policy import detect_architectural_issue

        found, msg = detect_architectural_issue(["/nonexistent/file.md"])
        assert found is False

    def test_empty_list(self):
        from src.autopilot.orchestrator.policy import detect_architectural_issue

        found, msg = detect_architectural_issue([])
        assert found is False


class TestScanDesignQueue:
    def test_scans_md_files(self, tmp_path):
        from src.autopilot.orchestrator.queue import scan_design_queue

        (tmp_path / "design_a.md").write_text("# Design A")
        (tmp_path / "design_b.md").write_text("# Design B")
        designs = scan_design_queue(tmp_path, set())
        assert len(designs) == 2

    def test_skips_processed(self, tmp_path):
        from src.autopilot.orchestrator.engine_client import file_hash
        from src.autopilot.orchestrator.queue import scan_design_queue

        f = tmp_path / "design.md"
        f.write_text("# Design")
        h = file_hash(f)
        designs = scan_design_queue(tmp_path, {h})
        assert len(designs) == 0

    def test_nonexistent_dir(self):
        from src.autopilot.orchestrator.queue import scan_design_queue

        designs = scan_design_queue(Path("/nonexistent"), set())
        assert designs == []

    def test_skips_directories(self, tmp_path):
        from src.autopilot.orchestrator.queue import scan_design_queue

        (tmp_path / "subdir.md").mkdir()
        designs = scan_design_queue(tmp_path, set())
        assert len(designs) == 0

    def test_queue_order(self, tmp_path):
        from src.autopilot.orchestrator.queue import scan_design_queue

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
    """Regression: pick_next_design's DB-first path uses get_db(), whose
    default (no HEPHAESTUS_TEST_DB) is the *relative* path "hephaestus.db"
    -- the real repo-root production database when pytest runs from there,
    not an isolated fixture. These tests only passed by accident: relying
    on a DB exception (no tables yet) to fall through to the file-scan path
    the assertions actually depend on. orch_db_env (used elsewhere in this
    file) doesn't fit here -- it calls create_tables(), which makes
    pick_next_design cleanly find "no active project" and hard-return None
    without ever reaching the file-scan fallback, breaking test_picks_first's
    own expectation. This fixture points at a guaranteed-uninitialized
    tmp_path DB instead, so the exception path fires deterministically.
    """

    @pytest.fixture
    def isolated_test_db(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(tmp_path / "isolated_pick_next.db"))

    def test_returns_none_empty(self, tmp_path, isolated_test_db):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design

        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger)
        assert result is None

    def test_picks_first(self, tmp_path, isolated_test_db):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design

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
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design
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
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design
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

    def test_project_id_wins_over_is_active_when_given(self, tmp_path, orch_db_env):
        """Regression: pick_next_design used to resolve "the project" purely
        via AutopilotProject.is_active=True, with no project_id parameter at
        all. Two concurrent run_continuous_pipeline loops (one per project,
        see AutopilotServiceRegistry) would both hit this same global flag --
        whichever project most recently (re)started flips is_active, so an
        earlier-started project's loop would silently start pulling the
        OTHER project's designs the moment a second project starts. Passing
        project_id must make pick_next_design pick from THAT project
        regardless of which one is currently is_active."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design
        from src.core.database import AutopilotDesign, AutopilotProject

        session = orch_db_env.get_session()
        session.add(
            AutopilotProject(
                id="proj-a", name="a", base_dir="/tmp/proj-a", is_active=False
            )
        )
        session.add(
            AutopilotProject(
                id="proj-b", name="b", base_dir="/tmp/proj-b", is_active=True
            )
        )
        (tmp_path / "a.md").write_text("# A")
        (tmp_path / "b.md").write_text("# B")
        session.add(
            AutopilotDesign(
                id="des-a", project_id="proj-a", filename="a.md", name="A",
                status="pending", ordinal=1, file_path=str(tmp_path / "a.md"),
            )
        )
        session.add(
            AutopilotDesign(
                id="des-b", project_id="proj-b", filename="b.md", name="B",
                status="pending", ordinal=1, file_path=str(tmp_path / "b.md"),
            )
        )
        session.commit()
        session.close()

        logger = OrchestratorLogger(tmp_path)

        # proj-a is passed explicitly even though proj-b is is_active=True.
        # file_path exists on disk for both designs, so the DB-first path
        # returns directly without ever falling through to file-scan --
        # this pins down the project = filter_by(id=project_id) lookup
        # itself, not the separate file-scan-fallback project linking.
        result = pick_next_design(tmp_path, set(), logger, project_id="proj-a")

        assert result is not None
        assert result.db_id == "des-a"

    def test_orphaned_failed_workflow_does_not_block_an_active_design(
        self, tmp_path, orch_db_env
    ):
        """Regression (live incident): a failed Feature Architect retry
        attempt left a Workflow row with design_id set but no Feature
        linking to it (the successful retry's own Workflow row was what
        actually got linked). pick_next_design's failed_wf check used to
        match on design_id alone, so this orphaned failure permanently
        blocked the design -- exhausting retries and marking it "failed",
        which derive_design_status then healed back to "active" since
        every real feature was fine, an infinite ping-pong with no actual
        problem to fix. Must instead recognize the workflow as orphaned,
        clear its design_id so it stops matching, and let the design
        proceed normally."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow

        (tmp_path / "d.md").write_text("# D")

        session = orch_db_env.get_session()
        session.add(
            AutopilotProject(id="proj-orphan", name="p", base_dir=str(tmp_path), is_active=True)
        )
        session.add(
            AutopilotDesign(
                id="des-orphan", project_id="proj-orphan", filename="d.md", name="D",
                status="active", file_path=str(tmp_path / "d.md"),
            )
        )
        # Genuine incomplete work -- no workflow yet, nothing wrong with it.
        session.add(
            Feature(
                id="feat-pending", design_id="des-orphan", feature_key="feat-a",
                name="Feature A", scope="s", status="pending",
            )
        )
        # The orphaned failed workflow: linked to the design, linked to no feature.
        session.add(
            Workflow(
                id="wf-orphaned-failure", name="Feature Architect", status="failed",
                phases_folder_path="/tmp", design_id="des-orphan",
            )
        )
        session.commit()
        session.close()

        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger, project_id="proj-orphan")

        assert result is not None
        assert result.db_id == "des-orphan"

        from src.core.database import get_db

        with get_db() as db:
            design = db.query(AutopilotDesign).filter_by(id="des-orphan").first()
            # "processing" (not "failed") -- pick_next_design resumed it
            # normally instead of exhausting retries on the orphan.
            assert design.status == "processing"
            assert design.error is None

            wf = db.query(Workflow).filter_by(id="wf-orphaned-failure").first()
            assert wf.design_id is None

    def test_failed_workflow_linked_to_incomplete_feature_still_blocks(
        self, tmp_path, orch_db_env
    ):
        """Companion to the orphaned-workflow regression above: a failed
        workflow that IS linked to a still-incomplete feature must keep
        blocking (retry, then eventually mark failed) -- the orphan
        fallback must not accidentally swallow genuine failures too."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow

        session = orch_db_env.get_session()
        session.add(
            AutopilotProject(id="proj-real-fail", name="p", base_dir=str(tmp_path), is_active=True)
        )
        session.add(
            AutopilotDesign(
                id="des-real-fail", project_id="proj-real-fail", filename="d.md", name="D",
                status="active",
            )
        )
        session.add(
            Workflow(
                id="wf-real-failure", name="autopilot", status="failed",
                phases_folder_path="/tmp", design_id="des-real-fail",
            )
        )
        session.add(
            Feature(
                id="feat-blocked", design_id="des-real-fail", feature_key="feat-a",
                name="Feature A", scope="s", status="failed",
                workflow_id="wf-real-failure",
            )
        )
        session.commit()
        session.close()

        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger, project_id="proj-real-fail")

        # Retried, not resumed directly -- pick_next_design resets the
        # design to "pending" for a fresh attempt rather than returning it.
        assert result is None

        from src.core.database import get_db

        with get_db() as db:
            design = db.query(AutopilotDesign).filter_by(id="des-real-fail").first()
            assert design.status == "pending"

            wf = db.query(Workflow).filter_by(id="wf-real-failure").first()
            assert wf.design_id == "des-real-fail"


class TestHasResumableActiveDesign:
    """Regression: the 'workflow still active' gate in run_continuous_pipeline
    used to block picking up ANY new work whenever any workflow was active
    anywhere in the project, even one belonging to a completely unrelated
    design. A design that's already active with ready, dependency-satisfied
    features would then sit untouched indefinitely behind an unrelated
    design's in-progress chain. _has_resumable_active_design lets the gate
    tell the two cases apart -- resuming an active design is always safe
    (Tier-1 Phase-0 skip + pause_existing=False), only a brand new design's
    Phase 0 dispatch is destructive."""

    def test_true_when_active_design_has_incomplete_features(self, tmp_path, orch_db_env):
        from src.autopilot.orchestrator.queue import _has_resumable_active_design
        from src.core.database import AutopilotDesign, Feature

        session = orch_db_env.get_session()
        session.add(
            AutopilotDesign(
                id="des-backend", project_id="proj-1", filename="d.md", name="Backend",
                status="active",
            )
        )
        session.add(
            Feature(
                id="feat-done", design_id="des-backend", feature_key="auth-fraud",
                name="Auth", scope="s", status="completed",
            )
        )
        session.add(
            Feature(
                id="feat-ready", design_id="des-backend", feature_key="credit-system",
                name="Credit", scope="s", status="pending", depends_on=["auth-fraud"],
            )
        )
        session.commit()
        session.close()

        assert _has_resumable_active_design("proj-1") is True

    def test_false_when_no_active_design(self, tmp_path, orch_db_env):
        from src.autopilot.orchestrator.queue import _has_resumable_active_design
        from src.core.database import AutopilotDesign

        session = orch_db_env.get_session()
        session.add(
            AutopilotDesign(
                id="des-pending", project_id="proj-1", filename="d.md", name="D",
                status="pending",
            )
        )
        session.commit()
        session.close()

        assert _has_resumable_active_design("proj-1") is False

    def test_false_when_active_design_has_no_incomplete_features(self, tmp_path, orch_db_env):
        from src.autopilot.orchestrator.queue import _has_resumable_active_design
        from src.core.database import AutopilotDesign, Feature

        session = orch_db_env.get_session()
        session.add(
            AutopilotDesign(
                id="des-done", project_id="proj-1", filename="d.md", name="D",
                status="active",
            )
        )
        session.add(
            Feature(
                id="feat-a", design_id="des-done", feature_key="a",
                name="A", scope="s", status="completed",
            )
        )
        session.commit()
        session.close()

        assert _has_resumable_active_design("proj-1") is False

    def test_false_without_project_id(self, tmp_path, orch_db_env):
        from src.autopilot.orchestrator.queue import _has_resumable_active_design

        assert _has_resumable_active_design(None) is False


class TestShouldPauseForReview:
    """Regression (SOLID review Theme B, 2026-08-20): _should_pause_for_review
    used to fail open (return False, "no review needed") on a DB error --
    for every one of its 5 call sites, that meant review_mode's human-
    approval gate could be silently skipped entirely on a transient DB
    hiccup, with no visible sign anything was bypassed. It now fails safe
    (True), which routes into a normal, human-clearable "paused for review"
    state instead -- worst case an unnecessary pause, not a silently
    skipped checkpoint."""

    def test_true_when_review_mode_enabled(self, orch_db_env):
        from src.autopilot.orchestrator import _should_pause_for_review
        from src.core.database import AutopilotProject

        session = orch_db_env.get_session()
        session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp", review_mode=True))
        session.commit()
        session.close()

        assert _should_pause_for_review("proj-1") is True

    def test_false_when_review_mode_disabled(self, orch_db_env):
        from src.autopilot.orchestrator import _should_pause_for_review
        from src.core.database import AutopilotProject

        session = orch_db_env.get_session()
        session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp", review_mode=False))
        session.commit()
        session.close()

        assert _should_pause_for_review("proj-1") is False

    def test_fails_safe_to_true_on_a_db_error(self, orch_db_env, monkeypatch):
        from sqlalchemy.exc import OperationalError

        import src.core.database as db_module
        from src.autopilot.orchestrator import _should_pause_for_review

        def _raise(*a, **kw):
            raise OperationalError("SELECT ...", {}, Exception("database is locked"))

        monkeypatch.setattr(db_module, "get_db", _raise)

        assert _should_pause_for_review("proj-1") is True


class TestCreateFeatureFolder:
    def test_creates_folder(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import create_feature_folder

        logger = OrchestratorLogger(tmp_path)
        folder = create_feature_folder(tmp_path, "test_feature", logger)
        assert folder.exists()
        assert folder.is_dir()
        assert "test_feature" in folder.name


class TestCopyDesignDocument:
    def test_copies_file(self, tmp_path):
        from src.autopilot.orchestrator.state import DesignEntry
        from src.autopilot.orchestrator.worktree_integration import copy_design_document

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
        from src.autopilot.orchestrator.reporting import _report_path

        result = _report_path(tmp_path, "report.md")
        assert result == tmp_path / "report.md"


class TestCollectReportSummaries:
    def test_collects_reports(self, tmp_path):
        from src.autopilot.orchestrator.reporting import collect_report_summaries

        # Reports are at project_path level, not in subdirectory
        (tmp_path / "qa.md").write_text("# QA Report\nAll tests passed")
        (tmp_path / "architecture.md").write_text("# Architecture")
        result = collect_report_summaries(tmp_path)
        assert "qa" in result
        assert "All tests passed" in result["qa"]
        assert "architecture" in result

    def test_empty_dir(self, tmp_path):
        from src.autopilot.orchestrator.reporting import collect_report_summaries

        result = collect_report_summaries(tmp_path)
        # All report files not found
        assert all("not found" in v for v in result.values())
        assert len(result) == 8


class TestRegisterOrchestratorAgent:
    """Regression: registering the orchestrator's own Agent row on restart
    tried to reuse Agent.tmux_session_name="orchestrator" by marking the
    OLD row from the previous session "terminated" -- but never freed the
    tmux_session_name value itself, which has a UNIQUE constraint. The old
    "terminated" row still occupied it, so the new row's INSERT always
    collided and the whole registration silently failed (caught, logged as
    just a warning). Every restart after the first left the returned agent
    id pointing at a row that was never actually persisted -- so any task
    creation using it as created_by_agent_id (_create_phase_task) hit a
    FOREIGN KEY failure the moment FK enforcement was turned on. Observed
    live: doc_review's phase task creation failed this exact way after a
    second restart.

    The first fix attempt renamed the freed-up row's tmux_session_name
    using existing.id[:8] -- but every orchestrator agent id shares the
    literal prefix "orchestrator-" (id = f"orchestrator-{uuid4().hex[:8]}"),
    so id[:8] is always the same string "orchestr" for every one of them.
    That's not unique at all: the SECOND collision (third restart) renamed
    its freed-up row to the exact same value the FIRST collision (second
    restart) already used, colliding with it and failing exactly the same
    way the original bug did. Observed live, restart after restart."""

    def test_three_consecutive_registrations_never_collide(
        self, orch_db_env, tmp_path, monkeypatch
    ):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _register_orchestrator_agent,
        )
        from src.core.database import Agent

        # _register_orchestrator_agent calls the bare DatabaseManager()
        # (defaults to hephaestus.db, not HEPHAESTUS_TEST_DB) -- redirect it
        # to this test's real sqlite fixture instead of the live production
        # database.
        monkeypatch.setattr(
            "src.core.database.DatabaseManager", lambda *a, **kw: orch_db_env
        )

        logger = OrchestratorLogger(tmp_path)

        # Three rounds, not two: the id[:8] bug only manifests on the
        # SECOND rename (third registration), when it collides with the
        # FIRST rename (from the second registration) instead of the live
        # "orchestrator" row.
        ids = [_register_orchestrator_agent(tmp_path, "pi", logger) for _ in range(3)]

        assert all(i is not None for i in ids), ids
        assert len(set(ids)) == 3, "each registration must produce a distinct agent id"

        with orch_db_env.session_scope() as session:
            current = session.query(Agent).filter_by(id=ids[-1]).first()
            assert current is not None
            assert current.tmux_session_name == "orchestrator"

            terminated = [
                session.query(Agent).filter_by(id=i).first() for i in ids[:-1]
            ]
            for agent in terminated:
                assert agent.status == "terminated"
                assert agent.terminated_at is not None
                assert agent.tmux_session_name != "orchestrator"

            # The real regression: both freed-up rows must land on
            # DISTINCT renamed names, not collide with each other.
            renamed_names = [agent.tmux_session_name for agent in terminated]
            assert len(set(renamed_names)) == len(renamed_names), renamed_names


class TestGetTasks:
    def _make_task(self, db, task_id, status="pending", workflow_id="wf-1"):
        from src.core.database import Task, Workflow

        with db.session_scope() as session:
            if workflow_id and not session.query(Workflow).filter_by(id=workflow_id).first():
                session.add(
                    Workflow(id=workflow_id, name="t", phases_folder_path="/tmp", status="active")
                )
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
        from src.autopilot.orchestrator.engine_client import get_tasks

        self._make_task(orch_db_env, "t1", status="done")
        result = get_tasks()
        assert len(result) == 1

    def test_returns_empty_on_none(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_tasks

        result = get_tasks()
        assert result == []

    def test_unwraps_dict(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_tasks

        self._make_task(orch_db_env, "t1")
        result = get_tasks()
        assert len(result) == 1
        assert result[0]["id"] == "t1"

    def test_with_params(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_tasks

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
        from src.autopilot.orchestrator.engine_client import get_tasks, increment_task_retry_count

        self._make_task(orch_db_env, "t1")
        assert get_tasks()[0]["retry_count"] == 0

        increment_task_retry_count("t1")
        increment_task_retry_count("t1")
        result = get_tasks()
        assert result[0]["retry_count"] == 2


class TestWorkflowBelongsToProject:
    """_workflow_belongs_to_project underlies both get_active_workflows'
    project scoping and run_continuous_pipeline's stale-workflow check --
    covering its decision logic directly, once, is more precise than
    re-deriving the same cases through each caller's DB/loop scaffolding."""

    def test_project_id_match_wins_even_with_no_working_directory(self):
        from src.autopilot.orchestrator.state import _workflow_belongs_to_project

        assert _workflow_belongs_to_project("proj-a", None, "proj-a", "/x/a") is True

    def test_project_id_mismatch_even_if_path_would_match(self):
        """project_id is authoritative -- a stale/incorrect working_directory
        (e.g. project directory renamed on disk after the workflow row was
        created) must not override a definitive project_id mismatch."""
        from src.autopilot.orchestrator.state import _workflow_belongs_to_project

        assert (
            _workflow_belongs_to_project("proj-b", "/x/a/.worktrees/wt_1", "proj-a", "/x/a")
            is False
        )

    def test_falls_back_to_path_when_no_project_id_on_either_side(self, tmp_path):
        from src.autopilot.orchestrator.state import _workflow_belongs_to_project

        project = tmp_path / "sotto"
        project.mkdir()
        wt = project / ".worktrees" / "wt_1"
        wt.mkdir(parents=True)

        assert (
            _workflow_belongs_to_project(None, str(wt), None, str(project)) is True
        )

    def test_sibling_directory_name_prefix_does_not_false_match(self, tmp_path):
        """Regression: a raw str.startswith() prefix match wrongly treated
        "/code/project-a" as matching "/code/project-ab/..." -- a sibling
        project whose name happens to be a superstring. Path.is_relative_to
        must be used instead, or a workflow in a same-parent-directory
        sibling project silently blocks/gets force-failed/gets its agents
        terminated by a different project's pipeline."""
        from src.autopilot.orchestrator.state import _workflow_belongs_to_project

        project_a = tmp_path / "project-a"
        project_a.mkdir()
        project_ab = tmp_path / "project-ab"
        wt = project_ab / ".worktrees" / "wt_1"
        wt.mkdir(parents=True)

        assert (
            _workflow_belongs_to_project(None, str(wt), None, str(project_a)) is False
        )

    def test_no_working_directory_and_no_project_id_defaults_to_false(self):
        """Regression: the previous-workflow check used to only clear stale
        state when working_directory was truthy AND mismatched -- a workflow
        with no working_directory recorded at all silently fell through as
        "still belongs to the current project," reproducing the original
        cross-project blocking/force-fail bug for any such row."""
        from src.autopilot.orchestrator.state import _workflow_belongs_to_project

        assert _workflow_belongs_to_project(None, None, "proj-a", "/x/a") is False

    def test_current_project_id_unknown_falls_back_to_path(self, tmp_path):
        """If the CURRENT project's id couldn't be resolved (e.g. no
        AutopilotProject row for this project_path yet), project_id
        comparison is skipped entirely and the path check still applies."""
        from src.autopilot.orchestrator.state import _workflow_belongs_to_project

        project = tmp_path / "sotto"
        project.mkdir()
        wt = project / ".worktrees" / "wt_1"
        wt.mkdir(parents=True)

        assert (
            _workflow_belongs_to_project("proj-a", str(wt), None, str(project)) is True
        )


class TestGetWorkflowStatus:
    def test_returns_project_id_and_working_directory(self, orch_db_env):
        """Regression: get_workflow_status used to omit project_id/
        working_directory entirely, so run_continuous_pipeline's stale-
        workflow check couldn't tell a previous run's workflow apart from a
        DIFFERENT project's workflow -- switching the active project in the
        UI left the pipeline blocked behind (and eventually force-failing)
        an unrelated, possibly deliberately-paused workflow belonging to a
        project it no longer had anything to do with."""
        from src.autopilot.orchestrator.engine_client import get_workflow_status
        from src.core.database import AutopilotProject, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                AutopilotProject(id="proj-other", name="other", base_dir="/tmp/other-project")
            )
            session.add(
                Workflow(
                    id="wf-1",
                    name="test",
                    phases_folder_path="/tmp",
                    status="paused",
                    project_id="proj-other",
                    working_directory="/Users/x/code/other-project/.worktrees/wt_1",
                )
            )

        result = get_workflow_status("wf-1")
        assert result["status"] == "paused"
        assert result["project_id"] == "proj-other"
        assert result["working_directory"] == "/Users/x/code/other-project/.worktrees/wt_1"

    def test_returns_empty_dict_for_missing_workflow(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_workflow_status

        assert get_workflow_status("nonexistent") == {}


class TestGetActiveWorkflows:
    def _make_workflow(self, db, wf_id, working_directory, status="active"):
        from src.core.database import Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id=wf_id,
                    name="test",
                    phases_folder_path="/tmp",
                    status=status,
                    working_directory=working_directory,
                )
            )

    def test_unscoped_returns_all_active(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_active_workflows

        self._make_workflow(orch_db_env, "wf-a", "/Users/x/code/project-a/.worktrees/wt_1")
        self._make_workflow(orch_db_env, "wf-b", "/Users/x/code/project-b/.worktrees/wt_1")

        result = get_active_workflows()
        assert {r["id"] for r in result} == {"wf-a", "wf-b"}

    def test_scoped_excludes_other_projects(self, orch_db_env):
        """Regression: get_active_workflows() had no project scoping at
        all -- an active workflow in a DIFFERENT project would block a new
        project's design-queue loop forever (no escalation/timeout on that
        branch, unlike the current_workflow_id completeness check) and,
        on pipeline stop, get forcibly paused as pure collateral damage
        from an unrelated project's pipeline stopping."""
        from src.autopilot.orchestrator.engine_client import get_active_workflows

        self._make_workflow(orch_db_env, "wf-a", "/Users/x/code/project-a/.worktrees/wt_1")
        self._make_workflow(orch_db_env, "wf-b", "/Users/x/code/project-b/.worktrees/wt_1")

        result = get_active_workflows(project_path="/Users/x/code/project-a")
        assert [r["id"] for r in result] == ["wf-a"]

    def test_scoped_ignores_workflow_with_no_working_directory(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_active_workflows

        self._make_workflow(orch_db_env, "wf-a", None)

        result = get_active_workflows(project_path="/Users/x/code/project-a")
        assert result == []

    def test_scoped_excludes_sibling_directory_name_prefix(self, orch_db_env, tmp_path):
        """Integration-level regression for the same str.startswith()
        boundary bug covered directly in TestWorkflowBelongsToProject: a
        workflow under a sibling directory whose name is a superstring of
        the target project's name must not be scoped in."""
        from src.autopilot.orchestrator.engine_client import get_active_workflows

        project_a = tmp_path / "project-a"
        project_a.mkdir()
        project_ab = tmp_path / "project-ab"
        wt = project_ab / ".worktrees" / "wt_1"
        wt.mkdir(parents=True)
        self._make_workflow(orch_db_env, "wf-ab", str(wt))

        result = get_active_workflows(project_path=str(project_a))
        assert result == []

    def test_paused_workflows_excluded_regardless_of_scope(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_active_workflows

        self._make_workflow(
            orch_db_env, "wf-a", "/Users/x/code/project-a/.worktrees/wt_1", status="paused"
        )

        assert get_active_workflows() == []
        assert get_active_workflows(project_path="/Users/x/code/project-a") == []


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
        from src.autopilot.orchestrator.engine_client import increment_task_retry_count
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
        from src.autopilot.orchestrator.engine_client import increment_task_retry_count

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
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks
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
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks
        from src.core.database import Task

        self._make_workflow_task_agent(orch_db_env, failure_reason=None)

        _clean_stale_assigned_tasks("wf-1", OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert "terminated unexpectedly" in task.failure_reason

    def test_cleans_pending_task_with_terminated_assigned_agent(self, orch_db_env, tmp_path):
        """Regression: a task can carry assigned_agent_id while status is
        still "pending" -- e.g. a dispatch loop that sets both fields in
        memory but only commits after the whole batch, or any other path
        that assigns before flipping to in_progress. phase_transitions.py's
        own _advance_phases sweep documents this exact live incident
        (a task observed "pending", pointing at an agent terminated hours
        earlier, reason "Orphaned: never dispatched to an agent") and
        already handles it for its own phase-scoped candidates. This
        workflow-wide cleanup pass claimed the same "stale task whose
        agent is terminated" job but its status filter only covered
        "assigned"/"in_progress", silently leaving a pending+orphaned task
        parked forever whenever this pass runs instead of (or before) the
        phase-scoped one."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks
        from src.core.database import Task

        self._make_workflow_task_agent(orch_db_env, task_status="pending", failure_reason=None)

        _clean_stale_assigned_tasks("wf-1", OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert "terminated unexpectedly" in task.failure_reason


class TestRetryFailedTasks:
    """Regression: the only thing that ever retried an individual failed
    task (not requiring every task in the phase to be failed) was
    attempt_recovery, called exactly once -- at pipeline startup, for
    whichever single workflow happened to be the last-tracked
    current_workflow_id. A failed task in any other active workflow (a
    parallel feature run, or one resumed outside that startup check) just
    sat "failed" forever with nothing to retry it, even while the system
    was fully unpaused. Extracted so the background sweep can call this
    piece alone on every tick for every active workflow, without also
    running attempt_recovery's other, destructive actions (git reset
    --hard on any dirty repo, killing every currently-working agent)."""

    def _make_workflow_and_failed_task(
        self, db, retry_count=0, phase_id=None, task_id="task-1"
    ):
        from src.core.database import Task, Workflow

        with db.session_scope() as session:
            if not session.query(Workflow).filter_by(id="wf-1").first():
                session.add(
                    Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
                )
            session.add(
                Task(
                    id=task_id,
                    workflow_id="wf-1",
                    phase_id=phase_id,
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="boom",
                    retry_count=retry_count,
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_retries_failed_task_and_dispatches_agent(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Regression: create_agent_for_task_direct does NOT update the
        task row itself (same contract _create_phase_task's callers rely
        on) -- a version of this that only reset status to "pending" and
        never wrote back assigned_agent_id/status="in_progress" left a
        successfully retried task "pending", pointing at the OLD dead
        agent from the failed attempt, completely disconnected from the
        real, live agent actually now working on it. Observed live: two
        fresh agents got created and started working while the task
        itself sat "pending" forever, invisible to every other self-heal
        check (all of which key off the task's own status/assigned_agent_id,
        not the agent's)."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_failed_tasks
        from src.core.database import Agent, Task

        self._make_workflow_and_failed_task(orch_db_env)
        with orch_db_env.session_scope() as session:
            # create_agent_for_task_direct is mocked below -- in production
            # it's the one that actually creates this Agent row as a side
            # effect, so seed it here to match (the FK on
            # Task.assigned_agent_id requires the row to exist).
            session.add(Agent(id="new-agent", system_prompt="p", status="working", cli_type="pi"))
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert recovered == ["retried task task-1"]
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "in_progress"
            assert task.assigned_agent_id == "new-agent"
            assert task.retry_count == 1

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_skips_task_at_retry_cap(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_failed_tasks
        from src.core.database import Task

        self._make_workflow_and_failed_task(orch_db_env, retry_count=5)

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert recovered == []
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert task.retry_count == 5

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_orphaned_task_incorrectly_capped_bug(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Fix verification for the orphan-retry-cap bug: orphaned tasks
        (never dispatched, failure_reason containing 'Orphaned') now retry
        indefinitely, exempt from max_task_retries -- matching
        _maybe_retry_failed_tasks's identical exemption.

        Previously broken because get_tasks() never included
        failure_reason in its returned dict, so is_orphan was always False.
        Fixed by adding failure_reason to get_tasks()."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_failed_tasks
        from src.core.database import Agent, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Task(
                    id="task-orphan-past-cap",
                    workflow_id="wf-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Orphaned: never dispatched to an agent",
                    retry_count=10,
                )
            )
            session.add(Agent(id="new-agent", system_prompt="p", status="working", cli_type="pi"))
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        # Fixed: orphaned tasks retry indefinitely, exempt from max_task_retries.
        orphan_id = "task-orphan-past-cap"
        assert recovered == [f"retried task {orphan_id[:8]}"]
        mock_create_agent.assert_called_once()
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-orphan-past-cap").first()
            assert task.status == "in_progress"
            # Orphans don't increment retry_count since they aren't real failures.
            assert task.retry_count == 10

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct", return_value=None)
    def test_agent_dispatch_failure_lands_back_on_failed_not_stuck_pending(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Same dead-end this exact fix closed for _maybe_retry_failed_tasks:
        leaving the task "pending" on a dispatch failure would strand it --
        nothing dispatches an agent for an already-existing pending task
        with no agent."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_failed_tasks
        from src.core.database import Task

        self._make_workflow_and_failed_task(orch_db_env)

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert recovered == []
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert task.retry_count == 1

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct", return_value=None)
    def test_superseded_by_active_sibling_is_skipped_not_failed(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Regression: create_agent_for_task_direct returns None for two
        different reasons -- a genuine creation failure, or its own
        deliberate "another active task already owns this phase"
        duplicate-dispatch guard (avoiding two agents on the same phase).
        Treating both as a retry failure marked a merely-superseded task
        "failed" with a misleading "agent creation failed" reason, and
        burned its retry budget for a decision that was actually correct.
        Observed live: a task reset to "pending" by a manual recovery
        collided with a fresh task the pipeline had already, legitimately
        created for the same phase in the meantime -- 5 retries later it
        was permanently "failed" with a reason unrelated to what actually
        happened."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_failed_tasks
        from src.core.database import Task

        self._make_workflow_and_failed_task(orch_db_env, phase_id="phase-1")
        with orch_db_env.session_scope() as session:
            # A fresh task the pipeline already dispatched for the SAME
            # phase, independent of the stale failed one being retried.
            session.add(Task(
                id="task-sibling", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="in_progress",
            ))

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert recovered == []
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "duplicated", "superseded task must be duplicated, not failed"
            assert task.duplicate_of_task_id == "task-sibling"
            assert "task-sib" in task.failure_reason
            sibling = session.query(Task).filter_by(id="task-sibling").first()
            assert sibling.status == "in_progress", "the active sibling must be untouched"

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_phase_less_task_is_not_falsely_superseded_by_an_unrelated_one(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Regression: Task.phase_id == phase_id compiles to "IS NULL" in
        SQLAlchemy when phase_id is None, not a no-match -- an unguarded
        sibling query treated EVERY phase-less task in the database (ad-hoc
        ones an agent created directly via create_task, e.g. an
        adversarial re-review request) as a "sibling" of every other one,
        regardless of workflow or how long apart they were created.
        Observed live: a phase-less adversarial re-review task got marked
        "duplicated" of an unrelated debris task from five weeks earlier
        that also happened to have no phase_id -- the only thing they had
        in common."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_failed_tasks
        from src.core.database import Agent, Task

        self._make_workflow_and_failed_task(orch_db_env, phase_id=None)
        with orch_db_env.session_scope() as session:
            # An unrelated, also phase-less task -- must NOT be treated as
            # this one's sibling just because both have phase_id=None.
            session.add(Task(
                id="unrelated-null-phase-task", workflow_id="wf-1", phase_id=None,
                raw_description="r", done_definition="d", status="assigned",
            ))
            session.add(Agent(id="new-agent", system_prompt="p", status="working", cli_type="pi"))
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert recovered == ["retried task task-1"]
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "in_progress"
            assert task.assigned_agent_id == "new-agent"
            unrelated = session.query(Task).filter_by(id="unrelated-null-phase-task").first()
            assert unrelated.status == "assigned", "the unrelated task must be untouched"


class TestMaybeRetryFailedTasksPreservesGotoTarget:
    """Regression: a task's action/action_target_phase, when it's still
    "failed" (never reached "done"), can only have come from
    _create_phase_task's creation-time tagging -- "I exist because an
    earlier phase goto'd/retried back to me, resume AT that target once
    I'm done" (see its action_target_phase= assignment). _tag_completing_
    task, the only other writer of these fields, tags a task only AFTER it
    completes and gets evaluated, which a failed task never reached.
    _maybe_retry_failed_tasks previously cleared both fields unconditionally
    on every retry, believing it was clearing a stale post-completion badge
    that in this code path never existed -- silently discarding the real
    resume target. Observed live: a development task that goto'd back from
    qa_validation got stuck (CLI session limit) and retried here, losing
    action_target_phase="qa_validation" entirely, so its eventual
    completion fell back to next-phase-by-order and re-ran the entire
    architectural_review -> adversarial_review -> security_review chain
    from scratch even though none of it had been invalidated."""

    def _seed(self, db, action="goto", action_target_phase="qa_validation"):
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Phase(
                    id="phase-dev",
                    workflow_id="wf-1",
                    name="development",
                    order=4,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-dev",
                    phase_id="phase-dev",
                    workflow_execution_id="wf-1",
                    status="in_progress",
                )
            )
            session.add(
                Task(
                    id="task-dev",
                    workflow_id="wf-1",
                    phase_id="phase-dev",
                    raw_description="Fix per qa_validation findings",
                    done_definition="d",
                    status="failed",
                    failure_reason="CLI session limit hit",
                    retry_count=0,
                    action=action,
                    action_target_phase=action_target_phase,
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_retry_preserves_action_target_phase(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks
        from src.core.database import Agent, Phase, Task

        self._seed(orch_db_env)
        with orch_db_env.session_scope() as session:
            session.add(Agent(id="new-agent", system_prompt="p", status="working", cli_type="pi"))
            phase = session.query(Phase).filter_by(id="phase-dev").first()
            phase_id, phase_name = phase.id, phase.name
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        with orch_db_env.session_scope() as session:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            result = _maybe_retry_failed_tasks(session, phase, OrchestratorLogger(tmp_path))

        assert result is True
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-dev").first()
            assert task.status == "in_progress"
            assert task.action == "goto"
            assert task.action_target_phase == "qa_validation"

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_retry_preserves_absence_of_action_target_phase(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """A task that was never a goto/retry target (plain fresh attempt)
        stays that way across a retry -- nothing to preserve, nothing
        fabricated either."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks
        from src.core.database import Agent, Phase, Task

        self._seed(orch_db_env, action="", action_target_phase=None)
        with orch_db_env.session_scope() as session:
            session.add(Agent(id="new-agent", system_prompt="p", status="working", cli_type="pi"))
            phase = session.query(Phase).filter_by(id="phase-dev").first()
            phase_id = phase.id
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        with orch_db_env.session_scope() as session:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            _maybe_retry_failed_tasks(session, phase, OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-dev").first()
            assert task.action == ""
            assert task.action_target_phase is None


class TestFailWorkflowDirect:
    """Regression: the backend-startup stale-workflow cleanup used
    complete_workflow_direct unconditionally for any workflow still "active"
    after a restart -- even one abandoned mid-run with most phases
    unfinished, mislabeling it "completed" and corrupting downstream status
    derivation. fail_workflow_direct gives that path a way to mark it
    accurately instead."""

    def test_marks_failed(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import fail_workflow_direct
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
        from src.autopilot.orchestrator.engine_client import fail_workflow_direct

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
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks

        result = _resume_stuck_workflow_tasks("does-not-exist", OrchestratorLogger(tmp_path))
        assert result == 0

    def test_unpauses_workflow(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
        from src.core.database import Workflow

        self._make_workflow(orch_db_env, "wf-1", "paused")

        _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_skips_user_paused_workflow(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: same class of bug _try_auto_resume_paused_workflow
        was fixed for -- this fires whenever the design/feature queue loop
        cycles back to a workflow it already has an id for, which can
        include one the user deliberately paused. Must not silently
        un-pause and restart work on it."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
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

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_restarts_failed_and_blocked_tasks(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
        from src.core.database import Task

        # create_agent_for_task_direct is mocked -- in production it's the
        # one that actually creates this Agent row as a side effect, so
        # seed it here to match (Task.assigned_agent_id's FK requires it).
        self._make_agent(orch_db_env, "new-agent", "working")
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

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_restarts_task_whose_agent_was_terminated(self, mock_create_agent, orch_db_env, tmp_path):
        """A task can be stuck 'in_progress' pointing at an agent that was
        already terminated (e.g. the service stop killed it) -- resume must
        detect this and restart it, not just plain 'failed'/'blocked'."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks

        self._make_agent(orch_db_env, "new-agent", "working")
        mock_create_agent.return_value = {"agent_id": "new-agent"}
        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_agent(orch_db_env, "dead-agent", "terminated")
        self._make_task(orch_db_env, "task-1", "wf-1", "in_progress", agent_id="dead-agent")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 1
        mock_create_agent.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_does_not_restart_task_with_live_agent(self, mock_create_agent, orch_db_env, tmp_path):
        """A task genuinely still being worked by a live agent must be left
        alone -- resume is for stuck work, not active work."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
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

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_clears_stale_goto_action_on_restart(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: this row is reused (not recreated) for the restart --
        a task previously tagged action='goto' by _tag_completing_task (from
        an earlier life, before ending up 'failed'/'blocked' here) kept
        showing that stale badge, with a now-meaningless action_target_phase,
        on what the UI displays as a brand new attempt. Observed live: a
        restarted task still showed "goto" with no (or a wrong) target."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
        from src.core.database import Task

        self._make_agent(orch_db_env, "new-agent", "working")
        mock_create_agent.return_value = {"agent_id": "new-agent"}
        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_task(orch_db_env, "task-1", "wf-1", "failed")
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            task.action = "goto"
            task.action_target_phase = "development"

        _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.action == ""
            assert task.action_target_phase is None

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_restarts_pending_task_with_dead_agent(self, mock_create_agent, orch_db_env, tmp_path):
        """A task can end up 'pending' with a stale assigned_agent_id (e.g.
        an agent manually terminated after the task was dispatched but
        before it was reset) -- same 'genuinely stuck' check as
        assigned/in_progress must apply, or the task is unrecoverable."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks

        self._make_agent(orch_db_env, "new-agent", "working")
        mock_create_agent.return_value = {"agent_id": "new-agent"}
        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_agent(orch_db_env, "dead-agent", "terminated")
        self._make_task(orch_db_env, "task-1", "wf-1", "pending", agent_id="dead-agent")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 1
        mock_create_agent.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_restarts_stale_pending_task_with_no_agent(self, mock_create_agent, orch_db_env, tmp_path):
        """A 'pending' task with no agent at all and no automatic pickup
        anywhere else in the codebase (see PENDING_STUCK_MINUTES comment)
        must still be recoverable once it's clearly been abandoned, not
        just mid-dispatch."""
        from datetime import datetime, timedelta

        from src.autopilot.orchestrator import OrchestratorLogger

        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
        from src.core.database import Task

        self._make_agent(orch_db_env, "new-agent", "working")
        mock_create_agent.return_value = {"agent_id": "new-agent"}
        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_task(orch_db_env, "task-1", "wf-1", "pending")
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            task.created_at = datetime.utcnow() - timedelta(minutes=10)

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 1
        mock_create_agent.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_does_not_restart_freshly_created_pending_task(self, mock_create_agent, orch_db_env, tmp_path):
        """A 'pending' task with no agent yet that was JUST created is
        normal -- creation and first dispatch happen in the same
        synchronous call elsewhere. Sweeping it up here would race that
        dispatch instead of waiting for it."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks

        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_task(orch_db_env, "task-1", "wf-1", "pending")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 0
        mock_create_agent.assert_not_called()


class TestCreateAgentForTaskDirectAppStateGuard:
    """Regression: get_app_state() was called BEFORE create_agent_for_task_
    direct's own try/except, so when app state isn't initialized it raised
    RuntimeError straight out of the function instead of the documented
    "returns None on failure" contract. Every caller (self-heal task
    creation, resume, and the new corrective-negotiation retries) relies on
    that contract to fail gracefully rather than crash the whole pipeline
    run. Hit live while testing manually outside the running server."""

    def test_app_state_not_initialized_returns_none_not_raises(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct
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


class TestCreateAgentForTaskDirectPhaseSiblingGuard:
    """The phase-sibling guard blocks dispatch when the phase already has
    another active SYSTEM-created task (the _retry_failed_tasks vs.
    _advance_phases race it exists for), but must not block a legitimate
    agent-created subtask sharing the same phase_id -- create_task's own
    documented contract has phase agents pass their OWN phase_id to spawn
    subtasks within their phase."""

    def _server_state(self, db):
        server_state = Mock()
        server_state.db_manager = db
        server_state.agent_manager.create_agent_for_task = AsyncMock(
            return_value=Mock(id="new-agent")
        )
        server_state.queue_service = None
        return server_state

    def _seed_phase_and_task(self, db):
        from src.core.database import Phase, Task, Workflow

        with db.session_scope() as session:
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp"))
            session.add(
                Phase(
                    id="phase-1", workflow_id="wf-1", order=1, name="development",
                    description="d", done_definitions=[],
                )
            )
            session.add(
                Task(
                    id="task-1", raw_description="r", done_definition="d",
                    status="pending", phase_id="phase-1", workflow_id="wf-1",
                )
            )

    def test_blocks_when_sibling_is_system_created(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct
        from src.core.database import Task

        self._seed_phase_and_task(orch_db_env)
        with orch_db_env.session_scope() as session:
            session.add(
                Task(
                    id="task-sibling-system", raw_description="r", done_definition="d",
                    status="in_progress", phase_id="phase-1", workflow_id="wf-1",
                    created_by_agent_id=None,
                )
            )

        server_state = self._server_state(orch_db_env)
        with patch("src.core.app_context.get_app_state", return_value=server_state):
            result = create_agent_for_task_direct("task-1", "wf-1", "phase-1")

        assert result is None
        server_state.agent_manager.create_agent_for_task.assert_not_called()

    def test_does_not_block_when_sibling_is_agent_created_subtask(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct
        from src.core.database import Agent, Task

        self._seed_phase_and_task(orch_db_env)
        with orch_db_env.session_scope() as session:
            session.add(
                Agent(id="creator-agent", system_prompt="p", status="working", cli_type="pi")
            )
            session.add(
                Task(
                    id="task-sibling-subtask", raw_description="r", done_definition="d",
                    status="in_progress", phase_id="phase-1", workflow_id="wf-1",
                    created_by_agent_id="creator-agent",
                )
            )

        server_state = self._server_state(orch_db_env)
        with patch("src.core.app_context.get_app_state", return_value=server_state):
            result = create_agent_for_task_direct("task-1", "wf-1", "phase-1")

        assert result == {"agent_id": "new-agent", "status": "created"}
        server_state.agent_manager.create_agent_for_task.assert_called_once()


class TestCreateAgentForTaskDirectCliModelConcurrencyLimit:
    """Regression: create_agent_for_task_direct is the orchestrator's OWN
    direct dispatch path for phase transitions -- it's what actually
    creates most phase tasks (scope_review, development, etc.) in a live
    run, entirely bypassing QueueService.get_next_queued_task's per-cli/
    model concurrency check. A local model's single inference slot could
    still get double-booked through this path even with that check in
    place on the queue-mediated path."""

    def _seed(self, db, cli_tool="pi", cli_model="qwen-local", saturate=True):
        from src.core.database import Agent, Phase, Task, Workflow

        with db.session_scope() as session:
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp"))
            session.add(
                Phase(
                    id="phase-1", workflow_id="wf-1", order=1, name="development",
                    description="d", done_definitions=[], cli_tool=cli_tool, cli_model=cli_model,
                )
            )
            session.add(
                Task(
                    id="task-1", raw_description="r", done_definition="d",
                    status="pending", phase_id="phase-1", workflow_id="wf-1",
                )
            )
            if saturate:
                session.add(
                    Agent(id="busy-agent", system_prompt="p", status="working", cli_type=cli_tool, cli_model=cli_model)
                )

    def _server_state(self, db, **queue_service_kwargs):
        from src.services.queue_service import QueueService

        server_state = Mock()
        server_state.db_manager = db
        server_state.agent_manager.create_agent_for_task = AsyncMock(
            return_value=Mock(id="new-agent")
        )
        server_state.queue_service = QueueService(db, max_concurrent_agents=10, **queue_service_kwargs)
        return server_state

    def test_dispatches_on_fallback_when_primary_combo_saturated(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct

        self._seed(orch_db_env, saturate=True)
        server_state = self._server_state(
            orch_db_env,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            cli_model_fallback="mimo-v2.5-pro",
        )

        with patch("src.core.app_context.get_app_state", return_value=server_state):
            result = create_agent_for_task_direct("task-1", "wf-1", "phase-1")

        assert result == {"agent_id": "new-agent", "status": "created"}
        _, kwargs = server_state.agent_manager.create_agent_for_task.call_args
        assert kwargs["phase_cli_tool"] == "pi"
        assert kwargs["phase_cli_model"] == "mimo-v2.5-pro"

    def test_dispatches_on_primary_when_combo_has_a_free_slot(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct

        self._seed(orch_db_env, saturate=False)
        server_state = self._server_state(
            orch_db_env,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            cli_model_fallback="mimo-v2.5-pro",
        )

        with patch("src.core.app_context.get_app_state", return_value=server_state):
            create_agent_for_task_direct("task-1", "wf-1", "phase-1")

        _, kwargs = server_state.agent_manager.create_agent_for_task.call_args
        assert kwargs["phase_cli_tool"] is None
        assert kwargs["phase_cli_model"] is None

    def test_no_limits_configured_is_a_noop(self, orch_db_env):
        """No queue_service.cli_model_concurrency_limits configured -- must
        behave exactly as before, no per-cli/model lookups at all."""
        from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct

        self._seed(orch_db_env, saturate=True)
        server_state = self._server_state(orch_db_env)

        with patch("src.core.app_context.get_app_state", return_value=server_state):
            create_agent_for_task_direct("task-1", "wf-1", "phase-1")

        _, kwargs = server_state.agent_manager.create_agent_for_task.call_args
        assert kwargs["phase_cli_tool"] is None
        assert kwargs["phase_cli_model"] is None

    def test_caller_supplied_override_is_respected_not_discarded(self, orch_db_env):
        """Regression: create_agent_for_task_direct also accepts
        phase_cli_tool_override/phase_cli_model_override as explicit
        parameters (used by the session-limit escalation retry at this
        function's other call site). This concurrency gate's own working
        variables used to share those exact names, and unconditionally
        reset them to None near the top of the function -- silently
        discarding whatever the caller passed in before the "only run if
        no override was passed" check ever saw it, breaking session-limit
        escalation's fallback dispatch outright. A caller-supplied override
        must win even when the primary combo has a free slot (no
        concurrency-driven override would fire on its own)."""
        from src.autopilot.orchestrator.engine_client import create_agent_for_task_direct

        self._seed(orch_db_env, saturate=False)
        server_state = self._server_state(orch_db_env)  # no concurrency limits configured

        with patch("src.core.app_context.get_app_state", return_value=server_state):
            create_agent_for_task_direct(
                "task-1", "wf-1", "phase-1",
                phase_cli_tool_override="claude",
                phase_cli_model_override="sonnet",
            )

        _, kwargs = server_state.agent_manager.create_agent_for_task.call_args
        assert kwargs["phase_cli_tool"] == "claude"
        assert kwargs["phase_cli_model"] == "sonnet"


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

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_creates_task_with_feedback_and_reopens_phase(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_corrective_task
        from src.core.database import Agent, PhaseExecution, Task, Workflow

        self._seed_workflow_and_phase(orch_db_env)
        with orch_db_env.session_scope() as session:
            # create_agent_for_task_direct is mocked -- in production it's
            # the one that actually creates this Agent row as a side
            # effect (Task.assigned_agent_id's FK requires it exist).
            session.add(Agent(id="new-agent", system_prompt="p", status="working", cli_type="pi"))
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

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_reopening_resets_stale_task_creation_claim(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: like _create_phase_task, this reopens a phase the
        engine already marked complete -- but until fixed it never reset
        task_creation_claimed_at. A phase visited earlier in the pipeline
        carries a claim already consumed by that prior cycle; leaving it
        set means _case_in_progress_complete's claim guard would see the
        stale value once the corrective task finishes and skip evaluating
        the transition forever, even though the work is done."""
        from datetime import datetime

        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import (
    _claim_phase_task_creation,
    _create_corrective_task,
)
        from src.core.database import Agent, PhaseExecution

        self._seed_workflow_and_phase(orch_db_env)
        with orch_db_env.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.task_creation_claimed_at = datetime(2020, 1, 1)
            session.add(Agent(id="new-agent", system_prompt="p", status="working", cli_type="pi"))
        mock_create_agent.return_value = {"agent_id": "new-agent"}

        _create_corrective_task(
            "wf-1", "phase-1", "Feature Architect", "got 6, expected 1-5",
            OrchestratorLogger(tmp_path),
        )

        with orch_db_env.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            assert _claim_phase_task_creation(session, "phase-1") is True

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_refuses_when_target_phase_is_freshly_claimed_by_another_caller(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Regression: _negotiate_validation_fix's corrective-task path had
        no claim protection at all -- while it's running (routinely
        against phase 0/1 of a feature_architect workflow), the background
        self-heal sweep could independently decide the same phase needs a
        task and create a sibling. Mirrors _create_phase_task's own
        target_already_claimed=False protection."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import (
    _claim_phase_task_creation,
    _create_corrective_task,
)
        from src.core.database import PhaseExecution, Task

        self._seed_workflow_and_phase(orch_db_env)
        with orch_db_env.session_scope() as session:
            # A genuinely live, concurrent claim on this exact phase.
            assert _claim_phase_task_creation(session, "phase-1") is True

        result = _create_corrective_task(
            "wf-1", "phase-1", "Feature Architect", "got 6, expected 1-5",
            OrchestratorLogger(tmp_path),
        )

        assert result is None
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            # The fresh claim is still held -- untouched by our refused attempt.
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None
            assert session.query(Task).filter_by(phase_id="phase-1").count() == 0

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_missing_workflow_returns_none(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_corrective_task

        result = _create_corrective_task(
            "does-not-exist", "phase-1", "Feature Architect", "bad output",
            OrchestratorLogger(tmp_path),
        )

        assert result is None
        mock_create_agent.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_skips_user_paused_workflow(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: same class of bug _try_auto_resume_paused_workflow
        was fixed for, but worse here -- unguarded, this would both
        reactivate the workflow AND immediately spawn a live agent against
        it, silently resuming real work on something the user explicitly
        paused."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_corrective_task
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

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct", return_value=None)
    def test_agent_creation_failure_marks_task_failed(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_corrective_task
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
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _wait_for_task_terminal
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="t-1", raw_description="r", done_definition="d", status="done")
            )

        result = _wait_for_task_terminal("t-1", timeout_seconds=5, logger=OrchestratorLogger(tmp_path))
        assert result == "done"

    def test_returns_failed_status(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _wait_for_task_terminal
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="t-1", raw_description="r", done_definition="d", status="failed")
            )

        result = _wait_for_task_terminal("t-1", timeout_seconds=5, logger=OrchestratorLogger(tmp_path))
        assert result == "failed"

    def test_times_out_on_non_terminal_status(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _wait_for_task_terminal
        from src.core.database import Task

        with orch_db_env.session_scope() as session:
            session.add(
                Task(id="t-1", raw_description="r", done_definition="d", status="in_progress")
            )

        with patch("src.autopilot.orchestrator.phase_transitions.POLL_INTERVAL", 0.01):
            result = _wait_for_task_terminal("t-1", timeout_seconds=0.05, logger=OrchestratorLogger(tmp_path))
        assert result == "timeout"

    def test_stop_requested_returns_interrupted(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _wait_for_task_terminal
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
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _negotiate_validation_fix

        output_path = tmp_path / "features.json"

        def fake_create_task(*a, **k):
            # Simulate the agent fixing the file before task completes.
            output_path.write_text('{"features": [1, 2]}')
            return "task-1"

        def validate_fn(parsed):
            if len(parsed["features"]) > 5:
                raise ValueError("too many")

        with patch(
            "src.autopilot.orchestrator.phase_transitions._create_corrective_task", side_effect=fake_create_task
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._wait_for_task_terminal", return_value="done"
        ):
            success, result = _negotiate_validation_fix(
                "wf-1", "phase-1", "Feature Architect", output_path, validate_fn,
                "got 6, expected 1-5", OrchestratorLogger(tmp_path),
            )

        assert success is True
        assert result == {"features": [1, 2]}

    def test_gives_up_after_max_attempts_still_invalid(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _negotiate_validation_fix

        output_path = tmp_path / "features.json"
        output_path.write_text('{"features": [1, 2, 3, 4, 5, 6]}')  # never fixed

        def validate_fn(parsed):
            if len(parsed["features"]) > 5:
                raise ValueError("too many")

        with patch(
            "src.autopilot.orchestrator.phase_transitions._create_corrective_task", return_value="task-1"
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._wait_for_task_terminal", return_value="done"
        ) as mock_wait:
            success, result = _negotiate_validation_fix(
                "wf-1", "phase-1", "Feature Architect", output_path, validate_fn,
                "too many", OrchestratorLogger(tmp_path), max_attempts=2,
            )

        assert success is False
        assert result is None
        assert mock_wait.call_count == 2  # exhausted both attempts

    def test_gives_up_immediately_if_task_creation_fails(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _negotiate_validation_fix

        output_path = tmp_path / "features.json"

        with patch(
            "src.autopilot.orchestrator.phase_transitions._create_corrective_task", return_value=None
        ):
            success, result = _negotiate_validation_fix(
                "wf-1", "phase-1", "Feature Architect", output_path, lambda x: None,
                "bad output", OrchestratorLogger(tmp_path),
            )

        assert success is False
        assert result is None

    def test_gives_up_if_corrective_task_fails(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _negotiate_validation_fix

        output_path = tmp_path / "features.json"

        with patch(
            "src.autopilot.orchestrator.phase_transitions._create_corrective_task", return_value="task-1"
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._wait_for_task_terminal", return_value="failed"
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
        from src.core.database import Task, Workflow

        with db.session_scope() as session:
            if workflow_id and not session.query(Workflow).filter_by(id=workflow_id).first():
                session.add(
                    Workflow(id=workflow_id, name="t", phases_folder_path="/tmp", status="active")
                )
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
        from src.autopilot.orchestrator.engine_client import get_agents

        self._make_agent(orch_db_env, "a1")
        result = get_agents()
        assert len(result) == 1

    def test_filters_by_workflow(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_agents

        self._make_agent(orch_db_env, "a1")
        self._make_agent(orch_db_env, "a2")
        self._make_task(orch_db_env, "t1", "wf-1", "a1")

        result = get_agents(workflow_id="wf-1")
        assert len(result) == 1
        assert result[0]["id"] == "a1"

    def test_returns_empty_on_none(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import get_agents

        result = get_agents()
        assert result == []


class TestPeekAgentOutput:
    def test_returns_output(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import peek_agent_output
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
        from src.autopilot.orchestrator.engine_client import peek_agent_output
        result = peek_agent_output("a1")
        assert result == ""


class TestGetTaskProgress:
    @patch("src.autopilot.orchestrator.engine_client.get_tasks")
    def test_counts(self, mock_tasks):
        from src.autopilot.orchestrator.engine_client import get_task_progress

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


class TestCheckApiCredits:
    @patch("src.autopilot.orchestrator.policy.get_tasks")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    def test_no_credits_issue(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator.policy import check_api_credits

        mock_agents.return_value = [{"status": "working", "error": ""}]
        mock_tasks.return_value = []
        found, msg = check_api_credits()
        assert found is False

    @patch("src.autopilot.orchestrator.policy.get_tasks")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    def test_agent_credit_error(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator.policy import check_api_credits

        mock_agents.return_value = [
            {"id": "a1", "status": "error", "error": "insufficient credits"}
        ]
        mock_tasks.return_value = []
        found, msg = check_api_credits()
        assert found is True
        assert "credit" in msg.lower()

    @patch("src.autopilot.orchestrator.policy.get_tasks")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    def test_task_credit_error(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator.policy import check_api_credits

        mock_agents.return_value = []
        mock_tasks.return_value = [{"id": "t1", "error": "rate limit exceeded"}]
        found, msg = check_api_credits()
        assert found is True

    @patch("src.autopilot.orchestrator.policy.get_tasks")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    def test_agent_output_log_credit(self, mock_agents, mock_tasks):
        from src.autopilot.orchestrator.policy import check_api_credits

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
    @patch("src.autopilot.orchestrator.queue.get_agents")
    @patch("src.autopilot.orchestrator.queue.get_tasks")
    @patch("src.autopilot.orchestrator.queue.get_workflow_status")
    def test_incomplete_workflow_status(self, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import is_design_fully_complete

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "unknown"}
        mock_tasks.return_value = []
        mock_agents.return_value = []
        result, msg = is_design_fully_complete("wf-1", logger)
        assert result is False
        assert "Workflow status" in msg

    @patch("src.autopilot.orchestrator.queue.get_agents")
    @patch("src.autopilot.orchestrator.queue.get_tasks")
    @patch("src.autopilot.orchestrator.queue.get_workflow_status")
    @patch("src.core.status_derivation.derive_workflow_status")
    def test_incomplete_has_active_tasks(self, mock_derive, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import is_design_fully_complete

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "active"}
        mock_derive.return_value = "active"
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

    @patch("src.autopilot.orchestrator.queue.get_agents")
    @patch("src.autopilot.orchestrator.queue.get_tasks")
    @patch("src.autopilot.orchestrator.queue.get_workflow_status")
    @patch("src.core.status_derivation.derive_workflow_status")
    def test_incomplete_has_failed_tasks(self, mock_derive, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import is_design_fully_complete

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "active"}
        mock_derive.return_value = "active"
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

    @patch("src.autopilot.orchestrator.queue.get_agents")
    @patch("src.autopilot.orchestrator.queue.get_tasks")
    @patch("src.autopilot.orchestrator.queue.get_workflow_status")
    @patch("src.core.status_derivation.derive_workflow_status")
    def test_incomplete_has_active_agents(self, mock_derive, mock_wf, mock_tasks, mock_agents):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import is_design_fully_complete

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_wf.return_value = {"status": "active"}
        mock_derive.return_value = "active"
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
    @patch("src.core.database.get_db")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    @patch("src.autopilot.orchestrator.engine_client.api_post")
    @patch("src.autopilot.orchestrator.policy.get_tasks")
    def test_no_recovery_needed(self, mock_tasks, mock_agents, mock_post, mock_get_db):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import attempt_recovery

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

    @patch("src.autopilot.orchestrator.phase_transitions.get_db")
    @patch("src.core.database.get_db")
    @patch("src.autopilot.orchestrator.phase_transitions.update_task_status")
    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    @patch("src.autopilot.orchestrator.phase_transitions.get_tasks")
    def test_retries_failed_tasks(
        self, mock_tasks, mock_agents, mock_create_agent, mock_update_status,
        mock_get_db, mock_pt_get_db
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import attempt_recovery

        # Mock get_db to avoid database queries
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        # _retry_failed_tasks's own sibling pre-check queries .first() on
        # this same chain before ever reaching create_agent_for_task_direct
        # -- an unstubbed MagicMock() there is truthy, so it reads as "a
        # sibling task already owns this phase" and skips the retry this
        # test exists to verify. Explicitly empty, matching mock_tasks'
        # single-task/no-sibling setup below.
        mock_db.query.return_value.filter.return_value.first.return_value = None
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_get_db.return_value = mock_db
        mock_pt_get_db.return_value = mock_db

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

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    def test_retry_count_persists_and_eventually_stops(
        self, mock_agents, mock_create_agent, orch_db_env
    ):
        """Regression (real DB, no get_db mocking): a task whose retry
        always fails (e.g. its worktree was deleted out from under it) must
        stop retrying after 2 attempts, not loop forever. Before this fix,
        retry_count was never persisted, so this ran every ~60s indefinitely
        in production."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import attempt_recovery
        from src.core.database import Phase, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Phase(
                    id="p1",
                    workflow_id="wf-1",
                    order=1,
                    name="phase-1",
                    description="d",
                    done_definitions=["done"],
                )
            )
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
        attempt_recovery("wf-1", logger)
        attempt_recovery("wf-1", logger)
        attempt_recovery("wf-1", logger)
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="t1").first()
            assert task.retry_count == 5

        # Sixth call must skip retrying entirely -- retry_count already at cap
        calls_before = mock_create_agent.call_count
        attempt_recovery("wf-1", logger)
        assert mock_create_agent.call_count == calls_before
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="t1").first()
            assert task.retry_count == 5  # unchanged -- never even attempted

    @patch("src.core.database.get_db")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    @patch("src.autopilot.orchestrator.engine_client.api_post")
    @patch("src.autopilot.orchestrator.policy.get_tasks")
    def test_skips_max_retries(self, mock_tasks, mock_agents, mock_post, mock_get_db):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import attempt_recovery

        # Mock get_db to avoid database queries
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.all.return_value = []
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_get_db.return_value = mock_db

        logger = OrchestratorLogger(Path("/tmp/logs"))
        mock_tasks.side_effect = [
            [{"id": "t1", "retry_count": 5, "phase_id": "p1"}],  # already retried 5x
        ]
        mock_agents.return_value = []
        success, msg = attempt_recovery("wf-1", logger)
        assert success is False

    @patch("src.autopilot.orchestrator.policy.terminate_agent_direct")
    @patch("src.core.database.get_db")
    @patch("src.core.database.get_db")
    @patch("src.autopilot.orchestrator.engine_client.api_post")
    @patch("src.autopilot.orchestrator.policy.get_agents")
    @patch("src.autopilot.orchestrator.policy.get_tasks")
    def test_terminates_stale_agents(
        self, mock_tasks, mock_agents, mock_post, mock_get_db,
        mock_core_get_db, mock_terminate, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import attempt_recovery

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

    @patch("src.autopilot.orchestrator.phase_transitions.get_tasks")
    def test_cleans_pending_task_with_terminated_assigned_agent(
        self, mock_tasks, orch_db_env, tmp_path, monkeypatch
    ):
        """Regression, same gap as _clean_stale_assigned_tasks
        (TestCleanStaleAssignedTasks above): step 1b's own status filter
        only covered "assigned"/"in_progress", missing "pending" -- even
        though a task carrying assigned_agent_id while still "pending" is
        a proven-live state in this codebase (see
        _advance_phases's own handling of the identical scenario in
        phase_transitions.py)."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import attempt_recovery
        from src.core.database import Agent, Task, Workflow

        monkeypatch.delenv("PROJECT_PATH", raising=False)
        mock_tasks.return_value = []  # step 1: nothing to retry

        with orch_db_env.session_scope() as session:
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
                    status="pending",
                    assigned_agent_id="agent-1",
                )
            )

        logger = OrchestratorLogger(tmp_path)
        success, msg = attempt_recovery("wf-1", logger)

        assert success is True
        assert "cleaned stale task" in msg
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert "terminated unexpectedly" in task.failure_reason


class TestGetLitellmConfig:
    def test_reads_env(self):
        import os

        from src.autopilot.orchestrator.engine_client import get_litellm_config

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
        from src.autopilot.orchestrator.engine_client import get_litellm_config

        config = get_litellm_config()
        assert config["url"] == ""
        assert config["api_key"] == ""
        assert config["cost_tracking"] is False


class TestRunOneFeatureStateIsolation:
    """Regression: _run_one_feature used to link a just-finished feature's
    workflow by reading thread_state.current_workflow_id after
    run_single_workflow returned -- but run_single_workflow's own success
    path clears that field back to None right before returning "completed"
    (see its final success branch), making that read a permanent no-op on
    every completed feature. Observed live: a feature ("Budget Enforcement
    and Pipeline Throttling") whose 12-phase workflow had genuinely
    completed still showed "active" in the UI indefinitely, because
    Feature.workflow_id was never set and derive_feature_status has no way
    to find the (unlinked) workflow. Fixed by resolving the link via a DB
    lookup (_relink_features_to_workflows, matching by feature_key in
    launch_params) instead of the field run_single_workflow clears."""

    def _make_design_entry(self, tmp_path, design_id):
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        return DesignEntry(
            path=design_path,
            name="Test Design",
            content_hash="hash",
            db_id=design_id,
        )

    def test_links_via_db_lookup_even_though_state_is_cleared_on_success(
        self, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            PipelineState,
            _run_one_feature,
        )
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        design_id = "design-1"
        feature_key = "feat-a"
        with orch_db_env.session_scope() as session:
            session.add(
                AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1")
            )
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key=feature_key,
                    name="Feature A",
                    scope="s",
                    status="pending",
                )
            )

        design_entry = self._make_design_entry(tmp_path, design_id)
        feature = {"id": feature_key, "name": "Feature A"}
        designs_folder = tmp_path / "designs"
        (designs_folder / "features" / feature_key).mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        with orch_db_env.session_scope() as session:
            from src.core.database import Workflow

            # The workflow _run_one_feature's own sdk.start_workflow call
            # would have created -- design_id + definition_id + feature_key
            # in launch_params is what _relink_features_to_workflows
            # matches on, same as a real "autopilot" workflow row.
            session.add(
                Workflow(
                    id="wf-correct",
                    name="t",
                    phases_folder_path="/tmp",
                    status="completed",
                    design_id=design_id,
                    definition_id="autopilot",
                    launch_params={"feature_id": feature_key},
                )
            )

        def fake_run_single_workflow(sdk, wf_def, wt, desc, logger, **kwargs):
            passed_state = kwargs["state"]
            # Mirrors run_single_workflow's real success path: it sets
            # current_workflow_id while running, then clears it back to
            # None right before returning "completed" -- the exact
            # behavior that made the old thread_state.current_workflow_id
            # read a permanent no-op.
            if passed_state:
                passed_state.current_workflow_id = "wf-correct"
                passed_state.current_workflow_id = None
            return FeatureRunStatus.COMPLETED

        with patch(
            "src.autopilot.orchestrator._create_integration_worktree",
            return_value=worktree_dir,
        ), patch(
            "src.autopilot.orchestrator.run_single_workflow",
            side_effect=fake_run_single_workflow,
        ):
            status = _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=PipelineState(),
            )

        assert status == FeatureRunStatus.COMPLETED
        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feature-row-1").first()
            assert feat.workflow_id == "wf-correct"


class TestRunOneFeatureDoesNotOverrideGotoBudget:
    """Regression: _run_one_feature used to pass its own max_iterations
    parameter straight through to run_single_workflow's max_iterations=
    kwarg -- but that parameter carries the CLI's --max-iterations value
    (default 3), a DESIGN-level retry concept (see MAX_DESIGN_RETRIES,
    which is separate and unaffected) that has nothing to do with a single
    feature workflow's goto budget. run_single_workflow's max_iterations
    maps directly to the engine's max_total_gotos -- so every feature
    pipeline in the system got silently capped at 3 total gotos across its
    entire 13-phase lifetime instead of workflow.yaml's real, deliberate
    max_total_gotos: 30. Observed live: adversarial_review found real
    BLOCKERs and scored correctly, but total_gotos had already reached 6
    from legitimate earlier review cycles -- "GOTO limit exceeded (6/3).
    Forcing continue" silently waved the findings through instead of
    sending them back to development."""

    def _make_design_entry(self, tmp_path, design_id):
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        return DesignEntry(path=design_path, name="Test Design", content_hash="hash", db_id=design_id)

    def test_max_iterations_is_not_forwarded_to_run_single_workflow(
        self, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            PipelineState,
            _run_one_feature,
        )
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        design_id = "design-1"
        feature_key = "feat-a"
        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D"))
            session.add(Feature(
                id="feature-row-1", design_id=design_id, feature_key=feature_key,
                name="Feature A", scope="s", status="pending",
            ))

        design_entry = self._make_design_entry(tmp_path, design_id)
        feature = {"id": feature_key, "name": "Feature A"}
        designs_folder = tmp_path / "designs"
        (designs_folder / "features" / feature_key).mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        captured_kwargs = {}

        def fake_run_single_workflow(sdk, wf_def, wt, desc, logger, **kwargs):
            captured_kwargs.update(kwargs)
            return "completed"

        with patch(
            "src.autopilot.orchestrator.worktree_integration._create_integration_worktree",
            return_value=worktree_dir,
        ), patch(
            "src.autopilot.orchestrator.run_single_workflow",
            side_effect=fake_run_single_workflow,
        ):
            _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=PipelineState(),
                # Simulates the CLI's --max-iterations default (3) flowing
                # in from AutopilotService -- must NOT reach
                # run_single_workflow's goto-budget override.
                max_iterations=3,
            )

        assert "max_iterations" not in captured_kwargs or captured_kwargs["max_iterations"] is None, (
            "max_iterations must not be forwarded to run_single_workflow -- "
            "it would silently override workflow.yaml's own max_total_gotos"
        )


class TestRunOneFeatureWorktreeCleanupTiming:
    """Regression: _run_one_feature used to delete its shared worktree
    unconditionally in a `finally:` block, on every exit -- including
    "paused"/"interrupted"/"timeout"/"failed" statuses that are all
    resumable later via the existing_workflow_id check (which re-uses this
    exact deterministic worktree path), and even on an unhandled exception.
    Deleting the worktree in any of those cases destroyed the very
    directory the next resume attempt needed, surfacing downstream as
    create_agent_for_task's "shared worktree missing" failure. The
    worktree must only be cleaned up once the feature has genuinely,
    permanently completed."""

    def _make_design_entry(self, tmp_path, design_id):
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        return DesignEntry(path=design_path, name="Test Design", content_hash="hash", db_id=design_id)

    def _setup(self, orch_db_env, tmp_path, feature_key="feat-a", design_id="design-1"):
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key=feature_key,
                    name="Feature A",
                    scope="s",
                    status="pending",
                )
            )

        design_entry = self._make_design_entry(tmp_path, design_id)
        feature = {"id": feature_key, "name": "Feature A"}
        designs_folder = tmp_path / "designs"
        (designs_folder / "features" / feature_key).mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()
        return design_entry, feature, designs_folder, project_path, worktree_dir

    def _run(self, orch_db_env, tmp_path, wf_status, feature_key="feat-a"):
        from src.autopilot.orchestrator import OrchestratorLogger, _run_one_feature

        design_entry, feature, designs_folder, project_path, worktree_dir = self._setup(
            orch_db_env, tmp_path, feature_key
        )
        with patch(
            "src.autopilot.orchestrator._create_integration_worktree",
            return_value=worktree_dir,
        ), patch(
            "src.autopilot.orchestrator.run_single_workflow",
            return_value=wf_status,
        ), patch(
            "src.autopilot.orchestrator._cleanup_worktree"
        ) as mock_cleanup:
            status = _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=None,
            )
        return status, mock_cleanup

    def test_completed_status_cleans_up_worktree(self, orch_db_env, tmp_path):
        status, mock_cleanup = self._run(orch_db_env, tmp_path, FeatureRunStatus.COMPLETED)
        assert status == FeatureRunStatus.COMPLETED
        mock_cleanup.assert_called_once()

    def test_failed_status_never_cleans_up_worktree(self, orch_db_env, tmp_path):
        status, mock_cleanup = self._run(orch_db_env, tmp_path, FeatureRunStatus.FAILED)
        assert status == FeatureRunStatus.FAILED
        mock_cleanup.assert_not_called()

    @pytest.mark.parametrize("wf_status", [FeatureRunStatus.INTERRUPTED, FeatureRunStatus.TIMEOUT])
    def test_non_terminal_statuses_never_clean_up_worktree_or_overwrite_feature_status(
        self, orch_db_env, tmp_path, wf_status
    ):
        """Phase 3 Tier 2 item 19: INTERRUPTED/TIMEOUT used to be folded
        into the same generic FAILED bucket as a genuine failure -- which
        silently defeated run_feature_pipelines' own non-terminal
        halt-early check one level up (it can never see a status this
        function never returns), and overwrote Feature.status="failed"
        for a feature that may still be genuinely running or resumable.
        Both must now report distinctly and leave Feature.status alone."""
        status, mock_cleanup = self._run(orch_db_env, tmp_path, wf_status)
        assert status == wf_status
        assert not status.is_terminal
        mock_cleanup.assert_not_called()

        from src.core.database import Feature

        with orch_db_env.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feature-row-1").first()
            # feat_record.status = "active" is set before run_single_workflow
            # is even called -- must survive untouched, not "failed".
            assert feature.status == "active"

    def test_paused_status_reports_paused_not_failed(self, orch_db_env, tmp_path):
        """Regression: a "paused" wf_status used to be folded into the same
        generic "failed" bucket as every other non-completed status. Since
        derive_design_status treats ANY failed feature as design-failed,
        this permanently killed the whole design's "active" status the
        moment a later-execution-group feature was created and legitimately
        sat paused waiting its turn -- even after an earlier group's
        feature that was genuinely running went on to complete
        successfully. "paused" must report as "paused", a real
        FeatureStatus, not get collapsed into "failed"."""
        status, mock_cleanup = self._run(orch_db_env, tmp_path, FeatureRunStatus.PAUSED)
        assert status == FeatureRunStatus.PAUSED
        mock_cleanup.assert_not_called()

        from src.core.database import Feature

        with orch_db_env.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feature-row-1").first()
            assert feature.status == "paused"

    def test_exception_mid_pipeline_never_cleans_up_worktree(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _run_one_feature

        design_entry, feature, designs_folder, project_path, worktree_dir = self._setup(
            orch_db_env, tmp_path
        )
        with patch(
            "src.autopilot.orchestrator._create_integration_worktree",
            return_value=worktree_dir,
        ), patch(
            "src.autopilot.orchestrator.run_single_workflow",
            side_effect=RuntimeError("boom"),
        ), patch(
            "src.autopilot.orchestrator._cleanup_worktree"
        ) as mock_cleanup:
            status = _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=None,
            )

        assert status == FeatureRunStatus.FAILED
        mock_cleanup.assert_not_called()


class TestRunDesignAggregateNonTerminalHandling:
    """Phase 3 Tier 2 item 19: a mixed dependency layer (one feature
    COMPLETED, another still genuinely in progress -- INTERRUPTED/TIMEOUT)
    used to fall through to run_design_aggregate's "some skipped but >=1
    completed -- partial success" branch and get marked
    DesignStatus.COMPLETED, even though real work was still outstanding.
    See FeatureRunStatus's docstring."""

    def _run(self, tmp_path, feature_results):
        from src.autopilot.orchestrator import OrchestratorLogger, run_design_aggregate
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        design_entry = DesignEntry(path=design_path, name="D", content_hash="h", db_id="des-1")
        designs_folder = tmp_path / "designs"
        designs_folder.mkdir()
        status, _report = run_design_aggregate(
            design_entry, feature_results, designs_folder, OrchestratorLogger(tmp_path)
        )
        return status

    def test_all_completed_is_design_completed(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator.state import DesignStatus

        status = self._run(
            tmp_path,
            {"f1": FeatureRunStatus.COMPLETED, "f2": FeatureRunStatus.COMPLETED},
        )
        assert status == DesignStatus.COMPLETED

    @pytest.mark.parametrize("non_terminal", [FeatureRunStatus.INTERRUPTED, FeatureRunStatus.TIMEOUT])
    def test_mixed_completed_and_non_terminal_is_not_completed(self, orch_db_env, tmp_path, non_terminal):
        from src.autopilot.orchestrator.state import DesignStatus

        status = self._run(
            tmp_path,
            {"f1": FeatureRunStatus.COMPLETED, "f2": non_terminal},
        )
        assert status != DesignStatus.COMPLETED
        assert status == DesignStatus.FAILED

    def test_partial_success_with_skipped_is_still_completed(self, orch_db_env, tmp_path):
        """Regression guard: the non-terminal check above must not
        over-trigger on the pre-existing, deliberate "some skipped but
        >=1 completed" partial-success case."""
        from src.autopilot.orchestrator.state import DesignStatus

        status = self._run(
            tmp_path,
            {"f1": FeatureRunStatus.COMPLETED, "f2": FeatureRunStatus.SKIPPED},
        )
        assert status == DesignStatus.COMPLETED

    def test_json_metrics_file_serializes_feature_statuses(self, orch_db_env, tmp_path):
        """FeatureRunStatus is a plain Enum, not natively json-serializable
        -- design_metrics.json must write the plain string value."""
        import json

        from src.autopilot.orchestrator import OrchestratorLogger, run_design_aggregate
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        design_entry = DesignEntry(path=design_path, name="D", content_hash="h", db_id="des-1")
        designs_folder = tmp_path / "designs"
        designs_folder.mkdir()
        run_design_aggregate(
            design_entry,
            {"f1": FeatureRunStatus.COMPLETED, "f2": FeatureRunStatus.TIMEOUT},
            designs_folder,
            OrchestratorLogger(tmp_path),
        )
        metrics = json.loads((designs_folder / "design_metrics.json").read_text())
        assert metrics["features"] == {"f1": "completed", "f2": "timeout"}


class TestRunOneFeatureWithDependencies:
    """Regression: _run_one_feature's local `from src.core.database import
    Feature, Workflow, get_db` (for its "find feature record" section)
    re-imported get_db and Workflow even though both are already imported
    at this module's top level -- Python treats a name as function-local
    for its ENTIRE enclosing function once it's assigned anywhere in that
    function, including via a later import statement. That made the
    EARLIER `with get_db() as db:` in the depends_on check above raise
    "cannot access local variable 'get_db'" the moment any feature
    actually had a non-empty depends_on list -- every dependent feature in
    every design, unconditionally. Never caught because every other test
    here mocks _run_one_feature itself instead of exercising its real
    body (see TestRunFeaturePipelinesDependencyHandling in
    test_run_feature_pipelines.py)."""

    def _make_design_entry(self, tmp_path, design_id):
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        return DesignEntry(path=design_path, name="Test Design", content_hash="hash", db_id=design_id)

    def test_feature_with_satisfied_dependency_does_not_crash(
        self, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger, _run_one_feature
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        design_id = "design-1"
        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Feature(
                    id="feature-dep",
                    design_id=design_id,
                    feature_key="dep-feature",
                    name="Dependency Feature",
                    scope="s",
                    status="completed",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key="feat-a",
                    name="Feature A",
                    scope="s",
                    status="pending",
                    depends_on=["dep-feature"],
                )
            )

        design_entry = self._make_design_entry(tmp_path, design_id)
        feature = {"id": "feat-a", "name": "Feature A", "depends_on": ["dep-feature"]}
        designs_folder = tmp_path / "designs"
        (designs_folder / "features" / "feat-a").mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        with patch(
            "src.autopilot.orchestrator._create_integration_worktree",
            return_value=worktree_dir,
        ), patch(
            "src.autopilot.orchestrator.run_single_workflow",
            return_value=FeatureRunStatus.COMPLETED,
        ), patch(
            "src.autopilot.orchestrator._cleanup_worktree"
        ):
            status = _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=None,
            )

        assert status == FeatureRunStatus.COMPLETED

    def test_feature_with_unsatisfied_dependency_is_skipped_not_crashed(
        self, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger, _run_one_feature
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        design_id = "design-2"
        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-2", name="p", base_dir="/tmp/proj-2"))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-2", filename="d.md", name="D")
            )
            session.add(
                Feature(
                    id="feature-dep-2",
                    design_id=design_id,
                    feature_key="dep-feature",
                    name="Dependency Feature",
                    scope="s",
                    status="pending",
                )
            )

        design_entry = self._make_design_entry(tmp_path, design_id)
        feature = {"id": "feat-b", "name": "Feature B", "depends_on": ["dep-feature"]}
        designs_folder = tmp_path / "designs"
        project_path = tmp_path / "project"
        project_path.mkdir()

        status = _run_one_feature(
            sdk=MagicMock(),
            design_entry=design_entry,
            feature=feature,
            designs_folder=designs_folder,
            project_path=project_path,
            logger=OrchestratorLogger(tmp_path),
            state=None,
        )

        assert status == FeatureRunStatus.SKIPPED


class TestRunOneFeatureThreadsProjectId:
    """Regression: run_single_workflow's project_path parameter is actually
    a worktree path, not the real project root (see its docstring) --
    project_id has to be threaded down as its own explicit parameter from
    _run_one_feature, not derived from project_path. Confirms
    _run_one_feature actually forwards the project_id it's given through to
    run_single_workflow, so _should_stop ends up checking the real
    project's stop signal instead of silently getting None."""

    def test_project_id_forwarded_to_run_single_workflow(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _run_one_feature,
        )
        from src.autopilot.orchestrator.state import DesignEntry
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        design_id = "design-1"
        feature_key = "feat-a"
        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key=feature_key,
                    name="Feature A",
                    scope="s",
                    status="pending",
                )
            )

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        design_entry = DesignEntry(
            path=design_path, name="Test Design", content_hash="hash", db_id=design_id
        )
        feature = {"id": feature_key, "name": "Feature A"}
        designs_folder = tmp_path / "designs"
        (designs_folder / "features" / feature_key).mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()
        worktree_dir = tmp_path / "worktree"
        worktree_dir.mkdir()

        captured = {}

        def fake_run_single_workflow(*args, **kwargs):
            captured["project_id"] = kwargs.get("project_id")
            return "completed"

        with patch(
            "src.autopilot.orchestrator._create_integration_worktree",
            return_value=worktree_dir,
        ), patch(
            "src.autopilot.orchestrator.run_single_workflow",
            side_effect=fake_run_single_workflow,
        ), patch(
            "src.autopilot.orchestrator._cleanup_worktree"
        ):
            _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=None,
                project_id="proj-1",
            )

        assert captured["project_id"] == "proj-1"


class TestRunOneFeatureSyncsFeatureStatusOnEarlyReturn:
    """Regression: found live for a real feature ("Advisor Pattern and
    Runtime Integration" in project sotto) -- its workflow had genuinely
    finished (git_expert ran, Workflow.status == "completed"), but the
    Feature row's own status stayed "active" forever, so the UI kept
    showing the feature as still running.

    Root cause: _run_one_feature sets feat_record.status = "active" right
    before starting the pipeline (see the "Update status to active" write
    above), then only flips it to "completed" via _update_feature_status at
    the very end of a run that actually executes the pipeline. But if this
    function is re-entered for the same feature after its workflow already
    reached "completed" (e.g. a backend restart resumes the design and
    walks the feature loop again), the existing_workflow_id fast path
    returns "completed" immediately without ever calling
    _update_feature_status -- leaving feat_record.status stuck at
    whatever an earlier, now-superseded call last set it to."""

    def test_feature_status_synced_when_workflow_already_completed(
        self, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _run_one_feature,
        )
        from src.autopilot.orchestrator.state import DesignEntry
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow

        design_id = "design-1"
        feature_key = "feat-a"
        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-done",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_pipeline",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key=feature_key,
                    name="Feature A",
                    scope="s",
                    status="active",
                    workflow_id="wf-done",
                )
            )

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        design_entry = DesignEntry(
            path=design_path, name="Test Design", content_hash="hash", db_id=design_id
        )
        feature = {"id": feature_key, "name": "Feature A"}
        designs_folder = tmp_path / "designs"
        (designs_folder / "features" / feature_key).mkdir(parents=True)
        project_path = tmp_path / "project"
        project_path.mkdir()

        with patch(
            "src.autopilot.orchestrator.run_single_workflow"
        ) as mock_run, patch("src.autopilot.orchestrator.worktree_integration._cleanup_worktree"):
            status = _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=None,
            )

        assert status == FeatureRunStatus.COMPLETED
        mock_run.assert_not_called()  # fast path never runs the pipeline again

        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feature-row-1").first()
            assert feat.status == "completed", (
                "Feature.status must be synced to the workflow's real "
                "outcome even on the already-completed fast path, not just "
                "the run-it-yourself path further down this function"
            )


class TestSyncStaleFeatureStatuses:
    """_sync_stale_feature_statuses: the Feature-table-wide self-heal for
    the same underlying bug TestRunOneFeatureSyncsFeatureStatusOnEarlyReturn
    covers, for the case that fix can't reach -- a feature whose workflow
    finished through some path other than a fresh _run_one_feature call
    for that exact feature (e.g. a resumed backend continuing an in-flight
    workflow directly, never re-walking the whole design), so nothing ever
    calls _update_feature_status for it again."""

    def test_flips_feature_to_completed_when_workflow_already_completed(
        self, orch_db_env
    ):
        from src.autopilot.orchestrator.features import _sync_stale_feature_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id="design-1", project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-done",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_pipeline",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id="design-1",
                    feature_key="feat-a",
                    name="Feature A",
                    scope="s",
                    status="active",
                    workflow_id="wf-done",
                )
            )
            # derive_feature_status needs at least one real task to derive
            # from -- with none, it conservatively returns the current
            # status unchanged ("no tasks yet").
            session.add(
                Task(
                    id="task-1",
                    raw_description="do work",
                    done_definition="done",
                    status="done",
                    workflow_id="wf-done",
                )
            )

        repaired = _sync_stale_feature_statuses(MagicMock())

        assert repaired == 1
        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feature-row-1").first()
            assert feat.status == "completed"
            assert feat.completed_at is not None

    def test_leaves_feature_alone_when_workflow_still_active(self, orch_db_env):
        from src.autopilot.orchestrator.features import _sync_stale_feature_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id="design-1", project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-active",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="active",
                    definition_id="feature_pipeline",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id="design-1",
                    feature_key="feat-a",
                    name="Feature A",
                    scope="s",
                    status="active",
                    workflow_id="wf-active",
                )
            )

        repaired = _sync_stale_feature_statuses(MagicMock())

        assert repaired == 0
        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feature-row-1").first()
            assert feat.status == "active"

    def test_leaves_already_completed_feature_alone(self, orch_db_env):
        """Sanity check: must not re-stamp completed_at on a feature that's
        already correctly synced."""
        from src.autopilot.orchestrator.features import _sync_stale_feature_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id="design-1", project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-done",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_pipeline",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id="design-1",
                    feature_key="feat-a",
                    name="Feature A",
                    scope="s",
                    status="completed",
                    workflow_id="wf-done",
                )
            )

        repaired = _sync_stale_feature_statuses(MagicMock())

        assert repaired == 0

    def test_never_marks_a_never_started_sibling_completed(self, orch_db_env):
        """Regression (live incident): a "no workflow + no active sibling
        implies done" heuristic briefly lived here. The instant a design's
        last in-flight feature completed, every genuinely not-yet-started
        sibling (workflow_id=None, status="pending", never dispatched --
        indistinguishable from an orphaned-but-actually-done feature by
        sibling status alone) got marked "completed" on the very next
        sweep tick, since none of them were "active" at that moment
        either. Real data loss: those features' actual pipeline work
        never ran, but the design looked finished. This must never fire
        for a feature with no workflow at all, no matter what its
        siblings look like."""
        from src.autopilot.orchestrator.features import _sync_stale_feature_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id="design-1", project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-done",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_pipeline",
                )
            )
            # A completed sibling, linked to a completed workflow.
            session.add(
                Feature(
                    id="feature-done",
                    design_id="design-1",
                    feature_key="feat-done",
                    name="Feature Done",
                    scope="s",
                    status="completed",
                    workflow_id="wf-done",
                )
            )
            # The never-started sibling: no workflow, "pending", no active
            # sibling anywhere else in the design at this instant.
            session.add(
                Feature(
                    id="feature-pending",
                    design_id="design-1",
                    feature_key="feat-pending",
                    name="Feature Pending",
                    scope="s",
                    status="pending",
                    workflow_id=None,
                )
            )

        repaired = _sync_stale_feature_statuses(MagicMock())

        assert repaired == 0
        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feature-pending").first()
            assert feat.status == "pending"
            assert feat.workflow_id is None
            assert feat.completed_at is None

    def test_relinks_orphaned_feature_before_syncing_status(self, orch_db_env):
        """Regression (live incident): a feature whose workflow completed
        without Feature.workflow_id ever being written stays workflow_id=
        None forever once its design's pipeline has fully finished --
        nothing is left to trigger the design re-walk that normally calls
        _relink_features_to_workflows, and the stale-status join above
        can't see it either (it requires a linked Workflow row). Must
        relink from the matching workflow's launch_params first, then sync
        status in the same tick."""
        from src.autopilot.orchestrator.features import _sync_stale_feature_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(id="design-1", project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-done",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="autopilot",
                    design_id="design-1",
                    launch_params={"feature_id": "feat-a"},
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id="design-1",
                    feature_key="feat-a",
                    name="Feature A",
                    scope="s",
                    status="active",
                    workflow_id=None,
                )
            )
            # derive_feature_status needs at least one real task to derive
            # from -- with none, it conservatively returns the current
            # status unchanged ("no tasks yet").
            session.add(
                Task(
                    id="task-1",
                    raw_description="do work",
                    done_definition="done",
                    status="done",
                    workflow_id="wf-done",
                )
            )

        repaired = _sync_stale_feature_statuses(MagicMock())

        assert repaired == 1
        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feature-row-1").first()
            assert feat.workflow_id == "wf-done"
            assert feat.status == "completed"
            assert feat.completed_at is not None


class TestSyncStaleDesignStatuses:
    """_sync_stale_design_statuses: the Design-table-wide self-heal for the
    same class of bug TestSyncStaleFeatureStatuses covers one level up --
    pick_next_design's own "all features done -> mark completed" decision
    only runs as a side effect of picking the NEXT design, so a design
    whose last feature finishes with nothing else ever needing to pick a
    new design again stays "active" forever without this."""

    def test_flips_design_to_completed_when_all_features_done(self, orch_db_env):
        from src.autopilot.orchestrator.features import _sync_stale_design_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(
                    id="design-1", project_id="proj-1", filename="d.md", name="D",
                    status="active",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1", design_id="design-1", feature_key="feat-a",
                    name="Feature A", scope="s", status="completed",
                )
            )
            session.add(
                Feature(
                    id="feature-row-2", design_id="design-1", feature_key="feat-b",
                    name="Feature B", scope="s", status="skipped",
                )
            )

        repaired = _sync_stale_design_statuses(MagicMock())

        assert repaired == 1
        with orch_db_env.session_scope() as session:
            design = session.query(AutopilotDesign).filter_by(id="design-1").first()
            assert design.status == "completed"

    def test_leaves_design_alone_when_a_feature_is_still_incomplete(self, orch_db_env):
        from src.autopilot.orchestrator.features import _sync_stale_design_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(
                    id="design-1", project_id="proj-1", filename="d.md", name="D",
                    status="active",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1", design_id="design-1", feature_key="feat-a",
                    name="Feature A", scope="s", status="completed",
                )
            )
            session.add(
                Feature(
                    id="feature-row-2", design_id="design-1", feature_key="feat-b",
                    name="Feature B", scope="s", status="active",
                )
            )

        repaired = _sync_stale_design_statuses(MagicMock())

        assert repaired == 0
        with orch_db_env.session_scope() as session:
            design = session.query(AutopilotDesign).filter_by(id="design-1").first()
            assert design.status == "active"

    def test_leaves_design_alone_when_a_feature_has_failed(self, orch_db_env):
        """A "failed" feature is neither completed nor skipped -- must not
        be treated as "done" just because nothing is actively in flight."""
        from src.autopilot.orchestrator.features import _sync_stale_design_statuses
        from src.core.database import AutopilotDesign, AutopilotProject, Feature

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(
                    id="design-1", project_id="proj-1", filename="d.md", name="D",
                    status="active",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1", design_id="design-1", feature_key="feat-a",
                    name="Feature A", scope="s", status="completed",
                )
            )
            session.add(
                Feature(
                    id="feature-row-2", design_id="design-1", feature_key="feat-b",
                    name="Feature B", scope="s", status="failed",
                )
            )

        repaired = _sync_stale_design_statuses(MagicMock())

        assert repaired == 0
        with orch_db_env.session_scope() as session:
            design = session.query(AutopilotDesign).filter_by(id="design-1").first()
            assert design.status == "active"

    def test_leaves_design_without_features_alone(self, orch_db_env):
        """A design not yet decomposed into features has zero Feature rows
        -- must not be mistaken for "all done" by an empty-set vacuous
        truth."""
        from src.autopilot.orchestrator.features import _sync_stale_design_statuses
        from src.core.database import AutopilotDesign, AutopilotProject

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(
                    id="design-1", project_id="proj-1", filename="d.md", name="D",
                    status="active",
                )
            )

        repaired = _sync_stale_design_statuses(MagicMock())

        assert repaired == 0
        with orch_db_env.session_scope() as session:
            design = session.query(AutopilotDesign).filter_by(id="design-1").first()
            assert design.status == "active"

    def test_leaves_non_active_design_alone(self, orch_db_env):
        """Only "active" designs are candidates -- a "pending" design that
        happens to have zero features (not yet decomposed) must not be
        touched."""
        from src.autopilot.orchestrator.features import _sync_stale_design_statuses
        from src.core.database import AutopilotDesign, AutopilotProject

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1"))
            session.add(
                AutopilotDesign(
                    id="design-1", project_id="proj-1", filename="d.md", name="D",
                    status="pending",
                )
            )

        repaired = _sync_stale_design_statuses(MagicMock())

        assert repaired == 0
        with orch_db_env.session_scope() as session:
            design = session.query(AutopilotDesign).filter_by(id="design-1").first()
            assert design.status == "pending"


class TestInterruptibleSleep:
    """docs/SAFE_RESTART_DESIGN.md §3.3: run_continuous_pipeline's loop used
    a plain time.sleep(N) at its two longest waits, making a stop request
    (including AutopilotService.pause_for_restart()) invisible to the loop
    for up to DESIGN_QUEUE_SCAN_INTERVAL (60s) if it landed mid-sleep."""

    @pytest.fixture(autouse=True)
    def _clean_stop_events(self):
        from src.autopilot import orchestrator

        orchestrator._stop_events.clear()
        yield
        orchestrator._stop_events.clear()

    def test_returns_promptly_once_should_stop_flips(self):
        import asyncio
        import threading
        import time as time_module

        from src.autopilot import orchestrator
        from src.autopilot.orchestrator import _interruptible_sleep

        event = asyncio.Event()
        orchestrator._stop_events["proj-a"] = event

        def _flip_after_delay():
            time_module.sleep(0.3)
            event.set()

        threading.Thread(target=_flip_after_delay, daemon=True).start()

        start = time_module.time()
        _interruptible_sleep(30, "proj-a")
        elapsed = time_module.time() - start

        assert elapsed < 2  # nowhere near the full 30s requested

    def test_sleeps_the_full_duration_when_never_asked_to_stop(self):
        import time as time_module

        from src.autopilot.orchestrator import _interruptible_sleep

        start = time_module.time()
        _interruptible_sleep(1, "proj-never-registered")
        elapsed = time_module.time() - start

        assert elapsed >= 0.9


class TestResyncPipelineRegistry:
    """docs/SAFE_RESTART_DESIGN.md §3.5: a project whose persisted state
    says its pipeline should be running, but AutopilotServiceRegistry has
    no live entry for it, has fallen through the one-shot startup resume
    -- restart it from the periodic background sweep instead of leaving it
    silently idle."""

    @pytest.mark.asyncio
    async def test_restarts_a_project_with_no_live_registry_entry(self):
        from unittest.mock import AsyncMock

        from src.autopilot.orchestrator import _resync_pipeline_registry

        persisted = [
            ("proj-a", {"project_path": "/tmp/proj-a", "design_queue": "", "max_iterations": 7}),
        ]
        mock_service = Mock()
        mock_service.start = AsyncMock(return_value={"started": True})
        mock_registry = Mock()
        mock_registry.get.return_value = None
        mock_registry.get_or_create.return_value = mock_service

        with patch(
            "src.autopilot.service.AutopilotService.enumerate_persisted_states",
            return_value=persisted,
        ), patch("src.autopilot.service.get_registry", return_value=mock_registry):
            loop = asyncio.get_running_loop()
            resumed = await loop.run_in_executor(
                None, _resync_pipeline_registry, MagicMock(), loop
            )

        assert resumed == 1
        mock_registry.get_or_create.assert_called_once_with("proj-a")
        mock_service.start.assert_called_once_with(
            project_path="/tmp/proj-a", design_queue="", max_iterations=7
        )

    @pytest.mark.asyncio
    async def test_leaves_an_already_running_project_alone(self):
        from unittest.mock import AsyncMock

        from src.autopilot.orchestrator import _resync_pipeline_registry

        persisted = [
            ("proj-a", {"project_path": "/tmp/proj-a"}),
        ]
        already_running = Mock()
        already_running.running = True
        mock_registry = Mock()
        mock_registry.get.return_value = already_running
        mock_registry.get_or_create = Mock(side_effect=AssertionError("must not be called"))

        with patch(
            "src.autopilot.service.AutopilotService.enumerate_persisted_states",
            return_value=persisted,
        ), patch("src.autopilot.service.get_registry", return_value=mock_registry):
            loop = asyncio.get_running_loop()
            resumed = await loop.run_in_executor(
                None, _resync_pipeline_registry, MagicMock(), loop
            )

        assert resumed == 0
        mock_registry.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_restarts_a_project_whose_registry_entry_exists_but_isnt_running(self):
        """Not just "no entry at all" -- a stale, non-running entry left
        over from a prior pause must also be restarted."""
        from unittest.mock import AsyncMock

        from src.autopilot.orchestrator import _resync_pipeline_registry

        persisted = [
            ("proj-a", {"project_path": "/tmp/proj-a"}),
        ]
        stale_entry = Mock()
        stale_entry.running = False
        mock_service = Mock()
        mock_service.start = AsyncMock(return_value={"started": True})
        mock_registry = Mock()
        mock_registry.get.return_value = stale_entry
        mock_registry.get_or_create.return_value = mock_service

        with patch(
            "src.autopilot.service.AutopilotService.enumerate_persisted_states",
            return_value=persisted,
        ), patch("src.autopilot.service.get_registry", return_value=mock_registry):
            loop = asyncio.get_running_loop()
            resumed = await loop.run_in_executor(
                None, _resync_pipeline_registry, MagicMock(), loop
            )

        assert resumed == 1
        mock_service.start.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_a_project_with_a_stop_already_in_flight(self):
        """Regression: a project mid pause_for_restart() (or an explicit
        stop()) looks identical to "should restart" here -- registry entry
        momentarily not-running, persisted marker deliberately left intact
        -- for as long as the pause takes to actually finish (up to 45s).
        Restarting it from this sweep would race the graceful pause
        itself. _should_stop(project_id) (the same signal
        pause_for_restart()/stop() set) must prevent that."""
        from unittest.mock import AsyncMock

        from src.autopilot import orchestrator
        from src.autopilot.orchestrator import _resync_pipeline_registry

        orchestrator._stop_events.clear()
        try:
            stop_event = asyncio.Event()
            stop_event.set()
            orchestrator._stop_events["proj-a"] = stop_event

            persisted = [
                ("proj-a", {"project_path": "/tmp/proj-a"}),
            ]
            stale_entry = Mock()
            stale_entry.running = False
            mock_registry = Mock()
            mock_registry.get.return_value = stale_entry
            mock_registry.get_or_create = Mock(side_effect=AssertionError("must not be called"))

            with patch(
                "src.autopilot.service.AutopilotService.enumerate_persisted_states",
                return_value=persisted,
            ), patch("src.autopilot.service.get_registry", return_value=mock_registry):
                loop = asyncio.get_running_loop()
                resumed = await loop.run_in_executor(
                    None, _resync_pipeline_registry, MagicMock(), loop
                )

            assert resumed == 0
            mock_registry.get_or_create.assert_not_called()
        finally:
            orchestrator._stop_events.clear()


class TestRecoverAbandonedWorkflowsMissingWorktree:
    """_recover_abandoned_workflows_missing_worktree: automated recovery for
    a workflow _escalate_stale_active_workflows marked "failed" as a false
    positive (its own message hedges: "likely lost mid-flight across a
    backend restart") whose shared worktree is now gone. Without this,
    such a workflow has no automated path back to progress -- every
    _advance_phases case requires status in ("active", "paused"), so a
    "failed" workflow is invisible to all of them forever."""

    def _make_repo_with_feature_branch(self, tmp_path, design_id, feature_key):
        import git as _git

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        repo = _git.Repo.init(repo_path)
        (repo_path / "README.md").write_text("# Test\n")
        repo.index.add(["README.md"])
        repo.index.commit("Initial commit")
        default_branch = repo.active_branch.name
        branch = f"feature/{design_id[:8]}/{feature_key}"
        repo.git.branch(branch)
        repo.git.checkout(branch)
        (repo_path / "phase_work.md").write_text("# Real prior-phase work\n")
        repo.index.add(["phase_work.md"])
        repo.index.commit("phase(development): did real work")
        repo.git.checkout(default_branch)
        return repo_path, branch

    def test_rebuilds_worktree_from_branch_and_resumes(self, orch_db_env, tmp_path, monkeypatch):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import _recover_abandoned_workflows_missing_worktree
        from src.core.database import (
            AutopilotDesign,
            AutopilotProject,
            Feature,
            Phase,
            PhaseExecution,
            Task,
            Workflow,
        )

        design_id = "des-73b1ced0"
        feature_key = "gateway-router-metrics"
        repo_path, branch = self._make_repo_with_feature_branch(
            tmp_path, design_id, feature_key
        )

        # _create_integration_worktree resolves its own DatabaseManager and
        # WorktreeManager config independently of orch_db_env's env var --
        # point both at the same test DB/repo.
        import src.core.simple_config

        cfg = src.core.simple_config.Config()
        cfg.paths.database_path = orch_db_env.engine.url.database
        cfg.git.main_repo_path = repo_path
        cfg.paths.worktree_base_path = tmp_path / ".worktrees"
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: cfg)

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(repo_path)))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-lost",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="failed",
                    status_reason=(
                        "Abandoned: no agent/task activity for 10 consecutive "
                        "scans -- likely lost mid-flight across a backend restart"
                    ),
                    working_directory=None,
                    definition_id="autopilot",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key=feature_key,
                    name="Gateway Router Metrics",
                    scope="s",
                    status="active",
                    workflow_id="wf-lost",
                )
            )
            session.add(
                Phase(
                    id="phase-security-review",
                    workflow_id="wf-lost",
                    name="security_review",
                    order=7,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-security-review",
                    phase_id="phase-security-review",
                    workflow_execution_id="wf-lost",
                    status="in_progress",
                )
            )
            session.add(
                Task(
                    id="task-stuck",
                    workflow_id="wf-lost",
                    phase_id="phase-security-review",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Task stuck: no agent activity for >5 minutes",
                    retry_count=0,
                )
            )

        recovered = _recover_abandoned_workflows_missing_worktree(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-lost").first()
            assert wf.status == "active"
            assert wf.status_reason is None
            assert wf.working_directory is not None
            wt_path = Path(wf.working_directory)
            assert wt_path.is_dir()
            assert (wt_path / ".git").exists()
            # Rebuilt from the branch -- prior phase work must be present.
            assert (wt_path / "phase_work.md").exists()

            # The stuck task is left for _maybe_retry_failed_tasks' own
            # already-tested path to pick up -- untouched here.
            task = session.query(Task).filter_by(id="task-stuck").first()
            assert task.status == "failed"
            assert task.retry_count == 0

    def test_leaves_workflow_alone_when_retry_cap_already_reached(
        self, orch_db_env, tmp_path, monkeypatch
    ):
        """Sanity check the fix isn't overbroad: a workflow whose stuck task
        already hit the retry cap must not be recovered again -- something
        is genuinely, persistently broken and needs a human, not another
        automated attempt."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import _recover_abandoned_workflows_missing_worktree
        from src.core.database import (
            AutopilotDesign,
            AutopilotProject,
            Feature,
            Phase,
            PhaseExecution,
            Task,
            Workflow,
        )

        design_id = "des-73b1ced0"
        feature_key = "gateway-router-metrics"
        repo_path, branch = self._make_repo_with_feature_branch(
            tmp_path, design_id, feature_key
        )

        import src.core.simple_config

        cfg = src.core.simple_config.Config()
        cfg.paths.database_path = orch_db_env.engine.url.database
        cfg.git.main_repo_path = repo_path
        cfg.paths.worktree_base_path = tmp_path / ".worktrees"
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: cfg)

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(repo_path)))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-lost",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="failed",
                    status_reason=(
                        "Abandoned: no agent/task activity for 10 consecutive "
                        "scans -- likely lost mid-flight across a backend restart"
                    ),
                    working_directory=None,
                    definition_id="autopilot",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key=feature_key,
                    name="Gateway Router Metrics",
                    scope="s",
                    status="active",
                    workflow_id="wf-lost",
                )
            )
            session.add(
                Phase(
                    id="phase-security-review",
                    workflow_id="wf-lost",
                    name="security_review",
                    order=7,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-security-review",
                    phase_id="phase-security-review",
                    workflow_execution_id="wf-lost",
                    status="in_progress",
                )
            )
            session.add(
                Task(
                    id="task-stuck",
                    workflow_id="wf-lost",
                    phase_id="phase-security-review",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Retry agent creation failed",
                    retry_count=2,
                )
            )

        recovered = _recover_abandoned_workflows_missing_worktree(OrchestratorLogger(tmp_path))

        assert recovered == 0
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-lost").first()
            assert wf.status == "failed"
            assert wf.working_directory is None

    def test_old_capped_out_task_in_a_different_completed_phase_does_not_block_recovery(
        self, orch_db_env, tmp_path, monkeypatch
    ):
        """Regression, observed live: a workflow that had already been
        through several goto cycles carried an old "failed" task from an
        early development-phase attempt that hit its own retry cap, from
        long before a later retry of that same phase succeeded and the
        pipeline moved on for real (development's own PhaseExecution had
        long since gone "completed"). The real, current blocker was a
        security_review task that had never been retried at all
        (retry_count=0). Checking retry_count across every failed task
        ever recorded for the whole workflow -- instead of just the phase
        actually stuck right now -- refused to recover a workflow whose
        real blocker was still fully eligible, purely because of this
        unrelated, ancient, already-superseded history."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import _recover_abandoned_workflows_missing_worktree
        from src.core.database import (
            AutopilotDesign,
            AutopilotProject,
            Feature,
            Phase,
            PhaseExecution,
            Task,
            Workflow,
        )

        design_id = "des-73b1ced0"
        feature_key = "gateway-router-metrics"
        repo_path, branch = self._make_repo_with_feature_branch(
            tmp_path, design_id, feature_key
        )

        import src.core.simple_config

        cfg = src.core.simple_config.Config()
        cfg.paths.database_path = orch_db_env.engine.url.database
        cfg.git.main_repo_path = repo_path
        cfg.paths.worktree_base_path = tmp_path / ".worktrees"
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: cfg)

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(repo_path)))
            session.add(
                AutopilotDesign(id=design_id, project_id="proj-1", filename="d.md", name="D")
            )
            session.add(
                Workflow(
                    id="wf-lost",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="failed",
                    status_reason=(
                        "Abandoned: no agent/task activity for 10 consecutive "
                        "scans -- likely lost mid-flight across a backend restart"
                    ),
                    working_directory=None,
                    definition_id="autopilot",
                )
            )
            session.add(
                Feature(
                    id="feature-row-1",
                    design_id=design_id,
                    feature_key=feature_key,
                    name="Gateway Router Metrics",
                    scope="s",
                    status="active",
                    workflow_id="wf-lost",
                )
            )
            # Old, superseded development-phase attempt: already completed
            # for real (a later retry succeeded), carrying an old capped-out
            # failed task as harmless history.
            session.add(
                Phase(
                    id="phase-development", workflow_id="wf-lost", name="development",
                    order=4, description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-development", phase_id="phase-development",
                    workflow_execution_id="wf-lost", status="completed",
                )
            )
            session.add(
                Task(
                    id="task-old-capped",
                    workflow_id="wf-lost",
                    phase_id="phase-development",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Orphaned: never dispatched to an agent",
                    retry_count=2,
                )
            )
            # The real, current blocker -- never retried.
            session.add(
                Phase(
                    id="phase-security-review", workflow_id="wf-lost", name="security_review",
                    order=7, description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-security-review", phase_id="phase-security-review",
                    workflow_execution_id="wf-lost", status="in_progress",
                )
            )
            session.add(
                Task(
                    id="task-stuck",
                    workflow_id="wf-lost",
                    phase_id="phase-security-review",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Task stuck: no agent activity for >5 minutes",
                    retry_count=0,
                )
            )

        recovered = _recover_abandoned_workflows_missing_worktree(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-lost").first()
            assert wf.status == "active"
            assert wf.working_directory is not None
            # The old, unrelated task must be left completely untouched.
            old_task = session.query(Task).filter_by(id="task-old-capped").first()
            assert old_task.status == "failed"
            assert old_task.retry_count == 2


class TestRecoverAbandonedWorkflowsWithCompletedPhase:
    """_recover_abandoned_workflows_with_completed_phase: sibling recovery
    for the case _recover_abandoned_workflows_missing_worktree doesn't
    cover -- a workflow marked "failed" (abandoned) whose worktree is
    still intact and whose current in-progress phase's task already
    finished ("done"), so there's nothing to retry, just a lost hand-off
    back to _advance_phases. Same "invisible to every case forever" bug
    as the sibling class's docstring describes, different starting state."""

    def _seed(self, session, task_status="done", extra_task=None, working_directory="/tmp/repo/.worktrees/wt_feature"):
        from src.core.database import (
            AutopilotDesign,
            AutopilotProject,
            Feature,
            Phase,
            PhaseExecution,
            Task,
            Workflow,
        )

        session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/repo"))
        session.add(
            AutopilotDesign(id="des-1", project_id="proj-1", filename="d.md", name="D")
        )
        session.add(
            Workflow(
                id="wf-stuck",
                name="feature pipeline",
                phases_folder_path="/tmp",
                status="failed",
                status_reason=(
                    "Abandoned: no agent/task activity for 10 consecutive "
                    "scans -- likely lost mid-flight across a backend restart"
                ),
                working_directory=working_directory,
                definition_id="autopilot",
            )
        )
        session.add(
            Feature(
                id="feature-row-1",
                design_id="des-1",
                feature_key="cost-derivation",
                name="Cost Derivation",
                scope="s",
                status="active",
                workflow_id="wf-stuck",
            )
        )
        session.add(
            Phase(
                id="phase-product-validation",
                workflow_id="wf-stuck",
                name="product_validation",
                order=9,
                description="d",
                done_definitions=["x"],
            )
        )
        session.add(
            PhaseExecution(
                id="exec-product-validation",
                phase_id="phase-product-validation",
                workflow_execution_id="wf-stuck",
                status="in_progress",
            )
        )
        session.add(
            Task(
                id="task-finished",
                workflow_id="wf-stuck",
                phase_id="phase-product-validation",
                raw_description="r",
                done_definition="d",
                status=task_status,
            )
        )
        if extra_task:
            session.add(extra_task)

    def test_resumes_workflow_whose_finished_task_was_never_evaluated(
        self, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import _recover_abandoned_workflows_with_completed_phase
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            self._seed(session, task_status="done")

        recovered = _recover_abandoned_workflows_with_completed_phase(
            OrchestratorLogger(tmp_path)
        )

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-stuck").first()
            assert wf.status == "active"
            assert wf.status_reason is None
            # Recovery only unblocks -- it must not touch working_directory,
            # unlike the missing-worktree sibling which has to rebuild it.
            assert wf.working_directory == "/tmp/repo/.worktrees/wt_feature"

    def test_leaves_workflow_alone_when_a_task_is_still_in_progress(
        self, orch_db_env, tmp_path
    ):
        """A phase with real in-flight work (not abandoned at all, or the
        abandonment flag is stale) must not be force-resumed -- that's a
        different, still-active situation this function must not touch."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import _recover_abandoned_workflows_with_completed_phase
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            self._seed(session, task_status="in_progress")

        recovered = _recover_abandoned_workflows_with_completed_phase(
            OrchestratorLogger(tmp_path)
        )

        assert recovered == 0
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-stuck").first()
            assert wf.status == "failed"

    def test_leaves_workflow_alone_when_no_task_has_finished(self, orch_db_env, tmp_path):
        """Nothing to evaluate yet (e.g. only a failed task exists) --
        that's _recover_abandoned_workflows_missing_worktree's or
        _maybe_retry_failed_tasks' territory, not this function's."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import _recover_abandoned_workflows_with_completed_phase
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            self._seed(session, task_status="failed")

        recovered = _recover_abandoned_workflows_with_completed_phase(
            OrchestratorLogger(tmp_path)
        )

        assert recovered == 0
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-stuck").first()
            assert wf.status == "failed"

    def test_ignores_workflows_with_no_working_directory(self, orch_db_env, tmp_path):
        """working_directory is None -- that's the OTHER recovery
        function's case (rebuild the worktree first); this one must not
        try to evaluate a phase with nowhere to read its output from."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.worktree_integration import _recover_abandoned_workflows_with_completed_phase
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            self._seed(session, task_status="done", working_directory=None)

        recovered = _recover_abandoned_workflows_with_completed_phase(
            OrchestratorLogger(tmp_path)
        )

        assert recovered == 0


class TestRetryExhaustedPausedWorkflows:
    """Regression: _maybe_retry_failed_tasks pauses a workflow
    (paused_by="system") once every task in a phase has failed past its
    retry cap -- e.g. every attempt failed the same way because an LLM
    provider account ran out of credits. The only auto-resume path,
    _try_auto_resume_paused_workflow, requires a Task.status=="done"
    already sitting in the stalled phase -- a phase where literally every
    attempt failed will never produce one on its own, so the workflow
    stayed paused forever even after the user fixed the underlying cause
    (e.g. topped up credits). _retry_exhausted_paused_workflows closes
    that gap: after a cooldown, it resets retry_count on the stuck phase's
    failed tasks and resumes the workflow, deferring the actual
    reset-and-dispatch to _maybe_retry_failed_tasks' own already-tested
    path. Capped at paused_workflow_max_retry_cycles so a genuinely
    unrecoverable workflow doesn't retry forever."""

    def _seed_paused_workflow(
        self,
        db,
        paused_at,
        paused_by="system",
        paused_retry_count=0,
        phase_execution_status="in_progress",
        task_status="failed",
        task_retry_count=2,
    ):
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-paused",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="paused",
                    paused_by=paused_by,
                    paused_at=paused_at,
                    paused_retry_count=paused_retry_count,
                    status_reason="development: exhausted retries -- insufficient credits",
                )
            )
            session.add(
                Phase(
                    id="phase-dev",
                    workflow_id="wf-paused",
                    name="development",
                    order=1,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-dev",
                    phase_id="phase-dev",
                    workflow_execution_id="wf-paused",
                    status=phase_execution_status,
                )
            )
            session.add(
                Task(
                    id="task-stuck",
                    workflow_id="wf-paused",
                    phase_id="phase-dev",
                    raw_description="r",
                    done_definition="d",
                    status=task_status,
                    failure_reason="insufficient credits",
                    retry_count=task_retry_count,
                )
            )

    def test_resets_retry_count_and_reactivates_past_cooldown(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_paused_workflows
        from src.core.database import Task, Workflow

        self._seed_paused_workflow(
            orch_db_env, paused_at=datetime.utcnow() - timedelta(seconds=999999)
        )

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            assert wf.status == "active"
            assert wf.paused_by is None
            assert wf.status_reason is None
            assert wf.paused_at is None
            assert wf.paused_retry_count == 1
            task = session.query(Task).filter_by(id="task-stuck").first()
            assert task.retry_count == 0
            # Deferred to _maybe_retry_failed_tasks -- untouched here.
            assert task.status == "failed"
            assert task.failure_reason == "insufficient credits"

    def test_leaves_workflow_alone_within_cooldown(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_paused_workflows
        from src.core.database import Task, Workflow

        self._seed_paused_workflow(orch_db_env, paused_at=datetime.utcnow() - timedelta(seconds=5))

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 0
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            assert wf.status == "paused"
            task = session.query(Task).filter_by(id="task-stuck").first()
            assert task.retry_count == 2

    def test_treats_null_paused_at_as_eligible(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_paused_workflows
        from src.core.database import Workflow

        self._seed_paused_workflow(orch_db_env, paused_at=None)

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            assert wf.status == "active"

    def test_leaves_user_paused_workflows_alone(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_paused_workflows
        from src.core.database import Workflow

        self._seed_paused_workflow(
            orch_db_env, paused_by="user", paused_at=datetime.utcnow() - timedelta(seconds=999999)
        )

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 0
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            assert wf.status == "paused"

    def test_leaves_process_stop_pause_alone(self, orch_db_env, tmp_path):
        """pause_workflow_direct (used for process/pipeline shutdown) pauses
        without setting paused_by at all -- a real, separate "paused
        forever, no auto-resume" gap, but not this bug's root cause, and
        not safe to blindly auto-retry the same way (unlike an exhausted-
        retry pause, nothing here confirms every task actually failed)."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_paused_workflows
        from src.core.database import Workflow

        self._seed_paused_workflow(
            orch_db_env, paused_by=None, paused_at=datetime.utcnow() - timedelta(seconds=999999)
        )

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 0
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            assert wf.status == "paused"

    def test_gives_up_permanently_after_max_retry_cycles(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _get_paused_workflow_max_retry_cycles,
        )
        from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_paused_workflows
        from src.core.database import Task, Workflow

        self._seed_paused_workflow(
            orch_db_env,
            paused_at=datetime.utcnow() - timedelta(seconds=999999),
            paused_retry_count=_get_paused_workflow_max_retry_cycles(),
        )

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            # Still "paused", but no longer eligible for another cycle --
            # paused_by no longer matches this function's own "system" filter.
            assert wf.status == "paused"
            assert wf.paused_by == "system-exhausted"
            assert "manual resume required" in wf.status_reason
            # Tasks left completely untouched -- this is a permanent give-up,
            # not another retry attempt.
            task = session.query(Task).filter_by(id="task-stuck").first()
            assert task.retry_count == 2

        # On a subsequent pass, system-exhausted workflows with failed tasks
        # get retried (conditions may have changed since exhausting retries)
        recovered_again = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))
        assert recovered_again == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            assert wf.status == "active"
            assert wf.paused_by is None
            # Retry count on task was reset
            task = session.query(Task).filter_by(id="task-stuck").first()
            assert task.retry_count == 0

    def test_only_resets_failed_tasks_in_in_progress_phase(self, orch_db_env, tmp_path):
        """Same scoping requirement _recover_abandoned_workflows_missing_worktree
        already enforces: an old, already-superseded failed task from a
        completed phase must not block or get swept up by recovery of the
        actually-stuck phase."""
        from src.core.database import Phase, PhaseExecution, Task

        self._seed_paused_workflow(
            orch_db_env, paused_at=datetime.utcnow() - timedelta(seconds=999999)
        )
        with orch_db_env.session_scope() as session:
            session.add(
                Phase(
                    id="phase-old",
                    workflow_id="wf-paused",
                    name="architectural_review",
                    order=0,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-old",
                    phase_id="phase-old",
                    workflow_execution_id="wf-paused",
                    status="completed",
                )
            )
            session.add(
                Task(
                    id="task-old-capped",
                    workflow_id="wf-paused",
                    phase_id="phase-old",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="unrelated old failure",
                    retry_count=2,
                )
            )

        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_paused_workflows

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            old_task = session.query(Task).filter_by(id="task-old-capped").first()
            assert old_task.retry_count == 2  # untouched
            stuck_task = session.query(Task).filter_by(id="task-stuck").first()
            assert stuck_task.retry_count == 0  # reset


class TestAutoResumePausedWorkflow:
    """_try_auto_resume_paused_workflow's paused_by guard used to be
    `is not None`, which every real pause site defeats -- every one of
    them (user, budget, review, system) sets paused_by to something
    non-None, so the guard made this function's whole body dead code.
    "system" (set only by _maybe_retry_failed_tasks's exhausted-retry-cap
    give-up) is a heuristic judgment, not deliberate operator intent, and
    should be reconsidered once a done task shows the thing it gave up on
    actually succeeded -- exactly the race a still-in-flight final attempt
    (its own retry path uncapped at 2) can win moments after the cap-based
    pause fires. user/budget/review pauses must stay untouched."""

    def _seed(self, db, paused_by, phase_execution_status="in_progress", task_status="done"):
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1",
                    name="feature pipeline",
                    phases_folder_path="/tmp",
                    status="paused",
                    paused_by=paused_by,
                    status_reason="security_review: exhausted retries -- some reason",
                )
            )
            session.add(
                Phase(
                    id="phase-1",
                    workflow_id="wf-1",
                    name="security_review",
                    order=7,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-1",
                    phase_id="phase-1",
                    workflow_execution_id="wf-1",
                    status=phase_execution_status,
                )
            )
            session.add(
                Task(
                    id="task-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status=task_status,
                )
            )

    def test_resumes_system_paused_workflow_with_done_task(self, orch_db_env):
        from src.autopilot.orchestrator.phase_transitions import _try_auto_resume_paused_workflow
        from src.core.database import Workflow

        self._seed(orch_db_env, paused_by="system")

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            _try_auto_resume_paused_workflow(session, "wf-1", wf, MagicMock())

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            assert wf.paused_by is None
            assert wf.status_reason is None

    def test_leaves_user_paused_workflow_alone_even_with_done_task(self, orch_db_env):
        from src.autopilot.orchestrator.phase_transitions import _try_auto_resume_paused_workflow
        from src.core.database import Workflow

        self._seed(orch_db_env, paused_by="user")

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            _try_auto_resume_paused_workflow(session, "wf-1", wf, MagicMock())

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "user"

    def test_leaves_budget_paused_workflow_alone(self, orch_db_env):
        from src.autopilot.orchestrator.phase_transitions import _try_auto_resume_paused_workflow
        from src.core.database import Workflow

        self._seed(orch_db_env, paused_by="budget")

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            _try_auto_resume_paused_workflow(session, "wf-1", wf, MagicMock())

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "budget"

    def test_leaves_system_exhausted_paused_workflow_alone(self, orch_db_env):
        """"system-exhausted" (the permanent give-up state after max retry
        cycles) must not be swept up by the "system" special-case -- it's
        an exact-match check, not a prefix match."""
        from src.autopilot.orchestrator.phase_transitions import _try_auto_resume_paused_workflow
        from src.core.database import Workflow

        self._seed(orch_db_env, paused_by="system-exhausted")

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            _try_auto_resume_paused_workflow(session, "wf-1", wf, MagicMock())

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "system-exhausted"

    def test_system_paused_without_done_task_stays_paused(self, orch_db_env):
        from src.autopilot.orchestrator.phase_transitions import _try_auto_resume_paused_workflow
        from src.core.database import Workflow

        self._seed(orch_db_env, paused_by="system", task_status="failed")

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            _try_auto_resume_paused_workflow(session, "wf-1", wf, MagicMock())

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "system"


class TestWorkflowAppearsAbandoned:
    """_workflow_appears_abandoned: the signal _escalate_stale_active_
    workflows uses to decide whether a workflow stuck "active" is
    genuinely dead versus still doing real work."""

    def test_true_with_no_tasks_at_all(self, orch_db_env):
        from src.autopilot.orchestrator.policy import _workflow_appears_abandoned

        assert _workflow_appears_abandoned("wf-nonexistent") is True

    def test_true_when_only_terminal_tasks_and_no_active_agent(self, orch_db_env):
        from src.autopilot.orchestrator.policy import _workflow_appears_abandoned
        from src.core.database import Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Task(
                    id="t1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    workflow_id="wf-1",
                )
            )

        assert _workflow_appears_abandoned("wf-1") is True

    def test_false_when_all_tasks_done(self, orch_db_env):
        from src.autopilot.orchestrator.policy import _workflow_appears_abandoned
        from src.core.database import Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Task(
                    id="t1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    workflow_id="wf-1",
                )
            )

        assert _workflow_appears_abandoned("wf-1") is False

    def test_false_with_pending_task(self, orch_db_env):
        from src.autopilot.orchestrator.policy import _workflow_appears_abandoned
        from src.core.database import Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Task(
                    id="t1",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                    workflow_id="wf-1",
                )
            )

        assert _workflow_appears_abandoned("wf-1") is False

    def test_false_with_active_agent_despite_terminal_task(self, orch_db_env):
        """A task can be 'done' while its agent hasn't been cleaned up as
        terminated yet -- must not be read as abandoned in that window."""
        from src.autopilot.orchestrator.policy import _workflow_appears_abandoned
        from src.core.database import Agent, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Agent(id="a1", system_prompt="p", status="working", cli_type="pi")
            )
            session.add(
                Task(
                    id="t1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    workflow_id="wf-1",
                    assigned_agent_id="a1",
                )
            )

        assert _workflow_appears_abandoned("wf-1") is False


class TestUpdateResumedWorkflowRecoveryAttempts:
    """Regression: run_continuous_pipeline's resume-across-restart path
    used to increment its recovery-attempts counter on every scan a
    workflow wasn't 100% complete, with no regard for whether real work
    was actively happening -- so ANY workflow not fully done within ~6
    scans of an orchestrator restart got force-failed, even mid-phase with
    a live agent. Observed live: adversarial_review's agent completed its
    task successfully, and the workflow was force-failed about two minutes
    later anyway, purely because enough scans had elapsed since the last
    backend restart."""

    def test_resets_to_zero_on_real_activity(self, orch_db_env, monkeypatch):
        from src.autopilot.orchestrator.policy import _update_resumed_workflow_recovery_attempts

        monkeypatch.setattr(
            "src.autopilot.orchestrator.policy._workflow_appears_abandoned",
            lambda wf_id: False,
        )

        assert _update_resumed_workflow_recovery_attempts("wf-1", 5) == 0

    def test_increments_when_genuinely_abandoned(self, orch_db_env, monkeypatch):
        from src.autopilot.orchestrator.policy import _update_resumed_workflow_recovery_attempts

        monkeypatch.setattr(
            "src.autopilot.orchestrator.policy._workflow_appears_abandoned",
            lambda wf_id: True,
        )

        assert _update_resumed_workflow_recovery_attempts("wf-1", 3) == 4

    def test_a_single_active_scan_prevents_escalation_across_many_calls(
        self, orch_db_env, monkeypatch
    ):
        """A workflow that's abandoned on most scans but shows one real
        activity blip in between must never reach the kill threshold from
        that blip's contribution -- each abandoned run has to restart from
        zero after it."""
        from src.autopilot.orchestrator.policy import _update_resumed_workflow_recovery_attempts

        abandoned_flags = iter([True, True, True, False, True, True, True])
        monkeypatch.setattr(
            "src.autopilot.orchestrator.policy._workflow_appears_abandoned",
            lambda wf_id: next(abandoned_flags),
        )

        attempts = 0
        seen = []
        for _ in range(7):
            attempts = _update_resumed_workflow_recovery_attempts("wf-1", attempts)
            seen.append(attempts)

        assert seen == [1, 2, 3, 0, 1, 2, 3]


class TestEscalateStaleActiveWorkflows:
    """Regression: run_continuous_pipeline's "wait for active workflow"
    gate had no escalation/timeout -- a workflow stuck "active" in the DB
    (e.g. its next phase task never got created after a backend restart
    lost the in-memory pipeline progress) blocked the design queue from
    ever picking up the next design or feature, forever. Observed live:
    a workflow whose only task completed 12+ hours earlier, with zero
    PhaseExecution rows ever advancing past phase 1, silently blocked the
    "sotto" project's entire design queue for 85+ minutes straight."""

    def _make_workflow(self, db, wf_id, status="active"):
        from src.core.database import Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(id=wf_id, name="t", phases_folder_path="/tmp", status=status)
            )

    def test_requires_consecutive_abandoned_checks_before_escalating(
        self, orch_db_env, tmp_path, monkeypatch
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS
        from src.autopilot.orchestrator.policy import _escalate_stale_active_workflows
        from src.core.database import Workflow

        self._make_workflow(orch_db_env, "wf-1")
        monkeypatch.setattr(
            "src.autopilot.orchestrator.policy._workflow_appears_abandoned",
            lambda wf_id: True,
        )

        streak: dict = {}
        active_workflows = [{"id": "wf-1"}]
        logger = OrchestratorLogger(tmp_path)

        for _ in range(STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS - 1):
            still_blocking = _escalate_stale_active_workflows(
                active_workflows, streak, logger
            )
            assert still_blocking == ["wf-1"]

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"

        # One more consecutive abandoned observation crosses the threshold.
        still_blocking = _escalate_stale_active_workflows(
            active_workflows, streak, logger
        )
        assert still_blocking == []
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"
            assert "Abandoned" in (wf.status_reason or "")

    def test_activity_resets_the_streak(self, orch_db_env, tmp_path, monkeypatch):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS
        from src.autopilot.orchestrator.policy import _escalate_stale_active_workflows
        from src.core.database import Workflow

        self._make_workflow(orch_db_env, "wf-1")
        logger = OrchestratorLogger(tmp_path)
        streak: dict = {}
        active_workflows = [{"id": "wf-1"}]

        abandoned = True

        def flip_flopping(wf_id):
            nonlocal abandoned
            abandoned = not abandoned
            return abandoned

        monkeypatch.setattr(
            "src.autopilot.orchestrator.policy._workflow_appears_abandoned",
            flip_flopping,
        )

        # Alternating abandoned/active observations should never accumulate
        # a long enough streak to escalate, however many cycles run.
        for _ in range(STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS * 3):
            still_blocking = _escalate_stale_active_workflows(
                active_workflows, streak, logger
            )
            assert still_blocking == ["wf-1"]

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"

    def test_workflow_with_real_activity_is_never_escalated(
        self, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.policy import STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS
        from src.autopilot.orchestrator.policy import _escalate_stale_active_workflows
        from src.core.database import Task, Workflow

        self._make_workflow(orch_db_env, "wf-1")
        with orch_db_env.session_scope() as session:
            session.add(
                Task(
                    id="t1",
                    raw_description="r",
                    done_definition="d",
                    status="in_progress",
                    workflow_id="wf-1",
                )
            )

        logger = OrchestratorLogger(tmp_path)
        streak: dict = {}
        active_workflows = [{"id": "wf-1"}]

        for _ in range(STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS + 5):
            still_blocking = _escalate_stale_active_workflows(
                active_workflows, streak, logger
            )
            assert still_blocking == ["wf-1"]

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"


class TestShouldStop:
    """Regression: _should_stop() used to read a single bare module global
    (_service_stop_event) -- a second project starting overwrote the first
    project's reference, so whichever project's stop() fired last won
    control of BOTH pipelines' stop signal. Now keyed by project_id via
    the _stop_events dict."""

    @pytest.fixture(autouse=True)
    def _clean_stop_events(self):
        from src.autopilot import orchestrator

        orchestrator._stop_events.clear()
        yield
        orchestrator._stop_events.clear()

    def test_returns_true_only_for_the_project_whose_event_is_set(self):
        import asyncio

        from src.autopilot import orchestrator
        from src.autopilot.orchestrator import _should_stop

        event_a = asyncio.Event()
        event_a.set()
        event_b = asyncio.Event()
        orchestrator._stop_events["proj-a"] = event_a
        orchestrator._stop_events["proj-b"] = event_b

        assert _should_stop("proj-a") is True
        assert _should_stop("proj-b") is False

    def test_unregistered_project_id_returns_false(self):
        from src.autopilot.orchestrator import _should_stop

        assert _should_stop("proj-never-registered") is False

    def test_none_project_id_returns_false_instead_of_guessing(self):
        import asyncio

        from src.autopilot import orchestrator
        from src.autopilot.orchestrator import _should_stop

        # Even if some other project's event happens to be set, a caller
        # that couldn't resolve its own project_id must not accidentally
        # inherit a different project's stop signal.
        event_a = asyncio.Event()
        event_a.set()
        orchestrator._stop_events["proj-a"] = event_a

        assert _should_stop(None) is False


class TestGetOrCreateProjectId:
    def test_creates_and_activates_new_project(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import AutopilotProject

        project = tmp_path / "myproject"
        project.mkdir()

        project_id = _get_or_create_project_id(str(project))

        with orch_db_env.session_scope() as session:
            proj = session.query(AutopilotProject).filter_by(id=project_id).first()
            assert proj is not None
            assert proj.base_dir == str(project.resolve())
            assert proj.is_active is True

    def test_repeat_call_is_idempotent_no_duplicate_row(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import AutopilotProject

        project = tmp_path / "myproject"
        project.mkdir()

        first_id = _get_or_create_project_id(str(project))
        second_id = _get_or_create_project_id(str(project))

        assert first_id == second_id
        with orch_db_env.session_scope() as session:
            matches = (
                session.query(AutopilotProject)
                .filter_by(base_dir=str(project.resolve()))
                .all()
            )
            assert len(matches) == 1

    def test_activating_new_project_does_not_evict_previous_under_cap(
        self, orch_db_env, tmp_path
    ):
        """Regression: this is the real pipeline-launch path (POST /start,
        AutopilotService.start()), called BEFORE /start's own
        AutopilotServiceRegistry.try_reserve() cap check. It used to
        unconditionally evict whatever else was is_active, which silently
        dropped the other project out of the phase-advancement sweep's
        coverage regardless of whether max_concurrent_projects was
        actually exceeded. Both must stay active here since the default
        cap (2) isn't exceeded by two projects."""
        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import AutopilotProject

        project_a = tmp_path / "project-a"
        project_a.mkdir()
        project_b = tmp_path / "project-b"
        project_b.mkdir()

        id_a = _get_or_create_project_id(str(project_a))
        id_b = _get_or_create_project_id(str(project_b))

        with orch_db_env.session_scope() as session:
            proj_a = session.query(AutopilotProject).filter_by(id=id_a).first()
            proj_b = session.query(AutopilotProject).filter_by(id=id_b).first()
            assert proj_a.is_active is True
            assert proj_b.is_active is True

    def test_respects_max_concurrent_projects_cap(self, orch_db_env, tmp_path, monkeypatch):
        """At the cap, a third project must be left inactive, not evict
        one of the two already-active ones -- try_reserve() (for the
        /start path) is the authoritative reject, this just must not
        silently steal the slot out from under an already-running project."""
        from unittest.mock import MagicMock

        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import AutopilotProject

        mock_config = MagicMock()
        mock_config.autopilot.max_concurrent_projects = 2
        monkeypatch.setattr(
            "src.core.simple_config.get_config", lambda: mock_config
        )

        project_a = tmp_path / "project-a"
        project_a.mkdir()
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        project_c = tmp_path / "project-c"
        project_c.mkdir()

        id_a = _get_or_create_project_id(str(project_a))
        id_b = _get_or_create_project_id(str(project_b))
        id_c = _get_or_create_project_id(str(project_c))

        with orch_db_env.session_scope() as session:
            proj_a = session.query(AutopilotProject).filter_by(id=id_a).first()
            proj_b = session.query(AutopilotProject).filter_by(id=id_b).first()
            proj_c = session.query(AutopilotProject).filter_by(id=id_c).first()
            assert proj_a.is_active is True
            assert proj_b.is_active is True
            assert proj_c.is_active is False

    def test_does_not_resume_paused_workflows_when_cap_reached(
        self, orch_db_env, tmp_path, monkeypatch
    ):
        """Regression: when the cap blocks reactivation, this function used
        to still unconditionally flip the project's user-paused workflows
        back to status="active" -- but background_phase_advancement_sweep
        (server.py) only ever looks at workflows belonging to is_active
        projects. A workflow left "active" on a project that failed to
        reactivate is invisible to that sweep forever: it looks like it's
        running but nothing ever advances it."""
        from unittest.mock import MagicMock

        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import AutopilotProject, Workflow

        mock_config = MagicMock()
        mock_config.autopilot.max_concurrent_projects = 2
        monkeypatch.setattr(
            "src.core.simple_config.get_config", lambda: mock_config
        )

        project_a = tmp_path / "project-a"
        project_a.mkdir()
        project_b = tmp_path / "project-b"
        project_b.mkdir()
        project_c = tmp_path / "project-c"
        project_c.mkdir()

        id_a = _get_or_create_project_id(str(project_a))
        id_b = _get_or_create_project_id(str(project_b))

        # project_c already exists (was active and running before), but is
        # currently paused and inactive -- simulates a project stopped via
        # /autopilot/stop, whose slot was then taken by a or b.
        with orch_db_env.session_scope() as session:
            proj_c = AutopilotProject(
                id="proj-c", name="project-c", base_dir=str(project_c.resolve()),
                is_active=False,
            )
            session.add(proj_c)
            session.add(
                Workflow(
                    id="wf-c", project_id="proj-c", definition_id="autopilot",
                    name="t", phases_folder_path="/tmp",
                    status="paused", paused_by="user",
                )
            )

        _get_or_create_project_id(str(project_c))

        with orch_db_env.session_scope() as session:
            proj_c = session.query(AutopilotProject).filter_by(id="proj-c").first()
            wf_c = session.query(Workflow).filter_by(id="wf-c").first()
            assert proj_c.is_active is False
            assert wf_c.status == "paused"
            assert wf_c.paused_by == "user"

    def test_concurrent_insert_race_recovers_instead_of_raising(
        self, orch_db_env, tmp_path
    ):
        """Simulates two callers racing to create the same brand-new
        project row: the second caller's db.flush() hits IntegrityError on
        AutopilotProject.base_dir's unique constraint, and must recover by
        re-querying rather than propagating the error."""
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.orm import Session

        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import AutopilotProject

        project = tmp_path / "myproject"
        project.mkdir()

        real_flush = Session.flush
        raised = {"done": False}

        def flaky_flush(self, *args, **kwargs):
            pending_new = [o for o in self.new if isinstance(o, AutopilotProject)]
            if pending_new and not raised["done"]:
                raised["done"] = True
                # Simulate a concurrent caller's insert landing first, in a
                # separate session/transaction, before this flush fails.
                other = orch_db_env.get_session()
                try:
                    other.add(
                        AutopilotProject(
                            id="proj-raced-in-first",
                            name="myproject",
                            base_dir=str(project.resolve()),
                            is_active=False,
                        )
                    )
                    real_flush(other)
                    other.commit()
                finally:
                    other.close()
                raise IntegrityError("insert", {}, Exception("UNIQUE constraint failed"))
            return real_flush(self, *args, **kwargs)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(Session, "flush", flaky_flush)
            project_id = _get_or_create_project_id(str(project))

        assert project_id == "proj-raced-in-first"

    def test_new_project_gets_worktrees_and_hephaestus_excluded(
        self, orch_db_env, tmp_path
    ):
        """A freshly-registered project is also a real git repo here
        (unlike this class's other tests, which use a plain directory) --
        _get_or_create_project_id should set up the local, untracked
        .git/info/exclude for .worktrees/ and .hephaestus/ so they don't
        show up as untracked cruft in the project's own git status."""
        from git import Repo

        from src.autopilot.orchestrator.state import _get_or_create_project_id

        project = tmp_path / "myproject"
        project.mkdir()
        repo = Repo.init(project)
        (project / "README.md").write_text("hi")
        repo.index.add(["README.md"])
        repo.index.commit("init")

        _get_or_create_project_id(str(project))

        exclude_text = (project / ".git" / "info" / "exclude").read_text()
        assert ".worktrees/" in exclude_text
        assert ".hephaestus/" in exclude_text

    def test_non_git_project_directory_does_not_raise(self, orch_db_env, tmp_path):
        """Regression: the exclude-setup call must be a no-op (not an
        exception) for a project directory that isn't a git repo -- this
        class's other tests all use a plain non-git tmp_path directory and
        must keep passing."""
        from src.autopilot.orchestrator.state import _get_or_create_project_id

        project = tmp_path / "myproject"
        project.mkdir()

        project_id = _get_or_create_project_id(str(project))  # must not raise

        assert project_id is not None
        assert not (project / ".git").exists()


class TestEnsureGitExcluded:
    """_ensure_git_excluded: the shared helper behind both the ash-scan
    cleanup backstop and new-project .worktrees//.hephaestus/ setup."""

    def _init_repo(self, path):
        from git import Repo

        path.mkdir()
        repo = Repo.init(path)
        (path / "README.md").write_text("hi")
        repo.index.add(["README.md"])
        repo.index.commit("init")
        return repo

    def test_adds_patterns_to_info_exclude(self, tmp_path):
        import logging

        from src.autopilot.orchestrator.worktree_integration import _ensure_git_excluded

        repo_path = tmp_path / "repo"
        self._init_repo(repo_path)

        _ensure_git_excluded(repo_path, {".ash/": "test comment --"}, logging.getLogger(__name__))

        text = (repo_path / ".git" / "info" / "exclude").read_text()
        assert ".ash/" in text

    def test_idempotent_no_duplicate_entries(self, tmp_path):
        import logging

        from src.autopilot.orchestrator.worktree_integration import _ensure_git_excluded

        repo_path = tmp_path / "repo"
        self._init_repo(repo_path)
        logger = logging.getLogger(__name__)

        _ensure_git_excluded(repo_path, {".ash/": "c1"}, logger)
        _ensure_git_excluded(repo_path, {".ash/": "c1"}, logger)

        text = (repo_path / ".git" / "info" / "exclude").read_text()
        assert text.count(".ash/") == 1

    def test_resolves_shared_exclude_from_a_worktree(self, tmp_path):
        """Worktrees don't have their own info/exclude -- it must resolve
        to the main repo's shared one via --git-common-dir, not a
        (nonexistent) per-worktree path."""
        import logging

        from src.autopilot.orchestrator.worktree_integration import _ensure_git_excluded

        repo_path = tmp_path / "repo"
        repo = self._init_repo(repo_path)
        repo.git.branch("feature-x")
        worktree_path = tmp_path / "wt"
        repo.git.worktree("add", str(worktree_path), "feature-x")

        _ensure_git_excluded(worktree_path, {".ash/": "c"}, logging.getLogger(__name__))

        assert not (worktree_path / ".git" / "info").exists()
        text = (repo_path / ".git" / "info" / "exclude").read_text()
        assert ".ash/" in text

    def test_non_git_directory_is_a_silent_noop(self, tmp_path):
        import logging

        from src.autopilot.orchestrator.worktree_integration import _ensure_git_excluded

        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()

        _ensure_git_excluded(not_a_repo, {".ash/": "c"}, logging.getLogger(__name__))  # must not raise

        assert not (not_a_repo / ".git").exists()

    def test_accepts_plain_stdlib_logger_and_orchestrator_logger(self, tmp_path):
        """Called from both _run_ash_scan (OrchestratorLogger) and
        _get_or_create_project_id (plain module-level logging.Logger) --
        must not assume either-specific methods beyond .warning()."""
        import logging

        from src.autopilot.orchestrator import OrchestratorLogger

        from src.autopilot.orchestrator.worktree_integration import _ensure_git_excluded

        repo_path = tmp_path / "repo"
        self._init_repo(repo_path)

        # Force the warning path (non-git target) with each logger type --
        # would raise AttributeError before the fix if the wrong method
        # were assumed for one of them.
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()
        _ensure_git_excluded(not_a_repo, {".ash/": "c"}, logging.getLogger(__name__))
        _ensure_git_excluded(not_a_repo, {".ash/": "c"}, OrchestratorLogger(tmp_path / "logs"))


class TestGetProjectContextsByPrefix:
    def test_returns_only_matching_prefix(self, orch_db_env):
        from src.autopilot.orchestrator.state import (
    _get_project_contexts_by_prefix,
    _set_project_context,
)

        with orch_db_env.session_scope() as session:
            _set_project_context(session, "autopilot_running_pipeline_proj-a", {"x": 1})
            _set_project_context(session, "autopilot_running_pipeline_proj-b", {"x": 2})
            _set_project_context(session, "unrelated_key", {"x": 3})

        with orch_db_env.session_scope() as session:
            result = _get_project_contexts_by_prefix(
                session, "autopilot_running_pipeline_"
            )

        assert set(result.keys()) == {
            "autopilot_running_pipeline_proj-a",
            "autopilot_running_pipeline_proj-b",
        }


class TestStageForensicsInputs:
    """Regression: forensics_analysis.yaml made listing a `phase_prompts/`
    directory its "MANDATORY FIRST ACTION" and read a `run_health.json`
    alongside it -- and nothing in the codebase has ever written either one.
    The phase whose entire job is comparing prompts against outcomes could
    not read the prompts, so its first act was a guaranteed failure on a
    directory that did not exist."""

    def _logger(self, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger

        return OrchestratorLogger(tmp_path)

    def _workflow(self, definition_id="autopilot"):
        return type("W", (), {"definition_id": definition_id})()

    def test_writes_run_health_and_phase_prompts(self, tmp_path):
        import json

        from src.autopilot.orchestrator.phase_transitions import _stage_forensics_inputs

        health = {"clean": False, "goto_count": 3, "error_count": 7, "tmux_errors": []}
        _stage_forensics_inputs(tmp_path, self._workflow(), health, self._logger(tmp_path))

        run_health = tmp_path / ".hephaestus" / "run_health.json"
        assert run_health.exists()
        assert json.loads(run_health.read_text())["goto_count"] == 3

        prompts = tmp_path / ".hephaestus" / "phase_prompts"
        staged = {f.name for f in prompts.glob("*.yaml")}
        # The phase YAMLs forensics is asked to quote verbatim, plus
        # workflow.yaml (where the retry/goto thresholds that shaped the run
        # actually live).
        assert {"development.yaml", "security_review.yaml", "workflow.yaml"} <= staged

    def test_staged_prompts_match_the_real_configs(self, tmp_path):
        """Copied, not summarised: forensics is told to quote these verbatim
        when proposing rewrites, so a drifted or truncated copy is worse than
        no copy at all."""
        from src.autopilot.orchestrator.phase_transitions import _stage_forensics_inputs
        from src.workflow_registry import _WORKFLOWS_DIR

        _stage_forensics_inputs(tmp_path, self._workflow(), {"clean": False}, self._logger(tmp_path))

        staged = tmp_path / ".hephaestus" / "phase_prompts" / "development.yaml"
        source = _WORKFLOWS_DIR / "autopilot" / "development.yaml"
        assert staged.read_text() == source.read_text()

    def test_unknown_definition_id_does_not_raise(self, tmp_path):
        """forensics_analysis is an optional phase -- a staging failure must
        never take down task creation for it. run_health.json is written
        first and independently, so it still lands."""
        from src.autopilot.orchestrator.phase_transitions import _stage_forensics_inputs

        _stage_forensics_inputs(
            tmp_path, self._workflow("no-such-workflow"), {"clean": False}, self._logger(tmp_path)
        )
        assert (tmp_path / ".hephaestus" / "run_health.json").exists()
        assert not (tmp_path / ".hephaestus" / "phase_prompts").exists()

    def test_missing_definition_id_does_not_raise(self, tmp_path):
        from src.autopilot.orchestrator.phase_transitions import _stage_forensics_inputs

        _stage_forensics_inputs(
            tmp_path, type("W", (), {"definition_id": None})(), {"clean": False}, self._logger(tmp_path)
        )
        assert (tmp_path / ".hephaestus" / "run_health.json").exists()


class TestCreatePhaseTaskReviewCap:
    """_create_phase_task's opt-in review-run cap + prior-findings
    injection (workflow.yaml's max_review_runs) -- closes the review-fix-
    review loop a forensics_analysis report found (architectural_review
    ran 19 times, adversarial_review 14 times on one feature): each re-run
    is a fresh agent session with zero memory of its own findings."""

    def _seed(self, db, phase_name="architectural_review", existing_task_count=0):
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-cap",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    working_directory="/tmp/wf-cap",
                )
            )
            session.add(
                Phase(
                    id="phase-cap",
                    workflow_id="wf-cap",
                    name=phase_name,
                    order=5,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-cap",
                    phase_id="phase-cap",
                    workflow_execution_id="wf-cap",
                    status="pending",
                )
            )
            for i in range(existing_task_count):
                session.add(
                    Task(
                        id=f"prior-task-{i}",
                        workflow_id="wf-cap",
                        phase_id="phase-cap",
                        raw_description="r",
                        done_definition="d",
                        status="done",
                    )
                )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_no_injection_or_cap_when_max_review_runs_unset(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task

        self._seed(orch_db_env, phase_name="security_review", existing_task_count=5)
        mock_create_agent.return_value = {"agent_id": "agent-x"}

        with patch("src.autopilot.spec.get_max_review_runs", return_value=None):
            result = _create_phase_task(
                "wf-cap", "phase-cap", "security_review", "continue",
                OrchestratorLogger(tmp_path),
            )

        assert result is True
        with orch_db_env.session_scope() as session:
            new_task = (
                session.query(Task)
                .filter(Task.phase_id == "phase-cap", Task.status == "in_progress")
                .first()
            )
            assert new_task is not None
            assert "PRIOR FINDINGS" not in new_task.raw_description

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_no_injection_on_first_run(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task

        self._seed(orch_db_env, existing_task_count=0)
        mock_create_agent.return_value = {"agent_id": "agent-x"}

        with patch("src.autopilot.spec.get_max_review_runs", return_value=3):
            result = _create_phase_task(
                "wf-cap", "phase-cap", "architectural_review", "continue",
                OrchestratorLogger(tmp_path),
            )

        assert result is True
        with orch_db_env.session_scope() as session:
            new_task = (
                session.query(Task)
                .filter(Task.phase_id == "phase-cap", Task.status == "in_progress")
                .first()
            )
            assert "PRIOR FINDINGS" not in new_task.raw_description

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_injects_prior_findings_on_re_entry_under_cap(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task

        self._seed(orch_db_env, existing_task_count=1)  # run_count=1, under cap of 3
        mock_create_agent.return_value = {"agent_id": "agent-x"}
        history = [
            {"run_number": 1, "blocker_count": 2, "summary": "B-1 and B-2", "timestamp": "t"}
        ]

        with patch("src.autopilot.spec.get_max_review_runs", return_value=3), patch(
            "src.autopilot.spec.get_review_findings_history", return_value=history
        ):
            result = _create_phase_task(
                "wf-cap", "phase-cap", "architectural_review", "continue",
                OrchestratorLogger(tmp_path),
            )

        assert result is True
        with orch_db_env.session_scope() as session:
            new_task = (
                session.query(Task)
                .filter(Task.phase_id == "phase-cap", Task.status == "in_progress")
                .first()
            )
            assert "PRIOR FINDINGS FROM 1 EARLIER RUN(S)" in new_task.raw_description
            assert "B-1 and B-2" in new_task.raw_description
            assert "Verify ONLY whether" in new_task.raw_description

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_caps_out_instead_of_creating_another_task(
        self, mock_create_agent, mock_fire_transition, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task, Workflow

        self._seed(orch_db_env, existing_task_count=3)  # run_count=3, AT cap of 3
        mock_fire_transition.return_value = True

        # Point the workflow's working_directory at a real tmp dir so
        # _cap_out_review_phase can actually write the synthetic artifacts.
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-cap").first()
            wf.working_directory = str(tmp_path)

        with patch("src.autopilot.spec.get_max_review_runs", return_value=3), patch(
            "src.autopilot.spec.get_review_findings_history", return_value=[]
        ):
            result = _create_phase_task(
                "wf-cap", "phase-cap", "architectural_review", "continue",
                OrchestratorLogger(tmp_path),
            )

        assert result is True
        mock_fire_transition.assert_called_once_with(
            "wf-cap", "phase-cap", "architectural_review", ANY, force_continue=True
        )
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            # No NEW task created -- still exactly the 3 seeded ones.
            count = session.query(Task).filter(Task.phase_id == "phase-cap").count()
            assert count == 3

        result_md = tmp_path / ".hephaestus" / "architectural_review" / "review.md"
        assert result_md.exists()
        from src.autopilot.okf_markdown import read_okf

        frontmatter, _ = read_okf(result_md)
        assert frontmatter["blocker_count"] == 0

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_caps_out_a_phase_with_no_gate_result_artifacts(
        self, mock_create_agent, mock_fire_transition, orch_db_env, tmp_path
    ):
        """Regression: doc_review has max_review_runs configured in
        workflow.yaml but no GATE_RESULT_ARTIFACTS entry (it isn't scored
        via a gate artifact the way architectural_review/adversarial_review/
        security_review/qa_validation/product_validation are).
        _cap_out_review_phase previously returned None for these ("isn't a
        known gated phase"), which _create_phase_task treats as "fall
        through and create a normal task" -- so the cap silently never
        engaged and the phase re-ran forever. Observed live: security_review
        ran 25 times with max_review_runs: 4 configured, doing nothing.
        (security_review has since become a genuinely gated phase and takes
        the synthetic-artifact branch instead -- see
        test_caps_out_security_review_via_its_gate_artifact below. doc_review
        is the sole remaining user of this branch.)"""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task, Workflow

        self._seed(orch_db_env, phase_name="doc_review", existing_task_count=4)
        mock_fire_transition.return_value = True

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-cap").first()
            wf.working_directory = str(tmp_path)

        with patch("src.autopilot.spec.get_max_review_runs", return_value=4), patch(
            "src.autopilot.spec.get_review_findings_history", return_value=[]
        ):
            result = _create_phase_task(
                "wf-cap", "phase-cap", "doc_review", "continue",
                OrchestratorLogger(tmp_path),
            )

        assert result is True
        mock_fire_transition.assert_called_once_with(
            "wf-cap", "phase-cap", "doc_review", ANY, force_continue=True
        )
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            # No NEW task created -- still exactly the 4 seeded ones.
            count = session.query(Task).filter(Task.phase_id == "phase-cap").count()
            assert count == 4

        notice = tmp_path / ".hephaestus" / "doc_review" / "doc_review_capped_notice.md"
        assert notice.exists()
        assert "capped after 4 runs" in notice.read_text()

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_caps_out_security_review_via_its_gate_artifact(
        self, mock_create_agent, mock_fire_transition, orch_db_env, tmp_path
    ):
        """security_review is now a real gated phase (spec_gate: true), so
        capping it out must write the synthetic clean result its OWN scorer
        reads -- score_security_review reads unresolved_count, not the
        blocker_count schema the other review phases use. A blocker_count-
        only synthetic result would read as unresolved_count=0 by accident
        here rather than by construction; assert the real shape, including
        the frontmatter `type` validate_gate_result_schema demands."""
        from src.autopilot.okf_markdown import read_okf
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.autopilot.spec import score_security_review
        from src.core.database import Workflow

        self._seed(orch_db_env, phase_name="security_review", existing_task_count=4)
        mock_fire_transition.return_value = True

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-cap").first()
            wf.working_directory = str(tmp_path)

        with patch("src.autopilot.spec.get_max_review_runs", return_value=4), patch(
            "src.autopilot.spec.get_review_findings_history", return_value=[]
        ):
            result = _create_phase_task(
                "wf-cap", "phase-cap", "security_review", "continue",
                OrchestratorLogger(tmp_path),
            )

        assert result is True
        mock_create_agent.assert_not_called()

        result_md = tmp_path / ".hephaestus" / "security_review" / "security.md"
        assert result_md.exists()
        frontmatter, body = read_okf(result_md)
        assert frontmatter["type"] == "security_review_report"
        assert frontmatter["unresolved_count"] == 0
        assert "capped after 4 runs" in body
        # The synthetic result must actually score as a pass through the
        # real scorer -- that is the entire point of writing it.
        assert score_security_review(frontmatter)[0] >= 0.7

    def test_cap_out_review_phase_returns_none_without_working_directory(
        self, orch_db_env, tmp_path
    ):
        """Regression: silently returning False here (instead of None) let
        a capped-out phase with no working_directory get zero forward
        progress -- no task, no synthetic completion, nothing -- forever,
        with only a debug-level log to explain why. None signals the
        caller to fall through to a normal task instead."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _cap_out_review_phase
        from src.core.database import Phase, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-no-dir",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    working_directory=None,
                )
            )
            session.add(
                Phase(
                    id="phase-no-dir",
                    workflow_id="wf-no-dir",
                    name="architectural_review",
                    order=5,
                    description="d",
                    done_definitions=["x"],
                )
            )

        with orch_db_env.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-no-dir").first()
            result = _cap_out_review_phase(
                session, "wf-no-dir", phase, run_count=3, max_runs=3,
                logger=OrchestratorLogger(tmp_path),
            )

        assert result is None

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_falls_through_to_normal_task_when_cap_out_cannot_write(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Integration-level version of the above: _create_phase_task must
        still dispatch a real task when the cap is hit but capping out
        isn't possible, instead of returning early with nothing done."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task, Workflow

        self._seed(orch_db_env, existing_task_count=3)  # at cap of 3
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-cap").first()
            wf.working_directory = None  # cap-out can't write anywhere
        mock_create_agent.return_value = {"agent_id": "agent-x"}

        with patch("src.autopilot.spec.get_max_review_runs", return_value=3):
            result = _create_phase_task(
                "wf-cap", "phase-cap", "architectural_review", "continue",
                OrchestratorLogger(tmp_path),
            )

        assert result is True
        mock_create_agent.assert_called_once()
        with orch_db_env.session_scope() as session:
            # A real 4th task was created -- not stranded at 3 with nothing.
            count = session.query(Task).filter(Task.phase_id == "phase-cap").count()
            assert count == 4

    def test_cap_out_review_phase_writes_caveats_from_history(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _cap_out_review_phase
        from src.core.database import Phase, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-caveats",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    working_directory=str(tmp_path),
                )
            )
            phase = Phase(
                id="phase-caveats",
                workflow_id="wf-caveats",
                name="adversarial_review",
                order=6,
                description="d",
                done_definitions=["x"],
            )
            session.add(phase)

        history = [
            {"run_number": 1, "blocker_count": 3, "summary": "B-1, B-2, B-3", "timestamp": "t"},
            {"run_number": 2, "blocker_count": 1, "summary": "B-2 still open", "timestamp": "t"},
        ]

        with patch(
            "src.autopilot.spec.get_review_findings_history", return_value=history
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition", return_value=True
        ) as mock_fire:
            with orch_db_env.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-caveats").first()
                result = _cap_out_review_phase(
                    session, "wf-caveats", phase, run_count=3, max_runs=3,
                    logger=OrchestratorLogger(tmp_path),
                )

        assert result is True
        mock_fire.assert_called_once()
        report = tmp_path / ".hephaestus" / "adversarial_review" / "adversarial.md"
        assert report.exists()
        text = report.read_text()
        assert "capped after 3 runs" in text
        assert "B-2 still open" in text


class TestCreatePhaseTaskOrphanedPendingAge:
    """_create_phase_task's "existing pending task with no agent yet ->
    treat as orphaned, replace it" check must require the task to actually
    be old (matching _case_in_progress_complete's own 1-minute orphan
    threshold), not fire on any momentarily-agentless pending task.
    Observed live: two callers evaluated the same phase 11 seconds apart --
    the second one found the first task still mid-dispatch (row committed,
    agent not attached yet, a normal few-second gap), "helpfully" marked it
    failed as an orphan, and spawned a full duplicate agent for the same
    phase."""

    def _seed(self, db, pending_created_at):
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-orphan-age",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    working_directory="/tmp/wf-orphan-age",
                )
            )
            session.add(
                Phase(
                    id="phase-orphan-age",
                    workflow_id="wf-orphan-age",
                    name="security_review",
                    order=7,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-orphan-age",
                    phase_id="phase-orphan-age",
                    workflow_execution_id="wf-orphan-age",
                    status="pending",
                )
            )
            session.add(
                Task(
                    id="task-maybe-orphan",
                    workflow_id="wf-orphan-age",
                    phase_id="phase-orphan-age",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                    assigned_agent_id=None,
                    created_at=pending_created_at,
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_recently_created_pending_task_is_not_treated_as_orphaned(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task

        self._seed(orch_db_env, pending_created_at=datetime.utcnow())
        mock_create_agent.return_value = {"agent_id": "agent-x"}

        result = _create_phase_task(
            "wf-orphan-age", "phase-orphan-age", "security_review", "continue",
            OrchestratorLogger(tmp_path),
        )

        assert result is False
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            original = session.query(Task).filter_by(id="task-maybe-orphan").first()
            assert original.status == "pending"
            assert original.failure_reason is None
            duplicate_count = (
                session.query(Task)
                .filter(Task.phase_id == "phase-orphan-age")
                .count()
            )
            assert duplicate_count == 1

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_genuinely_stale_pending_task_is_still_replaced(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task

        self._seed(
            orch_db_env,
            pending_created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        mock_create_agent.return_value = {"agent_id": "agent-x"}

        result = _create_phase_task(
            "wf-orphan-age", "phase-orphan-age", "security_review", "continue",
            OrchestratorLogger(tmp_path),
        )

        assert result is True
        with orch_db_env.session_scope() as session:
            original = session.query(Task).filter_by(id="task-maybe-orphan").first()
            assert original.status == "failed"
            assert original.failure_reason == "Orphaned: never dispatched to an agent"
            fresh = (
                session.query(Task)
                .filter(Task.phase_id == "phase-orphan-age", Task.status == "in_progress")
                .first()
            )
            assert fresh is not None
            assert fresh.id != "task-maybe-orphan"

    def _seed_with_agent(self, db, agent_status, pending_created_at=None):
        from src.core.database import Agent, Phase, PhaseExecution, Task, Workflow

        pending_created_at = pending_created_at or (datetime.utcnow() - timedelta(minutes=5))
        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-orphan-age",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    working_directory="/tmp/wf-orphan-age",
                )
            )
            session.add(
                Phase(
                    id="phase-orphan-age",
                    workflow_id="wf-orphan-age",
                    name="security_review",
                    order=7,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-orphan-age",
                    phase_id="phase-orphan-age",
                    workflow_execution_id="wf-orphan-age",
                    status="pending",
                )
            )
            session.add(
                Agent(id="dead-or-alive-agent", system_prompt="p", status=agent_status, cli_type="pi")
            )
            session.add(
                Task(
                    id="task-maybe-orphan",
                    workflow_id="wf-orphan-age",
                    phase_id="phase-orphan-age",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                    assigned_agent_id="dead-or-alive-agent",
                    created_at=pending_created_at,
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_pending_task_pointing_at_terminated_agent_is_replaced(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Regression: a task dispatched to a real agent that later died
        (killed mid-launch by a backend restart, or manually terminated as
        stuck-agent cleanup) before ever flipping the task to in_progress
        stayed "pending" with a non-null assigned_agent_id forever -- the
        orphan check only ever looked at assigned_agent_id being NULL, so a
        task assigned to a now-dead agent looked identical to one still
        being actively worked."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task

        self._seed_with_agent(orch_db_env, agent_status="terminated")
        mock_create_agent.return_value = {"agent_id": "agent-x"}

        result = _create_phase_task(
            "wf-orphan-age", "phase-orphan-age", "security_review", "continue",
            OrchestratorLogger(tmp_path),
        )

        assert result is True
        with orch_db_env.session_scope() as session:
            original = session.query(Task).filter_by(id="task-maybe-orphan").first()
            assert original.status == "failed"
            assert "no longer active" in original.failure_reason
            fresh = (
                session.query(Task)
                .filter(Task.phase_id == "phase-orphan-age", Task.status == "in_progress")
                .first()
            )
            assert fresh is not None
            assert fresh.id != "task-maybe-orphan"

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_pending_task_pointing_at_working_agent_is_left_alone(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """A task assigned to a genuinely still-working agent must not be
        touched, even if it hasn't flipped to in_progress yet (a brief
        normal gap) -- only a DEAD agent's assignment counts as orphaned."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Task

        self._seed_with_agent(orch_db_env, agent_status="working")

        result = _create_phase_task(
            "wf-orphan-age", "phase-orphan-age", "security_review", "continue",
            OrchestratorLogger(tmp_path),
        )

        assert result is False
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            original = session.query(Task).filter_by(id="task-maybe-orphan").first()
            assert original.status == "pending"
            assert original.failure_reason is None


class TestCreatePhaseTaskStaleClaimFallback:
    """Characterization test for _create_phase_task's own inline
    stale-claim-clear-and-retry fallback (target_already_claimed=False
    path) -- one of three near-identical copies of this pattern in this
    module (the others: _create_corrective_task, already covered by
    TestCreateCorrectiveTask.test_reopening_resets_stale_task_creation_claim
    / test_refuses_when_target_phase_is_freshly_claimed_by_another_caller;
    and the periodic-sweep version _release_stale_task_creation_claims,
    covered in test_advance_phases.py). Captures current behavior before
    any future consolidation of these three copies, so that consolidation
    is provably behavior-preserving rather than just "didn't crash"."""

    def _seed(self, db):
        from src.core.database import Phase, PhaseExecution, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-stale-claim",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    working_directory="/tmp/wf-stale-claim",
                )
            )
            session.add(
                Phase(
                    id="phase-stale-claim",
                    workflow_id="wf-stale-claim",
                    name="architecture_design",
                    order=2,
                    description="d",
                    done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-stale-claim",
                    phase_id="phase-stale-claim",
                    workflow_execution_id="wf-stale-claim",
                    status="pending",
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_stale_claim_is_cleared_and_dispatch_proceeds(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """A claim held well past CLAIM_STALE_TIMEOUT_SECONDS must be
        treated as abandoned: cleared, re-claimed, and dispatch proceeds --
        mirroring _create_corrective_task's identical fallback."""
        from datetime import datetime

        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import PhaseExecution

        self._seed(orch_db_env)
        with orch_db_env.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-stale-claim").first()
            execution.task_creation_claimed_at = datetime(2020, 1, 1)
        mock_create_agent.return_value = {"agent_id": "agent-stale-claim"}

        result = _create_phase_task(
            "wf-stale-claim", "phase-stale-claim", "architecture_design", "continue",
            OrchestratorLogger(tmp_path),
        )

        assert result is True
        mock_create_agent.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_refuses_when_claim_is_fresh(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """A genuinely live, concurrent claim (well within the staleness
        window) must not be cleared -- dispatch is skipped, matching
        _create_corrective_task's own negative case."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import (
            _claim_phase_task_creation,
            _create_phase_task,
        )

        self._seed(orch_db_env)
        with orch_db_env.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-stale-claim") is True

        result = _create_phase_task(
            "wf-stale-claim", "phase-stale-claim", "architecture_design", "continue",
            OrchestratorLogger(tmp_path),
        )

        assert result is False
        mock_create_agent.assert_not_called()


class TestWaitForPendingReviews:
    """_wait_for_pending_reviews: the project-wide review-mode gate called
    before starting each new feature-execution-group, so review mode
    "gates the entire pipeline, not just individual features" (its own
    docstring)."""

    def test_returns_immediately_when_nothing_pending(self, orch_db_env):
        from src.autopilot.orchestrator import _wait_for_pending_reviews

        with patch("src.autopilot.orchestrator.time.sleep") as mock_sleep:
            _wait_for_pending_reviews("proj-none-pending", MagicMock())

        mock_sleep.assert_not_called()

    def test_blocks_on_real_feature_review_pause(self, orch_db_env):
        from src.autopilot.orchestrator import _wait_for_pending_reviews
        from src.core.database import Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-feat-paused",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    definition_id="autopilot",
                    status="paused",
                    paused_by="review",
                    project_id="proj-feat-review",
                )
            )
            session.add(
                Feature(
                    id="feat-paused-1",
                    design_id="des-1",
                    feature_key="core",
                    name="Core",
                    scope="s",
                    status="paused",
                    workflow_id="wf-feat-paused",
                )
            )

        def _clear(*args, **kwargs):
            with orch_db_env.session_scope() as session:
                wf = session.query(Workflow).filter_by(id="wf-feat-paused").first()
                wf.status = "active"
                wf.paused_by = None

        with patch(
            "src.autopilot.orchestrator.time.sleep", side_effect=_clear
        ) as mock_sleep:
            _wait_for_pending_reviews("proj-feat-review", MagicMock(), poll_interval=0)

        mock_sleep.assert_called_once()

    def test_blocks_on_phase0_review_pause(self, orch_db_env):
        """Regression: Phase 0 has no Feature row to join through -- it's
        what CREATES Feature rows -- so a paused-for-review decomposition
        (see _pause_phase0_for_review) used to be invisible to this
        query's Feature-join, letting a different design's next
        feature-execution-group start while this design's Phase 0 sat
        paused for review, defeating the "gates the entire pipeline"
        guarantee."""
        from src.autopilot.orchestrator import _wait_for_pending_reviews
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-phase0-paused",
                    name="Phase 0",
                    phases_folder_path="/tmp",
                    definition_id="feature_architect",
                    status="paused",
                    paused_by="review",
                    project_id="proj-phase0-review",
                )
            )

        def _clear(*args, **kwargs):
            with orch_db_env.session_scope() as session:
                wf = session.query(Workflow).filter_by(id="wf-phase0-paused").first()
                wf.status = "active"
                wf.paused_by = None

        with patch(
            "src.autopilot.orchestrator.time.sleep", side_effect=_clear
        ) as mock_sleep:
            _wait_for_pending_reviews("proj-phase0-review", MagicMock(), poll_interval=0)

        mock_sleep.assert_called_once()

    def test_does_not_block_on_a_different_projects_phase0_pause(self, orch_db_env):
        """Project-scoping must still hold for the new Phase 0 check --
        review mode in one project must not stall a different project's
        pipeline."""
        from src.autopilot.orchestrator import _wait_for_pending_reviews
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-other-project-phase0",
                    name="Phase 0",
                    phases_folder_path="/tmp",
                    definition_id="feature_architect",
                    status="paused",
                    paused_by="review",
                    project_id="proj-other",
                )
            )

        with patch("src.autopilot.orchestrator.time.sleep") as mock_sleep:
            _wait_for_pending_reviews("proj-mine", MagicMock())

        mock_sleep.assert_not_called()


class TestTerminateAgentDirectResetsTask:
    """terminate_agent_direct is a separate termination primitive (direct
    DB write, no tmux kill) from AgentManager.terminate_agent -- used at 4
    call sites in this file. It never got the same safety net terminate_
    agent already has for its own ~15 call sites (src/agents/manager.py):
    resetting any Task still pointing at the agent being terminated. A
    Task left "assigned"/"in_progress" pointing at a now-terminated agent
    is indistinguishable from one whose agent is still genuinely working,
    until an unrelated periodic sweep (attempt_recovery's stale-assigned-
    task cleanup) eventually notices the mismatch and fails it with a
    generic "terminated unexpectedly" reason instead of resetting it for
    a clean retry."""

    def test_resets_task_assigned_to_the_terminated_agent(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import terminate_agent_direct
        from src.core.database import Agent, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
            session.add(Agent(id="agent-1", status="working", cli_type="claude", system_prompt="x", current_task_id="task-1"))
            session.add(Task(
                id="task-1", workflow_id="wf-1", raw_description="x", done_definition="x",
                status="in_progress", assigned_agent_id="agent-1",
            ))

        result = terminate_agent_direct("agent-1")
        assert result is True

        with orch_db_env.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-1").first()
            task = session.query(Task).filter_by(id="task-1").first()
            assert agent.status == "terminated"
            assert task.status == "pending", "task must be reset, not left dangling"
            assert task.assigned_agent_id is None

    def test_does_not_touch_a_different_agents_task(self, orch_db_env):
        """Only the Task actually assigned to the terminated agent is
        reset -- a Task belonging to some other, still-working agent must
        survive untouched."""
        from src.autopilot.orchestrator.engine_client import terminate_agent_direct
        from src.core.database import Agent, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
            session.add(Agent(id="agent-1", status="working", cli_type="claude", system_prompt="x"))
            session.add(Agent(id="agent-2", status="working", cli_type="claude", system_prompt="x", current_task_id="task-2"))
            session.add(Task(
                id="task-2", workflow_id="wf-1", raw_description="x", done_definition="x",
                status="in_progress", assigned_agent_id="agent-2",
            ))

        terminate_agent_direct("agent-1")

        with orch_db_env.session_scope() as session:
            other_task = session.query(Task).filter_by(id="task-2").first()
            other_agent = session.query(Agent).filter_by(id="agent-2").first()
            assert other_task.status == "in_progress"
            assert other_task.assigned_agent_id == "agent-2"
            assert other_agent.status == "working"


class TestTerminateAgentInvariant:
    """Parametrized test asserting the three-field termination invariant
    holds at every confirmed raw write site. Each test seeds an agent in
    a "working" state, terminates it via the named path, and asserts
    status/terminated_at/current_task_id all end in the invariant-correct
    state.

    The invariant (CLAUDE.md, agent-termination): status="terminated",
    current_task_id=None, terminated_at IS NOT NULL. The bug class this
    closes has independently recurred eight times in this codebase's
    history. Two of those caused confirmed live data loss.
    """

    def test_terminate_agent_sets_all_three_fields(self, orch_db_env):
        """terminate_agent (engine_client.py) is the canonical primitive."""
        from src.autopilot.orchestrator.engine_client import terminate_agent
        from src.core.database import Agent

        with orch_db_env.session_scope() as session:
            session.add(Agent(
                id="a-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="t-1",
            ))

        result = terminate_agent("a-1")
        assert result is True

        with orch_db_env.session_scope() as session:
            agent = session.query(Agent).filter_by(id="a-1").first()
            assert agent.status == "terminated"
            assert agent.current_task_id is None
            assert agent.terminated_at is not None

    def test_terminate_agent_resets_stray_task_before_agent_row(self, orch_db_env):
        """Ordering: stray task must be reset BEFORE the agent row flips.
        The primitive does this in one transaction, so both writes commit
        atomically -- but the task-reset write must come first in the
        session so SQLAlchemy flushes it before the agent update.
        """
        from src.autopilot.orchestrator.engine_client import terminate_agent
        from src.core.database import Agent, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
            session.add(Agent(
                id="a-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="t-1",
            ))
            session.add(Task(
                id="t-1", workflow_id="wf-1", raw_description="x",
                done_definition="x", status="in_progress",
                assigned_agent_id="a-1",
            ))

        result = terminate_agent("a-1")
        assert result is True

        with orch_db_env.session_scope() as session:
            agent = session.query(Agent).filter_by(id="a-1").first()
            task = session.query(Task).filter_by(id="t-1").first()
            assert agent.status == "terminated"
            assert agent.current_task_id is None
            assert agent.terminated_at is not None
            assert task.status == "pending"
            assert task.assigned_agent_id is None

    def test_terminate_agent_rejects_nonexistent_agent(self, orch_db_env):
        """terminate_agent returns False for a nonexistent agent."""
        from src.autopilot.orchestrator.engine_client import terminate_agent

        result = terminate_agent("nonexistent")
        assert result is False

    @pytest.mark.parametrize("stray_status", ["under_review", "needs_work"])
    def test_terminate_agent_resets_stray_task_in_review_or_needs_work(self, orch_db_env, stray_status):
        """Regression: the stray-task reset only covered
        assigned/in_progress/pending -- missing under_review (kept-alive-
        for-validation) and needs_work (validator sent feedback to the
        same still-running agent), both states where assigned_agent_id
        still points at this agent. Terminating that agent left the task
        permanently pointing at a dead agent, invisible to every self-heal
        sweep scoped to assigned_agent_id."""
        from src.autopilot.orchestrator.engine_client import terminate_agent
        from src.core.database import Agent, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
            session.add(Agent(
                id="a-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="t-1",
            ))
            session.add(Task(
                id="t-1", workflow_id="wf-1", raw_description="x",
                done_definition="x", status=stray_status,
                assigned_agent_id="a-1",
            ))

        result = terminate_agent("a-1")
        assert result is True

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="t-1").first()
            assert task.status == "pending"
            assert task.assigned_agent_id is None

    def test_terminate_agent_backward_compat_alias(self, orch_db_env):
        """terminate_agent_direct is a backward-compatible alias."""
        from src.autopilot.orchestrator.engine_client import (
            terminate_agent, terminate_agent_direct,
        )

        assert terminate_agent_direct is terminate_agent

    def test_task_reset_before_agent_row_flip(self, orch_db_env):
        """Ordering invariant: the stray task must be reset BEFORE the
        agent row flips to "terminated". If a completion call arrives
        in the gap (under old-shaped code where the agent row flips
        first), the task is still "in_progress" pointing at a
        terminated agent — the completion gets rejected and real work
        is lost. With the correct ordering, the task is already
        "pending" when the completion arrives, so it's cleanly
        rejected without dangling state.
        """
        from src.autopilot.orchestrator.engine_client import terminate_agent
        from src.core.database import Agent, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
            session.add(Agent(
                id="a-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="t-1",
            ))
            session.add(Task(
                id="t-1", workflow_id="wf-1", raw_description="x",
                done_definition="x", status="in_progress",
                assigned_agent_id="a-1",
            ))

        result = terminate_agent("a-1")
        assert result is True

        # Verify the task was reset — a completion call arriving after
        # termination sees task.status == "pending" and is cleanly
        # rejected, never left dangling with a terminated agent.
        with orch_db_env.session_scope() as session:
            agent = session.query(Agent).filter_by(id="a-1").first()
            task = session.query(Task).filter_by(id="t-1").first()
            assert agent.status == "terminated"
            assert agent.current_task_id is None
            assert agent.terminated_at is not None
            assert task.status == "pending"
            assert task.assigned_agent_id is None


class TestCheckPhaseSiblingActive:
    """Regression: the sibling-active guard (used to prevent duplicate
    task/agent creation on the same phase, including the validator spawn
    path) only covered pending/assigned/in_progress/queued -- missing
    under_review/validation_in_progress/needs_work. A sibling task mid-
    review or mid-validation still owns the phase; missing it here means
    a second task/agent can get spawned onto the same phase concurrently."""

    @pytest.mark.parametrize(
        "sibling_status", ["under_review", "validation_in_progress", "needs_work"]
    )
    def test_sees_sibling_in_review_or_validation_statuses(self, orch_db_env, sibling_status):
        from src.autopilot.orchestrator.engine_client import check_phase_sibling_active
        from src.core.database import Task, Workflow, Phase

        with orch_db_env.session_scope() as session:
            session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
            session.add(Phase(
                id="phase-1", workflow_id="wf-1", order=1, name="development",
                description="d", done_definitions=["d"],
            ))
            session.add(Task(
                id="t-sibling", workflow_id="wf-1", phase_id="phase-1",
                raw_description="x", done_definition="x", status=sibling_status,
            ))

        with orch_db_env.session_scope() as session:
            sibling = check_phase_sibling_active(
                session, "t-new", "phase-1", created_by_filter=False,
            )
            assert sibling is not None
            assert sibling.id == "t-sibling"
