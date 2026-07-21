"""Tests for autopilot/orchestrator.py — pure utilities + detection functions."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, MagicMock, Mock, patch

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
        from src.autopilot.orchestrator import (
            CLAIM_STALE_TIMEOUT_SECONDS,
            DESIGN_QUEUE_SCAN_INTERVAL,
            STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS,
        )

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
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

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
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState

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
        from src.autopilot.orchestrator import PersistentPipelineState, PipelineState
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
        from src.autopilot.orchestrator import OrchestratorLogger, pick_next_design

        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(tmp_path, set(), logger)
        assert result is None

    def test_picks_first(self, tmp_path, isolated_test_db):
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
        from src.autopilot.orchestrator import OrchestratorLogger, pick_next_design
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


class TestWorkflowBelongsToProject:
    """_workflow_belongs_to_project underlies both get_active_workflows'
    project scoping and run_continuous_pipeline's stale-workflow check --
    covering its decision logic directly, once, is more precise than
    re-deriving the same cases through each caller's DB/loop scaffolding."""

    def test_project_id_match_wins_even_with_no_working_directory(self):
        from src.autopilot.orchestrator import _workflow_belongs_to_project

        assert _workflow_belongs_to_project("proj-a", None, "proj-a", "/x/a") is True

    def test_project_id_mismatch_even_if_path_would_match(self):
        """project_id is authoritative -- a stale/incorrect working_directory
        (e.g. project directory renamed on disk after the workflow row was
        created) must not override a definitive project_id mismatch."""
        from src.autopilot.orchestrator import _workflow_belongs_to_project

        assert (
            _workflow_belongs_to_project("proj-b", "/x/a/.worktrees/wt_1", "proj-a", "/x/a")
            is False
        )

    def test_falls_back_to_path_when_no_project_id_on_either_side(self, tmp_path):
        from src.autopilot.orchestrator import _workflow_belongs_to_project

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
        from src.autopilot.orchestrator import _workflow_belongs_to_project

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
        from src.autopilot.orchestrator import _workflow_belongs_to_project

        assert _workflow_belongs_to_project(None, None, "proj-a", "/x/a") is False

    def test_current_project_id_unknown_falls_back_to_path(self, tmp_path):
        """If the CURRENT project's id couldn't be resolved (e.g. no
        AutopilotProject row for this project_path yet), project_id
        comparison is skipped entirely and the path check still applies."""
        from src.autopilot.orchestrator import _workflow_belongs_to_project

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
        from src.autopilot.orchestrator import get_workflow_status
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
        from src.autopilot.orchestrator import get_workflow_status

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
        from src.autopilot.orchestrator import get_active_workflows

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
        from src.autopilot.orchestrator import get_active_workflows

        self._make_workflow(orch_db_env, "wf-a", "/Users/x/code/project-a/.worktrees/wt_1")
        self._make_workflow(orch_db_env, "wf-b", "/Users/x/code/project-b/.worktrees/wt_1")

        result = get_active_workflows(project_path="/Users/x/code/project-a")
        assert [r["id"] for r in result] == ["wf-a"]

    def test_scoped_ignores_workflow_with_no_working_directory(self, orch_db_env):
        from src.autopilot.orchestrator import get_active_workflows

        self._make_workflow(orch_db_env, "wf-a", None)

        result = get_active_workflows(project_path="/Users/x/code/project-a")
        assert result == []

    def test_scoped_excludes_sibling_directory_name_prefix(self, orch_db_env, tmp_path):
        """Integration-level regression for the same str.startswith()
        boundary bug covered directly in TestWorkflowBelongsToProject: a
        workflow under a sibling directory whose name is a superstring of
        the target project's name must not be scoped in."""
        from src.autopilot.orchestrator import get_active_workflows

        project_a = tmp_path / "project-a"
        project_a.mkdir()
        project_ab = tmp_path / "project-ab"
        wt = project_ab / ".worktrees" / "wt_1"
        wt.mkdir(parents=True)
        self._make_workflow(orch_db_env, "wf-ab", str(wt))

        result = get_active_workflows(project_path=str(project_a))
        assert result == []

    def test_paused_workflows_excluded_regardless_of_scope(self, orch_db_env):
        from src.autopilot.orchestrator import get_active_workflows

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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
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
        from src.autopilot.orchestrator import OrchestratorLogger, _retry_failed_tasks
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_skips_task_at_retry_cap(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _retry_failed_tasks
        from src.core.database import Task

        self._make_workflow_and_failed_task(orch_db_env, retry_count=2)

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert recovered == []
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert task.retry_count == 2

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct", return_value=None)
    def test_agent_dispatch_failure_lands_back_on_failed_not_stuck_pending(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        """Same dead-end this exact fix closed for _maybe_retry_failed_tasks:
        leaving the task "pending" on a dispatch failure would strand it --
        nothing dispatches an agent for an already-existing pending task
        with no agent."""
        from src.autopilot.orchestrator import OrchestratorLogger, _retry_failed_tasks
        from src.core.database import Task

        self._make_workflow_and_failed_task(orch_db_env)

        recovered = _retry_failed_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert recovered == []
        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "failed"
            assert task.retry_count == 1


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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_restarts_task_whose_agent_was_terminated(self, mock_create_agent, orch_db_env, tmp_path):
        """A task can be stuck 'in_progress' pointing at an agent that was
        already terminated (e.g. the service stop killed it) -- resume must
        detect this and restart it, not just plain 'failed'/'blocked'."""
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks

        self._make_agent(orch_db_env, "new-agent", "working")
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_clears_stale_goto_action_on_restart(self, mock_create_agent, orch_db_env, tmp_path):
        """Regression: this row is reused (not recreated) for the restart --
        a task previously tagged action='goto' by _tag_completing_task (from
        an earlier life, before ending up 'failed'/'blocked' here) kept
        showing that stale badge, with a now-meaningless action_target_phase,
        on what the UI displays as a brand new attempt. Observed live: a
        restarted task still showed "goto" with no (or a wrong) target."""
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_restarts_pending_task_with_dead_agent(self, mock_create_agent, orch_db_env, tmp_path):
        """A task can end up 'pending' with a stale assigned_agent_id (e.g.
        an agent manually terminated after the task was dispatched but
        before it was reset) -- same 'genuinely stuck' check as
        assigned/in_progress must apply, or the task is unrecoverable."""
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks

        self._make_agent(orch_db_env, "new-agent", "working")
        mock_create_agent.return_value = {"agent_id": "new-agent"}
        self._make_workflow(orch_db_env, "wf-1", "paused")
        self._make_agent(orch_db_env, "dead-agent", "terminated")
        self._make_task(orch_db_env, "task-1", "wf-1", "pending", agent_id="dead-agent")

        restarted = _resume_stuck_workflow_tasks("wf-1", OrchestratorLogger(tmp_path))

        assert restarted == 1
        mock_create_agent.assert_called_once()

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_restarts_stale_pending_task_with_no_agent(self, mock_create_agent, orch_db_env, tmp_path):
        """A 'pending' task with no agent at all and no automatic pickup
        anywhere else in the codebase (see PENDING_STUCK_MINUTES comment)
        must still be recoverable once it's clearly been abandoned, not
        just mid-dispatch."""
        from datetime import datetime, timedelta

        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_does_not_restart_freshly_created_pending_task(self, mock_create_agent, orch_db_env, tmp_path):
        """A 'pending' task with no agent yet that was JUST created is
        normal -- creation and first dispatch happen in the same
        synchronous call elsewhere. Sweeping it up here would race that
        dispatch instead of waiting for it."""
        from src.autopilot.orchestrator import OrchestratorLogger, _resume_stuck_workflow_tasks

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


class TestRunOneFeatureStateIsolation:
    """Regression: run_feature_pipelines' ThreadPoolExecutor hands every
    parallel feature the SAME PipelineState object (MAX_PARALLEL_FEATURES
    concurrent threads). run_single_workflow mutates state.current_workflow_id
    while it launches/polls a feature's 12-phase workflow -- without a
    thread-local copy, one feature's workflow_id can be stomped by a sibling
    feature's concurrent write before _link_workflow_to_feature reads it back,
    permanently linking the WRONG workflow to this Feature row."""

    def _make_design_entry(self, tmp_path, design_id):
        from src.autopilot.orchestrator import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design\n")
        return DesignEntry(
            path=design_path,
            name="Test Design",
            content_hash="hash",
            db_id=design_id,
        )

    def test_link_uses_this_calls_own_workflow_id_not_a_sibling_overwrite(
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

            session.add(
                Workflow(id="wf-correct", name="t", phases_folder_path="/tmp", status="completed")
            )

        # The SAME PipelineState instance a real ThreadPoolExecutor run would
        # hand to every parallel feature.
        shared_state = PipelineState()

        def fake_run_single_workflow(sdk, wf_def, wt, desc, logger, **kwargs):
            passed_state = kwargs["state"]
            # This call's own, correct workflow id.
            passed_state.current_workflow_id = "wf-correct"
            # Simulate a sibling feature thread finishing its own
            # run_single_workflow call afterward and stomping the shared
            # object -- this is what a caller reading the ORIGINAL `state`
            # (instead of a private copy) would see.
            shared_state.current_workflow_id = "wf-from-sibling-feature"
            return "completed"

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
                state=shared_state,
            )

        assert status == "completed"
        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feature-row-1").first()
            assert feat.workflow_id == "wf-correct"


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
        from src.autopilot.orchestrator import DesignEntry

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
        status, mock_cleanup = self._run(orch_db_env, tmp_path, "completed")
        assert status == "completed"
        mock_cleanup.assert_called_once()

    @pytest.mark.parametrize("wf_status", ["interrupted", "timeout", "failed"])
    def test_non_completed_statuses_never_clean_up_worktree(
        self, orch_db_env, tmp_path, wf_status
    ):
        status, mock_cleanup = self._run(orch_db_env, tmp_path, wf_status)
        assert status == "failed"
        mock_cleanup.assert_not_called()

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
        status, mock_cleanup = self._run(orch_db_env, tmp_path, "paused")
        assert status == "paused"
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

        assert status == "failed"
        mock_cleanup.assert_not_called()


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
            DesignEntry,
            OrchestratorLogger,
            _run_one_feature,
        )
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
    finished (git_commit_push ran, Workflow.status == "completed"), but the
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
            DesignEntry,
            OrchestratorLogger,
            _run_one_feature,
        )
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
        ) as mock_run, patch("src.autopilot.orchestrator._cleanup_worktree"):
            status = _run_one_feature(
                sdk=MagicMock(),
                design_entry=design_entry,
                feature=feature,
                designs_folder=designs_folder,
                project_path=project_path,
                logger=OrchestratorLogger(tmp_path),
                state=None,
            )

        assert status == "completed"
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
        from src.autopilot.orchestrator import _sync_stale_feature_statuses
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
                    status="active",
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
        from src.autopilot.orchestrator import _sync_stale_feature_statuses
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
        from src.autopilot.orchestrator import _sync_stale_feature_statuses
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _recover_abandoned_workflows_missing_worktree,
        )
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
        cfg.database_path = orch_db_env.engine.url.database
        cfg.main_repo_path = repo_path
        cfg.worktree_base_path = tmp_path / ".worktrees"
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _recover_abandoned_workflows_missing_worktree,
        )
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
        cfg.database_path = orch_db_env.engine.url.database
        cfg.main_repo_path = repo_path
        cfg.worktree_base_path = tmp_path / ".worktrees"
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _recover_abandoned_workflows_missing_worktree,
        )
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
        cfg.database_path = orch_db_env.engine.url.database
        cfg.main_repo_path = repo_path
        cfg.worktree_base_path = tmp_path / ".worktrees"
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _retry_exhausted_paused_workflows,
        )
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _retry_exhausted_paused_workflows,
        )
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _retry_exhausted_paused_workflows,
        )
        from src.core.database import Workflow

        self._seed_paused_workflow(orch_db_env, paused_at=None)

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-paused").first()
            assert wf.status == "active"

    def test_leaves_user_paused_workflows_alone(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _retry_exhausted_paused_workflows,
        )
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _retry_exhausted_paused_workflows,
        )
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
            _retry_exhausted_paused_workflows,
        )
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

        # And it stays excluded on a subsequent pass.
        recovered_again = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))
        assert recovered_again == 0

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

        from src.autopilot.orchestrator import (
            OrchestratorLogger,
            _retry_exhausted_paused_workflows,
        )

        recovered = _retry_exhausted_paused_workflows(OrchestratorLogger(tmp_path))

        assert recovered == 1
        with orch_db_env.session_scope() as session:
            old_task = session.query(Task).filter_by(id="task-old-capped").first()
            assert old_task.retry_count == 2  # untouched
            stuck_task = session.query(Task).filter_by(id="task-stuck").first()
            assert stuck_task.retry_count == 0  # reset


