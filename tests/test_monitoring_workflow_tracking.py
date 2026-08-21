"""Characterization tests for MonitoringLoop's two DB-querying helpers.

SOLID review 3.4 extracted `_maybe_switch_tracked_workflow` and
`_log_active_workflow_diagnostics` out of `_monitoring_cycle` as
"verbatim extractions, zero behavior change" -- but nothing tested what
they actually DO. The only tests referencing them
(test_monitoring_cycle_offloading.py) replace both with `Mock()` to
assert they are offloaded to an executor, which pins that they are
*called off the event loop* and nothing about their behaviour.

That left the extraction's "zero behavior change" claim resting on
inspection alone, and left any future decomposition of the 210-line
`_monitoring_cycle` with no safety net for these two.

These use a real DatabaseManager rather than the mocked `mock_db`
fixture the other monitor tests share: both methods are pure
query-and-decide logic, and a mocked session accepts a correct and a
broken query equally, so it would characterize nothing.
"""

import logging
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.core.database import DatabaseManager, Task, Workflow


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "monitor.db"))
    manager.create_tables()
    return manager


class FakePhaseManager:
    """Minimal stand-in exposing only what the method under test touches."""

    def __init__(self, workflow_id=None):
        self.workflow_id = workflow_id
        self.active_workflow = "sentinel-not-none"
        self.load_calls = 0

    def load_active_workflow(self):
        self.load_calls += 1


def _make_loop(db, phase_manager=None):
    from src.monitoring.monitor import MonitoringLoop

    with patch("src.monitoring.monitor.get_config") as cfg:
        cfg.return_value = Mock(
            monitoring=Mock(stuck_detection_minutes=10),
            agents=Mock(agent_timeout_minutes=60),
        )
        loop = MonitoringLoop(
            db_manager=db,
            agent_manager=Mock(),
            llm_provider=AsyncMock(),
        )
    loop.phase_manager = phase_manager
    return loop


def _add_workflow(db, wf_id, status, created_at, name=None):
    with db.session_scope() as s:
        s.add(
            Workflow(
                id=wf_id,
                name=name or wf_id,
                status=status,
                created_at=created_at,
                phases_folder_path="/tmp/phases",
            )
        )


def _add_task(db, task_id, workflow_id, status):
    with db.session_scope() as s:
        s.add(
            Task(
                id=task_id,
                workflow_id=workflow_id,
                raw_description="r",
                done_definition="d",
                status=status,
            )
        )


# ── _maybe_switch_tracked_workflow ──────────────────────────────────


def test_no_phase_manager_is_a_no_op(db):
    """The guard is `self.phase_manager and self.phase_manager.workflow_id`
    -- a monitor running before a phase manager is wired must not raise."""
    loop = _make_loop(db, phase_manager=None)
    loop._maybe_switch_tracked_workflow()  # must not raise


def test_phase_manager_without_workflow_id_is_a_no_op(db):
    _add_workflow(db, "wf-active", "active", datetime(2026, 1, 1))
    pm = FakePhaseManager(workflow_id=None)
    loop = _make_loop(db, pm)

    loop._maybe_switch_tracked_workflow()

    # Must NOT adopt the active workflow -- the method only switches an
    # already-tracked workflow, it does not pick one up from nothing.
    assert pm.workflow_id is None
    assert pm.load_calls == 0


def test_switches_to_a_newer_active_workflow(db):
    """The method's whole reason to exist: the pipeline restarted with a
    new design, so track the new workflow instead of the old one."""
    _add_workflow(db, "wf-old", "completed", datetime(2026, 1, 1))
    _add_workflow(db, "wf-new", "active", datetime(2026, 1, 2))
    pm = FakePhaseManager(workflow_id="wf-old")
    loop = _make_loop(db, pm)

    loop._maybe_switch_tracked_workflow()

    assert pm.workflow_id == "wf-new"
    # active_workflow is cleared to force a reload, then reloaded.
    assert pm.active_workflow is None
    assert pm.load_calls == 1


def test_picks_the_most_recent_active_workflow(db):
    """`order_by(created_at.desc()).first()` -- with several active
    workflows the newest wins, not whichever the DB returns first."""
    _add_workflow(db, "wf-tracked", "completed", datetime(2026, 1, 1))
    _add_workflow(db, "wf-older-active", "active", datetime(2026, 1, 2))
    _add_workflow(db, "wf-newest-active", "active", datetime(2026, 1, 5))
    pm = FakePhaseManager(workflow_id="wf-tracked")
    loop = _make_loop(db, pm)

    loop._maybe_switch_tracked_workflow()

    assert pm.workflow_id == "wf-newest-active"


def test_already_tracking_the_latest_active_workflow_changes_nothing(db):
    _add_workflow(db, "wf-current", "active", datetime(2026, 1, 1))
    pm = FakePhaseManager(workflow_id="wf-current")
    loop = _make_loop(db, pm)

    loop._maybe_switch_tracked_workflow()

    assert pm.workflow_id == "wf-current"
    # No reload churn on the steady-state path -- this runs every cycle.
    assert pm.load_calls == 0
    assert pm.active_workflow == "sentinel-not-none"


