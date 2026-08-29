"""Tests for orchestrator._advance_phases and related phase transition functions.

These tests address the critical test coverage gap identified in ARCHITECTURE_REVIEW.md:
"_advance_phases has no test referencing it anywhere in tests/"
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

from src.core.database import (
    Agent,
    AutopilotProject,
    DatabaseManager,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
    WorkflowDefinition,
)


def _agent_row_side_effect(agent_id="new-agent-1"):
    """Return a side_effect for create_agent_for_task_direct that also
    inserts an Agent row into the DB, satisfying FK constraints.
    Uses task_id as suffix for unique agent_id per call."""
    def _side_effect(task_id, workflow_id, phase_id, **kwargs):
        from src.core.database import get_db
        # Use full task_id so multiple calls produce unique IDs/tmux names
        aid = f"{agent_id}-{task_id}".replace(" ", "-")
        with get_db() as db:
            db.add(Agent(
                id=aid,
                system_prompt="test",
                status="working",
                cli_type="pi",
                tmux_session_name=f"tmux-{aid[:32]}",
                current_task_id=task_id,
            ))
            db.commit()
        return {"agent_id": aid}
    return _side_effect


@pytest.fixture(autouse=True)
def _seed_sentinel_agents(db_manager):
    """Insert sentinel agent rows that the orchestrator references by
    constant string (ARBITRATION_CREATED_BY, _orchestrator_agent_id).
    Without these, any code path that sets
    task.created_by_agent_id=ARBITRATION_CREATED_BY FK-fails."""
    from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
    with db_manager.session_scope() as session:
        for aid in (ARBITRATION_CREATED_BY, "orchestrator"):
            if not session.query(Agent).filter_by(id=aid).first():
                session.add(Agent(
                    id=aid,
                    system_prompt="sentinel",
                    status="idle",
                    cli_type="system",
                ))


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    """Create a test database manager.

    _advance_phases and friends open their own session via the module-level
    get_db(), which reads HEPHAESTUS_TEST_DB — point it at the same sqlite
    file this fixture's DatabaseManager uses, or those functions would
    silently read/write the default hephaestus.db instead of test data.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def sample_workflow(db_manager):
    """Create a sample workflow with phases."""
    with db_manager.session_scope() as session:
        # Create workflow
        wf = Workflow(
            id="wf-1",
            name="Test Workflow",
            status="active",
            phases_folder_path="/tmp",
        )
        session.add(wf)

        # Create phases
        phase1 = Phase(
            id="phase-1",
            workflow_id="wf-1",
            name="requirements",
            order=1,
            description="Gather requirements",
            done_definitions=["requirements documented"],
        )
        phase2 = Phase(
            id="phase-2",
            workflow_id="wf-1",
            name="implementation",
            order=2,
            description="Implement the feature",
            done_definitions=["code written and tested"],
        )
        session.add(phase1)
        session.add(phase2)

        # Create phase executions
        exec1 = PhaseExecution(
            id="exec-1",
            phase_id="phase-1",
            workflow_execution_id="wf-1",
            status="in_progress",
        )
        session.add(exec1)

        return wf


