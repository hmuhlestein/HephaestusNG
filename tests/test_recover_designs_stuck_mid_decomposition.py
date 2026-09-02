"""A design stranded mid-decomposition must recover without a human.

run_phase0 flips AutopilotDesign.status to "decomposing" before Phase 0
starts, and pick_next_design only ever selects "pending" designs. Any
interruption between those two points -- a backend restart, a kill, or an
exception on a path that fails to write the outcome back -- strands the
design: not in the queue (not "pending"), not running (no live workflow),
and invisible to _sync_stale_design_statuses (which only looks at "active").
It simply disappears from the pipeline.

Observed live twice in one session, both needing a manual status reset.
"""

from unittest.mock import MagicMock

import pytest

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Workflow,
)


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed(db, *, design_status="decomposing", phase0_status=None, retry_count=None):
    with db.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
        session.add(AutopilotDesign(
            id="des-1", project_id="proj-1", filename="d.md", name="my-design",
            status=design_status,
        ))
        if phase0_status:
            session.add(Workflow(
                id="wf-phase0", name="Feature Architect", phases_folder_path="/tmp",
                definition_id="feature_architect", status=phase0_status,
                design_id="des-1", project_id="proj-1",
            ))
    if retry_count is not None:
        from src.autopilot.orchestrator.state import _set_project_context

        with db.session_scope() as session:
            _set_project_context(session, "autopilot_retry_des-1", retry_count)


def _run():
    from src.autopilot.orchestrator.features import _recover_designs_stuck_mid_decomposition

    return _recover_designs_stuck_mid_decomposition(MagicMock())


def _status(db):
    with db.session_scope() as session:
        return session.query(AutopilotDesign).filter_by(id="des-1").first().status


class TestRecovery:
    @pytest.mark.parametrize("stranded_status", ["decomposing", "processing"])
    def test_resets_a_stranded_design_to_pending(self, db_env, stranded_status):
        _seed(db_env, design_status=stranded_status)

        assert _run() == 1
        assert _status(db_env) == "pending", "must go back in the queue pick_next_design reads"

    def test_counts_the_retry_so_it_cannot_loop_forever(self, db_env):
        from src.autopilot.orchestrator.state import _get_project_context
        from src.core.database import get_db

        _seed(db_env)
        _run()

        with get_db() as session:
            assert _get_project_context(session, "autopilot_retry_des-1") == 1

    def test_gives_up_once_past_the_retry_cap(self, db_env):
        from src.autopilot.orchestrator.queue import MAX_DESIGN_RETRIES

        _seed(db_env, retry_count=MAX_DESIGN_RETRIES)

        assert _run() == 1
        assert _status(db_env) == "failed", "a design that keeps dying must not retry forever"

        with db_env.session_scope() as session:
            design = session.query(AutopilotDesign).filter_by(id="des-1").first()
            assert "Gave up" in design.error

    def test_the_give_up_is_not_re_announced_every_tick(self, db_env):
        from src.autopilot.orchestrator.queue import MAX_DESIGN_RETRIES

        _seed(db_env, retry_count=MAX_DESIGN_RETRIES)
        _run()

        assert _run() == 0, "already-failed designs must drop out of the candidate set"


class TestScoping:
    @pytest.mark.parametrize("live_status", ["active", "paused"])
    def test_leaves_a_design_whose_phase0_is_genuinely_running(self, db_env, live_status):
        """Decomposition still in flight -- resetting here would
        double-dispatch it."""
        _seed(db_env, phase0_status=live_status)

        assert _run() == 0
        assert _status(db_env) == "decomposing"

    def test_recovers_when_the_phase0_workflow_is_dead(self, db_env):
        """A failed Phase 0 is exactly the interruption this exists for."""
        _seed(db_env, phase0_status="failed")

        assert _run() == 1
        assert _status(db_env) == "pending"

    @pytest.mark.parametrize("untouched_status", ["pending", "active", "completed", "failed"])
    def test_ignores_designs_not_in_a_transient_status(self, db_env, untouched_status):
        _seed(db_env, design_status=untouched_status)

        assert _run() == 0
        assert _status(db_env) == untouched_status

    def test_ignores_an_archived_design(self, db_env):
        from src.core.database import utc_now

        _seed(db_env)
        with db_env.session_scope() as session:
            session.query(AutopilotDesign).filter_by(id="des-1").first().archived_at = utc_now()

        assert _run() == 0
        assert _status(db_env) == "decomposing"


def test_the_recovery_is_registered_in_the_background_sweep():
    """A recovery nothing calls is not automation."""
    import inspect

    from src.mcp.server.background_loops import _run_phase_advancement_sweep_once

    source = inspect.getsource(_run_phase_advancement_sweep_once)
    assert "_recover_designs_stuck_mid_decomposition(sweep_logger)" in source