@pytest.mark.parametrize("terminal_status", ["completed", "failed", "paused"])
def test_clears_tracking_when_finished_and_nothing_active(db, terminal_status):
    """Second branch: tracked workflow reached a terminal state and there
    is no active workflow to move to -- stop tracking anything."""
    _add_workflow(db, "wf-done", terminal_status, datetime(2026, 1, 1))
    pm = FakePhaseManager(workflow_id="wf-done")
    loop = _make_loop(db, pm)

    loop._maybe_switch_tracked_workflow()

    assert pm.workflow_id is None


def test_finished_workflow_switches_rather_than_clears_when_an_active_one_exists(db):
    """Branch ORDER matters and is easy to invert while refactoring: the
    switch branch is checked first, so a completed tracked workflow with
    another active workflow present SWITCHES to it. Only the
    no-active-workflow case clears. Inverting these would silently stop
    the monitor following the pipeline across a restart."""
    _add_workflow(db, "wf-done", "completed", datetime(2026, 1, 1))
    _add_workflow(db, "wf-live", "active", datetime(2026, 1, 2))
    pm = FakePhaseManager(workflow_id="wf-done")
    loop = _make_loop(db, pm)

    loop._maybe_switch_tracked_workflow()

    assert pm.workflow_id == "wf-live"


def test_tracked_workflow_row_missing_entirely_does_not_raise(db):
    """`tracked_wf` can be None (row deleted by a rerun's clean slate);
    the log line reads `tracked_wf.status if tracked_wf else 'unknown'`."""
    _add_workflow(db, "wf-live", "active", datetime(2026, 1, 2))
    pm = FakePhaseManager(workflow_id="wf-deleted")
    loop = _make_loop(db, pm)

    loop._maybe_switch_tracked_workflow()

    assert pm.workflow_id == "wf-live"


def test_switch_failure_is_swallowed_not_propagated(db):
    """The method wraps everything in `except Exception` and logs -- a
    monitoring cycle must not die because this check failed."""
    pm = FakePhaseManager(workflow_id="wf-x")
    loop = _make_loop(db, pm)
    loop.phase_manager.load_active_workflow = Mock(side_effect=RuntimeError("boom"))
    _add_workflow(db, "wf-newer", "active", datetime(2026, 1, 2))

    loop._maybe_switch_tracked_workflow()  # must not raise


# ── _log_active_workflow_diagnostics ────────────────────────────────


def test_diagnostics_reports_per_status_task_counts(db, caplog):
    """Pins the four counts and that they are scoped per workflow."""
    _add_workflow(db, "wf-a", "active", datetime(2026, 1, 1), name="Alpha")
    _add_task(db, "t1", "wf-a", "done")
    _add_task(db, "t2", "wf-a", "done")
    _add_task(db, "t3", "wf-a", "failed")
    _add_task(db, "t4", "wf-a", "pending")
    _add_task(db, "t5", "wf-a", "in_progress")
    # A second workflow's tasks must not leak into wf-a's counts.
    _add_workflow(db, "wf-b", "active", datetime(2026, 1, 2), name="Beta")
    _add_task(db, "t6", "wf-b", "done")

    loop = _make_loop(db)
    with caplog.at_level(logging.INFO, logger="src.monitoring.monitor"):
        loop._log_active_workflow_diagnostics()

    text = "\n".join(r.message for r in caplog.records)
    assert "Active workflows in database: 2" in text
    # wf-a: 5 total, 2 done, 1 failed, 2 active (pending + in_progress)
    assert "Alpha" in text and "5 total: 2 done, 1 failed, 2 active" in text
    assert "Beta" in text and "1 total: 1 done, 0 failed, 0 active" in text


def test_diagnostics_ignores_non_active_workflows(db, caplog):
    _add_workflow(db, "wf-done", "completed", datetime(2026, 1, 1))
    _add_workflow(db, "wf-live", "active", datetime(2026, 1, 2))

    loop = _make_loop(db)
    with caplog.at_level(logging.INFO, logger="src.monitoring.monitor"):
        loop._log_active_workflow_diagnostics()

    text = "\n".join(r.message for r in caplog.records)
    assert "Active workflows in database: 1" in text


def test_diagnostics_mutates_nothing(db):
    """Explicitly diagnostic-only -- it must never write. Guards against a
    future refactor folding a decision into this logging path."""
    _add_workflow(db, "wf-a", "active", datetime(2026, 1, 1))
    _add_task(db, "t1", "wf-a", "pending")

    loop = _make_loop(db)
    loop._log_active_workflow_diagnostics()

    with db.session_scope() as s:
        assert s.query(Workflow).filter_by(id="wf-a").first().status == "active"
        assert s.query(Task).filter_by(id="t1").first().status == "pending"
        assert s.query(Task).count() == 1


def test_diagnostics_with_no_active_workflows_does_not_raise(db, caplog):
    loop = _make_loop(db)
    with caplog.at_level(logging.INFO, logger="src.monitoring.monitor"):
        loop._log_active_workflow_diagnostics()

    assert "Active workflows in database: 0" in "\n".join(
        r.message for r in caplog.records
    )