class TestClaimPhaseTaskCreation:
    """Tests for _claim_phase_task_creation, the atomic guard that stops two
    independent code paths (server.py's synchronous initial-task creation
    and the orchestrator's background self-heal) from both creating a
    phase's first task."""

    def test_first_caller_wins(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True

    def test_second_caller_loses(self, db_manager, sample_workflow):
        """The core guarantee: only one of two callers racing for the same
        phase can ever win the claim, regardless of ordering."""
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            first = _claim_phase_task_creation(session, "phase-1")
        with db_manager.session_scope() as session:
            second = _claim_phase_task_creation(session, "phase-1")

        assert first is True
        assert second is False

    def test_different_phases_both_win(self, db_manager, sample_workflow):
        """Claims are scoped per phase -- unrelated phases don't block each other."""
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            session.add(
                PhaseExecution(
                    id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1",
                    status="pending",
                )
            )

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True
        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-2") is True


class TestReleasePhaseTaskCreationClaim:
    """Tests for _release_phase_task_creation_claim.

    Regression: server.py's /start_workflow_execution creates phase 1's
    initial task via the generic /create_task handler, which has no
    knowledge of PhaseExecution bookkeeping at all -- unlike
    _create_phase_task (used for every phase after this one), it never
    flipped execution.status to "in_progress" or cleared
    task_creation_claimed_at. Left unset, the claim from
    _claim_phase_task_creation stayed held forever: _case_in_progress_complete
    reuses that same field as a guard against evaluating a phase transition
    while another caller is mid-creation, so a permanently-held claim
    silently blocked phase 1 from ever being recognized as complete, no
    matter how many times its task actually finished. Observed live: phase
    1's task completed successfully but the pipeline never advanced to
    phase 2, indefinitely, for every UI-launched workflow.
    """

    def test_clears_claim_and_marks_in_progress(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.phase_transitions import (
    _claim_phase_task_creation,
    _release_phase_task_creation_claim,
)

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "pending"

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True

        with db_manager.session_scope() as session:
            _release_phase_task_creation_claim(session, "phase-1")

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert exec1.task_creation_claimed_at is None
            assert exec1.status == "in_progress"
            assert exec1.started_at is not None

    def test_missing_phase_execution_is_a_noop(self, db_manager, sample_workflow):
        """No PhaseExecution row for this phase yet -- must not raise."""
        from src.autopilot.orchestrator.phase_transitions import _release_phase_task_creation_claim

        with db_manager.session_scope() as session:
            _release_phase_task_creation_claim(session, "nonexistent-phase")

    def test_already_in_progress_status_left_untouched(self, db_manager, sample_workflow):
        """Only flip pending/completed -> in_progress -- don't stomp a status
        this call didn't set (e.g. a race where something else already
        advanced it further)."""
        from src.autopilot.orchestrator.phase_transitions import (
    _claim_phase_task_creation,
    _release_phase_task_creation_claim,
)

        with db_manager.session_scope() as session:
            _claim_phase_task_creation(session, "phase-1")
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "in_progress"
            original_started_at = exec1.started_at

        with db_manager.session_scope() as session:
            _release_phase_task_creation_claim(session, "phase-1")

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert exec1.task_creation_claimed_at is None
            assert exec1.status == "in_progress"
            assert exec1.started_at == original_started_at

    def test_clears_claim_when_execution_was_already_loaded_in_the_same_session(
        self, db_manager, sample_workflow
    ):
        """Regression: found via test_maybe_retry_failed_tasks_is_claim_
        protected. This project's sessions run expire_on_commit=False, and
        _claim_phase_task_creation's own claiming UPDATE uses
        synchronize_session=False -- so if the PhaseExecution row was
        already loaded into the SAME session's identity map before the
        claim was taken (exactly what every real caller does: _advance_
        phases reads it via _get_phase_statuses, then later claims and
        releases on that same session), a plain query in this function
        returned that stale in-memory object, whose task_creation_
        claimed_at still showed the pre-claim value -- writing None over
        an attribute that already read as None isn't a change SQLAlchemy
        persists, so the claim silently stayed held in the database
        forever. Unlike the tests above, this one deliberately keeps
        claim + release on ONE session with a pre-load in between, since
        the bug only reproduces when they share a session."""
        from src.autopilot.orchestrator.phase_transitions import (
    _claim_phase_task_creation,
    _release_phase_task_creation_claim,
)

        with db_manager.session_scope() as session:
            # The pre-load every real caller does (e.g. _get_phase_statuses)
            # before ever taking the claim.
            preloaded = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert preloaded.task_creation_claimed_at is None

            assert _claim_phase_task_creation(session, "phase-1") is True
            _release_phase_task_creation_claim(session, "phase-1")

            # Re-query within the SAME session -- must reflect the release,
            # not the stale pre-loaded object's view.
            refetched = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert refetched.task_creation_claimed_at is None

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert exec1.task_creation_claimed_at is None  # actually persisted to the DB

    def test_started_at_anchors_to_the_guarded_tasks_created_at_not_now(
        self, db_manager, sample_workflow
    ):
        """Regression: server.py's /start_workflow_execution claims, then
        calls create_task() (which can spend real seconds on enrichment /
        embedding / dedup / capacity-queue checks) before this function ever
        runs -- so datetime.utcnow() at release time is always later than
        the task it's releasing the claim for, sometimes by several
        seconds. _case_in_progress_complete uses execution.started_at as
        cycle_start and filters tasks with `Task.created_at >= cycle_start`
        to find tasks in the phase's current cycle -- stamping "now" here
        put cycle_start after the very task this call exists to release,
        so that task fell outside its own cycle and the phase looked
        task-less, spawning a duplicate. started_at must anchor to the
        task's own created_at instead. Observed live: a UI-launched
        workflow's phase 1 task completed successfully while a duplicate
        self-heal task sat pending beside it, created ~15s later."""
        from src.autopilot.orchestrator.phase_transitions import (
    _claim_phase_task_creation,
    _release_phase_task_creation_claim,
)

        task_created_at = datetime.utcnow() - timedelta(seconds=15)
        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "pending"
            # Mirrors server.py's create_task(): the task is committed
            # well before the claim is released, same as the real
            # enrichment/dedup/capacity-queue pipeline taking real time.
            session.add(Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="pending",
                created_at=task_created_at,
            ))

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True

        with db_manager.session_scope() as session:
            _release_phase_task_creation_claim(session, "phase-1")

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert exec1.started_at == task_created_at
            task = session.query(Task).filter_by(id="task-1").first()
            assert exec1.started_at <= task.created_at


class TestPhaseHasArbitrationInFlight:
    """Tests for _phase_has_arbitration_in_flight, the shared guard that
    stops _case_in_progress_complete's and _create_phase_task's own
    unconditional claim-release finally blocks from wiping out a claim
    _trigger_arbitration is still relying on -- see its own docstring for
    the live incident (workflow a7695dc5) this closes."""

    def test_no_tasks_at_all_is_not_in_flight(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.phase_transitions import _phase_has_arbitration_in_flight

        with db_manager.session_scope() as session:
            assert _phase_has_arbitration_in_flight(session, "phase-1") is False

    def test_non_arbitration_task_is_not_in_flight(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.phase_transitions import _phase_has_arbitration_in_flight

        with db_manager.session_scope() as session:
            session.add(Task(
                id="task-1", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="pending",
            ))

        with db_manager.session_scope() as session:
            assert _phase_has_arbitration_in_flight(session, "phase-1") is False

    def test_pending_arbitration_task_is_in_flight(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _phase_has_arbitration_in_flight

        with db_manager.session_scope() as session:
            session.add(Task(
                id="task-1", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="pending",
                created_by_agent_id=ARBITRATION_CREATED_BY,
            ))

        with db_manager.session_scope() as session:
            assert _phase_has_arbitration_in_flight(session, "phase-1") is True

    def test_done_arbitration_task_is_not_in_flight(self, db_manager, sample_workflow):
        """Already resolved -- _resolve_arbitration_outcome's own finally
        owns clearing the claim from here on, not this guard."""
        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _phase_has_arbitration_in_flight

        with db_manager.session_scope() as session:
            session.add(Task(
                id="task-1", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="done",
                created_by_agent_id=ARBITRATION_CREATED_BY,
            ))

        with db_manager.session_scope() as session:
            assert _phase_has_arbitration_in_flight(session, "phase-1") is False

    def test_failed_arbitration_dispatch_is_not_in_flight(self, db_manager, sample_workflow):
        """A failed dispatch is a terminal, already-handled outcome
        (_trigger_arbitration's own force_action="fail" path) -- must not
        be mistaken for still-running and hold the claim hostage forever."""
        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _phase_has_arbitration_in_flight

        with db_manager.session_scope() as session:
            session.add(Task(
                id="task-1", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="failed",
                created_by_agent_id=ARBITRATION_CREATED_BY,
            ))

        with db_manager.session_scope() as session:
            assert _phase_has_arbitration_in_flight(session, "phase-1") is False

    def test_only_the_most_recent_task_governs(self, db_manager, sample_workflow):
        """An old, already-done arbitration task followed by a normal
        (non-arbitration) task must not read as in-flight."""
        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _phase_has_arbitration_in_flight

        with db_manager.session_scope() as session:
            session.add(Task(
                id="task-old-arb", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="done",
                created_by_agent_id=ARBITRATION_CREATED_BY,
                created_at=datetime.utcnow() - timedelta(minutes=5),
            ))
            session.add(Task(
                id="task-new", workflow_id="wf-1", phase_id="phase-1",
                raw_description="r", done_definition="d", status="in_progress",
                created_at=datetime.utcnow(),
            ))

        with db_manager.session_scope() as session:
            assert _phase_has_arbitration_in_flight(session, "phase-1") is False


class TestAdvancePhases:
    """Tests for _advance_phases function."""

    def test_returns_false_when_workflow_not_found(self, db_manager):
        """Should return False when workflow doesn't exist."""
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        logger = MagicMock()
        result = _advance_phases("nonexistent-wf", logger)
        assert result is False

    def test_returns_false_when_workflow_paused(self, db_manager, sample_workflow):
        """Should return False when workflow is paused (no done tasks)."""
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        # Pause the workflow
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"

        logger = MagicMock()
        result = _advance_phases("wf-1", logger)
        assert result is False

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_auto_resumes_paused_workflow_with_done_task(self, mock_create_agent, db_manager, sample_workflow):
        """Should auto-resume paused workflow if it has a done task in stalled phase."""
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        mock_create_agent.return_value = {"agent_id": "new-agent-1"}

        # Pause the workflow and add a done task
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"

            task = Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="Test task",
                done_definition="Done when complete",
                status="done",
            )
            session.add(task)

        logger = MagicMock()
        _advance_phases("wf-1", logger)

        # Verify workflow was resumed
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"

    def test_does_not_auto_resume_a_user_paused_workflow(self, db_manager, sample_workflow):
        """Regression: a deliberate user pause (paused_by="user", set by the
        /workflow-executions/{id}/stop endpoint) must not be silently
        reverted just because its stalled phase happens to have a done
        task -- that's a state pausing itself commonly produces (the
        running task finishes right after being told to stop). Before this
        check, the background sweep would flip the workflow back to
        "active" on its very next tick (~20s later), fighting the user's
        pause until whatever made the phase look stalled resolved on its
        own."""
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "user"

            task = Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="Test task",
                done_definition="Done when complete",
                status="done",
            )
            session.add(task)

        logger = MagicMock()
        result = _advance_phases("wf-1", logger)
        assert result is False

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_review_paused_workflow_still_retries_an_unrelated_phase(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Regression: paused_by="review" means one specific phase
        (MANUAL_ONLY_PHASES, i.e. git_expert) is waiting on a human --
        it must not freeze every OTHER in-progress phase too. Before this
        fix, the top-level `if wf.status == "paused": return False` gate
        short-circuited _advance_phases entirely regardless of paused_by,
        so a workflow paused for git_expert approval silently stopped
        retrying/self-healing every unrelated phase as well. Observed
        live: task a1efdda6 (an adversarial_review-phase task, nothing to
        do with git_expert) sat orphaned and was never retried while
        the workflow was paused for a later phase's approval gate.

        phase-1 here is "requirements" -- not in MANUAL_ONLY_PHASES -- so a
        stale orphaned task on it must still self-heal (failed -> retried)
        even while the workflow sits paused_by="review" for an unrelated
        reason."""
        from src.autopilot.orchestrator.phase_transitions import _advance_phases
        from src.core.database import Agent, Task as _Task

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "review"
            wf.status_reason = "git_expert is manual-only; human approval is required"

            session.add(Agent(id="dead-agent", system_prompt="p", status="terminated", cli_type="pi"))
            session.add(Agent(id="fresh-agent", system_prompt="p", status="working", cli_type="pi"))
            session.add(Task(
                id="task-orphaned-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="pending",
                assigned_agent_id="dead-agent",
                created_at=datetime.utcnow() - timedelta(minutes=5),
            ))
        mock_create_agent.return_value = {"agent_id": "fresh-agent"}

        logger = MagicMock()
        _advance_phases("wf-1", logger)

        with db_manager.session_scope() as session:
            task = session.query(_Task).filter_by(id="task-orphaned-1").first()
            assert task.status == "in_progress"
            assert task.assigned_agent_id == "fresh-agent"

            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "review"


class TestCaseStartFirstPhase:
    """Tests for _case_start_first_phase function."""

    def test_starts_first_phase_when_no_progress(self, db_manager, sample_workflow):
        """Should start first phase when no phases are in progress or completed."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_start_first_phase,
    _get_phase_statuses,
)

        with db_manager.session_scope() as session:
            # Reset phase execution to pending
            exec = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            if exec:
                exec.status = "pending"

            phase_statuses = _get_phase_statuses(session, "wf-1")
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            completed = [p for p in phase_statuses if p["status"] == "completed"]

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_start_first_phase(session, "wf-1", pending, in_progress, completed, logger)
                assert result is True
                mock_create.assert_called_once()


    def test_race_guard_skips_duplicate_when_other_path_wins_the_claim(
        self, db_manager, sample_workflow
    ):
        """Regression: /start_workflow_execution creates phase 1's initial
        task synchronously, but _advance_phases's polling loop can also
        decide to create phase 1's task independently, racing it. A plain
        Task.count()==0 check (even with a sleep-and-retry) isn't safe --
        both sides can observe zero tasks. Observed live: a duplicate
        task+agent got spawned for the same phase, burning a full agent run
        duplicating work the first task had already completed.

        _case_start_first_phase now closes this by construction: it must
        win an atomic claim (PhaseExecution.task_creation_claimed_at) before
        creating a task. Simulates the other code path winning that claim
        first -- _case_start_first_phase must see the lost claim and skip.
        """
        from src.autopilot.orchestrator.phase_transitions import (
    _case_start_first_phase,
    _claim_phase_task_creation,
    _get_phase_statuses,
)

        with db_manager.session_scope() as session:
            exec = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            if exec:
                exec.status = "pending"

            phase_statuses = _get_phase_statuses(session, "wf-1")
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            completed = [p for p in phase_statuses if p["status"] == "completed"]

        # Simulate /start_workflow_execution's synchronous path winning the
        # claim first (it hasn't created the Task row yet -- e.g. still
        # queued behind agent creation -- which is exactly the live scenario
        # this regression came from).
        with db_manager.session_scope() as session:
            won = _claim_phase_task_creation(session, "phase-1")
            assert won is True

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_start_first_phase(
                    session, "wf-1", pending, in_progress, completed, logger
                )
                assert result is None, (
                    "should not create a duplicate task once the other "
                    "path has already won the task-creation claim"
                )
                mock_create.assert_not_called()

    def test_reverifies_existing_right_before_creating(self, db_manager, sample_workflow):
        """Regression, observed live: two Task rows (ed82ce49, 83e86c54) for
        the same brand-new phase 1, ~15s apart -- the original existing==0
        read happens BEFORE the claim attempt, so a task committed by the
        OTHER claim-protected path (workflow_execution_routes.py's own
        initial-task flow, on a separate DB connection/session) in the
        window between that read and this call winning ITS OWN claim would
        previously go unnoticed: the stale existing==0 snapshot survives
        untouched all the way to _create_phase_task. Simulates that window
        by having the claim call itself, as a side effect of "winning", also
        commit a competing Task row -- exactly what a slow concurrent
        creator finishing in that gap would look like from this function's
        perspective."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_start_first_phase,
    _get_phase_statuses,
)

        with db_manager.session_scope() as session:
            exec = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            if exec:
                exec.status = "pending"

            phase_statuses = _get_phase_statuses(session, "wf-1")
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            completed = [p for p in phase_statuses if p["status"] == "completed"]

        def _win_claim_but_race_a_task_in(*_args, **_kwargs):
            with db_manager.session_scope() as race_session:
                race_session.add(Task(
                    id="task-raced-in",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                ))
            return True

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._claim_phase_task_creation",
            side_effect=_win_claim_but_race_a_task_in,
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True
        ) as mock_create, patch(
            "src.autopilot.orchestrator.phase_transitions._release_phase_task_creation_claim"
        ) as mock_release:
            with db_manager.session_scope() as session:
                result = _case_start_first_phase(
                    session, "wf-1", pending, in_progress, completed, logger
                )
                assert result is None, (
                    "must not create a duplicate task for a phase that "
                    "picked up a task in the window between the initial "
                    "existing==0 read and winning the claim"
                )
                mock_create.assert_not_called()
                mock_release.assert_called_once_with(session, "phase-1")


class TestCaseInProgressNoTasks:
    """Tests for _case_in_progress_no_tasks function."""

    def test_creates_task_for_phase_without_tasks(self, db_manager, sample_workflow):
        """Should create task when in-progress phase has no tasks."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_no_tasks

        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            in_progress = [{"phase": phase, "status": "in_progress"}]

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is True
                mock_create.assert_called_once()

    def test_creates_task_when_only_a_stale_terminal_task_predates_this_cycle(
        self, db_manager, sample_workflow
    ):
        """Regression, observed live: workflow b7bd02cc's git_expert phase
        sat "in_progress" for hours with deploy never budging, because its
        only task was "duplicated" -- left behind when a ticket-blocked
        git_expert task got routed to development to fix the ticket (see
        _retry_failed_tasks/_maybe_retry_failed_tasks's routing branch).
        The old unscoped `Task.filter_by(phase_id=...).count()` counted
        that stale, terminal task forever, so this case never saw
        task_count == 0 and never dispatched a fresh one -- exactly the
        "stale count blocks fresh dispatch forever" class
        _case_completed_with_successor's own cycle_filter already guards
        against (see its "product_validation stalled 2+ days" note); this
        case had no equivalent."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_no_tasks

        cycle_start = datetime.utcnow()
        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.started_at = cycle_start
            session.add(Task(
                id="task-stale-duplicated",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="duplicated",
                created_at=cycle_start - timedelta(minutes=10),
            ))
            in_progress = [{"phase": phase, "execution": execution, "status": "in_progress"}]

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is True
                mock_create.assert_called_once()

    def test_does_not_duplicate_a_real_task_that_predates_cycle_start_by_a_few_ms(
        self, db_manager, sample_workflow
    ):
        """Regression, observed live: workflow 2ee7f496's product_requirements
        phase had a genuine, real task (created via the UI-launched
        synchronous /start_workflow_execution step) sitting "pending" only
        ~12ms before execution.started_at was stamped (an independent
        utc_now() call a moment later -- see _correct_skewed_cycle_start's
        own docstring) -- the strict `Task.created_at >= execution.
        started_at` cycle_filter excluded it, this case saw task_count == 0,
        and created a SECOND task 15s later. Unlike the stale-duplicated-
        task case above, this task is genuinely live (status="pending", no
        terminal status to exclude) -- only the clock-skew correction
        closes this, not the "duplicated" exclusion."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_no_tasks, _get_phase_statuses

        real_task_created_at = datetime.utcnow()
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.started_at = real_task_created_at + timedelta(milliseconds=12)
            session.add(Task(
                id="task-real-just-created",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="pending",
                created_at=real_task_created_at,
            ))

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is None
                mock_create.assert_not_called()

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.started_at <= real_task_created_at

    def test_does_not_duplicate_a_real_task_when_skew_exceeds_the_correction_window(
        self, db_manager, sample_workflow
    ):
        """Regression, observed live: workflow 0be376f2's product_requirements
        phase got a duplicate task (abf3f36f) ~15s after the real one
        (d66b39ab), despite _correct_skewed_cycle_start existing
        specifically to catch this. Root cause: PhaseManager._start_phase
        stamps execution.started_at = utc_now() at WORKFLOW CREATION time
        (before the real task even exists) and flips status to
        "in_progress" -- so _release_phase_task_creation_claim's own
        anchoring-to-the-real-task's-created_at is gated on status in
        ("pending", "completed", "skipped"), which is already false by the
        time it runs, and never fires. If the resulting skew exceeds
        _correct_skewed_cycle_start's 10s window (a slow enrichment/
        embedding/dedup pass in create_task easily can), the cycle-scoped
        task_count read misses the real task -- and so does a re-check
        that reuses the same cycle_filter. This is the case the re-check's
        own unscoped fix (mirroring _case_start_first_phase's identical
        fix) exists for: it must catch the real task regardless of
        whatever skew fooled the cycle-scoped read above it."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_no_tasks, _get_phase_statuses

        real_task_created_at = datetime.utcnow()
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # Beyond _correct_skewed_cycle_start's 10s skew_floor -- this
            # skew survives that correction and reaches the cycle-scoped
            # task_count read (and, without this fix, the re-check) unfixed.
            execution.started_at = real_task_created_at + timedelta(seconds=15)
            session.add(Task(
                id="task-real-just-created",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="pending",
                created_at=real_task_created_at,
            ))

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is None
                mock_create.assert_not_called()

        with db_manager.session_scope() as session:
            # The real task must still be the only one -- no duplicate,
            # and the claim taken (since task_count's own scoped read saw
            # zero) must have been released again by the unscoped re-check.
            tasks = session.query(Task).filter_by(phase_id="phase-1").all()
            assert len(tasks) == 1
            assert tasks[0].id == "task-real-just-created"
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    def test_creates_task_when_the_only_task_is_duplicated_even_within_the_cycle(
        self, db_manager, sample_workflow
    ):
        """Regression, observed live: workflow 81b399c7's git_expert phase
        stuck "in_progress" with only a "duplicated" task (from the same
        ticket-blocked routing as the stale-terminal-task case above), but
        this time execution.started_at was NEVER refreshed past the
        duplicated task's own created_at -- something set status=
        "in_progress" directly without going through reopen_phase_
        execution/_create_phase_task's own reopening logic (which always
        stamps started_at="now"), so the cycle_filter's `>=` boundary
        stayed stale and the duplicated task still satisfied it. Excluding
        status="duplicated" from the count directly closes this regardless
        of why started_at didn't advance.

        Exercises BOTH count sites in this function, not just the first:
        with the claim mocked to succeed (matching the real live case),
        this reaches the TOCTOU re-check a few lines below the initial
        count too -- that second query had its own, separate copy of the
        same unfiltered `Task.filter_by(phase_id=...)` count, missed on
        the first pass at this fix, which wrongly treated the duplicated
        task as "a real task raced in" and released the claim without
        creating anything. This test fails if either site regresses."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_no_tasks

        cycle_start = datetime.utcnow()
        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.started_at = cycle_start
            session.add(Task(
                id="task-duplicated-in-cycle",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="r",
                done_definition="d",
                status="duplicated",
                created_at=cycle_start,
            ))
            in_progress = [{"phase": phase, "execution": execution, "status": "in_progress"}]

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is True
                mock_create.assert_called_once()

    def test_releases_its_own_claim_when_a_task_raced_in(self, db_manager, sample_workflow):
        """Same TOCTOU gap as _case_start_first_phase's own regression
        (see test_reverifies_existing_right_before_creating): a task
        committed by another claim-protected path in the window between
        this function's initial task_count read and winning its own claim
        must be caught by a fresh re-check -- but the fix must also
        release the claim it just won when that happens, or it stays held
        (up to CLAIM_STALE_TIMEOUT_SECONDS) for no reason, since
        _create_phase_task's own success path (which normally releases
        it) is never reached."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_no_tasks

        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            in_progress = [{"phase": phase, "execution": execution, "status": "in_progress"}]

        def _win_claim_but_race_a_task_in(*_args, **_kwargs):
            with db_manager.session_scope() as race_session:
                race_session.add(Task(
                    id="task-raced-in",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                ))
            return True

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions._claim_phase_task_creation",
            side_effect=_win_claim_but_race_a_task_in,
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True
        ) as mock_create, patch(
            "src.autopilot.orchestrator.phase_transitions._release_phase_task_creation_claim"
        ) as mock_release:
            with db_manager.session_scope() as session:
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is None
                mock_create.assert_not_called()
                mock_release.assert_called_once_with(session, "phase-1")


class TestMaybeRetryFailedTasks:
    """Tests for _maybe_retry_failed_tasks function."""

    def test_git_expert_retries_normally_regardless_of_review_mode(self, db_manager, sample_workflow):
        """git_expert dispatches and retries like any other phase in
        both full autopilot and review mode -- the agent commits, pushes,
        and opens a PR either way; scripts/agent-safe-bin/git (not this
        retry path) is what actually blocks merge/push-to-main pending
        human approval."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1", review_mode=True))
            workflow = session.query(Workflow).filter_by(id="wf-1").first()
            workflow.project_id = "proj-1"
            phase = session.query(Phase).filter_by(id="phase-1").first()
            phase.name = "git_expert"
            session.add(Task(
                id="task-manual-git",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="Git hand-off",
                done_definition="Committed and pushed",
                status="failed",
                failure_reason="transient git error",
            ))

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("git-agent"),
        ) as dispatch:
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                assert _maybe_retry_failed_tasks(session, phase, logger) is True
                dispatch.assert_called_once()

        with db_manager.session_scope() as session:
            workflow = session.query(Workflow).filter_by(id="wf-1").first()
            assert workflow.status == "active"
            assert workflow.paused_by is None

    def test_retries_all_failed_tasks(self, db_manager, sample_workflow):
        """Should reset failed tasks and dispatch a fresh agent for each,
        landing on in_progress -- not just reset to pending and abandoned
        (the old behavior was a dead end: nothing else ever picked a
        pending-with-no-agent task back up)."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            # Add failed tasks
            for i in range(3):
                task = Task(
                    id=f"task-fail-{i}",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description=f"Failed task {i}",
                    done_definition="Done",
                    status="failed",
                    failure_reason="Error",
                )
                session.add(task)

            phase = session.query(Phase).filter_by(id="phase-1").first()

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)
                assert result is True

        with db_manager.session_scope() as session:
            # Verify tasks were reset and re-dispatched, not left pending
            tasks = session.query(Task).filter_by(phase_id="phase-1", status="in_progress").all()
            assert len(tasks) == 3
            assert all(t.assigned_agent_id.startswith("new-agent-1") for t in tasks)

    def test_folds_failure_reason_into_description_before_clearing(
        self, db_manager, sample_workflow
    ):
        """Regression: a blind bulk reset used to wipe failure_reason
        without ever surfacing it anywhere, so the retried agent got the
        exact same generic task description and no idea what went wrong
        last time -- e.g. a specific 'missing output artifact: X' from
        update_task_status's validation gate. The reason must survive into
        enriched_description (what the agent's prompt actually reads)."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-fail-0",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="Execute phase X",
                    enriched_description="Execute phase X: do the thing",
                    done_definition="Done",
                    status="failed",
                    failure_reason="Missing output artifact: docs/report.md",
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)
                assert result is True

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "in_progress"
            assert task.failure_reason is None
            # Uses raw_description as base (not enriched_description) to avoid
            # accumulating retry messages from previous attempts
            assert "Execute phase X" in task.enriched_description
            assert "Missing output artifact: docs/report.md" in task.enriched_description

    def test_task_without_failure_reason_gets_plain_reset(self, db_manager, sample_workflow):
        """A failed task with no recorded reason (e.g. a hard crash before
        anything could be logged) just resets normally -- nothing to fold in."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-fail-0",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="Execute phase X",
                    done_definition="Done",
                    status="failed",
                    failure_reason=None,
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                _maybe_retry_failed_tasks(session, phase, logger)

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "in_progress"
            assert task.enriched_description is None

    def test_orphaned_failure_reason_gets_plain_reset_not_a_retry_banner(
        self, db_manager, sample_workflow
    ):
        """Regression: 'Orphaned: ...' means no agent ever actually
        received this task -- a scheduling/claim-race artifact, not a real
        attempt that made a mistake. Wrapping it in "your previous attempt
        failed... fix it rather than repeating the same mistake" was
        actively misleading on what is, from the next agent's own point of
        view, genuinely its FIRST prompt for this task."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-fail-0",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="Execute phase X",
                    enriched_description="Execute phase X: do the thing",
                    done_definition="Done",
                    status="failed",
                    failure_reason="Orphaned: never dispatched to an agent",
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)
                assert result is True

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "in_progress"
            assert task.failure_reason is None
            assert "RETRY" not in (task.enriched_description or "")
            assert "repeating the same mistake" not in (task.enriched_description or "")
            # enriched_description left untouched -- not overwritten with
            # a misleading banner, and not blanked either.
            assert task.enriched_description == "Execute phase X: do the thing"

    def test_agent_dispatch_failure_lands_back_on_failed_not_stuck_pending(
        self, db_manager, sample_workflow
    ):
        """If create_agent_for_task_direct fails, the task must land back on
        "failed" (not "pending") -- _maybe_retry_failed_tasks only ever
        triggers on status="failed", so leaving it "pending" here would
        permanently strand the task: no other code path dispatches an
        agent for an already-existing pending task."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-fail-0",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="Execute phase X",
                    done_definition="Done",
                    status="failed",
                    failure_reason="Error",
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            return_value=None,
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                _maybe_retry_failed_tasks(session, phase, logger)

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "failed"
            assert task.assigned_agent_id is None

    def test_ignores_older_cycles_done_task_when_scoped_to_current_cycle(
        self, db_manager, sample_workflow
    ):
        """Regression: a phase revisited via goto reuses the same phase_id
        -- an old 'done' task from a prior cycle must not make the current
        cycle's failed task invisible to this retry path. Without
        cycle_start scoping, total_count counted the old done task too, so
        failed_count == total_count was never true once a phase had ever
        succeeded before -- this retry never fired for a later failing
        re-attempt, the exact live bug this covers."""
        from datetime import timedelta

        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks
        from src.core.database import Agent

        with db_manager.session_scope() as session:
            session.add(
                Agent(id="new-agent-1", system_prompt="p", status="working", cli_type="pi")
            )
            session.add(
                Task(
                    id="task-old-done",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    created_at=datetime.utcnow() - timedelta(hours=2),
                )
            )
            session.add(
                Task(
                    id="task-current-failed",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="boom",
                    created_at=datetime.utcnow() - timedelta(minutes=4),
                )
            )

        cycle_start = datetime.utcnow() - timedelta(minutes=5)
        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger, cycle_start=cycle_start)
                assert result is True

        with db_manager.session_scope() as session:
            current_task = session.query(Task).filter_by(id="task-current-failed").first()
            assert current_task.status == "in_progress"
            # The prior cycle's task is untouched.
            old_task = session.query(Task).filter_by(id="task-old-done").first()
            assert old_task.status == "done"

    def test_pauses_workflow_once_retry_cap_exhausted_instead_of_retrying_forever(
        self, db_manager, sample_workflow
    ):
        """The exact live bug: a task whose failure is permanent (a deleted
        git worktree raises instantly, no LLM call in between) got reset
        and re-dispatched every single poll cycle forever -- burning a
        cycle every few seconds indefinitely and starving every other
        workflow's turn in the same poll loop, since nothing here ever
        checked retry_count. Once every failed task in the phase is past
        the cap, this must stop retrying and pause the workflow instead."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-exhausted",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Workflow's shared worktree is missing",
                    retry_count=5,
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct"
        ) as mock_create_agent:
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)

        assert result is None
        mock_create_agent.assert_not_called()
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-exhausted").first()
            assert task.status == "failed"  # left alone, not reset to pending
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "system"
            assert "worktree is missing" in wf.status_reason

    def test_orphaned_task_retries_past_the_cap(self, db_manager, sample_workflow):
        """Characterization: orphaned tasks (never dispatched to an agent)
        are scheduling issues, not agent failures, and per this function's
        retryable_tasks filter must keep retrying indefinitely even past
        max_task_retries -- mirroring _retry_failed_tasks's identical
        exemption. Captured ahead of any future consolidation of the two
        retry implementations, so the exemption can't be silently dropped
        when they're merged."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-orphan-past-cap",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Orphaned: never dispatched to an agent",
                    retry_count=10,
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("orphan-retry-agent"),
        ) as mock_create_agent:
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)

        assert result is True
        mock_create_agent.assert_called_once()
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-orphan-past-cap").first()
            assert task.status == "in_progress"
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"

    def test_action_target_phase_survives_retry_reset(self, db_manager, sample_workflow):
        """Regression (this function's own inline comment): a failed task
        created by an earlier phase's goto/retry carries action_target_
        phase so the pipeline resumes at the right phase once it finally
        completes. Previously this reset cleared both action fields
        unconditionally, silently discarding the resume target -- observed
        live: a development task that goto'd back from qa_validation lost
        action_target_phase entirely on retry and, once done, fell back to
        next-phase-by-order, re-running the entire review chain from
        scratch even though none of it had been invalidated."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-goto-target",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="CLI session limit reached",
                    action="goto",
                    action_target_phase="qa_validation",
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("goto-retry-agent"),
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)

        assert result is True
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-goto-target").first()
            assert task.status == "in_progress"
            assert task.action == "goto"
            assert task.action_target_phase == "qa_validation"

    def test_does_not_re_pause_or_touch_reason_if_already_paused(
        self, db_manager, sample_workflow
    ):
        """A human may have already looked at this and left the workflow
        paused with their own note -- don't clobber it on every poll."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-exhausted",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="boom",
                    retry_count=2,
                )
            )
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "user"
            wf.status_reason = "user's own note"

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct"):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                _maybe_retry_failed_tasks(session, phase, logger)

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.paused_by == "user"
            assert wf.status_reason == "user's own note"


class TestCaseInProgressComplete:
    """Regression: mark_phase_complete's engine evaluation can take minutes
    (an LLM call in phase_manager.py), and nothing previously stopped a
    concurrent poll (this same orchestrator's next cycle, or monitor.py's
    separate _check_workflow_stuck_state process examining the same
    workflow) from re-entering this exact branch while the first
    evaluation was still in flight -- "all tasks done, 0 active" stays
    true the whole time. Observed live: a second, orphaned task + agent
    got created for an already-completed qa_validation phase a minute
    into the first evaluation; the pipeline moved on via that first
    evaluation's goto decision, leaving the second agent running against
    an abandoned phase, confusedly trying to manually create the next
    phase's task on its own."""

    def _seed_done_task(self, db_manager, phase_id="phase-1", workflow_id="wf-1"):
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-done-1",
                    workflow_id=workflow_id,
                    phase_id=phase_id,
                    raw_description="r",
                    done_definition="d",
                    status="done",
                )
            )

    def _seed_pending_task_with_agent(
        self, db_manager, agent_status, phase_id="phase-1", workflow_id="wf-1"
    ):
        from src.core.database import Agent

        with db_manager.session_scope() as session:
            session.add(
                Agent(id="agent-x", system_prompt="p", status=agent_status, cli_type="pi")
            )
            session.add(
                Task(
                    id="task-pending-1",
                    workflow_id=workflow_id,
                    phase_id=phase_id,
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                    assigned_agent_id="agent-x",
                    created_at=datetime.utcnow() - timedelta(minutes=5),
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_pending_task_pointing_at_dead_agent_is_marked_failed(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Regression: this is the actual gate the periodic sweep uses to
        decide a phase has real active work -- unlike _create_phase_task's
        own orphan check (only reached once a phase has zero tasks or all-
        failed tasks), a single stale "pending" task here short-circuits
        every case before that check ever runs. Previously only checked
        assigned_agent_id IS NULL, so a task dispatched to an agent that
        later died (killed mid-launch by a backend restart, or manually
        terminated) stayed "pending" forever, invisible to every self-heal
        path. Observed live: a security_review task sat this way for
        hours. Once marked failed here, the SAME sweep pass immediately
        sees "all tasks failed" and retries it via _maybe_retry_failed_tasks
        -- mocking create_agent_for_task_direct lets that full, real,
        one-pass self-heal (orphan -> failed -> retried -> in_progress with
        a fresh agent) actually succeed instead of erroring on a missing
        live agent manager."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses
        from src.core.database import Agent, Task as _Task

        self._seed_pending_task_with_agent(db_manager, agent_status="terminated")
        with db_manager.session_scope() as session:
            session.add(Agent(id="fresh-agent", system_prompt="p", status="working", cli_type="pi"))
        mock_create_agent.return_value = {"agent_id": "fresh-agent"}

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(_Task).filter_by(id="task-pending-1").first()
            assert task.status == "in_progress"
            assert task.assigned_agent_id == "fresh-agent"

    def test_stale_cycle_start_later_than_own_earliest_task_is_corrected(
        self, db_manager, sample_workflow
    ):
        """Regression: PhaseExecution.started_at and a task's created_at
        are stamped by independent utc_now() calls that can land a few
        milliseconds apart in either order. When started_at ends up LATER
        than the phase's own earliest real task, every cycle-scoped query
        in this function (Task.created_at >= cycle_start) silently
        excludes that task forever -- the "genuinely empty cycle"
        fresh-dispatch fallback further down correctly refuses to fire
        (its own unscoped total_cycle_tasks check still sees the task),
        but nothing else ever looks at it again either. Observed live:
        workflow 81b399c7's product_requirements phase stuck 18+ minutes
        this way, with a real pending task sitting right there the whole
        time. started_at must self-correct back to the earliest task."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses
        from src.core.database import Task as _Task

        task_created_at = datetime.utcnow()
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-pending-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                    created_at=task_created_at,
                )
            )
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            execution.started_at = task_created_at + timedelta(milliseconds=14)

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            assert execution.started_at <= task_created_at
            # The task itself is untouched by this correction alone -- it's
            # not yet a minute stale, so _mark_orphaned_and_stale_pending_
            # tasks_failed correctly leaves it "pending" here. What matters
            # is it's now visible to every cycle-scoped check from here on,
            # instead of permanently invisible.
            task = session.query(_Task).filter_by(id="task-pending-1").first()
            assert task.status == "pending"

    def test_pending_task_pointing_at_working_agent_is_left_alone(
        self, db_manager, sample_workflow
    ):
        """A task assigned to a genuinely still-working agent is real
        active work, not an orphan -- must not be touched."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses
        from src.core.database import Task as _Task

        self._seed_pending_task_with_agent(db_manager, agent_status="working")

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            task = session.query(_Task).filter_by(id="task-pending-1").first()
            assert task.status == "pending"
            assert task.failure_reason is None

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_fires_transition_when_claim_succeeds(self, mock_fire, db_manager, sample_workflow):
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_done_task(db_manager)
        mock_fire.return_value = True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is True
        mock_fire.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_claim_is_released_after_transition_fires(
        self, mock_fire, db_manager, sample_workflow
    ):
        """Regression, found live: this claim only ever existed to guard
        against a CONCURRENT re-entry during evaluation -- it was never
        released once _fire_phase_transition returned, so it became a
        permanently stale non-null value on the now-"completed" phase's
        row forever (nothing else needed to check it again... until
        _trigger_arbitration's exhaustion path tried to claim this exact
        phase later and read the leftover stale value as "arbitration
        already in flight", silently refusing to ever arbitrate it --
        crashed outright on a separate bug (OrchestratorLogger has no
        .debug) the one time this was hit live, but even without that
        crash it would have silently done nothing forever)."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _get_phase_statuses,
)

        self._seed_done_task(db_manager)
        mock_fire.return_value = True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_claim_is_released_even_if_transition_raises(
        self, mock_fire, db_manager, sample_workflow
    ):
        """The release must happen regardless of outcome -- an exception
        mid-transition must not leave the phase permanently unclaimable
        either."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _get_phase_statuses,
)

        self._seed_done_task(db_manager)
        mock_fire.side_effect = RuntimeError("boom")

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            try:
                _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())
            except RuntimeError:
                pass

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_claim_survives_an_arbitrate_transition(
        self, mock_fire, db_manager, sample_workflow
    ):
        """Regression, observed live (workflow a7695dc5): when
        _fire_phase_transition's action is "arbitrate", _trigger_arbitration
        (called from inside it) deliberately reuses THIS SAME claim to mark
        "arbitration in flight" for _maybe_resolve_arbitration to find once
        the arbiter agent finishes. The prior unconditional release here
        wiped that claim out within milliseconds of the arbiter being
        dispatched -- long before it could finish. _maybe_resolve_arbitration
        then never found a claimed phase to resolve, so the arbiter's
        eventual "continue" decision was silently dropped and the workflow
        was abandoned after hours of zero agent activity. The claim must
        stay held while the phase's latest task is a still-in-flight
        (not-yet-done) arbitration task."""
        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import (
            _case_in_progress_complete,
            _get_phase_statuses,
        )

        self._seed_done_task(db_manager)

        def _fake_trigger_arbitration(*args, **kwargs):
            # Mirrors what the real _trigger_arbitration does: creates the
            # arbitration task (not yet "done") while still holding this
            # phase's task_creation_claimed_at claim.
            with db_manager.session_scope() as session:
                session.add(
                    Task(
                        id="task-arbitration-1",
                        workflow_id="wf-1",
                        phase_id="phase-1",
                        raw_description="Arbitrate stuck phase: requirements",
                        done_definition="d",
                        status="pending",
                        created_by_agent_id=ARBITRATION_CREATED_BY,
                    )
                )
            return True

        mock_fire.side_effect = _fake_trigger_arbitration

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_claim_is_released_once_arbitration_task_is_done(
        self, mock_fire, db_manager, sample_workflow
    ):
        """The other half of the fix: once the arbitration task has actually
        completed (resolved or not), this function must not hold the claim
        hostage forever -- only a genuinely still-running arbitration task
        keeps it held."""
        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import (
            _case_in_progress_complete,
            _get_phase_statuses,
        )

        self._seed_done_task(db_manager)

        def _fake_trigger_arbitration(*args, **kwargs):
            with db_manager.session_scope() as session:
                session.add(
                    Task(
                        id="task-arbitration-1",
                        workflow_id="wf-1",
                        phase_id="phase-1",
                        raw_description="Arbitrate stuck phase: requirements",
                        done_definition="d",
                        status="done",
                        created_by_agent_id=ARBITRATION_CREATED_BY,
                    )
                )
            return True

        mock_fire.side_effect = _fake_trigger_arbitration

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_skips_when_evaluation_already_claimed(self, mock_fire, db_manager, sample_workflow):
        """Simulates a concurrent caller having already claimed this
        phase's evaluation (e.g. still awaiting a slow engine decision) --
        this call must not also fire a transition for the same phase."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _claim_phase_task_creation,
    _get_phase_statuses,
)

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            won = _claim_phase_task_creation(session, "phase-1")
            assert won is True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is None
        mock_fire.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_any_held_claim_blocks_evaluation(
        self, mock_fire, db_manager, sample_workflow
    ):
        """A held claim -- stale or not -- blocks this function on its own;
        staleness is handled earlier, by _release_stale_task_creation_claims
        (see TestReleaseStaleTaskCreationClaims), before phase_statuses is
        even read for this cycle. By the time this loop runs, any claim
        still present is a genuinely live one (e.g. mid-arbitration)."""
        from datetime import timedelta

        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _get_phase_statuses,
)

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # utcnow, not now: the stale-claim sweep compares this against
            # a datetime.utcnow()-derived cutoff. East of UTC a local-time
            # stamp is still 'in the future' relative to that cutoff, so the
            # claim never reads as stale and is never cleared (verified:
            # passes at UTC-6, fails at UTC+9).
            execution.task_creation_claimed_at = datetime.utcnow() - timedelta(minutes=5)

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is None
        mock_fire.assert_not_called()
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_orphaned_diagnostic_task_does_not_block_completion(
        self, mock_fire, db_manager, sample_workflow
    ):
        """Regression: an orphaned DIAGNOSTIC task (created by the monitor
        when it thought the workflow was stuck, then reset to "pending"
        after its agent was terminated without closing it) must not count
        as real incomplete work. _check_workflow_stuck_state already
        excludes DIAGNOSTIC: prefixed tasks from its own completion check
        for exactly this reason -- this case hadn't adopted the same
        convention, so a leftover diagnostic task permanently blocked the
        phase from ever being recognized as complete, even with its real
        task already done."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="diagnostic-task-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="DIAGNOSTIC: Analyze why workflow has stalled",
                    done_definition="d",
                    status="pending",
                )
            )
        mock_fire.return_value = True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is True
        mock_fire.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_claimed_phase_with_failed_task_is_not_retried(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Gap found in review: a FAILED task in a claimed phase (e.g. a
        dead/crashed arbitration agent -- see _trigger_arbitration) used to
        reach _maybe_retry_failed_tasks, which re-dispatches through the
        generic retry path with agent_type="phase" -- losing the
        arbitration-specific prompt entirely and launching a confused
        agent. Only _maybe_resolve_arbitration may act on a claimed
        phase's failed task."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _claim_phase_task_creation,
    _get_phase_statuses,
)

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="failed-task-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                )
            )
            won = _claim_phase_task_creation(session, "phase-1")
            assert won is True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is None
        mock_create_agent.assert_not_called()
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="failed-task-1").first()
            assert task.status == "failed"  # untouched, not reset to pending

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_old_cycles_done_task_does_not_mask_current_cycle_failure(
        self, mock_fire, db_manager, sample_workflow
    ):
        """The exact live bug: phase-1 succeeded once already (an earlier
        cycle, before a later goto sent the pipeline back to it), so a
        'done' task from that old cycle exists. The CURRENT cycle's own
        task then failed. Without scoping done_count/incomplete to the
        current cycle (execution.started_at), that old done task alone
        satisfied "phase complete" and fired the transition against
        whatever -- usually nothing -- the failed current attempt left on
        disk, producing a false goto-back with a "result not found"
        reason instead of just retrying the failed attempt."""
        from datetime import timedelta

        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _get_phase_statuses,
)
        from src.core.database import Agent

        with db_manager.session_scope() as session:
            session.add(
                Agent(id="new-agent-1", system_prompt="p", status="working", cli_type="pi")
            )
            session.add(
                Task(
                    id="task-old-done",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    created_at=datetime.utcnow() - timedelta(hours=2),
                )
            )
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.started_at = datetime.utcnow() - timedelta(minutes=5)
            session.add(
                Task(
                    id="task-current-failed",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="boom",
                    created_at=datetime.utcnow() - timedelta(minutes=4),
                )
            )

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        mock_fire.assert_not_called()
        assert result is True  # retried the failed task instead of transitioning
        with db_manager.session_scope() as session:
            current_task = session.query(Task).filter_by(id="task-current-failed").first()
            assert current_task.status == "in_progress"
            old_task = session.query(Task).filter_by(id="task-old-done").first()
            assert old_task.status == "done"  # untouched

    def test_creates_fresh_task_when_cycle_has_zero_tasks_not_just_zero_failed(
        self, db_manager, sample_workflow
    ):
        """The exact live bug: a goto resets a phase's PhaseExecution.status
        back to "pending" for a fresh cycle, but (before the goto-reset fix
        in phase_manager.py) never cleared started_at -- so a later flip
        back to "in_progress" (e.g. _release_pending_phases_with_done_tasks
        finding the phase's own now-ancient done task as "evidence" and
        backfilling only if started_at was empty) can leave started_at
        newer than every task the phase actually has. done_count and
        incomplete are both cycle-scoped (Task.created_at >= started_at),
        so both come back 0 -- indistinguishable, before this fix, from "0
        active, 0 done, so check for all-failed" -- but _maybe_retry_failed_
        tasks ALSO cycle-scopes and finds literally nothing to retry
        (failed_count == total_count == 0 is never true), so it silently
        no-ops forever. The phase is "in_progress" yet permanently invisible
        to every dispatch case: Case 0b's own unscoped task-count check
        sees the phase's stale task and doesn't fire either. Observed live
        for a real feature: a completed predecessor phase never advanced
        to its successor because the successor itself was stuck exactly
        this way."""
        from datetime import timedelta

        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _get_phase_statuses,
)
        from src.core.database import Agent

        with db_manager.session_scope() as session:
            session.add(
                Agent(id="new-agent-1", system_prompt="p", status="working", cli_type="pi")
            )
            # The phase's only task, from a cycle now fully consumed
            # (mirrors a goto-completed task -- old, "done", nothing left
            # to do with it).
            session.add(
                Task(
                    id="task-old-done",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                )
            )
            session.flush()
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # Stale anchor: newer than the only task tied to this phase.
            execution.started_at = datetime.utcnow() + timedelta(minutes=5)

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is True
        with db_manager.session_scope() as session:
            tasks = session.query(Task).filter_by(phase_id="phase-1").all()
            assert len(tasks) == 2, "a fresh task must be created, not silently skipped"
            fresh = [t for t in tasks if t.id != "task-old-done"][0]
            assert fresh.status == "in_progress"

    def test_creates_fresh_task_when_only_task_in_cycle_is_duplicated(
        self, db_manager, sample_workflow
    ):
        """Regression, same class as the stale-started_at case above but a
        different cause: a "duplicated" task left behind by a ticket-
        blocked git_expert/doc_review routing to development (see
        _maybe_retry_failed_tasks) still satisfies the cycle_filter (it's
        genuinely within the current cycle), so total_cycle_tasks alone
        would read as "phase already has a task" and this branch would
        never fire -- exactly the bug observed live on workflow 81b399c7's
        git_expert phase. "duplicated" must be excluded here the same way
        it already is in _case_in_progress_no_tasks."""
        from src.core.database import Agent, PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import (
            _case_in_progress_complete,
            _get_phase_statuses,
        )

        with db_manager.session_scope() as session:
            session.add(
                Agent(id="new-agent-2", system_prompt="p", status="working", cli_type="pi")
            )
            session.add(
                Task(
                    id="task-duplicated-in-cycle",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="duplicated",
                )
            )
            session.flush()
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.started_at = datetime.utcnow() - timedelta(minutes=1)

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-2"),
        ):
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is True
        with db_manager.session_scope() as session:
            tasks = session.query(Task).filter_by(phase_id="phase-1").all()
            assert len(tasks) == 2, "a fresh task must be created, not silently skipped"
            fresh = [t for t in tasks if t.id != "task-duplicated-in-cycle"][0]
            assert fresh.status == "in_progress"

    def test_maybe_retry_failed_tasks_is_claim_protected(self, db_manager, sample_workflow):
        """Regression: _maybe_retry_failed_tasks used to run with zero
        claim protection, unlike the sibling _fire_phase_transition path a
        few lines below it -- whose own comment already documents the
        exact race this class of gap causes (a concurrent poll re-entering
        mid-dispatch, creating two agents for the same task). Verifies
        both that the claim was actually taken during the retry (by
        checking it isn't the reason nothing happened) and that it's
        released afterward -- a bug in the release would strand the phase
        behind a permanently-held claim, a worse outcome than the race
        this fix closes."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import (
    _case_in_progress_complete,
    _get_phase_statuses,
)

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-fail-0",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="boom",
                )
            )

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is True
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "in_progress"  # the retry actually ran
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None  # released, not stranded

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_exhausted_retry_cap_fails_the_workflow_instead_of_firing_transition(
        self, mock_fire, db_manager, sample_workflow
    ):
        """Regression, found live: once every failed task in a phase (that
        also has a done task from an earlier cycle) is past the retry cap,
        this used to only set execution.status = "failed" and fall
        straight through into firing a transition anyway --
        _fire_phase_transition's engine evaluation reads the failed task's
        own stale action/completion data, not execution.status, so it
        would advance to the next phase as if the failed one had passed.
        Observed live: architectural_review exhausted its retry cap on a
        real frontmatter-schema defect and the pipeline advanced straight
        to qa_validation as if the review had passed."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-exhausted-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="architectural_review's report has the wrong frontmatter type",
                    retry_count=5,
                )
            )

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        mock_fire.assert_not_called()
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"
            assert "phase-1" not in wf.status_reason  # uses phase.name, not phase.id
            assert "requirements" in wf.status_reason  # phase-1's name, from sample_workflow
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "failed"

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_exhausted_retry_cap_clears_a_stale_review_pause(
        self, mock_fire, db_manager, sample_workflow
    ):
        """Regression, found live: this phase's own retry-cap-exhaustion
        sets wf.status = "failed" directly, bypassing pause_workflow's
        shared status/paused_by/paused_at primitive -- if an UNRELATED,
        concurrent phase's review gate had already set paused_by="review"
        on this same workflow (_advance_phases deliberately keeps other
        in-progress phases moving while one sits paused for review), that
        stale marker survived the "failed" write untouched. resume_workflow
        then permanently no-ops (it requires status=="paused"), while
        feature_routes' approve handler doesn't check that return value and
        sets Feature.status="active" anyway -- a workflow stuck "failed"
        forever with Approve as a silent no-op."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-exhausted-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="architectural_review's report has the wrong frontmatter type",
                    retry_count=5,
                )
            )
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.paused_by = "review"
            wf.paused_at = datetime.utcnow()

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"
            assert wf.paused_by is None
            assert wf.paused_at is None

    def test_a_queued_sibling_task_counts_as_incomplete(self, db_manager, sample_workflow):
        """Regression, found live: the "incomplete" check omitted "queued"
        -- every OTHER "does this phase have real outstanding work" query
        in this module (_create_phase_task's own existing-task check,
        check_phase_sibling_active, the corrective-task path) already
        includes it, this one didn't. An agent can spawn subtasks via
        create_task for the remainder of its own assigned work and then
        mark ITS OWN task done -- those subtasks can legitimately sit
        "queued" (QueueService's own capacity-gated status, distinct from
        "pending") waiting for a dispatch slot, not orphaned. Without this,
        the phase was declared complete and the pipeline advanced past it
        the moment the one dispatched task finished, leaving the queued
        subtasks to sit forever (nothing re-checks a phase already
        advanced past) while a LATER phase reviewed work that was never
        actually finished. Confirmed live: task 4bf4518f (development)
        completed having spawned 5 subtasks (C1 through C10); all 5 sat
        "queued" while adversarial_review ran and completed against the
        incomplete implementation."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-queued-subtask",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="C1+C2: remaining work the primary task delegated",
                    done_definition="d",
                    status="queued",
                )
            )

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is None, "must not fire the completion transition while a sibling task is still queued"
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"

    def test_a_blocked_sibling_task_counts_as_incomplete(self, db_manager, sample_workflow):
        """Regression, des-c7b9 tech-debt pass: this same "incomplete"
        check already includes "queued" (see the test directly above, for
        the 4bf4518f incident) but was still missing "blocked" -- same bug
        class, same consequence. A phase with only a "blocked" sibling task
        remaining would be wrongly declared complete by this exact check."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-blocked-subtask",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="C1+C2: remaining work the primary task delegated",
                    done_definition="d",
                    status="blocked",
                )
            )

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is None, "must not fire the completion transition while a sibling task is still blocked"
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"

    def test_empty_cycle_dispatch_releases_its_own_claim_when_a_task_raced_in(
        self, db_manager, sample_workflow
    ):
        """Same TOCTOU gap as _case_start_first_phase's own regression
        (see test_reverifies_existing_right_before_creating), for the
        "in_progress but no tasks within its own cycle (stale started_at?)"
        empty-cycle dispatch: a task committed by another claim-protected
        path in the window between the total_cycle_tasks read and winning
        the claim must be caught by a fresh re-check, releasing the claim
        it just won rather than leaving it held for no reason."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        cycle_start = datetime.utcnow()
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.started_at = cycle_start

        def _win_claim_but_race_a_task_in(*_args, **_kwargs):
            with db_manager.session_scope() as race_session:
                race_session.add(Task(
                    id="task-raced-in",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                    created_at=cycle_start,
                ))
            return True

        with patch(
            "src.autopilot.orchestrator.phase_transitions._claim_phase_task_creation",
            side_effect=_win_claim_but_race_a_task_in,
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True
        ) as mock_create, patch(
            "src.autopilot.orchestrator.phase_transitions._release_phase_task_creation_claim"
        ) as mock_release:
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

                assert result is None
                mock_create.assert_not_called()
                mock_release.assert_called_once_with(session, "phase-1")


