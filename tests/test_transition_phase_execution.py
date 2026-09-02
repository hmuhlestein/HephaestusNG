"""Step 2 of docs/designs/PHASE_EXECUTION_STATE_MACHINE_REFACTOR.md:
transition_phase_execution -- a single atomic function + explicit
transition table, additive and not yet wired into any existing call
site. See that document for why the transition must be a single
UPDATE ... WHERE status = :from_status rather than SELECT-then-mutate.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from src.autopilot.orchestrator import phase_transitions as pt
from src.core.database import DatabaseManager, Phase, PhaseExecution, Workflow


@pytest.fixture
def db_manager(tmp_path):
    db = DatabaseManager(str(tmp_path / "test.db"))
    db.create_tables()
    return db


def _seed(db_manager, status: str, **extra):
    with db_manager.session_scope() as session:
        session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
        session.add(Phase(id="phase-1", workflow_id="wf-1", order=1, name="development", description="d", done_definitions=["x"]))
        session.add(PhaseExecution(id="exec-1", phase_id="phase-1", status=status, **extra))


class TestValidTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("pending", "in_progress"),
            ("pending", "skipped"),
            ("in_progress", "completed"),
            ("in_progress", "failed"),
            ("in_progress", "pending"),
            ("completed", "in_progress"),
            ("completed", "pending"),
            ("failed", "in_progress"),
            ("failed", "pending"),
            ("skipped", "in_progress"),
        ],
    )
    def test_allowed_transition_succeeds(self, db_manager, from_status, to_status):
        _seed(db_manager, status=from_status)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", to_status, reason="test")
            assert result is not None
            assert result.status == to_status


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            ("pending", "completed"),
            ("pending", "failed"),
            ("completed", "failed"),
            ("completed", "skipped"),
            ("failed", "completed"),
            ("failed", "skipped"),
            ("skipped", "pending"),
            ("skipped", "completed"),
            ("skipped", "failed"),
        ],
    )
    def test_disallowed_transition_returns_none_and_does_not_mutate(self, db_manager, from_status, to_status):
        _seed(db_manager, status=from_status)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", to_status, reason="test")
            assert result is None

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == from_status


class TestMissingExecution:
    def test_raises_when_no_phase_execution_exists(self, db_manager):
        with db_manager.session_scope() as session:
            session.add(Workflow(id="wf-1", name="w", phases_folder_path="/tmp", status="active"))
            session.add(Phase(id="phase-1", workflow_id="wf-1", order=1, name="development", description="d", done_definitions=["x"]))

        with db_manager.session_scope() as session:
            with pytest.raises(ValueError, match="No PhaseExecution for phase phase-1"):
                pt.transition_phase_execution(session, "phase-1", "in_progress", reason="test")


class TestAtomicRace:
    def test_loses_the_race_cleanly_when_status_changes_between_read_and_update(self, db_manager, monkeypatch):
        """Simulates the exact race this function exists to close: between
        this call's own read of from_status and its own atomic UPDATE, a
        concurrent writer (a separate connection) already moved the row.
        The UPDATE's WHERE status=:from_status must then match zero rows,
        so this caller loses cleanly instead of stomping the interloper's
        write."""
        _seed(db_manager, status="pending")

        with db_manager.session_scope() as session:
            original_query = session.query
            calls = {"n": 0}

            def query_spy(*args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 2:
                    # This is the moment transition_phase_execution is about
                    # to issue its own atomic UPDATE, having already read
                    # from_status='pending' via the first query() call above.
                    # Land a concurrent write through a separate connection
                    # right now, before that UPDATE executes.
                    raw = sqlite3.connect(db_manager.database_path)
                    raw.execute(
                        "UPDATE phase_executions SET status = 'in_progress' WHERE phase_id = 'phase-1'"
                    )
                    raw.commit()
                    raw.close()
                return original_query(*args, **kwargs)

            monkeypatch.setattr(session, "query", query_spy)
            result = pt.transition_phase_execution(session, "phase-1", "in_progress", reason="test")

        assert result is None

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"  # interloper's write, untouched


class TestFieldResets:
    def test_completed_to_pending_clears_timestamps_and_claim(self, db_manager):
        now = datetime.utcnow()
        _seed(db_manager, status="completed", started_at=now, completed_at=now, task_creation_claimed_at=now)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "pending", reason="goto")
            assert result.started_at is None
            assert result.completed_at is None
            assert result.task_creation_claimed_at is None

    def test_pending_to_in_progress_sets_started_at_and_clears_claim(self, db_manager):
        claimed = datetime.utcnow() - timedelta(minutes=5)
        _seed(db_manager, status="pending", task_creation_claimed_at=claimed)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "in_progress", reason="dispatch")
            assert result.started_at is not None
            assert result.task_creation_claimed_at is None

    def test_in_progress_to_completed_leaves_started_at_untouched_no_reset_listed(self, db_manager):
        started = datetime.utcnow() - timedelta(minutes=10)
        _seed(db_manager, status="in_progress", started_at=started)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "completed", reason="done")
            assert result.started_at == started
            assert result.completed_at is None  # not in _FIELD_RESETS -- caller sets it separately

    def test_skipped_to_in_progress_sets_started_at(self, db_manager):
        _seed(db_manager, status="skipped")
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "in_progress", reason="goto-back")
            assert result.started_at is not None

    def test_completed_to_in_progress_clears_claim(self, db_manager):
        """reopen_phase_execution (phase_transitions.py) clears
        task_creation_claimed_at unconditionally on every reopen to
        in_progress, regardless of the from-status -- this table must
        match that for every X -> in_progress entry, not just
        (pending, in_progress), or a phase reopened from completed/failed/
        skipped would keep a stale claim and stay invisible to
        _case_in_progress_complete's own claim-gated evaluation forever."""
        claimed = datetime.utcnow() - timedelta(minutes=5)
        _seed(db_manager, status="completed", task_creation_claimed_at=claimed)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "in_progress", reason="goto-reentry")
            assert result.task_creation_claimed_at is None

    def test_failed_to_in_progress_clears_claim(self, db_manager):
        claimed = datetime.utcnow() - timedelta(minutes=5)
        _seed(db_manager, status="failed", task_creation_claimed_at=claimed)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "in_progress", reason="retry")
            assert result.task_creation_claimed_at is None

    def test_skipped_to_in_progress_clears_claim(self, db_manager):
        claimed = datetime.utcnow() - timedelta(minutes=5)
        _seed(db_manager, status="skipped", task_creation_claimed_at=claimed)
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "in_progress", reason="goto-back")
            assert result.task_creation_claimed_at is None

    def test_pending_to_skipped_sets_completed_at(self, db_manager):
        _seed(db_manager, status="pending")
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "skipped", reason="condition-not-met")
            assert result.completed_at is not None


class TestExtraFields:
    """extra_fields lets a call site (e.g. _close_execution) atomically set
    fields the transition table itself doesn't otherwise touch, such as
    completion_summary, in the same UPDATE as the status change."""

    def test_extra_fields_are_written_atomically_with_the_transition(self, db_manager):
        _seed(db_manager, status="in_progress")
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(
                session, "phase-1", "completed", reason="test",
                extra_fields={"completion_summary": "all good"},
            )
            assert result.status == "completed"
            assert result.completion_summary == "all good"

    def test_extra_fields_are_not_applied_when_the_transition_is_invalid(self, db_manager):
        """A caller's summary must not land on a row this call didn't
        actually touch."""
        _seed(db_manager, status="skipped")
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(
                session, "phase-1", "completed", reason="test",
                extra_fields={"completion_summary": "should not stick"},
            )
            assert result is None

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.completion_summary is None

    def test_no_extra_fields_behaves_as_before(self, db_manager):
        _seed(db_manager, status="in_progress")
        with db_manager.session_scope() as session:
            result = pt.transition_phase_execution(session, "phase-1", "completed", reason="test")
            assert result.status == "completed"
            assert result.completion_summary is None