class TestWorkflowAppearsAbandoned:
    """_workflow_appears_abandoned: the signal _escalate_stale_active_
    workflows uses to decide whether a workflow stuck "active" is
    genuinely dead versus still doing real work."""

    def test_true_with_no_tasks_at_all(self, orch_db_env):
        from src.autopilot.orchestrator import _workflow_appears_abandoned

        assert _workflow_appears_abandoned("wf-nonexistent") is True

    def test_true_when_only_terminal_tasks_and_no_active_agent(self, orch_db_env):
        from src.autopilot.orchestrator import _workflow_appears_abandoned
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

        assert _workflow_appears_abandoned("wf-1") is True

    def test_false_with_pending_task(self, orch_db_env):
        from src.autopilot.orchestrator import _workflow_appears_abandoned
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
        from src.autopilot.orchestrator import _workflow_appears_abandoned
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
        from src.autopilot.orchestrator import (
            _update_resumed_workflow_recovery_attempts,
        )

        monkeypatch.setattr(
            "src.autopilot.orchestrator._workflow_appears_abandoned",
            lambda wf_id: False,
        )

        assert _update_resumed_workflow_recovery_attempts("wf-1", 5) == 0

    def test_increments_when_genuinely_abandoned(self, orch_db_env, monkeypatch):
        from src.autopilot.orchestrator import (
            _update_resumed_workflow_recovery_attempts,
        )

        monkeypatch.setattr(
            "src.autopilot.orchestrator._workflow_appears_abandoned",
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
        from src.autopilot.orchestrator import (
            _update_resumed_workflow_recovery_attempts,
        )

        abandoned_flags = iter([True, True, True, False, True, True, True])
        monkeypatch.setattr(
            "src.autopilot.orchestrator._workflow_appears_abandoned",
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
        from src.autopilot.orchestrator import (
            STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS,
            OrchestratorLogger,
            _escalate_stale_active_workflows,
        )
        from src.core.database import Workflow

        self._make_workflow(orch_db_env, "wf-1")
        monkeypatch.setattr(
            "src.autopilot.orchestrator._workflow_appears_abandoned",
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
        from src.autopilot.orchestrator import (
            STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS,
            OrchestratorLogger,
            _escalate_stale_active_workflows,
        )
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
            "src.autopilot.orchestrator._workflow_appears_abandoned",
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
        from src.autopilot.orchestrator import (
            STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS,
            OrchestratorLogger,
            _escalate_stale_active_workflows,
        )
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
        from src.autopilot.orchestrator import _get_or_create_project_id
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
        from src.autopilot.orchestrator import _get_or_create_project_id
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

    def test_activating_new_project_deactivates_previous(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import _get_or_create_project_id
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
            assert proj_a.is_active is False
            assert proj_b.is_active is True

    def test_concurrent_insert_race_recovers_instead_of_raising(
        self, orch_db_env, tmp_path
    ):
        """Simulates two callers racing to create the same brand-new
        project row: the second caller's db.flush() hits IntegrityError on
        AutopilotProject.base_dir's unique constraint, and must recover by
        re-querying rather than propagating the error."""
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.orm import Session

        from src.autopilot.orchestrator import _get_or_create_project_id
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


class TestGetProjectContextsByPrefix:
    def test_returns_only_matching_prefix(self, orch_db_env):
        from src.autopilot.orchestrator import (
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_no_injection_or_cap_when_max_review_runs_unset(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger, _create_phase_task
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_no_injection_on_first_run(self, mock_create_agent, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _create_phase_task
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_injects_prior_findings_on_re_entry_under_cap(
        self, mock_create_agent, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger, _create_phase_task
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

    @patch("src.autopilot.orchestrator._fire_phase_transition")
    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_caps_out_instead_of_creating_another_task(
        self, mock_create_agent, mock_fire_transition, orch_db_env, tmp_path
    ):
        from src.autopilot.orchestrator import OrchestratorLogger, _create_phase_task
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
            "wf-cap", "phase-cap", "architectural_review", ANY
        )
        mock_create_agent.assert_not_called()
        with orch_db_env.session_scope() as session:
            # No NEW task created -- still exactly the 3 seeded ones.
            count = session.query(Task).filter(Task.phase_id == "phase-cap").count()
            assert count == 3

        result_json = tmp_path / "docs" / "architectural_review" / "architectural_review_result.json"
        assert result_json.exists()
        assert json.loads(result_json.read_text())["blocker_count"] == 0

    def test_cap_out_review_phase_writes_caveats_from_history(self, orch_db_env, tmp_path):
        from src.autopilot.orchestrator import OrchestratorLogger, _cap_out_review_phase
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
            "src.autopilot.orchestrator._fire_phase_transition", return_value=True
        ) as mock_fire:
            with orch_db_env.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-caveats").first()
                result = _cap_out_review_phase(
                    session, "wf-caveats", phase, run_count=3, max_runs=3,
                    logger=OrchestratorLogger(tmp_path),
                )

        assert result is True
        mock_fire.assert_called_once()
        report = tmp_path / "docs" / "adversarial_review" / "adversarial_review_report.md"
        assert report.exists()
        text = report.read_text()
        assert "capped after 3 runs" in text
        assert "B-2 still open" in text