class TestCaseInProgressCompleteResolvesDoneArbitrationInstead:
    """Regression, observed live (workflow e9019930, phase design_review):
    when a phase's most recent task is a DONE arbitration decision that
    hasn't been consumed yet, _case_in_progress_complete used to fall
    through to the generic "phase complete, evaluate transition" path --
    which has no idea the "done" task it's looking at is itself an
    arbitration attempt, so it just recomputes "retry budget exhausted"
    fresh and requests a BRAND NEW arbitration for the exact question the
    first one just answered. fire_spec_gate_if_ready already has an
    equivalent guard for the event-driven path (see its own docstring,
    prior incident workflow ca539a75) -- this is the periodic-sweep
    sibling of that fix. Without it, whichever path's sweep tick lands
    first wins the race, and the two can duplicate arbitration agents
    indefinitely until MAX_ARBITRATIONS_PER_PHASE forces a resolution."""

    def test_resolves_arbitration_instead_of_re_firing_the_gate(
        self, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import (
            ARBITRATION_CREATED_BY,
            _case_in_progress_complete,
            _get_phase_statuses,
        )

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-arb-done",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="Arbitrate stuck phase: requirements",
                    done_definition="d",
                    status="done",
                    created_by_agent_id=ARBITRATION_CREATED_BY,
                )
            )

        with patch(
            "src.autopilot.orchestrator.phase_transitions._maybe_resolve_arbitration"
        ) as mock_resolve, patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition"
        ) as mock_fire:
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        mock_resolve.assert_called_once_with("wf-1", ANY)
        mock_fire.assert_not_called()

    def test_still_running_arbitration_task_is_untouched(self, db_manager, sample_workflow):
        """The companion case: an arbitration task that's still pending/
        in_progress must keep hitting the pre-existing "has active tasks"
        skip (incomplete > 0), not this new branch -- it's not done yet,
        there's nothing to resolve."""
        from src.autopilot.orchestrator.phase_transitions import (
            ARBITRATION_CREATED_BY,
            _case_in_progress_complete,
            _get_phase_statuses,
        )

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-arb-running",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="Arbitrate stuck phase: requirements",
                    done_definition="d",
                    status="in_progress",
                    created_by_agent_id=ARBITRATION_CREATED_BY,
                )
            )

        with patch(
            "src.autopilot.orchestrator.phase_transitions._maybe_resolve_arbitration"
        ) as mock_resolve, patch(
            "src.autopilot.orchestrator.phase_transitions._fire_phase_transition"
        ) as mock_fire:
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is None
        mock_resolve.assert_not_called()
        mock_fire.assert_not_called()


class TestReleaseStaleTaskCreationClaims:
    """Regression, found live: _case_in_progress_complete's own claim check
    only ever sees phases already "in_progress" -- but a phase whose claim
    was never released also never had its status flipped to "in_progress"
    in the first place (that flip is itself part of releasing the claim).
    So a phase stuck "pending" with a stale claim and an already-done task
    was invisible to every case in _advance_phases's dispatch, not just
    Case 2 -- no matter how many times its task actually finished.
    Observed live: a phase's task completed successfully over a day
    earlier; the claim, set before the claim/release wiring existed, was
    never released, and the workflow sat blocking the entire design queue
    indefinitely. _release_stale_task_creation_claims runs workflow-wide,
    before phase_statuses is read, so it catches this regardless of the
    phase's current status."""

    def _seed_done_task(self, db_manager, phase_id="phase-1", workflow_id="wf-1"):
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-done-1",
                    workflow_id=workflow_id,
                    phase_id=phase_id,
                    raw_description="r",
                    done_definition="d",
                    status="done",
                )
            )

    def test_stale_claim_on_pending_phase_with_done_task_flips_to_in_progress(
        self, db_manager, sample_workflow
    ):
        from datetime import timedelta

        from src.autopilot.orchestrator.phase_transitions import CLAIM_STALE_TIMEOUT_SECONDS
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_stale_task_creation_claims

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # sample_workflow's fixture defaults phase-1 to "in_progress" --
            # the actual live precondition is "pending" (its status never
            # got flipped, because that flip is itself part of releasing
            # the claim, which never happened).
            execution.status = "pending"
            execution.task_creation_claimed_at = datetime.utcnow() - timedelta(
                seconds=CLAIM_STALE_TIMEOUT_SECONDS + 1
            )

        with db_manager.session_scope() as session:
            _release_stale_task_creation_claims(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            assert execution.status == "in_progress"

    def test_stale_claim_with_no_task_just_clears_claim(self, db_manager, sample_workflow):
        """No task exists yet -- don't fabricate progress that didn't
        happen; just free the claim so Case 0/0b can create one fresh."""
        from datetime import timedelta

        from src.autopilot.orchestrator.phase_transitions import CLAIM_STALE_TIMEOUT_SECONDS
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_stale_task_creation_claims

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"
            execution.task_creation_claimed_at = datetime.utcnow() - timedelta(
                seconds=CLAIM_STALE_TIMEOUT_SECONDS + 1
            )

        with db_manager.session_scope() as session:
            _release_stale_task_creation_claims(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            assert execution.status == "pending"  # unchanged -- no task to justify the flip

    def test_stale_claim_with_in_flight_arbitration_is_left_alone(
        self, db_manager, sample_workflow
    ):
        """Regression, found by adversarial review of the fix above: an
        arbiter is a real LLM-driven agent dispatch (spawn, read context,
        reason, write arbitration_result.json) that can legitimately run
        longer than CLAIM_STALE_TIMEOUT_SECONDS (8 minutes) -- this sweep
        must not clear its claim just because it's stale by the clock. A
        claim reused by a still-running arbitration is not the same as a
        genuinely abandoned one; clearing it here reproduces the exact
        silently-dropped-decision bug _phase_has_arbitration_in_flight's
        other two call sites were added to fix, just on a longer clock."""
        from datetime import timedelta

        from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import (
            CLAIM_STALE_TIMEOUT_SECONDS,
            _release_stale_task_creation_claims,
        )
        from src.core.database import PhaseExecution

        with db_manager.session_scope() as session:
            session.add(Task(
                id="task-arbitration-1", workflow_id="wf-1", phase_id="phase-1",
                raw_description="Arbitrate stuck phase: requirements",
                done_definition="d", status="pending",
                created_by_agent_id=ARBITRATION_CREATED_BY,
            ))
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.task_creation_claimed_at = datetime.utcnow() - timedelta(
                seconds=CLAIM_STALE_TIMEOUT_SECONDS + 1
            )

        with db_manager.session_scope() as session:
            _release_stale_task_creation_claims(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None

    def test_recent_claim_is_left_alone(self, db_manager, sample_workflow):
        """A claim well within CLAIM_STALE_TIMEOUT_SECONDS is exactly the
        legitimate in-flight case (e.g. mid-arbitration) this guard exists
        for -- must be left completely untouched."""
        from datetime import timedelta

        from src.core.database import PhaseExecution

        from src.autopilot.orchestrator.phase_transitions import _release_stale_task_creation_claims

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.task_creation_claimed_at = datetime.utcnow() - timedelta(minutes=1)

        with db_manager.session_scope() as session:
            _release_stale_task_creation_claims(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None
            assert execution.status == "in_progress"  # unchanged from fixture default

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_advance_phases_end_to_end_fires_transition_for_stale_pending_phase(
        self, mock_fire, db_manager, sample_workflow
    ):
        """The exact live bug, end to end through the real dispatcher: a
        "pending" phase (not "in_progress") with a done task and a
        day-old, never-released claim must actually advance -- not just
        have its claim cleared in isolation."""
        from datetime import timedelta

        from src.autopilot.orchestrator.phase_transitions import CLAIM_STALE_TIMEOUT_SECONDS
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # sample_workflow's fixture defaults phase-1 to "in_progress" --
            # the actual live precondition is "pending".
            execution.status = "pending"
            execution.task_creation_claimed_at = datetime.utcnow() - timedelta(
                seconds=CLAIM_STALE_TIMEOUT_SECONDS + 1
            )
        mock_fire.return_value = True

        result = _advance_phases("wf-1", MagicMock())

        assert result is True
        mock_fire.assert_called_once()


class TestReleasePendingPhasesWithDoneTasks:
    """Regression, found live: none of _advance_phases's four dispatch
    cases recognize a PhaseExecution stuck "pending" despite already
    having a "done" Task (Case 0/0b act on a lack of tasks, Case 1 needs
    the *predecessor* completed, Case 2 only ever looks at phases already
    "in_progress") -- so a phase in this state is invisible to every one
    of them, forever. Several paths create/complete a task without
    re-flipping PhaseExecution to "in_progress" the way _create_phase_task
    does (e.g. _maybe_retry_failed_tasks's reset-and-redispatch loop never
    touches PhaseExecution at all). Observed live: two workflows sat this
    way for days, invisible to every self-heal path, while an unrelated
    workflow's endlessly-retried task hogged every poll cycle so these
    never got a design-queue turn to be noticed."""

    def _seed_done_task(self, db_manager, phase_id="phase-1", workflow_id="wf-1"):
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-done-1",
                    workflow_id=workflow_id,
                    phase_id=phase_id,
                    raw_description="r",
                    done_definition="d",
                    status="done",
                )
            )

    def test_pending_phase_with_done_task_flips_to_in_progress(
        self, db_manager, sample_workflow
    ):
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_done_tasks

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # sample_workflow's fixture defaults phase-1 to "in_progress" --
            # the actual live precondition is "pending".
            execution.status = "pending"
            execution.started_at = None

        with db_manager.session_scope() as session:
            _release_pending_phases_with_done_tasks(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"
            assert execution.started_at is not None

    def test_pending_phase_with_no_task_is_left_alone(self, db_manager, sample_workflow):
        """No task exists yet -- don't fabricate progress that didn't
        happen; Case 0/0b already own creating this phase's first task."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_done_tasks

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"

        with db_manager.session_scope() as session:
            _release_pending_phases_with_done_tasks(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "pending"  # unchanged -- no task to justify the flip

    def test_already_in_progress_phase_is_untouched(self, db_manager, sample_workflow):
        """Only 'pending' is the broken state here -- don't touch a phase
        that's already correctly flipped."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_done_tasks

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            fixed_started_at = datetime(2020, 1, 1)
            execution.started_at = fixed_started_at

        with db_manager.session_scope() as session:
            _release_pending_phases_with_done_tasks(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"
            assert execution.started_at == fixed_started_at  # untouched

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    def test_advance_phases_end_to_end_fires_transition_for_stuck_pending_phase(
        self, mock_fire, db_manager, sample_workflow
    ):
        """The exact live bug, end to end through the real dispatcher: a
        "pending" phase with a done task and no claim at all (so
        _release_stale_task_creation_claims alone can't catch it) must
        still actually advance."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"
            execution.task_creation_claimed_at = None
        mock_fire.return_value = True

        result = _advance_phases("wf-1", MagicMock())

        assert result is True
        mock_fire.assert_called_once()

    def test_only_flips_the_phase_with_the_most_recent_done_task(
        self, db_manager, sample_workflow
    ):
        """Regression, found live: a workflow with any real goto history
        has MANY pending phases each carrying SOME old done task from an
        earlier cycle -- that's normal, not stuck. Flipping every one of
        them in a single pass previously created several
        simultaneously-active phases for the same workflow at once (5
        concurrent agents on 3 different phases, confirmed live). Only the
        phase matching the workflow's most recent completion -- whatever
        it was actually working on right before getting stuck -- may be
        repaired."""
        from datetime import timedelta

        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_done_tasks

        with db_manager.session_scope() as session:
            # phase-1: an OLD completion from an earlier goto cycle.
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "pending"
            session.add(
                Task(
                    id="task-old",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    created_at=datetime.utcnow() - timedelta(hours=2),
                )
            )
            # phase-2: what the workflow was actually doing most recently.
            session.add(
                PhaseExecution(
                    id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1",
                    status="pending",
                )
            )
            session.add(
                Task(
                    id="task-recent",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    created_at=datetime.utcnow() - timedelta(minutes=5),
                )
            )

        with db_manager.session_scope() as session:
            _release_pending_phases_with_done_tasks(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec2 = session.query(PhaseExecution).filter_by(phase_id="phase-2").first()
            assert exec1.status == "pending"  # untouched -- old cycle, not the frontier
            assert exec2.status == "in_progress"  # the actual frontier

    def test_skips_entirely_if_any_phase_already_in_progress(
        self, db_manager, sample_workflow
    ):
        """A workflow legitimately doing something must never gain a
        second concurrent in-progress phase -- even if some OTHER pending
        phase happens to carry an old done task from an earlier cycle."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_done_tasks

        with db_manager.session_scope() as session:
            # phase-1 stays "in_progress" (sample_workflow's default) --
            # genuinely active work.
            session.add(
                PhaseExecution(
                    id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1",
                    status="pending",
                )
            )
            session.add(
                Task(
                    id="task-done-2",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                )
            )

        with db_manager.session_scope() as session:
            _release_pending_phases_with_done_tasks(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            exec2 = session.query(PhaseExecution).filter_by(phase_id="phase-2").first()
            assert exec2.status == "pending"  # left alone -- phase-1 is already active

    def test_ignores_diagnostic_tasks_when_finding_the_most_recent_completion(
        self, db_manager, sample_workflow
    ):
        """A diagnostic task (created by the monitor against a stuck
        phase's phase_id -- see _create_diagnostic_agent) completing its
        investigation is not real phase progress. If it's the most RECENT
        done task workflow-wide, it must not be mistaken for "what the
        workflow was actually working on," matching the same exclusion
        _case_in_progress_complete's own queries already apply."""
        from datetime import timedelta

        from src.core.constants import DIAGNOSTIC_TASK_PREFIX
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_done_tasks

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "pending"
            # The real, earlier phase task.
            session.add(
                Task(
                    id="task-real",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    created_at=datetime.utcnow() - timedelta(minutes=10),
                )
            )
            # A LATER diagnostic task against the same phase, also done.
            session.add(
                Task(
                    id="task-diagnostic",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description=f"{DIAGNOSTIC_TASK_PREFIX} investigate stuck phase",
                    done_definition="d",
                    status="done",
                    created_at=datetime.utcnow(),
                )
            )

        with db_manager.session_scope() as session:
            _release_pending_phases_with_done_tasks(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # Still repaired -- via the real task, not skipped outright.
            assert exec1.status == "in_progress"
            assert exec1.started_at == session.query(Task).filter_by(
                id="task-real"
            ).first().created_at


class TestReleasePendingPhasesWithOrphanedTask:
    """Regression, found live: sibling to _release_pending_phases_with_
    done_tasks (same blind spot -- a PhaseExecution stuck "pending" is
    invisible to every one of _advance_phases's four dispatch cases -- but
    for a non-terminal (orphaned) task instead of a done one. Its own
    "skip entirely if ANY phase is in_progress" guard doesn't hold here: a
    manual-only phase (git_expert) sitting "in_progress" only because
    it's paused for review, with its own task already failed, must not
    block this repair for an unrelated phase behind it.

    Observed live: development (task 66e7c1ff) sat "pending" -- reverted
    by an earlier goto cycle -- for the entire time its workflow was
    paused for git_expert review, invisible to every dispatch case."""

    def _seed_pending_task(self, db_manager, status="pending", phase_id="phase-1", workflow_id="wf-1"):
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-orphan-1",
                    workflow_id=workflow_id,
                    phase_id=phase_id,
                    raw_description="r",
                    done_definition="d",
                    status=status,
                )
            )

    def test_pending_phase_with_orphaned_task_flips_to_in_progress(
        self, db_manager, sample_workflow
    ):
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_orphaned_task

        self._seed_pending_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"
            execution.started_at = None

        with db_manager.session_scope() as session:
            _release_pending_phases_with_orphaned_task(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"
            assert execution.started_at is not None

    def test_pending_phase_with_no_task_is_left_alone(self, db_manager, sample_workflow):
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_orphaned_task

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"

        with db_manager.session_scope() as session:
            _release_pending_phases_with_orphaned_task(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "pending"  # unchanged -- no task to justify the flip

    def test_done_task_does_not_count_as_orphaned(self, db_manager, sample_workflow):
        """A 'done' task is _release_pending_phases_with_done_tasks's own
        territory -- this function only matches non-terminal statuses, so
        it must not also fire for one, which would just be redundant (not
        wrong, but worth locking down that the status filter is precise)."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_orphaned_task

        self._seed_pending_task(db_manager, status="done")
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"

        with db_manager.session_scope() as session:
            _release_pending_phases_with_orphaned_task(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "pending"

    def test_does_not_flip_while_another_task_is_genuinely_active(
        self, db_manager, sample_workflow
    ):
        """A real, live agent working elsewhere in the workflow (status
        assigned/in_progress) must still block this repair -- flipping a
        second phase to "in_progress" concurrently would let two agents
        burn tokens on unrelated phases in the same shared worktree at
        once, the exact hazard _release_pending_phases_with_done_tasks's
        own 'skip if anything is in_progress' guard exists to prevent."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_orphaned_task

        self._seed_pending_task(db_manager, phase_id="phase-1")
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"
            session.add(
                Task(
                    id="task-live-elsewhere",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="in_progress",
                )
            )

        with db_manager.session_scope() as session:
            _release_pending_phases_with_orphaned_task(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "pending"  # left alone -- a real agent is active elsewhere

    def test_dead_in_progress_phase_does_not_block_an_unrelated_repair(
        self, db_manager, sample_workflow
    ):
        """A phase sitting "in_progress" with only a dead/failed task (no
        live agent) must not block an unrelated phase's own orphaned-task
        repair -- only a genuinely live task anywhere in the workflow
        should (see test_does_not_flip_while_another_task_is_genuinely_
        active above)."""
        from src.core.database import PhaseExecution
        from src.autopilot.orchestrator.phase_transitions import _release_pending_phases_with_orphaned_task

        self._seed_pending_task(db_manager, phase_id="phase-1")
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"
            # phase-2 stands in for an unrelated phase that's dead-ended:
            # in_progress, but its own task already failed -- nothing live
            # about it.
            session.add(
                Task(
                    id="task-gcp-failed",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="failed",
                    failure_reason="Orphaned: never dispatched to an agent",
                )
            )
            exec2 = PhaseExecution(
                id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1",
                status="in_progress",
            )
            session.add(exec2)

        with db_manager.session_scope() as session:
            _release_pending_phases_with_orphaned_task(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"


class TestCaseCompletedWithSuccessor:
    """Regression: this case only ever fires when last_completed's
    PhaseExecution.status is ALREADY "completed" (that's what puts it in the
    `completed` list). The old code re-ran the transition via
    _fire_phase_transition -> mark_phase_complete on that same phase_id,
    which always hit mark_phase_complete's own idempotency guard
    (execution.status == "completed") and returned "already_completed" -- a
    permanent no-op. The one real scenario this case exists for -- the
    process crashing between mark_phase_complete's commit of the goto/
    continue decision and _create_phase_task's Task-row insert for the
    successor -- could therefore never actually recover: every future poll
    repeated the same no-op forever, leaving the workflow permanently
    stalled with a completed phase, a pending successor, and zero tasks."""

    def _seed_completed_with_pending_successor(self, db_manager):
        """Matches real Workflow-creation shape: every Phase gets a
        PhaseExecution row upfront (status="pending") -- see
        phase_manager.py's Phase/PhaseExecution creation loop. The
        `sample_workflow` fixture only seeds phase-1's execution row, so
        phase-2 needs one added here for the claim (an UPDATE against an
        existing row) to have anything to claim."""
        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "completed"
            session.add(
                PhaseExecution(
                    id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1",
                    status="pending",
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_creates_successor_task_directly(self, mock_create, db_manager, sample_workflow):
        """The fix: call _create_phase_task for the successor directly
        instead of re-deciding an already-made decision."""
        from src.autopilot.orchestrator.phase_transitions import _case_completed_with_successor, _get_phase_statuses

        self._seed_completed_with_pending_successor(db_manager)
        mock_create.return_value = True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_completed_with_successor(
                session, "wf-1", completed, pending, in_progress, MagicMock()
            )

        assert result is True
        assert mock_create.call_args[0][:4] == ("wf-1", "phase-2", "implementation", "continue")

    def test_releases_its_own_claim_when_a_task_raced_in(self, db_manager, sample_workflow):
        """Same TOCTOU gap as _case_start_first_phase's own regression
        (see test_reverifies_existing_right_before_creating): a task
        committed by another claim-protected path in the window between
        this function's initial existing_tasks read and winning its own
        claim must be caught by a fresh re-check -- but the fix must also
        release the claim it just won when that happens, or it stays held
        for no reason, since _create_phase_task's own success path (which
        normally releases it) is never reached."""
        from src.autopilot.orchestrator.phase_transitions import _case_completed_with_successor, _get_phase_statuses

        self._seed_completed_with_pending_successor(db_manager)

        def _win_claim_but_race_a_task_in(*_args, **_kwargs):
            with db_manager.session_scope() as race_session:
                race_session.add(Task(
                    id="task-raced-in",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                ))
            return True

        with patch(
            "src.autopilot.orchestrator.phase_transitions._claim_phase_task_creation",
            side_effect=_win_claim_but_race_a_task_in,
        ), patch(
            "src.autopilot.orchestrator.phase_transitions._create_phase_task", return_value=True
        ) as mock_create, patch(
            "src.autopilot.orchestrator.phase_transitions._release_phase_task_creation_claim"
        ) as mock_release:
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                completed = [p for p in phase_statuses if p["status"] == "completed"]
                pending = [p for p in phase_statuses if p["status"] == "pending"]
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_completed_with_successor(
                    session, "wf-1", completed, pending, in_progress, MagicMock()
                )

                assert result is False
                mock_create.assert_not_called()
                mock_release.assert_called_once_with(session, "phase-2")

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_skips_when_successor_already_has_task(self, mock_create, db_manager, sample_workflow):
        from src.autopilot.orchestrator.phase_transitions import _case_completed_with_successor, _get_phase_statuses

        self._seed_completed_with_pending_successor(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-1",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                )
            )

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_completed_with_successor(
                session, "wf-1", completed, pending, in_progress, MagicMock()
            )

        assert result is False
        mock_create.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_creates_task_when_successors_only_task_is_duplicated(
        self, mock_create, db_manager, sample_workflow
    ):
        """Regression: a "duplicated" task left behind on the successor
        phase (e.g. a ticket-blocked git_expert/doc_review task routed to
        development, which names THIS phase as its own action_target_phase
        -- see _maybe_retry_failed_tasks's routing branch) must not read as
        "successor already has a task" -- same class of bug fixed in
        _case_in_progress_no_tasks, same "duplicated means doesn't count"
        convention this codebase already uses elsewhere. Observed live:
        workflow 81b399c7's git_expert phase was left exactly this way."""
        from src.autopilot.orchestrator.phase_transitions import _case_completed_with_successor, _get_phase_statuses

        self._seed_completed_with_pending_successor(db_manager)
        mock_create.return_value = True
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-duplicated",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="duplicated",
                )
            )

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_completed_with_successor(
                session, "wf-1", completed, pending, in_progress, MagicMock()
            )

        assert result is True
        mock_create.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_skips_when_claim_already_held(self, mock_create, db_manager, sample_workflow):
        """Simulates a concurrent caller having already claimed the
        successor's task creation -- this call must not also create one."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_completed_with_successor,
    _claim_phase_task_creation,
    _get_phase_statuses,
)

        self._seed_completed_with_pending_successor(db_manager)
        with db_manager.session_scope() as session:
            won = _claim_phase_task_creation(session, "phase-2")
            assert won is True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_completed_with_successor(
                session, "wf-1", completed, pending, in_progress, MagicMock()
            )

        assert result is False
        mock_create.assert_not_called()

    def test_honors_explicit_goto_target_over_next_pending_by_order(
        self, db_manager, sample_workflow
    ):
        """Regression, observed live: development, after fixing
        adversarial_review's BLOCKERs, goto's directly back to
        adversarial_review (order 6) -- deliberately skipping
        architectural_review (order 5), which is still sitting "pending"
        from an earlier, broader reset (adversarial_review's own goto back
        to development resets every phase at/after order 4). Blindly
        picking "next pending phase by order" finds architectural_review
        first and dispatches a redundant, unnecessary re-review -- even
        though development's own recorded decision (action="goto",
        action_target_phase="adversarial_review") already specified a
        different, later target. The explicit decision must win."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_completed_with_successor,
    _get_phase_statuses,
)

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "completed"
            session.add(
                Task(
                    id="task-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    action="goto",
                    action_target_phase="adversarial_review",
                    completion_notes="3 BLOCKERs verified fixed.",
                )
            )
            # phase-2 ("implementation") stands in for architectural_review:
            # still pending, deliberately being skipped.
            session.add(
                PhaseExecution(
                    id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1",
                    status="pending",
                )
            )
            # A later-order phase standing in for adversarial_review --
            # development's actual, explicit goto target.
            session.add(
                Phase(
                    id="phase-3", workflow_id="wf-1", name="adversarial_review",
                    order=3, description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-3", phase_id="phase-3", workflow_execution_id="wf-1",
                    status="pending",
                )
            )

        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task") as mock_create:
            mock_create.return_value = True
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                completed = [p for p in phase_statuses if p["status"] == "completed"]
                pending = [p for p in phase_statuses if p["status"] == "pending"]
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_completed_with_successor(
                    session, "wf-1", completed, pending, in_progress, MagicMock()
                )

        assert result is True
        args, kwargs = mock_create.call_args
        assert args[:4] == ("wf-1", "phase-3", "adversarial_review", "goto")
        assert kwargs["feedback"] == "3 BLOCKERs verified fixed."
        assert kwargs["source_phase_name"] == "requirements"

    def test_falls_back_to_order_when_no_explicit_target_recorded(
        self, db_manager, sample_workflow
    ):
        """Sanity check the fix isn't overbroad: a plain "continue"
        completion with no goto/retry target must still use the normal
        next-pending-by-order successor selection, unchanged."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_completed_with_successor,
    _get_phase_statuses,
)

        self._seed_completed_with_pending_successor(db_manager)
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    action="continue",
                )
            )

        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task") as mock_create:
            mock_create.return_value = True
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                completed = [p for p in phase_statuses if p["status"] == "completed"]
                pending = [p for p in phase_statuses if p["status"] == "pending"]
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                result = _case_completed_with_successor(
                    session, "wf-1", completed, pending, in_progress, MagicMock()
                )

        assert result is True
        assert mock_create.call_args[0][:4] == ("wf-1", "phase-2", "implementation", "continue")

    def test_picks_the_most_recently_completed_phase_not_the_highest_order(
        self, db_manager, sample_workflow
    ):
        """Regression, observed live (workflow ca539a75): a long-running,
        goto-heavy workflow can have a downstream, HIGH-order phase (e.g.
        forensics_analysis, order 12) still sitting "completed" from many
        hours/cycles ago, while an UPSTREAM, LOW-order phase (e.g.
        development, order 5) just NOW re-completed via a goto loop and
        recorded an explicit goto target. The old `completed.sort(key=
        phase.order); last_completed = completed[-1]` picked the STALE
        high-order phase every time -- its own unrelated "continue" action
        bore no relation to the low-order phase's actual goto, so the real
        successor was silently never found, even sitting right there in
        `pending`. Must pick by recency (completed_at), not order."""
        from src.autopilot.orchestrator.phase_transitions import (
    _case_completed_with_successor,
    _get_phase_statuses,
)

        with db_manager.session_scope() as session:
            # phase-1 ("requirements", order 1) stands in for development:
            # just completed NOW, with an explicit goto target.
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "completed"
            exec1.completed_at = datetime.utcnow()
            session.add(
                Task(
                    id="task-1",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    action="goto",
                    action_target_phase="git_expert",
                    completion_notes="Ticket resolved.",
                )
            )
            # phase-2 ("implementation", order 2) is a lower-order pending
            # decoy -- must NOT be picked just because it's the lowest-order
            # pending phase; the explicit goto target below must win.
            #
            # phase-4 (order 10) stands in for forensics_analysis: it
            # "completed" long ago, via an unrelated "continue" -- stale,
            # must be ignored despite its higher order.
            session.add(
                Phase(
                    id="phase-4", workflow_id="wf-1", name="forensics_analysis",
                    order=10, description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-4", phase_id="phase-4", workflow_execution_id="wf-1",
                    status="completed",
                    completed_at=datetime.utcnow() - timedelta(hours=7),
                )
            )
            session.add(
                Task(
                    id="task-4",
                    workflow_id="wf-1",
                    phase_id="phase-4",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    action="continue",
                    completed_at=datetime.utcnow() - timedelta(hours=7),
                )
            )
            # phase-3 (order 3): the real, explicit goto target.
            session.add(
                Phase(
                    id="phase-3", workflow_id="wf-1", name="git_expert",
                    order=3, description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-3", phase_id="phase-3", workflow_execution_id="wf-1",
                    status="pending",
                )
            )

        with patch("src.autopilot.orchestrator.phase_transitions._create_phase_task") as mock_create:
            mock_create.return_value = True
            with db_manager.session_scope() as session:
                phase_statuses = _get_phase_statuses(session, "wf-1")
                completed = [p for p in phase_statuses if p["status"] == "completed"]
                pending = [p for p in phase_statuses if p["status"] == "pending"]
                in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
                assert len(completed) == 2, "both phase-1 and phase-4 must be 'completed' for this to test anything"
                result = _case_completed_with_successor(
                    session, "wf-1", completed, pending, in_progress, MagicMock()
                )

        assert result is True
        args, kwargs = mock_create.call_args
        assert args[:4] == ("wf-1", "phase-3", "git_expert", "goto")
        assert kwargs["feedback"] == "Ticket resolved."
        assert kwargs["source_phase_name"] == "requirements"

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_ignores_a_done_task_from_a_prior_cycle(self, mock_create, db_manager, sample_workflow):
        """Regression, observed live: phase-2 succeeded once weeks ago,
        then got reset back to "pending" by a later goto for a fresh pass
        -- but its PhaseExecution never got a new started_at, and its only
        Task row is the old 'done' one from that first cycle. The old
        unscoped existing_tasks check saw that row, concluded "transition
        already fired", and returned False forever -- the phase stalled
        2+ days with zero tasks from its current cycle. Scoping to tasks
        created since last_completed's own completion must exclude the
        stale task and dispatch a fresh one."""
        from datetime import datetime, timedelta

        from src.autopilot.orchestrator.phase_transitions import _case_completed_with_successor, _get_phase_statuses

        self._seed_completed_with_pending_successor(db_manager)
        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.completed_at = datetime.utcnow()
            session.add(
                Task(
                    id="task-old",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    created_at=datetime.utcnow() - timedelta(days=21),
                    completed_at=datetime.utcnow() - timedelta(days=21),
                )
            )

        mock_create.return_value = True
        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_completed_with_successor(
                session, "wf-1", completed, pending, in_progress, MagicMock()
            )

        assert result is True
        assert mock_create.call_args[0][:4] == ("wf-1", "phase-2", "implementation", "continue")

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_does_not_duplicate_a_real_successor_task_created_a_few_ms_before_completion(
        self, mock_create, db_manager, sample_workflow
    ):
        """Regression, same clock-skew class as _correct_skewed_cycle_start
        (see its docstring): last_completed_execution.completed_at and the
        successor's own first task's created_at are stamped by independent
        utc_now() calls that can land a few milliseconds apart in either
        order. A bare `Task.created_at >= completed_at` would exclude a
        genuine successor task created moments before completed_at was
        stamped, see existing_tasks == 0, and dispatch a duplicate. The
        10s grace window on the boundary must not reopen the "weeks-old
        stale task" gap the test above guards against."""
        from datetime import datetime, timedelta

        from src.autopilot.orchestrator.phase_transitions import _case_completed_with_successor, _get_phase_statuses

        self._seed_completed_with_pending_successor(db_manager)
        completed_at = datetime.utcnow()
        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.completed_at = completed_at
            session.add(
                Task(
                    id="task-real-successor",
                    workflow_id="wf-1",
                    phase_id="phase-2",
                    raw_description="r",
                    done_definition="d",
                    status="pending",
                    created_at=completed_at - timedelta(milliseconds=15),
                )
            )

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_completed_with_successor(
                session, "wf-1", completed, pending, in_progress, MagicMock()
            )

        assert result is False  # "already fired" -- the real task is found, no duplicate
        mock_create.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_advance_phases_skips_this_case_when_workflow_paused_for_review(
        self, mock_create, db_manager, sample_workflow
    ):
        """Regression, observed live: workflow e6437c3f kept re-running its
        entire qa_validation -> ... -> deploy tail every ~6 minutes for
        hours after being paused_by="review" (every phase already
        "completed", nothing left but a human's approval). The narrow
        review-mode carve-out in _advance_phases (see
        TestAdvancePhases.test_review_paused_workflow_still_retries_an_
        unrelated_phase) exists to keep self-healing an unrelated ORPHANED
        in-progress task, but this case dispatches brand-new downstream
        work -- reset_stale_executions_on_goto resets a downstream
        PhaseExecution to "pending" whenever a goto/retry action fires,
        with no paused_by check of its own, so a review-paused workflow's
        last phase recording a stale goto still produces a pending
        "successor" that this case would otherwise happily dispatch a
        fresh task for on every sweep tick. Exercised through the full
        _advance_phases entry point (not the case function directly) since
        the paused_by guard lives at that call site."""
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        self._seed_completed_with_pending_successor(db_manager)
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "review"
            wf.status_reason = "All phases complete -- awaiting human review and merge approval"

        logger = MagicMock()
        result = _advance_phases("wf-1", logger)

        assert result is False
        mock_create.assert_not_called()

        with db_manager.session_scope() as session:
            exec2 = session.query(PhaseExecution).filter_by(phase_id="phase-2").first()
            assert exec2.status == "pending"
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.paused_by == "review"

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_marks_a_jumped_over_intermediate_phase_skipped(
        self, mock_create, db_manager, sample_workflow
    ):
        """Regression, observed live: workflow c1f0839c's design_review
        (order 4) sat PhaseExecution.status="pending" from 2026-08-23,
        after a goto jumped architecture_design (order 3) straight to
        development (order 5) via this exact case (a goto's
        action_target_phase, or a by-order successor pick, landing past an
        intervening phase). _start_next_phase (phase_manager.py) already
        downgrades a jumped-over "pending" phase to "skipped" for its OWN
        equivalent jump, but this case had no matching logic -- leaving
        design_review "pending" forever, which permanently blocked
        derive_workflow_status's completeness check even after every phase
        through deploy (order 14) had finished, so the workflow could
        never complete or pause for review."""
        from src.autopilot.orchestrator.phase_transitions import _case_completed_with_successor, _get_phase_statuses

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "completed"
            session.add(
                Phase(
                    id="phase-3", workflow_id="wf-1", name="deploy", order=3,
                    description="Deploy", done_definitions=["deployed"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1", status="pending",
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-3", phase_id="phase-3", workflow_execution_id="wf-1", status="pending",
                )
            )
            session.add(
                Task(
                    id="task-goto", workflow_id="wf-1", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="done",
                    action="goto", action_target_phase="deploy",
                )
            )
        mock_create.return_value = True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            completed = [p for p in phase_statuses if p["status"] == "completed"]
            pending = [p for p in phase_statuses if p["status"] == "pending"]
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_completed_with_successor(
                session, "wf-1", completed, pending, in_progress, MagicMock()
            )

        assert result is True
        assert mock_create.call_args[0][:4] == ("wf-1", "phase-3", "deploy", "goto")

        with db_manager.session_scope() as session:
            exec2 = session.query(PhaseExecution).filter_by(phase_id="phase-2").first()
            assert exec2.status == "skipped"


class TestGetPhaseStatuses:
    """Tests for _get_phase_statuses helper."""

    def test_returns_phase_statuses(self, db_manager, sample_workflow):
        """Should return all phases with their execution statuses."""
        from src.autopilot.orchestrator.phase_transitions import _get_phase_statuses

        with db_manager.session_scope() as session:
            statuses = _get_phase_statuses(session, "wf-1")

            assert len(statuses) == 2
            assert statuses[0]["phase"].name == "requirements"
            assert statuses[0]["status"] == "in_progress"
            assert statuses[1]["phase"].name == "implementation"
            assert statuses[1]["status"] == "pending"


class TestCreatePhaseTaskResetsClaim:
    """Regression: _create_phase_task is the only place that actually flips
    a GOTO target phase's PhaseExecution back to in_progress --
    _handle_evaluation_goto itself never touches the target phase's
    execution row at all. Every OTHER reopen point (_start_next_phase,
    _handle_evaluation_retry, _handle_evaluation_arbitrate) resets
    task_creation_claimed_at when it reopens a phase; this one didn't.
    Observed live: a phase visited earlier in the pipeline (its claim
    already consumed from that prior cycle) came back in_progress with a
    stale non-null task_creation_claimed_at. When its new task finished,
    _case_in_progress_complete's claim guard saw the stale claim and
    concluded another caller already owned the evaluation -- permanently
    skipping the transition. The task was done; the phase just never
    advanced, until a stall-detector eventually noticed and spawned a
    (confused, chasing-a-red-herring) diagnostic agent."""

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_resets_stale_claim_on_reactivation(self, mock_create_agent, db_manager, sample_workflow):
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        mock_create_agent.side_effect = _agent_row_side_effect("new-agent")

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "completed"
            # Stale claim from a prior cycle through this same phase.
            exec1.task_creation_claimed_at = datetime(2020, 1, 1)

        result = _create_phase_task("wf-1", "phase-1", "requirements", "goto", MagicMock())

        assert result is True
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_reactivated_phase_can_claim_after_completion(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """End-to-end version of the same regression: after reactivation via
        _create_phase_task, a fresh _claim_phase_task_creation call for that
        same phase must succeed -- proving the transition-evaluation claim
        isn't permanently blocked by the stale value from the prior cycle."""
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation, _create_phase_task

        mock_create_agent.side_effect = _agent_row_side_effect("new-agent")

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "completed"
            exec1.task_creation_claimed_at = datetime(2020, 1, 1)

        _create_phase_task("wf-1", "phase-1", "requirements", "goto", MagicMock())

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_resets_claim_when_entry_status_already_in_progress(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Regression: the reset used to be gated on entry status being
        "pending"/"completed", but _case_in_progress_no_tasks calls
        _create_phase_task for phases a DIFFERENT path (e.g. the
        synchronous /start_workflow_execution step) already flipped to
        "in_progress" before any task existed. For those, entry status is
        already "in_progress" so the old gate never matched, and the claim
        taken to create this task was never released -- permanently
        blocking this same phase's own later completion-transition check.
        Observed live: a Feature Architect task finished successfully but
        its phase sat in_progress forever, and a stall-detector kept
        spawning fresh replacement agents for it."""
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation, _create_phase_task

        mock_create_agent.side_effect = _agent_row_side_effect("new-agent")

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "in_progress"
            exec1.task_creation_claimed_at = datetime.utcnow()

        # target_already_claimed=True: mirrors the real call site
        # (_case_in_progress_no_tasks), which takes this exact claim
        # itself immediately before calling _create_phase_task for the
        # same phase_id -- without it, _create_phase_task would now try
        # to claim phase-1 a second time on top of the fresh claim just
        # simulated above and correctly refuse (a genuinely different
        # scenario: two independent, uncoordinated callers).
        result = _create_phase_task(
            "wf-1", "phase-1", "requirements", "continue", MagicMock(),
            target_already_claimed=True,
        )

        assert result is True
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            assert _claim_phase_task_creation(session, "phase-1") is True

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_reopens_a_previously_skipped_phase(self, mock_create_agent, db_manager, sample_workflow):
        """Regression: a phase whose PhaseExecution reads "skipped" (e.g. an
        optional review phase the pipeline skipped on its first pass) must
        also be reopened to "in_progress" when a later goto/redo cycle
        creates a real task for it -- the reopen condition only checked
        "pending"/"completed", so a "skipped" phase kept that status even
        with a live task running against it. derive_workflow_status treats
        "skipped" as terminal, same as "completed", so the whole workflow
        got derived (and write-back committed) as "completed" while this
        task was still actively in flight -- which then got the task itself
        killed as a false "Orphaned: workflow already completed", and let
        the design queue advance to the next feature before this redo cycle
        (and its pending human review) ever finished. Confirmed live: task
        860508ac (adversarial_review, workflow ca539a75)."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        mock_create_agent.side_effect = _agent_row_side_effect("new-agent")

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "skipped"
            exec1.started_at = None

        result = _create_phase_task("wf-1", "phase-1", "requirements", "goto", MagicMock())

        assert result is True
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "in_progress"
            assert execution.started_at is not None


class TestCreatePhaseTaskClaimsTargetPhase:
    """Regression: _fire_phase_transition and _resolve_arbitration_outcome
    both claim the SOURCE phase they're evaluating before calling
    _create_phase_task, but its target_phase_id is routinely a DIFFERENT
    phase (a goto target) that nothing claims -- _create_phase_task's own
    existing-task check openly admits it can't fully cover this alone (a
    task mid-dispatch, still "pending" with no agent, looks exactly like a
    genuine orphan once its age crosses the 1-minute cutoff). Observed
    live: two goto tasks landed on architecture_design 85s apart. Fixed by
    having _create_phase_task claim its own phase_id when the caller
    hasn't already (target_already_claimed=False, the default) -- this
    class tests that claim directly, independent of the reset-on-
    reactivation behavior covered above."""

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_refuses_when_target_phase_is_freshly_claimed_by_another_caller(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation, _create_phase_task

        mock_create_agent.side_effect = _agent_row_side_effect("new-agent")

        with db_manager.session_scope() as session:
            # A genuinely live, concurrent claim -- e.g. another
            # _create_phase_task call for this same phase_id already in
            # flight, mid-dispatch.
            assert _claim_phase_task_creation(session, "phase-1") is True

        result = _create_phase_task("wf-1", "phase-1", "requirements", "goto", MagicMock())

        assert result is False
        mock_create_agent.assert_not_called()
        with db_manager.session_scope() as session:
            # The fresh claim is still held -- untouched by our refused attempt.
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_claims_and_creates_when_target_phase_is_unclaimed(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """The default (target_already_claimed=False) path: no pre-existing
        claim on phase_id at all -- _create_phase_task takes one itself,
        creates the task, and releases it."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        mock_create_agent.side_effect = _agent_row_side_effect("new-agent")

        result = _create_phase_task("wf-1", "phase-1", "requirements", "continue", MagicMock())

        assert result is True
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None


class TestFirePhaseTransition:
    """Tests for _fire_phase_transition function."""

    @patch("src.autopilot.orchestrator.phase_transitions.PhaseManager")
    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_fires_transition_successfully(self, mock_create, mock_pm_class, db_manager, sample_workflow):
        """Should fire phase transition and create next task."""
        from src.autopilot.orchestrator.phase_transitions import _fire_phase_transition

        # Mock phase manager
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase_id": "phase-2",
            "target_phase": "implementation",
        }
        mock_create.return_value = True

        logger = MagicMock()
        result = _fire_phase_transition("wf-1", "phase-1", "requirements", logger)

        assert result is True
        mock_pm.mark_phase_complete.assert_called_once()
        mock_create.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions.PhaseManager")
    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_security_review_with_open_bug_ticket_forces_goto_to_development(
        self, mock_create, mock_pm_class, db_manager, sample_workflow
    ):
        """security_review's own gate (score_security_review) only scores
        unresolved critical/high vulnerabilities -- a medium/low finding it
        deliberately tickets instead of fixing is, by design, invisible to
        that gate. Without this check, the ticket rides through
        qa_validation and product_validation untouched (neither phase's own
        "done" claim is gated on open tickets either) and only gets caught
        once doc_review's own hard floor rejects it, two full review passes
        later than the ticket was already known. Must redirect straight to
        development instead, via the same forced-goto machinery a real gate
        decision uses -- not a post-hoc override of the normal "continue"
        result, which would leave qa_validation's PhaseExecution wrongly
        flipped to in_progress by _start_next_phase's own bookkeeping."""
        from src.autopilot.orchestrator.phase_transitions import _fire_phase_transition
        from src.core.database import Agent, Ticket

        with db_manager.session_scope() as session:
            session.add(Agent(id="agent-sec", system_prompt="p", status="idle", cli_type="pi"))
            session.add(Ticket(
                id="ticket-1", workflow_id="wf-1", created_by_agent_id="agent-sec",
                title="[BUG] Outdated dependency", description="d",
                ticket_type="bug", priority="medium", status="open",
            ))

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "goto",
            "target_phase_id": "phase-dev",
            "target_phase": "development",
            "reason": "forced",
            "metadata": {},
        }
        mock_create.return_value = True

        logger = MagicMock()
        result = _fire_phase_transition("wf-1", "phase-1", "security_review", logger)

        assert result is True
        call_kwargs = mock_pm.mark_phase_complete.call_args.kwargs
        assert call_kwargs.get("force_action") == "goto"
        assert call_kwargs.get("force_target_phase") == "development"
        assert "ticket-1" in call_kwargs.get("force_reason", "")
        args, _ = mock_create.call_args
        assert args[1] == "phase-dev"
        assert args[2] == "development"
        assert args[3] == "goto"

    @patch("src.autopilot.orchestrator.phase_transitions.PhaseManager")
    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_security_review_with_no_open_tickets_continues_normally(
        self, mock_create, mock_pm_class, db_manager, sample_workflow
    ):
        """No open bug tickets -- the normal evaluation path must run
        unmodified, not the forced-goto branch."""
        from src.autopilot.orchestrator.phase_transitions import _fire_phase_transition

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase_id": "phase-2",
            "target_phase": "qa_validation",
        }
        mock_create.return_value = True

        logger = MagicMock()
        result = _fire_phase_transition("wf-1", "phase-1", "security_review", logger)

        assert result is True
        call_kwargs = mock_pm.mark_phase_complete.call_args.kwargs
        assert "force_action" not in call_kwargs
        args, _ = mock_create.call_args
        assert args[1] == "phase-2"
        assert args[2] == "qa_validation"

    @patch("src.autopilot.orchestrator.phase_transitions.PhaseManager")
    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.phase_transitions.build_phase_output")
    def test_gated_phase_with_working_directory_does_not_raise(
        self, mock_build_output, mock_create, mock_pm_class, db_manager, sample_workflow, tmp_path
    ):
        """Regression: a redundant `from pathlib import Path` inside this
        function (after Path was already used on an earlier line in the
        same block) made Python treat Path as local to the whole function,
        so the earlier use raised UnboundLocalError every single time for
        any of the three GATED_PHASES (scope_review, qa_validation,
        product_validation). Silently caught by this function's own
        try/except and logged as "[PHASE-ADVANCE] Transition error" --
        meaning a gated phase could complete its task but never actually
        advance, forever. Only a gated phase name with a real,
        existing working_directory exercises the buggy line at all (the
        Path(...).exists() check on the same line as the redundant import).
        """
        from src.autopilot.orchestrator.phase_transitions import _fire_phase_transition

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.working_directory = str(tmp_path)

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase_id": "phase-2",
            "target_phase": "architecture_design",
        }
        mock_create.return_value = True
        mock_build_output.return_value = {}

        logger = MagicMock()
        result = _fire_phase_transition("wf-1", "phase-1", "scope_review", logger)

        assert result is True
        logger.warning.assert_not_called()  # no "[PHASE-ADVANCE] Transition error"
        mock_build_output.assert_called_once()
        mock_pm.mark_phase_complete.assert_called_once()

    @patch("src.autopilot.orchestrator.phase_transitions.PhaseManager")
    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_prefers_completing_tasks_own_notes_over_result_missing_reason(
        self, mock_create, mock_pm_class, db_manager, sample_workflow
    ):
        """Regression, observed live: an adversarial_review gate scored
        "no adversarial_review_result.json found" (result_missing=True) at
        the exact instant it evaluated -- a pure file-read timing artifact
        -- even though the reviewing agent's own completion_notes described
        3 concrete BLOCKERs it had genuinely found and reported. The
        resulting goto embedded the generic "missing" message as the
        corrective development task's "WHY YOU'RE HERE" reason, so the
        developer had to rediscover the real findings itself instead of
        being told directly. A "result_missing" gate reason says nothing
        about whether the agent actually did the work -- the completing
        task's own completion_notes, when present, is strictly more
        accurate and must win."""
        from src.autopilot.orchestrator.phase_transitions import _fire_phase_transition

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-review-done",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    completion_notes="Adversarial review found 3 BLOCKERs: B-1 ..., B-2 ..., B-3 ...",
                )
            )

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "goto",
            "target_phase_id": "phase-2",
            "target_phase": "development",
            "reason": "no adversarial_review_result.json found",
            "metadata": {
                "spec_gate": {
                    "reason": "no adversarial_review_result.json found",
                    "result_missing": True,
                }
            },
        }
        mock_create.return_value = True

        logger = MagicMock()
        result = _fire_phase_transition("wf-1", "phase-1", "adversarial_review", logger)

        assert result is True
        _, kwargs = mock_create.call_args
        assert kwargs["feedback"] == (
            "Adversarial review found 3 BLOCKERs: B-1 ..., B-2 ..., B-3 ..."
        )

    @patch("src.autopilot.orchestrator.phase_transitions.PhaseManager")
    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    def test_uses_missing_reason_when_no_completion_notes_available(
        self, mock_create, mock_pm_class, db_manager, sample_workflow
    ):
        """Sanity check the fix isn't overbroad: with no completing task
        (or no completion_notes on it), the gate's own "missing" reason is
        still the best available signal and must be used as before."""
        from src.autopilot.orchestrator.phase_transitions import _fire_phase_transition

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "goto",
            "target_phase_id": "phase-2",
            "target_phase": "development",
            "reason": "no adversarial_review_result.json found",
            "metadata": {
                "spec_gate": {
                    "reason": "no adversarial_review_result.json found",
                    "result_missing": True,
                }
            },
        }
        mock_create.return_value = True

        logger = MagicMock()
        _fire_phase_transition("wf-1", "phase-1", "adversarial_review", logger)

        _, kwargs = mock_create.call_args
        assert kwargs["feedback"] == "no adversarial_review_result.json found"


class TestFirePhaseTransitionArbitrate:
    """Regression: the "arbitrate" action from PhaseManager was a dead-end
    TODO stub -- it logged a warning and did nothing else, leaving the
    phase's PhaseExecution.status="pending" forever with no agent ever
    dispatched to resolve it. Must now actually trigger arbitration."""

    @patch("src.autopilot.orchestrator.phase_transitions._trigger_arbitration")
    @patch("src.autopilot.orchestrator.phase_transitions.PhaseManager")
    def test_arbitrate_action_triggers_arbitration(
        self, mock_pm_class, mock_trigger, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _fire_phase_transition

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "arbitrate",
            "target_phase_id": "phase-1",
            "target_phase": "requirements",
            "reason": "budget exhausted after 3 retries",
        }

        result = _fire_phase_transition("wf-1", "phase-1", "requirements", MagicMock())

        assert result is True
        mock_trigger.assert_called_once_with(
            "wf-1", "phase-1", "requirements", "budget exhausted after 3 retries", ANY
        )


class TestCreatePhaseTaskExhaustionArbitrates:
    """Regression: hitting the cross-source retry/goto bound used to pause
    the whole workflow silently (wf.status="paused", paused_by=None, no
    reason anywhere but one WARNING log line) with nothing to ever resume
    it short of a human noticing. Must trigger arbitration instead, and the
    workflow must stay "active" -- arbitration is live, visible progress,
    not a silent stop."""

    @patch("src.autopilot.orchestrator.phase_transitions._trigger_arbitration")
    def test_exhausting_bound_triggers_arbitration_not_pause(
        self, mock_trigger, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        # 5 prior orchestrator-created retry/goto tasks for phase-1 --
        # MAX_PHASE_ATTEMPTS is 5, so the 6th attempt must exhaust it.
        with db_manager.session_scope() as session:
            for i in range(5):
                session.add(
                    Task(
                        id=f"prior-{i}",
                        raw_description="r",
                        done_definition="d",
                        status="failed",
                        phase_id="phase-1",
                        workflow_id="wf-1",
                        created_by_agent_id="orchestrator",
                        action="goto",
                    )
                )

        result = _create_phase_task(
            "wf-1", "phase-1", "requirements", "goto", MagicMock(), feedback="still failing"
        )

        assert result is False
        mock_trigger.assert_called_once()
        args = mock_trigger.call_args[0]
        assert args[0] == "wf-1"
        assert args[1] == "phase-1"
        assert args[2] == "requirements"
        assert "still failing" in args[3]

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"  # never silently paused

    def test_four_prior_attempts_does_not_exhaust(self, db_manager, sample_workflow):
        """Confirms the bound is actually 5, not still 3 -- one attempt
        short of exhaustion must proceed normally."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        with db_manager.session_scope() as session:
            for i in range(4):
                session.add(
                    Task(
                        id=f"prior-{i}",
                        raw_description="r",
                        done_definition="d",
                        status="failed",
                        phase_id="phase-1",
                        workflow_id="wf-1",
                        created_by_agent_id="orchestrator",
                        action="goto",
                    )
                )

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("a1"),
        ):
            result = _create_phase_task(
                "wf-1", "phase-1", "requirements", "goto", MagicMock()
            )

        assert result is True

    def test_goto_records_source_phase_as_action_target_phase(
        self, db_manager, sample_workflow
    ):
        """Regression: Task.action="goto" has meant "why was I created"
        since before this session (set here, at creation, by
        _create_phase_task) -- a DIFFERENT, non-conflicting piece of
        information than _tag_completing_task's "what did I decide when I
        completed" (set on the phase that DECIDED the goto, a different
        task row). Without source_phase_name, a goto-created task's own
        action_target_phase was never populated at all, showing a bare
        "goto" badge with no indication of which phase's finding sent it
        back."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Agent

        # create_agent_for_task_direct is mocked below (this test isn't
        # exercising real agent dispatch) but the Task row it returns an id
        # for still gets an UPDATE against Agent.id via a real FK -- must
        # exist for that update to succeed under PRAGMA foreign_keys=ON.
        with db_manager.session_scope() as session:
            session.add(Agent(id="a1", system_prompt="p", status="working", cli_type="pi"))

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("a1"),
        ):
            result = _create_phase_task(
                "wf-1", "phase-1", "requirements", "goto", MagicMock(),
                feedback="Scope drift detected — missing FR-3",
                source_phase_name="scope_review",
            )

        assert result is True
        with db_manager.session_scope() as session:
            task = (
                session.query(Task)
                .filter_by(phase_id="phase-1", action="goto")
                .order_by(Task.created_at.desc())
                .first()
            )
            assert task is not None
            assert task.action_target_phase == "scope_review"

    def test_continue_never_sets_action_target_phase(self, db_manager, sample_workflow):
        """source_phase_name is irrelevant for normal forward advancement --
        must not leak onto a "continue"-created task even if a caller
        passed one."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task
        from src.core.database import Agent

        with db_manager.session_scope() as session:
            session.add(Agent(id="a1", system_prompt="p", status="working", cli_type="pi"))

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("a1"),
        ):
            result = _create_phase_task(
                "wf-1", "phase-1", "requirements", "continue", MagicMock(),
                source_phase_name="some_prior_phase",
            )

        assert result is True
        with db_manager.session_scope() as session:
            task = (
                session.query(Task)
                .filter_by(phase_id="phase-1", action="continue")
                .order_by(Task.created_at.desc())
                .first()
            )
            assert task is not None
            assert task.action_target_phase is None


class TestCreatePhaseTaskRetryBoundHonorsConfig:
    """Regression: _create_phase_task's retry/goto bound used to be a
    hardcoded constant (5), completely disconnected from
    eval_point.max_retries -- the config-driven budget
    WorkflowOrchestrator.evaluate() actually enforces. An operator setting
    max_retries higher than 5 in workflow.yaml got silently overridden:
    this bound force-arbitrated at 5 regardless. Now reads the same
    config value, so the two independent counting mechanisms (this one's
    DB Task-row count, evaluate()'s in-memory per-call counter) agree on
    the threshold."""

    def _seed(self, db_manager, max_retries):
        with db_manager.session_scope() as session:
            session.add(
                WorkflowDefinition(
                    id="def-configured",
                    name="Configured",
                    orchestrator_config={
                        "type": "evaluating",
                        "evaluation_points": [
                            {"after_phase": "requirements", "max_retries": max_retries}
                        ],
                    },
                )
            )
            session.add(
                Workflow(
                    id="wf-configured",
                    name="Configured Workflow",
                    status="active",
                    phases_folder_path="/tmp",
                    definition_id="def-configured",
                )
            )
            session.add(
                Phase(
                    id="phase-configured",
                    workflow_id="wf-configured",
                    name="requirements",
                    order=1,
                    description="Gather requirements",
                    done_definitions=["requirements documented"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-configured",
                    phase_id="phase-configured",
                    workflow_execution_id="wf-configured",
                    status="in_progress",
                )
            )
            for i in range(8):
                session.add(
                    Task(
                        id=f"configured-prior-{i}",
                        raw_description="r",
                        done_definition="d",
                        status="failed",
                        phase_id="phase-configured",
                        workflow_id="wf-configured",
                        created_by_agent_id="orchestrator",
                        action="goto",
                    )
                )

    @patch("src.autopilot.orchestrator.phase_transitions._trigger_arbitration")
    def test_higher_configured_max_retries_is_not_overridden_by_hardcoded_default(
        self, mock_trigger, db_manager
    ):
        """8 prior attempts, workflow.yaml says max_retries=10 -- must NOT
        arbitrate yet (the old hardcoded bound of 5 would have)."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        self._seed(db_manager, max_retries=10)

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("a1"),
        ):
            with db_manager.session_scope() as session:
                session.add(Agent(id="a1", system_prompt="p", status="working", cli_type="pi"))
            result = _create_phase_task(
                "wf-configured", "phase-configured", "requirements", "goto", MagicMock(),
            )

        assert result is True
        mock_trigger.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions._trigger_arbitration")
    def test_lower_configured_max_retries_arbitrates_before_the_old_hardcoded_bound(
        self, mock_trigger, db_manager
    ):
        """8 prior attempts, workflow.yaml says max_retries=3 -- must
        arbitrate (the old hardcoded bound of 5 would have allowed this
        through)."""
        from src.autopilot.orchestrator.phase_transitions import _create_phase_task

        self._seed(db_manager, max_retries=3)

        result = _create_phase_task(
            "wf-configured", "phase-configured", "requirements", "goto", MagicMock(),
            feedback="still drifting",
        )

        assert result is False
        mock_trigger.assert_called_once()


class TestArbitrationDoesNotConfuseAdvancement:
    """Gap found in review: _trigger_arbitration (and the pre-existing
    _handle_evaluation_arbitrate it now actually wires up) left the
    arbitrating phase's PhaseExecution.status = "pending" -- mid-pipeline,
    with LATER phases already "completed" (the phase that fired the
    goto/arbitrate decision closes its own execution to "completed" before
    handing off). _case_completed_with_successor picks its target by
    "next pending phase with order > the LATEST completed phase's order",
    not "the next phase in full pipeline order regardless of completion" --
    so a "pending" arbitrating phase sitting BEHIND a later-order completed
    phase is invisible to it, and it races ahead to whatever pending phase
    comes after the latest completed one instead, completely bypassing the
    phase actually awaiting arbitration."""

    def _seed_realistic_pipeline(self, db_manager):
        """development (order 4) exhausted its budget and is awaiting
        arbitration; security_review (7) and qa_validation (8) already ran
        and completed (qa_validation is what fired the goto that triggered
        arbitration); product_validation (9) has never been touched."""
        with db_manager.session_scope() as session:
            # Delete in FK-safe order
            # Agent.current_task_id → Task.id: null out before deleting tasks
            from src.core.database import Agent
            for a in session.query(Agent).all():
                a.current_task_id = None
            session.flush()
            session.query(Task).delete()
            session.query(PhaseExecution).delete()
            session.query(Phase).delete()

            completed_orders = {1, 2, 3, 7, 8}
            names = [
                (1, "product_requirements"), (2, "scope_review"),
                (3, "architecture_design"), (4, "development"),
                (7, "security_review"), (8, "qa_validation"),
                (9, "product_validation"),
            ]
            for order, name in names:
                session.add(
                    Phase(
                        id=f"phase-{order}", workflow_id="wf-1", order=order,
                        name=name, description="d", done_definitions=["x"],
                    )
                )
                session.add(
                    PhaseExecution(
                        id=f"exec-{order}", phase_id=f"phase-{order}",
                        workflow_execution_id="wf-1",
                        status="completed" if order in completed_orders else "pending",
                    )
                )

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_advance_phases_does_not_bypass_arbitrating_phase(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _advance_phases, _trigger_arbitration

        self._seed_realistic_pipeline(db_manager)
        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")
        _trigger_arbitration(
            "wf-1", "phase-4", "development", "exhausted 5 attempts", MagicMock()
        )

        # _advance_phases must NOT race ahead to product_validation (order 9)
        # while development (order 4) is still awaiting arbitration.
        _advance_phases("wf-1", MagicMock())

        with db_manager.session_scope() as session:
            pv_tasks = session.query(Task).filter_by(phase_id="phase-9").count()
            assert pv_tasks == 0, (
                "product_validation got a task created while development "
                "was still awaiting arbitration -- arbitration was bypassed"
            )


class TestTriggerArbitration:
    """_trigger_arbitration: spawns a one-shot arbitration agent, claiming
    the phase's task_creation_claimed_at so a repeat sweep tick can't spawn
    a duplicate. See _maybe_resolve_arbitration for consumption."""

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_creates_task_and_dispatches_arbitration_agent(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted 5 attempts", MagicMock()
        )

        assert result is True
        mock_create_agent.assert_called_once()
        _, kwargs = mock_create_agent.call_args
        # Not "arbitration" -- Agent.agent_type's CHECK constraint doesn't
        # allow it (see test_agent_type_satisfies_the_check_constraint).
        # "diagnostic" is the deliberate substitute -- prompt_builder.py
        # treats the two identically for prompt-building purposes.
        assert kwargs["agent_type"] == "diagnostic"
        assert "validation_prompt" in kwargs["enriched_data_override"]
        assert "requirements" in kwargs["enriched_data_override"]["validation_prompt"]

        with db_manager.session_scope() as session:
            task = (
                session.query(Task)
                .filter_by(created_by_agent_id=ARBITRATION_CREATED_BY)
                .first()
            )
            assert task is not None
            assert task.phase_id == "phase-1"

            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None
            # "in_progress", not "pending" -- see TestArbitrationDoesNotConfuseAdvancement
            # for why "pending" here bypasses the phase entirely.
            assert execution.status == "in_progress"

            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert "requirements" in wf.status_reason
            assert "exhausted 5 attempts" in wf.status_reason

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_creates_its_own_sentinel_agent_if_missing(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Regression: this file's own _seed_sentinel_agents autouse
        fixture pre-creates the ARBITRATION_CREATED_BY Agent row for
        every test in this file specifically because, per its own
        docstring, "any code path that sets
        task.created_by_agent_id=ARBITRATION_CREATED_BY FK-fails"
        without it -- a test-only workaround for a gap that was never
        actually closed in _trigger_arbitration itself. Production never
        seeds that row, so every real call hit
        sqlite3.IntegrityError: FOREIGN KEY constraint failed on the
        Task insert, silently caught by _fire_phase_transition's
        catch-all and logged as "[PHASE-ADVANCE] Transition error" --
        arbitration could never actually happen. Observed live: 1180+
        failed attempts over ~30 hours on one workflow, zero arbitration
        tasks ever created. This test undoes the fixture's seeding to
        reproduce the true production condition."""
        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        with db_manager.session_scope() as session:
            session.query(Agent).filter_by(id=ARBITRATION_CREATED_BY).delete()

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted 5 attempts", MagicMock()
        )

        assert result is True
        with db_manager.session_scope() as session:
            assert session.query(Agent).filter_by(id=ARBITRATION_CREATED_BY).first() is not None
            task = (
                session.query(Task)
                .filter_by(created_by_agent_id=ARBITRATION_CREATED_BY)
                .first()
            )
            assert task is not None

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_agent_type_satisfies_the_check_constraint(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Regression: Agent.agent_type has a CHECK constraint ('phase',
        'validator', 'result_validator', 'monitor', 'diagnostic',
        'orchestrator') that "arbitration" was never a member of -- every
        real (non-mocked) call from create_agent_for_task_direct into
        AgentManager.create_agent_for_task raised sqlite3.IntegrityError:
        CHECK constraint failed on the Agent insert, caught and logged
        only at DEBUG (invisible at the default log level) and returned
        as None, so every arbitration dispatch failed silently --
        _trigger_arbitration always hit its "if not agent_data" branch
        and failed the workflow, even after the Task-creation FK bug was
        separately fixed. This mocks create_agent_for_task_direct like
        the other tests in this class (a full dispatch is a heavier
        integration concern), but then actually tries to persist an
        Agent row with the captured agent_type value against the real
        schema -- the authoritative check, not a hardcoded copy of the
        constraint's allowed list that could itself drift out of sync."""
        from src.core.database import Agent as _Agent
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted", MagicMock()
        )

        _, kwargs = mock_create_agent.call_args
        agent_type = kwargs["agent_type"]

        with db_manager.session_scope() as session:
            session.add(_Agent(
                id="agent-type-check-probe",
                system_prompt="x", status="idle", cli_type="pi",
                agent_type=agent_type,
            ))

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_prompt_lists_valid_phase_names(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """An LLM-hallucinated/mis-cased target_phase makes goto silently
        fall back (see TestResolveArbitrationOutcome's unresolvable-target
        test) -- enumerating the real, exact names up front is the
        prevention half of that defense, not just the fallback."""
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted", MagicMock()
        )

        _, kwargs = mock_create_agent.call_args
        prompt = kwargs["enriched_data_override"]["validation_prompt"]
        assert "requirements" in prompt
        assert "implementation" in prompt  # sample_workflow's phase-2

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_caps_repeated_arbitration_forces_continue_with_no_project_to_check(
        self, mock_create_agent, mock_create_agent_pt, db_manager, sample_workflow
    ):
        """A persistently-confused arbiter that keeps choosing "goto" back
        into a phase that keeps re-exhausting its budget must not be able
        to cycle forever (5 real attempts, arbitrate, goto, 5 more,
        arbitrate again...). Past MAX_ARBITRATIONS_PER_PHASE, this used to
        unconditionally fail the workflow -- now it forces the phase
        through instead (see the review-mode-escalation and full-autopilot
        tests below for the two real outcomes); sample_workflow has no
        project_id, so review mode can't be determined and this defaults
        to the same full-autopilot "force continue" behavior a project
        with review_mode=False would get."""
        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")
        mock_create_agent_pt.side_effect = _agent_row_side_effect("next-phase-agent")

        # 3 prior arbitration tasks already exist for this phase, plus the
        # next phase's own PhaseExecution row -- sample_workflow only seeds
        # one for phase-1, but a real workflow has one for every phase from
        # initialization, and _create_phase_task's claim is a no-op UPDATE
        # (0 rows matched, not an error) against a phase with none at all.
        with db_manager.session_scope() as session:
            session.add(PhaseExecution(id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1", status="pending"))
            for i in range(3):
                session.add(
                    Task(
                        id=f"prior-arb-{i}",
                        raw_description="Arbitrate stuck phase: requirements",
                        done_definition="x",
                        status="done",
                        phase_id="phase-1",
                        workflow_id="wf-1",
                        created_by_agent_id=ARBITRATION_CREATED_BY,
                        action="arbitrate",
                    )
                )

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "still not converging", MagicMock()
        )

        assert result is True
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "completed"
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status != "failed"

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_caps_repeated_arbitration_escalates_to_human_in_review_mode(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """When the project has review_mode enabled, an exhausted
        arbitration budget must pause for a human decision instead of
        silently failing OR silently forcing through -- creates the same
        request-file pulsing notification prompt_human uses (no frontend
        change needed), and pauses non-blocking (paused_by="review") since
        this can be reached from the shared background sweep, which would
        otherwise be frozen for every other workflow by an inline
        blocking wait."""
        import glob
        import json as _json

        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration
        from src.core.constants import AUTOPILOT_STATE_DIR

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-review", name="p", base_dir="/tmp", review_mode=True))
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.project_id = "proj-review"
            for i in range(3):
                session.add(
                    Task(
                        id=f"prior-arb-{i}",
                        raw_description="Arbitrate stuck phase: requirements",
                        done_definition="x",
                        status="done",
                        phase_id="phase-1",
                        workflow_id="wf-1",
                        created_by_agent_id=ARBITRATION_CREATED_BY,
                        action="arbitrate",
                    )
                )

        before = set(glob.glob(f"{AUTOPILOT_STATE_DIR}/input_request_*.json"))
        try:
            result = _trigger_arbitration(
                "wf-1", "phase-1", "requirements", "still not converging", MagicMock()
            )

            assert result is False
            mock_create_agent.assert_not_called()
            with db_manager.session_scope() as session:
                wf = session.query(Workflow).filter_by(id="wf-1").first()
                assert wf.status == "paused"
                assert wf.paused_by == "review"
                assert "ARBITRATION-ESCALATION" in wf.status_reason
                assert "requirements" in wf.status_reason

            after = set(glob.glob(f"{AUTOPILOT_STATE_DIR}/input_request_*.json"))
            new_files = after - before
            assert len(new_files) == 1
            data = _json.loads(Path(new_files.pop()).read_text())
            assert data["workflow_id"] == "wf-1"
            assert data["phase_id"] == "phase-1"
            assert data["kind"] == "arbitration_escalation"
            assert set(data["options"]) == {"c", "s"}
        finally:
            for f in glob.glob(f"{AUTOPILOT_STATE_DIR}/input_request_*.json"):
                if f not in before:
                    Path(f).unlink(missing_ok=True)

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_re_evaluating_an_already_escalated_phase_does_not_create_a_second_request(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Regression: this phase's task is "done" and its PhaseExecution
        stays "in_progress" forever (nothing here ever completes/fails
        it) -- without a durable guard, _case_in_progress_complete re-
        fires this exact gate evaluation on EVERY sweep tick (~20s)
        regardless of the workflow-level pause, since design_review isn't
        an "unrelated" in-progress phase the "review" pause carve-out is
        meant to keep flowing past -- it's the one phase that caused the
        pause. Each re-fire landing back in _trigger_arbitration must not
        create a SECOND request/overwrite status_reason with a new
        request_id -- that would orphan the first request/response file
        pair a human may already be mid-response to, with no way to ever
        resolve the abandoned one. Confirmed by calling _trigger_arbitration
        twice for the same still-exhausted, still-unresolved phase."""
        import glob

        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration
        from src.core.constants import AUTOPILOT_STATE_DIR

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-review", name="p", base_dir="/tmp", review_mode=True))
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.project_id = "proj-review"
            for i in range(3):
                session.add(
                    Task(
                        id=f"prior-arb-{i}",
                        raw_description="Arbitrate stuck phase: requirements",
                        done_definition="x",
                        status="done",
                        phase_id="phase-1",
                        workflow_id="wf-1",
                        created_by_agent_id=ARBITRATION_CREATED_BY,
                        action="arbitrate",
                    )
                )

        before = set(glob.glob(f"{AUTOPILOT_STATE_DIR}/input_request_*.json"))
        try:
            result1 = _trigger_arbitration(
                "wf-1", "phase-1", "requirements", "still not converging", MagicMock()
            )
            with db_manager.session_scope() as session:
                status_reason_after_first = session.query(Workflow).filter_by(id="wf-1").first().status_reason

            # A later sweep tick re-evaluates the SAME still-in-progress,
            # still-exhausted phase again -- same call, nothing resolved
            # in between.
            result2 = _trigger_arbitration(
                "wf-1", "phase-1", "requirements", "still not converging", MagicMock()
            )

            assert result1 is False
            assert result2 is False
            with db_manager.session_scope() as session:
                wf = session.query(Workflow).filter_by(id="wf-1").first()
                assert wf.status_reason == status_reason_after_first, "must not overwrite the pending request's marker"

            after = set(glob.glob(f"{AUTOPILOT_STATE_DIR}/input_request_*.json"))
            new_files = after - before
            assert len(new_files) == 1, "a second call must not create a second request file"
        finally:
            for f in glob.glob(f"{AUTOPILOT_STATE_DIR}/input_request_*.json"):
                if f not in before:
                    Path(f).unlink(missing_ok=True)

    @patch("src.autopilot.orchestrator.phase_transitions._fire_phase_transition")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    @patch("src.autopilot.orchestrator.arbitration.build_phase_output")
    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_cap_exhausted_with_no_pending_decision_advances_if_output_already_passes(
        self, mock_create_agent, mock_build_output, mock_pm_class, mock_fire_transition,
        db_manager, sample_workflow, tmp_path
    ):
        """Regression: once the arbitration cap is hit AND there's no
        pending decision left to resolve (already consumed, or the last
        arbitration agent never wrote one), the old behavior was to fail
        the workflow unconditionally -- even if a later redo cycle had
        already fixed the underlying issue and the phase's real output
        now genuinely passes. Check the current output fresh (bypassing
        WorkflowOrchestrator.evaluate()'s retry-count gate, which would
        otherwise short-circuit straight back to "arbitrate" without ever
        reading the score) and advance instead of failing when it does."""
        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.working_directory = str(tmp_path)
            # 3 prior arbitration tasks -- cap is exhausted -- but none of
            # them left a pending, unprocessed arbitration_result.json (no
            # such file exists in tmp_path), so there's nothing to resolve.
            for i in range(3):
                session.add(
                    Task(
                        id=f"prior-arb-{i}",
                        raw_description="Arbitrate stuck phase: qa_validation",
                        done_definition="x",
                        status="done",
                        phase_id="phase-1",
                        workflow_id="wf-1",
                        created_by_agent_id=ARBITRATION_CREATED_BY,
                        action="arbitrate",
                    )
                )

        mock_build_output.return_value = {"score": 0.9}

        fake_eval_point = MagicMock(evaluator="heuristic", conditions=[])
        fake_orchestrator = MagicMock()
        fake_orchestrator._find_evaluation_point.return_value = fake_eval_point
        fake_orchestrator._heuristic_evaluate.return_value = (0.9, {})
        fake_orchestrator._evaluate_conditions.return_value = MagicMock(
            action=MagicMock(value="continue"), reason="score >= 0.7"
        )
        mock_pm_instance = MagicMock()
        mock_pm_instance._get_orchestrator.return_value = fake_orchestrator
        mock_pm_class.return_value = mock_pm_instance

        mock_fire_transition.return_value = True

        with patch("src.autopilot.orchestrator.arbitration.get_gated_phases", lambda: ("qa_validation",)):
            result = _trigger_arbitration(
                "wf-1", "phase-1", "qa_validation", "still not converging", MagicMock()
            )

        assert result is True
        mock_create_agent.assert_not_called()
        mock_fire_transition.assert_called_once_with(
            "wf-1", "phase-1", "qa_validation", ANY, force_continue=True
        )
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            # Advancing, not failing.
            assert wf.status != "failed"

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_retry_resets_the_arbitration_cap_via_gotos_reset_at(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """Regression: historical arbitration Task rows are never deleted,
        so a workflow that already exhausted the cap once stayed
        PERMANENTLY unrecoverable via Retry -- every future retry
        immediately re-hit the same all-time count and re-failed with zero
        real attempt in between, even after _resume_interrupted_workflows'
        reactivate branch reset total_gotos to give the phase a genuinely
        fresh goto budget. gotos_reset_at (set by that same reactivate
        branch) must exclude prior-to-retry arbitration tasks from the cap
        count, so a retried workflow actually gets a real shot again."""
        from datetime import datetime, timedelta

        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY

        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        # 3 prior arbitration tasks, all from BEFORE the retry.
        with db_manager.session_scope() as session:
            for i in range(3):
                session.add(
                    Task(
                        id=f"prior-arb-{i}",
                        raw_description="Arbitrate stuck phase: requirements",
                        done_definition="x",
                        status="done",
                        phase_id="phase-1",
                        workflow_id="wf-1",
                        created_by_agent_id=ARBITRATION_CREATED_BY,
                        action="arbitrate",
                    )
                )
            # Simulates the on-demand Retry that just happened: reset
            # total_gotos and stamp gotos_reset_at AFTER the prior
            # arbitration tasks above.
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.total_gotos = 0
            wf.gotos_reset_at = datetime.utcnow() + timedelta(seconds=1)

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "still not converging", MagicMock()
        )

        assert result is True
        mock_create_agent.assert_called_once()
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status != "failed"

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_already_in_flight_uses_real_orchestrator_logger(
        self, mock_create_agent, db_manager, sample_workflow, tmp_path
    ):
        """Regression: every other test in this class passes MagicMock()
        for logger, which silently accepts ANY method call (.debug,
        .whatever) with no AttributeError -- completely masking an
        interface mismatch. Observed live: _trigger_arbitration's
        "already in flight" branch called logger.debug(...), but
        OrchestratorLogger (the real class passed in production, see
        _run_phase_advancement_sweep_once's sweep_logger) only implements
        .info/.warning/.error -- crashed with AttributeError in production
        (dropped straight from _create_phase_task's exception handler as
        "Error creating task for development: 'OrchestratorLogger' object
        has no attribute 'debug'"), right as the retry bound fired for
        real. At least the "already claimed" path must be exercised
        against the REAL logger class, not a mock that hides this."""
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.phase_transitions import (
    _claim_phase_task_creation,
    _trigger_arbitration,
)

        with db_manager.session_scope() as session:
            _claim_phase_task_creation(session, "phase-1")

        real_logger = OrchestratorLogger(tmp_path)
        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "reason", real_logger
        )

        assert result is False
        mock_create_agent.assert_not_called()

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_prompt_forbids_editing_files(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """The arbitration agent has full read/write/bash tool access in
        the shared worktree (same as any other phase agent) -- without an
        explicit boundary, an eager coding agent could "just fix it"
        directly instead of writing a decision, skipping the fix's own
        review/test cycle entirely."""
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

        _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted", MagicMock()
        )

        _, kwargs = mock_create_agent.call_args
        prompt = kwargs["enriched_data_override"]["validation_prompt"].lower()
        assert "do not edit" in prompt or "not edit" in prompt

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_already_in_flight_skips_duplicate(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation, _trigger_arbitration

        with db_manager.session_scope() as session:
            _claim_phase_task_creation(session, "phase-1")

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "reason", MagicMock()
        )

        assert result is False
        mock_create_agent.assert_not_called()

    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_dispatch_failure_fails_workflow_instead_of_silent_pause(
        self, mock_create_agent, mock_pm_class, db_manager, sample_workflow
    ):
        """Regression scenario within a regression fix: if even spawning the
        arbitration agent fails, the phase must not end up silently
        re-claimed forever -- it fails loudly and immediately."""
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.return_value = None
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "reason", MagicMock()
        )

        assert result is False
        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1", ANY, force_action="fail"
        )
        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(phase_id="phase-1").first()
            assert task.status == "failed"
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status_reason is not None


class TestResolveHumanArbitrationChoice:
    """Unit tests for _resolve_human_arbitration_choice, the half of the
    human-escalation flow that applies a decision once one exists --
    "c" force-continues the deadlocked phase, "s" gives up and fails the
    workflow. See TestMaybeResolveHumanArbitrationEscalations for the
    file-polling half that finds a decision to apply."""

    def test_give_up_fails_workflow_and_clears_a_stale_review_pause(
        self, db_manager, sample_workflow
    ):
        """Regression, mirrors the pre-escalation fix this replaces: must
        go through the pause_workflow-equivalent triad (status/paused_by/
        paused_at together), not a partial write that leaves paused_by
        stale -- a stale paused_by="review" survives untouched otherwise,
        permanently blocking resume_workflow (requires status=="paused")
        while the review-approve handler doesn't check its own return
        value, so Approve becomes a silent no-op and the workflow stays
        stuck "failed" forever."""
        from src.autopilot.orchestrator.arbitration import _resolve_human_arbitration_choice

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "review"
            wf.paused_at = datetime.utcnow()
            wf.status_reason = "[ARBITRATION-ESCALATION:abc12345] requirements: ..."

        _resolve_human_arbitration_choice("wf-1", "phase-1", "s", MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"
            assert wf.paused_by is None
            assert wf.paused_at is None
            assert "requirements" in wf.status_reason

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_force_continue_dispatches_the_next_phase(self, mock_create_agent, db_manager, sample_workflow):
        from src.autopilot.orchestrator.arbitration import _resolve_human_arbitration_choice

        mock_create_agent.side_effect = _agent_row_side_effect("next-phase-agent")

        with db_manager.session_scope() as session:
            session.add(PhaseExecution(id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1", status="pending"))
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "review"
            wf.paused_at = datetime.utcnow()
            wf.status_reason = "[ARBITRATION-ESCALATION:abc12345] requirements: ..."

        _resolve_human_arbitration_choice("wf-1", "phase-1", "c", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "completed"
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status != "failed"

    @patch("src.autopilot.orchestrator.engine_client.resume_workflow")
    @patch("src.autopilot.orchestrator.arbitration._resolve_arbitration_outcome")
    def test_goto_choice_sends_the_phase_to_the_chosen_target(
        self, mock_resolve_outcome, mock_resume, db_manager, sample_workflow
    ):
        """"g" mirrors the AI arbiter's own "goto" decision -- reuses
        _resolve_arbitration_outcome directly so a human's choice gets
        identical phase/task bookkeeping to an AI arbiter's, rather than a
        second, parallel implementation."""
        from src.autopilot.orchestrator.arbitration import _resolve_human_arbitration_choice

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "review"
            wf.paused_at = datetime.utcnow()

        _resolve_human_arbitration_choice(
            "wf-1", "phase-1", "g", MagicMock(), target_phase="implementation"
        )

        mock_resume.assert_called_once_with("wf-1", force=True)
        mock_resolve_outcome.assert_called_once_with(
            "wf-1", "phase-1", "requirements", "goto", "implementation", ANY, ANY,
        )

    def test_goto_choice_with_unresolvable_phase_is_a_no_op(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.arbitration import _resolve_human_arbitration_choice

        # phase_id "nonexistent" resolves to no Phase row.
        _resolve_human_arbitration_choice(
            "wf-1", "nonexistent", "g", MagicMock(), target_phase="implementation"
        )

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"  # untouched -- nothing to resolve


class TestMaybeResolveHumanArbitrationEscalations:
    """Tests for _maybe_resolve_human_arbitration_escalations, the sweep
    step that finds a response (or a dismissal) to a pending arbitration-
    deadlock escalation and applies it -- see
    _escalate_arbitration_deadlock_to_human for the write side."""

    def _paused_workflow(self, db_manager, request_id="abc12345", phase_id="phase-1"):
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
            wf.paused_by = "review"
            wf.paused_at = datetime.utcnow()
            wf.status_reason = f"[ARBITRATION-ESCALATION:{request_id}:{phase_id}] requirements: still not converging"

    def _write_request(self, state_dir, request_id="abc12345", phase_id="phase-1"):
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"input_request_{request_id}.json").write_text(json.dumps({
            "id": request_id, "reason": "r", "timestamp": datetime.utcnow().isoformat(),
            "options": ["c", "s"], "labels": {"c": "Continue", "s": "Give up"},
            "workflow_id": "wf-1", "phase_id": phase_id, "kind": "arbitration_escalation",
        }))

    def _write_response(self, state_dir, choice, request_id="abc12345", message=None):
        payload = {"request_id": request_id, "choice": choice}
        if message:
            payload["message"] = message
        (state_dir / f"input_response_{request_id}.json").write_text(json.dumps(payload))

    def test_no_response_yet_leaves_the_workflow_paused(self, db_manager, sample_workflow, tmp_path, monkeypatch):
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        self._paused_workflow(db_manager)
        self._write_request(tmp_path)

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
        assert (tmp_path / "input_request_abc12345.json").exists()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_continue_response_resolves_and_cleans_up_files(
        self, mock_create_agent, db_manager, sample_workflow, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        mock_create_agent.side_effect = _agent_row_side_effect("next-phase-agent")

        with db_manager.session_scope() as session:
            session.add(PhaseExecution(id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1", status="pending"))
        self._paused_workflow(db_manager)
        self._write_request(tmp_path)
        self._write_response(tmp_path, "c")

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status != "failed"
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "completed"
        assert not (tmp_path / "input_request_abc12345.json").exists()
        assert not (tmp_path / "input_response_abc12345.json").exists()

    def test_give_up_response_fails_workflow_and_cleans_up_files(
        self, db_manager, sample_workflow, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        self._paused_workflow(db_manager)
        self._write_request(tmp_path)
        self._write_response(tmp_path, "s")

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"
            assert wf.paused_by is None
        assert not (tmp_path / "input_request_abc12345.json").exists()
        assert not (tmp_path / "input_response_abc12345.json").exists()

    def test_stop_response_is_treated_as_give_up_not_a_dead_click(
        self, db_manager, sample_workflow, tmp_path, monkeypatch
    ):
        """Regression: this escalation only ever declares options ["c", "s"]
        in its own request JSON, but MessageCenter's response UI is generic
        across every human_input_required message -- it always renders all
        three buttons (Continue/Skip/Stop) and never reads a request's own
        options/labels fields at all. A human clicking the always-visible
        "Stop" button (choice="q") must not leave the response file
        unprocessed forever with the workflow stuck paused and no feedback
        that the click did nothing -- it must resolve the same as "Give
        up" (choice="s")."""
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        self._paused_workflow(db_manager)
        self._write_request(tmp_path)
        self._write_response(tmp_path, "q")

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"
            assert wf.paused_by is None
        assert not (tmp_path / "input_request_abc12345.json").exists()
        assert not (tmp_path / "input_response_abc12345.json").exists()

    @patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct")
    def test_dismissed_request_auto_continues(
        self, mock_create_agent, db_manager, sample_workflow, tmp_path, monkeypatch
    ):
        """Mirrors human_escalation.prompt_human's own dismiss convention:
        a request deleted via the UI's X button with no response ever
        written must not leave the workflow paused forever with nothing
        left to answer."""
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        mock_create_agent.side_effect = _agent_row_side_effect("next-phase-agent")

        with db_manager.session_scope() as session:
            session.add(PhaseExecution(id="exec-2", phase_id="phase-2", workflow_execution_id="wf-1", status="pending"))
        self._paused_workflow(db_manager)
        # No request file written -- simulates dismissal.

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status != "failed"
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.status == "completed"

    def test_goto_response_without_target_phase_is_left_for_next_tick(
        self, db_manager, sample_workflow, tmp_path, monkeypatch
    ):
        """A malformed "g" response (no target_phase) must not be silently
        dropped or misapplied -- leave both files for the next tick rather
        than guessing."""
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        self._paused_workflow(db_manager)
        self._write_request(tmp_path)
        self._write_response(tmp_path, "g")  # no target_phase

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
        assert (tmp_path / "input_request_abc12345.json").exists()
        assert (tmp_path / "input_response_abc12345.json").exists()

    @patch("src.autopilot.orchestrator.engine_client.resume_workflow")
    @patch("src.autopilot.orchestrator.arbitration._resolve_arbitration_outcome")
    def test_goto_response_resolves_with_its_target_phase_and_cleans_up_files(
        self, mock_resolve_outcome, mock_resume, db_manager, sample_workflow, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        self._paused_workflow(db_manager)
        self._write_request(tmp_path)
        payload = {"request_id": "abc12345", "choice": "g", "target_phase": "implementation"}
        (tmp_path / "input_response_abc12345.json").write_text(json.dumps(payload))

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        mock_resolve_outcome.assert_called_once_with(
            "wf-1", "phase-1", "requirements", "goto", "implementation", ANY, ANY,
        )
        assert not (tmp_path / "input_request_abc12345.json").exists()
        assert not (tmp_path / "input_response_abc12345.json").exists()

    def test_message_only_response_keeps_waiting(self, db_manager, sample_workflow, tmp_path, monkeypatch):
        monkeypatch.setattr("src.core.constants.AUTOPILOT_STATE_DIR", str(tmp_path))
        from src.autopilot.orchestrator.arbitration import _maybe_resolve_human_arbitration_escalations

        self._paused_workflow(db_manager)
        self._write_request(tmp_path)
        self._write_response(tmp_path, "m", message="checking on this now")

        _maybe_resolve_human_arbitration_escalations(MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
        assert (tmp_path / "input_request_abc12345.json").exists()
        assert not (tmp_path / "input_response_abc12345.json").exists()


class TestBuildArbitrationDecisionContext:
    """Unit tests for _build_arbitration_decision_context, which
    reconstructs an arbitration deadlock's actual disagreement from the
    phase's own "Arbitrate stuck phase: X" task history -- the data source
    behind the Decide UI's richer breakdown of what each attempt actually
    concluded."""

    def _seed_arbitration_task(self, db_manager, completion_notes, created_at, task_id):
        with db_manager.session_scope() as session:
            session.add(Task(
                id=task_id,
                raw_description="Arbitrate stuck phase: requirements",
                done_definition="decide",
                status="done",
                workflow_id="wf-1",
                created_by_agent_id="arbitration",
                completion_notes=completion_notes,
                created_at=created_at,
            ))

    def test_parses_decision_target_phase_and_reason(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.arbitration import _build_arbitration_decision_context

        self._seed_arbitration_task(
            db_manager,
            "Arbitration complete. Decision: goto architecture_design. The same 2 "
            "BLOCKERs were never fixed in architecture.md.",
            datetime(2026, 1, 1, 10, 0, 0),
            "arb-1",
        )
        self._seed_arbitration_task(
            db_manager,
            "Arbitration complete. Decision: continue. The BLOCKERs are trivial doc bugs.",
            datetime(2026, 1, 1, 11, 0, 0),
            "arb-2",
        )

        with db_manager.session_scope() as session:
            ctx = _build_arbitration_decision_context(session, "wf-1", "requirements")

        assert ctx["phase_name"] == "requirements"
        assert len(ctx["attempts"]) == 2
        assert ctx["attempts"][0]["decision"] == "goto"
        assert ctx["attempts"][0]["target_phase"] == "architecture_design"
        assert "BLOCKERs were never fixed" in ctx["attempts"][0]["reason"]
        assert ctx["attempts"][1]["decision"] == "continue"
        assert ctx["attempts"][1]["target_phase"] is None

    def test_deduplicates_repeated_identical_decisions(self, db_manager, sample_workflow):
        """The same "goto architecture_design" decided 3 times in a row
        must offer ONE button, not three identical ones."""
        from src.autopilot.orchestrator.arbitration import _build_arbitration_decision_context

        for i, at in enumerate([
            datetime(2026, 1, 1, 10, 0, 0),
            datetime(2026, 1, 1, 11, 0, 0),
            datetime(2026, 1, 1, 12, 0, 0),
        ]):
            self._seed_arbitration_task(
                db_manager,
                "Arbitration complete. Decision: goto architecture_design. Same blocker again.",
                at, f"arb-{i}",
            )

        with db_manager.session_scope() as session:
            ctx = _build_arbitration_decision_context(session, "wf-1", "requirements")

        assert len(ctx["attempts"]) == 3
        assert len(ctx["distinct_options"]) == 1
        assert ctx["distinct_options"][0]["target_phase"] == "architecture_design"

    def test_unparseable_completion_notes_falls_back_to_raw_text(self, db_manager, sample_workflow):
        """An arbiter that doesn't follow the exact "Decision: X." shape
        must not crash or silently disappear -- the raw text is still
        surfaced as this attempt's reason, just without a
        decision/target_phase (so it's excluded from distinct_options,
        which requires a parsed decision)."""
        from src.autopilot.orchestrator.arbitration import _build_arbitration_decision_context

        self._seed_arbitration_task(
            db_manager, "I looked into it and things seem fine I guess.",
            datetime(2026, 1, 1, 10, 0, 0), "arb-1",
        )

        with db_manager.session_scope() as session:
            ctx = _build_arbitration_decision_context(session, "wf-1", "requirements")

        assert ctx["attempts"][0]["decision"] is None
        assert ctx["attempts"][0]["reason"] == "I looked into it and things seem fine I guess."
        assert ctx["distinct_options"] == []

    def test_no_arbitration_tasks_returns_empty_attempts(self, db_manager, sample_workflow):
        from src.autopilot.orchestrator.arbitration import _build_arbitration_decision_context

        with db_manager.session_scope() as session:
            ctx = _build_arbitration_decision_context(session, "wf-1", "requirements")

        assert ctx == {"phase_name": "requirements", "attempts": [], "distinct_options": []}


class TestReadArbitrationResult:
    def test_valid_file(self, tmp_path):
        from src.autopilot.orchestrator.phase_transitions import _read_arbitration_result
        from src.core.constants import CONTEXT_DIR_NAME

        d = tmp_path / CONTEXT_DIR_NAME
        d.mkdir()
        (d / "arbitration_result.json").write_text(
            '{"decision": "goto", "target_phase": "development", "reason": "fix x"}'
        )

        decision, target, reason = _read_arbitration_result(str(tmp_path))
        assert decision == "goto"
        assert target == "development"
        assert reason == "fix x"

    def test_missing_file(self, tmp_path):
        from src.autopilot.orchestrator.phase_transitions import _read_arbitration_result

        assert _read_arbitration_result(str(tmp_path)) == (None, None, None)

    def test_no_working_directory(self):
        from src.autopilot.orchestrator.phase_transitions import _read_arbitration_result

        assert _read_arbitration_result(None) == (None, None, None)

    def test_malformed_json(self, tmp_path):
        from src.autopilot.orchestrator.phase_transitions import _read_arbitration_result
        from src.core.constants import CONTEXT_DIR_NAME

        d = tmp_path / CONTEXT_DIR_NAME
        d.mkdir()
        (d / "arbitration_result.json").write_text("not json")

        assert _read_arbitration_result(str(tmp_path)) == (None, None, None)

    def test_invalid_decision_value(self, tmp_path):
        from src.autopilot.orchestrator.phase_transitions import _read_arbitration_result
        from src.core.constants import CONTEXT_DIR_NAME

        d = tmp_path / CONTEXT_DIR_NAME
        d.mkdir()
        (d / "arbitration_result.json").write_text('{"decision": "maybe"}')

        assert _read_arbitration_result(str(tmp_path)) == (None, None, None)


class TestResolveArbitrationOutcome:
    """_resolve_arbitration_outcome must always release the phase's claim,
    regardless of outcome -- otherwise the phase stays permanently locked
    out of both normal advancement and future arbitration attempts.

    Critical regression: mark_phase_complete NEVER creates the next task
    itself (see _fire_phase_transition -- it always calls
    _create_phase_task explicitly using mark_phase_complete's returned
    target_phase_id). An earlier version of this function discarded that
    return value entirely -- "continue"/"goto" closed out the arbitrating
    phase successfully but never dispatched anything next, silently
    stranding the pipeline with workflow.status="active" and zero agents
    running. Every test below asserts _create_phase_task actually fires
    with the right target -- asserting mark_phase_complete was CALLED is
    not enough, since that was exactly what the earlier buggy version did
    too."""

    def _seed_claimed_phase(self, db_manager):
        with db_manager.session_scope() as session:
            from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation

            _claim_phase_task_creation(session, "phase-1")

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_continue_dispatches_next_phase_task(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase": "implementation",
            "target_phase_id": "phase-2",
            "should_continue": True,
        }

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "continue", None, "fine, proceed", MagicMock()
        )

        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1", ANY, force_action="continue"
        )
        mock_create_task.assert_called_once_with(
            "wf-1", "phase-2", "implementation", "continue", ANY,
            feedback=None, source_phase_name="requirements",
        )
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status_reason is None

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_continue_at_last_phase_does_not_dispatch(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """Workflow-complete case: no next phase, so nothing to dispatch --
        must not crash or call _create_phase_task with a None target."""
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": False,
        }

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "continue", None, "done", MagicMock()
        )

        mock_create_task.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_goto_dispatches_target_phase_task(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "goto",
            "target_phase": "implementation",
            "target_phase_id": "phase-2",
            "should_continue": True,
            "reason": "fix x",
        }

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "goto", "implementation", "fix x", MagicMock()
        )

        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1",
            ANY,
            force_action="goto",
            force_target_phase="implementation",
            force_reason="fix x",
        )
        mock_create_task.assert_called_once_with(
            "wf-1", "phase-2", "implementation", "goto", ANY,
            feedback="fix x", source_phase_name="requirements",
        )
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_goto_dispatch_failure_is_logged_loudly(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """If _create_phase_task itself fails (e.g. agent creation error),
        this must be logged as an error, not silently swallowed -- "surface
        errors better" applies to this failure mode too."""
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "goto",
            "target_phase": "implementation",
            "target_phase_id": "phase-2",
            "should_continue": True,
            "reason": "fix x",
        }
        mock_create_task.return_value = False
        logger = MagicMock()

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "goto", "implementation", "fix x", logger
        )

        assert logger.error.called

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_fail_calls_force_fail_clears_claim_and_sets_status_reason(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "fail",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": False,
        }

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "fail", None, "no credentials available", MagicMock()
        )

        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1", ANY, force_action="fail"
        )
        mock_create_task.assert_not_called()
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert "no credentials available" in wf.status_reason
            assert "requirements" in wf.status_reason

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_goto_without_target_treated_as_fail(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """A malformed decision (goto with no target_phase) must not crash
        or hang -- falls back to fail rather than silently doing nothing."""
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "fail",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": False,
        }

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "goto", None, "malformed", MagicMock()
        )

        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1", ANY, force_action="fail"
        )
        mock_create_task.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_goto_with_unresolvable_target_phase_is_surfaced_not_hidden(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """An LLM-hallucinated or mis-cased target_phase (_find_phase_by_
        name_or_order does an EXACT string match) makes _handle_force_goto
        fall back to _advance_or_complete internally, returning an action
        that ISN'T "goto". The raw decision the arbiter wrote must not be
        trusted blindly for clearing status_reason -- check what actually
        happened."""
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        # _handle_force_goto's fallback when the name doesn't resolve:
        # _advance_or_complete's shape (no "reason"/target info about the
        # bad name the arbiter actually asked for).
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase": "next_real_phase",
            "target_phase_id": "phase-2",
            "should_continue": True,
        }

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "goto", "totally_made_up_phase", "fix x", MagicMock()
        )

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status_reason is not None
            assert "totally_made_up_phase" in wf.status_reason
        # The fallback's own decision still gets dispatched -- pipeline
        # keeps making progress even though the arbiter's exact target
        # didn't resolve.
        mock_create_task.assert_called_once_with(
            "wf-1", "phase-2", "next_real_phase", "continue", ANY,
            feedback=None, source_phase_name="requirements",
        )

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_claim_stays_held_until_after_next_task_is_dispatched(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """Regression: releasing task_creation_claimed_at right after
        mark_phase_complete (before dispatching the next task) reopens the
        exact race this claim exists to close -- phase.retry_count is
        never reset once a phase's retry budget is exhausted, so a
        concurrent caller that sees this phase as unclaimed in that gap
        can re-evaluate it via the normal path and immediately re-hit the
        still-exhausted budget, re-arbitrating the same question this call
        is in the middle of answering. Observed live (workflow b1019f3d):
        design_review arbitrated 3 times in 4 minutes, all reaching
        "continue" against the same already-fixed architecture.md.

        Asserts ordering directly: the claim must still be held at the
        moment _create_phase_task runs, and only cleared after."""
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase": "implementation",
            "target_phase_id": "phase-2",
            "should_continue": True,
        }

        claim_state_during_dispatch = {}

        def _check_claim_still_held(*args, **kwargs):
            with db_manager.session_scope() as session:
                execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
                claim_state_during_dispatch["held"] = execution.task_creation_claimed_at is not None
            return True

        mock_create_task.side_effect = _check_claim_still_held

        _resolve_arbitration_outcome(
            "wf-1", "phase-1", "requirements", "continue", None, "fine, proceed", MagicMock()
        )

        assert claim_state_during_dispatch["held"] is True, (
            "claim was released before _create_phase_task ran -- reopens the race"
        )
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None, "claim must still be released once done"

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_claim_released_even_if_dispatch_raises(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """The claim must not be permanently stranded if _create_phase_task
        itself raises -- moving the release to after the dispatch (the fix
        for the race above) must not reintroduce the original bug this
        claim's finally-release was written to prevent."""
        from src.autopilot.orchestrator.phase_transitions import _resolve_arbitration_outcome

        self._seed_claimed_phase(db_manager)
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase": "implementation",
            "target_phase_id": "phase-2",
            "should_continue": True,
        }
        mock_create_task.side_effect = RuntimeError("simulated dispatch failure")

        with pytest.raises(RuntimeError):
            _resolve_arbitration_outcome(
                "wf-1", "phase-1", "requirements", "continue", None, "fine, proceed", MagicMock()
            )

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None


class TestMaybeResolveArbitration:
    """End-to-end-ish: seeds a real arbitration Task + claimed
    PhaseExecution + arbitration_result.json on disk, and confirms the
    sweep-tick consumer resolves it."""

    def _seed_arbitration_in_flight(self, db_manager, task_status="done"):
        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            _claim_phase_task_creation(session, "phase-1")
            session.add(
                Task(
                    id="arb-task-1",
                    raw_description="Arbitrate stuck phase: requirements",
                    done_definition="x",
                    status=task_status,
                    phase_id="phase-1",
                    workflow_id="wf-1",
                    created_by_agent_id=ARBITRATION_CREATED_BY,
                    action="arbitrate",
                )
            )

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_resolves_done_arbitration_with_valid_result(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        from src.core.constants import CONTEXT_DIR_NAME
        from src.autopilot.orchestrator.phase_transitions import _maybe_resolve_arbitration

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.working_directory = str(tmp_path)
        self._seed_arbitration_in_flight(db_manager, task_status="done")

        d = tmp_path / CONTEXT_DIR_NAME
        d.mkdir()
        (d / "arbitration_result.json").write_text(
            '{"decision": "continue", "target_phase": null, "reason": "fine"}'
        )
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "continue",
            "target_phase": "implementation",
            "target_phase_id": "phase-2",
            "should_continue": True,
        }

        _maybe_resolve_arbitration("wf-1", MagicMock())

        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1", ANY, force_action="continue"
        )
        # The critical regression: the next task must actually get created.
        mock_create_task.assert_called_once_with(
            "wf-1", "phase-2", "implementation", "continue", ANY,
            feedback=None, source_phase_name="requirements",
        )
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_still_running_arbitration_is_left_alone(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        from src.autopilot.orchestrator.phase_transitions import _maybe_resolve_arbitration

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.working_directory = str(tmp_path)
        self._seed_arbitration_in_flight(db_manager, task_status="in_progress")

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm

        _maybe_resolve_arbitration("wf-1", MagicMock())

        mock_pm.mark_phase_complete.assert_not_called()
        mock_create_task.assert_not_called()
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None  # still claimed

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_failed_arbitration_agent_resolves_as_fail(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        """The arbitration agent itself dying/failing must not leave the
        phase stuck forever -- resolves as a fail decision automatically."""
        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation, _maybe_resolve_arbitration

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.working_directory = str(tmp_path)
            _claim_phase_task_creation(session, "phase-1")
            session.add(
                Task(
                    id="arb-task-1",
                    raw_description="Arbitrate stuck phase: requirements",
                    done_definition="x",
                    status="failed",
                    failure_reason="agent crashed",
                    phase_id="phase-1",
                    workflow_id="wf-1",
                    created_by_agent_id=ARBITRATION_CREATED_BY,
                    action="arbitrate",
                )
            )

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "fail",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": False,
        }

        _maybe_resolve_arbitration("wf-1", MagicMock())

        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1", ANY, force_action="fail"
        )
        mock_create_task.assert_not_called()

    @patch("src.autopilot.orchestrator.phase_transitions._create_phase_task")
    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_done_without_result_file_resolves_as_fail(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        """Regression backstop: an arbitration agent that calls
        update_task_status(done) without ever writing
        arbitration_result.json must not hang the phase forever."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_resolve_arbitration

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.working_directory = str(tmp_path)
        self._seed_arbitration_in_flight(db_manager, task_status="done")
        # deliberately no arbitration_result.json written

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.mark_phase_complete.return_value = {
            "action": "fail",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": False,
        }

        _maybe_resolve_arbitration("wf-1", MagicMock())

        mock_pm.mark_phase_complete.assert_called_once_with(
            "phase-1", ANY, force_action="fail"
        )

    @patch("src.autopilot.orchestrator.arbitration.PhaseManager")
    def test_no_arbitration_task_is_a_noop(
        self, mock_pm_class, db_manager, sample_workflow
    ):
        """A phase with a stale/unrelated claim but no arbitration task at
        all (e.g. a normal in-flight task-creation claim) must be left
        alone -- only genuine arbitration in-flight is acted on."""
        from src.autopilot.orchestrator.phase_transitions import _claim_phase_task_creation, _maybe_resolve_arbitration

        with db_manager.session_scope() as session:
            _claim_phase_task_creation(session, "phase-1")

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm

        _maybe_resolve_arbitration("wf-1", MagicMock())

        mock_pm.mark_phase_complete.assert_not_called()


class TestRetryFailedTasksWithDone:
    """Tests for _retry_failed_tasks_with_done's own RETRY-banner guard --
    the done+failed sibling of _maybe_retry_failed_tasks's same fix."""

    def test_orphaned_failure_reason_gets_plain_reset_not_a_retry_banner(
        self, db_manager, sample_workflow
    ):
        """Same fix as _maybe_retry_failed_tasks's own regression test:
        'Orphaned: ...' means no agent ever actually received this task --
        skip the misleading RETRY banner; this is genuinely the next
        agent's first prompt for the task."""
        from src.autopilot.orchestrator._phase_case_steps import _retry_failed_tasks_with_done

        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-fail-0",
                    workflow_id="wf-1",
                    phase_id="phase-1",
                    raw_description="Execute phase X",
                    enriched_description="Execute phase X: do the thing",
                    done_definition="Done",
                    status="failed",
                    failure_reason="Orphaned: never dispatched to an agent",
                )
            )

        logger = MagicMock()
        with patch(
            "src.autopilot.orchestrator._phase_case_steps.create_agent_for_task_direct",
            side_effect=_agent_row_side_effect("new-agent-1"),
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _retry_failed_tasks_with_done(
                    session, phase, "wf-1", execution=None,
                    logger=logger, failed_count=1, done_count=1, cycle_filter=(),
                )
                assert result is True

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "in_progress"
            assert task.failure_reason is None
            assert "RETRY" not in (task.enriched_description or "")
            assert task.enriched_description == "Execute phase X: do the thing"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

