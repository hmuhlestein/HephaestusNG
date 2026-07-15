"""Tests for orchestrator._advance_phases and related phase transition functions.

These tests address the critical test coverage gap identified in ARCHITECTURE_REVIEW.md:
"_advance_phases has no test referencing it anywhere in tests/"
"""

from datetime import datetime
from unittest.mock import ANY, MagicMock, patch

import pytest

from src.core.database import (
    DatabaseManager,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
)


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
        from src.autopilot.orchestrator import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True

    def test_second_caller_loses(self, db_manager, sample_workflow):
        """The core guarantee: only one of two callers racing for the same
        phase can ever win the claim, regardless of ordering."""
        from src.autopilot.orchestrator import _claim_phase_task_creation

        with db_manager.session_scope() as session:
            first = _claim_phase_task_creation(session, "phase-1")
        with db_manager.session_scope() as session:
            second = _claim_phase_task_creation(session, "phase-1")

        assert first is True
        assert second is False

    def test_different_phases_both_win(self, db_manager, sample_workflow):
        """Claims are scoped per phase -- unrelated phases don't block each other."""
        from src.autopilot.orchestrator import _claim_phase_task_creation

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
        from src.autopilot.orchestrator import (
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
        from src.autopilot.orchestrator import _release_phase_task_creation_claim

        with db_manager.session_scope() as session:
            _release_phase_task_creation_claim(session, "nonexistent-phase")

    def test_already_in_progress_status_left_untouched(self, db_manager, sample_workflow):
        """Only flip pending/completed -> in_progress -- don't stomp a status
        this call didn't set (e.g. a race where something else already
        advanced it further)."""
        from src.autopilot.orchestrator import (
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


class TestAdvancePhases:
    """Tests for _advance_phases function."""
    
    def test_returns_false_when_workflow_not_found(self, db_manager):
        """Should return False when workflow doesn't exist."""
        from src.autopilot.orchestrator import _advance_phases
        
        logger = MagicMock()
        result = _advance_phases("nonexistent-wf", logger)
        assert result is False
    
    def test_returns_false_when_workflow_paused(self, db_manager, sample_workflow):
        """Should return False when workflow is paused (no done tasks)."""
        from src.autopilot.orchestrator import _advance_phases
        
        # Pause the workflow
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            wf.status = "paused"
        
        logger = MagicMock()
        result = _advance_phases("wf-1", logger)
        assert result is False
    
    def test_auto_resumes_paused_workflow_with_done_task(self, db_manager, sample_workflow):
        """Should auto-resume paused workflow if it has a done task in stalled phase."""
        from src.autopilot.orchestrator import _advance_phases
        
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
        # This should auto-resume and then try to advance
        # (may return True or False depending on phase state)
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
        from src.autopilot.orchestrator import _advance_phases

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


class TestCaseStartFirstPhase:
    """Tests for _case_start_first_phase function."""
    
    def test_starts_first_phase_when_no_progress(self, db_manager, sample_workflow):
        """Should start first phase when no phases are in progress or completed."""
        from src.autopilot.orchestrator import (
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
        with patch("src.autopilot.orchestrator._create_phase_task", return_value=True) as mock_create:
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
        from src.autopilot.orchestrator import (
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
        with patch("src.autopilot.orchestrator._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_start_first_phase(
                    session, "wf-1", pending, in_progress, completed, logger
                )
                assert result is None, (
                    "should not create a duplicate task once the other "
                    "path has already won the task-creation claim"
                )
                mock_create.assert_not_called()


class TestCaseInProgressNoTasks:
    """Tests for _case_in_progress_no_tasks function."""
    
    def test_creates_task_for_phase_without_tasks(self, db_manager, sample_workflow):
        """Should create task when in-progress phase has no tasks."""
        from src.autopilot.orchestrator import _case_in_progress_no_tasks
        
        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            in_progress = [{"phase": phase, "status": "in_progress"}]
        
        logger = MagicMock()
        with patch("src.autopilot.orchestrator._create_phase_task", return_value=True) as mock_create:
            with db_manager.session_scope() as session:
                result = _case_in_progress_no_tasks(session, "wf-1", in_progress, logger)
                assert result is True
                mock_create.assert_called_once()


class TestMaybeRetryFailedTasks:
    """Tests for _maybe_retry_failed_tasks function."""
    
    def test_retries_all_failed_tasks(self, db_manager, sample_workflow):
        """Should reset failed tasks and dispatch a fresh agent for each,
        landing on in_progress -- not just reset to pending and abandoned
        (the old behavior was a dead end: nothing else ever picked a
        pending-with-no-agent task back up)."""
        from src.autopilot.orchestrator import _maybe_retry_failed_tasks

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
            "src.autopilot.orchestrator.create_agent_for_task_direct",
            return_value={"agent_id": "new-agent-1"},
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)
                assert result is True

        with db_manager.session_scope() as session:
            # Verify tasks were reset and re-dispatched, not left pending
            tasks = session.query(Task).filter_by(phase_id="phase-1", status="in_progress").all()
            assert len(tasks) == 3
            assert all(t.assigned_agent_id == "new-agent-1" for t in tasks)

    def test_folds_failure_reason_into_description_before_clearing(
        self, db_manager, sample_workflow
    ):
        """Regression: a blind bulk reset used to wipe failure_reason
        without ever surfacing it anywhere, so the retried agent got the
        exact same generic task description and no idea what went wrong
        last time -- e.g. a specific 'missing output artifact: X' from
        update_task_status's validation gate. The reason must survive into
        enriched_description (what the agent's prompt actually reads)."""
        from src.autopilot.orchestrator import _maybe_retry_failed_tasks

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
            "src.autopilot.orchestrator.create_agent_for_task_direct",
            return_value={"agent_id": "new-agent-1"},
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                result = _maybe_retry_failed_tasks(session, phase, logger)
                assert result is True

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "in_progress"
            assert task.failure_reason is None
            assert "Execute phase X: do the thing" in task.enriched_description
            assert "Missing output artifact: docs/report.md" in task.enriched_description

    def test_task_without_failure_reason_gets_plain_reset(self, db_manager, sample_workflow):
        """A failed task with no recorded reason (e.g. a hard crash before
        anything could be logged) just resets normally -- nothing to fold in."""
        from src.autopilot.orchestrator import _maybe_retry_failed_tasks

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
            "src.autopilot.orchestrator.create_agent_for_task_direct",
            return_value={"agent_id": "new-agent-1"},
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                _maybe_retry_failed_tasks(session, phase, logger)

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "in_progress"
            assert task.enriched_description is None

    def test_agent_dispatch_failure_lands_back_on_failed_not_stuck_pending(
        self, db_manager, sample_workflow
    ):
        """If create_agent_for_task_direct fails, the task must land back on
        "failed" (not "pending") -- _maybe_retry_failed_tasks only ever
        triggers on status="failed", so leaving it "pending" here would
        permanently strand the task: no other code path dispatches an
        agent for an already-existing pending task."""
        from src.autopilot.orchestrator import _maybe_retry_failed_tasks

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
            "src.autopilot.orchestrator.create_agent_for_task_direct",
            return_value=None,
        ):
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                _maybe_retry_failed_tasks(session, phase, logger)

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id="task-fail-0").first()
            assert task.status == "failed"
            assert task.assigned_agent_id is None


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

    @patch("src.autopilot.orchestrator._fire_phase_transition")
    def test_fires_transition_when_claim_succeeds(self, mock_fire, db_manager, sample_workflow):
        from src.autopilot.orchestrator import _case_in_progress_complete, _get_phase_statuses

        self._seed_done_task(db_manager)
        mock_fire.return_value = True

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is True
        mock_fire.assert_called_once()

    @patch("src.autopilot.orchestrator._fire_phase_transition")
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
        from src.autopilot.orchestrator import (
            PhaseExecution,
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

    @patch("src.autopilot.orchestrator._fire_phase_transition")
    def test_claim_is_released_even_if_transition_raises(
        self, mock_fire, db_manager, sample_workflow
    ):
        """The release must happen regardless of outcome -- an exception
        mid-transition must not leave the phase permanently unclaimable
        either."""
        from src.autopilot.orchestrator import (
            PhaseExecution,
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

    @patch("src.autopilot.orchestrator._fire_phase_transition")
    def test_skips_when_evaluation_already_claimed(self, mock_fire, db_manager, sample_workflow):
        """Simulates a concurrent caller having already claimed this
        phase's evaluation (e.g. still awaiting a slow engine decision) --
        this call must not also fire a transition for the same phase."""
        from src.autopilot.orchestrator import (
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

    @patch("src.autopilot.orchestrator._fire_phase_transition")
    def test_any_held_claim_blocks_evaluation(
        self, mock_fire, db_manager, sample_workflow
    ):
        """A held claim -- stale or not -- blocks this function on its own;
        staleness is handled earlier, by _release_stale_task_creation_claims
        (see TestReleaseStaleTaskCreationClaims), before phase_statuses is
        even read for this cycle. By the time this loop runs, any claim
        still present is a genuinely live one (e.g. mid-arbitration)."""
        from datetime import timedelta

        from src.autopilot.orchestrator import (
            PhaseExecution,
            _case_in_progress_complete,
            _get_phase_statuses,
        )

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.task_creation_claimed_at = datetime.now() - timedelta(minutes=5)

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-1")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            result = _case_in_progress_complete(session, "wf-1", in_progress, MagicMock())

        assert result is None
        mock_fire.assert_not_called()
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None

    @patch("src.autopilot.orchestrator._fire_phase_transition")
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
        from src.autopilot.orchestrator import _case_in_progress_complete, _get_phase_statuses

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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
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
        from src.autopilot.orchestrator import (
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

        from src.autopilot.orchestrator import (
            CLAIM_STALE_TIMEOUT_SECONDS,
            PhaseExecution,
            _release_stale_task_creation_claims,
        )

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # sample_workflow's fixture defaults phase-1 to "in_progress" --
            # the actual live precondition is "pending" (its status never
            # got flipped, because that flip is itself part of releasing
            # the claim, which never happened).
            execution.status = "pending"
            execution.task_creation_claimed_at = datetime.now() - timedelta(
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

        from src.autopilot.orchestrator import (
            CLAIM_STALE_TIMEOUT_SECONDS,
            PhaseExecution,
            _release_stale_task_creation_claims,
        )

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.status = "pending"
            execution.task_creation_claimed_at = datetime.now() - timedelta(
                seconds=CLAIM_STALE_TIMEOUT_SECONDS + 1
            )

        with db_manager.session_scope() as session:
            _release_stale_task_creation_claims(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            assert execution.status == "pending"  # unchanged -- no task to justify the flip

    def test_recent_claim_is_left_alone(self, db_manager, sample_workflow):
        """A claim well within CLAIM_STALE_TIMEOUT_SECONDS is exactly the
        legitimate in-flight case (e.g. mid-arbitration) this guard exists
        for -- must be left completely untouched."""
        from datetime import timedelta

        from src.autopilot.orchestrator import PhaseExecution, _release_stale_task_creation_claims

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            execution.task_creation_claimed_at = datetime.now() - timedelta(minutes=1)

        with db_manager.session_scope() as session:
            _release_stale_task_creation_claims(session, "wf-1", MagicMock())

        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is not None
            assert execution.status == "in_progress"  # unchanged from fixture default

    @patch("src.autopilot.orchestrator._fire_phase_transition")
    def test_advance_phases_end_to_end_fires_transition_for_stale_pending_phase(
        self, mock_fire, db_manager, sample_workflow
    ):
        """The exact live bug, end to end through the real dispatcher: a
        "pending" phase (not "in_progress") with a done task and a
        day-old, never-released claim must actually advance -- not just
        have its claim cleared in isolation."""
        from datetime import timedelta

        from src.autopilot.orchestrator import (
            CLAIM_STALE_TIMEOUT_SECONDS,
            PhaseExecution,
            _advance_phases,
        )

        self._seed_done_task(db_manager)
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            # sample_workflow's fixture defaults phase-1 to "in_progress" --
            # the actual live precondition is "pending".
            execution.status = "pending"
            execution.task_creation_claimed_at = datetime.now() - timedelta(
                seconds=CLAIM_STALE_TIMEOUT_SECONDS + 1
            )
        mock_fire.return_value = True

        result = _advance_phases("wf-1", MagicMock())

        assert result is True
        mock_fire.assert_called_once()


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

    @patch("src.autopilot.orchestrator._create_phase_task")
    def test_creates_successor_task_directly(self, mock_create, db_manager, sample_workflow):
        """The fix: call _create_phase_task for the successor directly
        instead of re-deciding an already-made decision."""
        from src.autopilot.orchestrator import _case_completed_with_successor, _get_phase_statuses

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    def test_skips_when_successor_already_has_task(self, mock_create, db_manager, sample_workflow):
        from src.autopilot.orchestrator import _case_completed_with_successor, _get_phase_statuses

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    def test_skips_when_claim_already_held(self, mock_create, db_manager, sample_workflow):
        """Simulates a concurrent caller having already claimed the
        successor's task creation -- this call must not also create one."""
        from src.autopilot.orchestrator import (
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


class TestGetPhaseStatuses:
    """Tests for _get_phase_statuses helper."""
    
    def test_returns_phase_statuses(self, db_manager, sample_workflow):
        """Should return all phases with their execution statuses."""
        from src.autopilot.orchestrator import _get_phase_statuses
        
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_resets_stale_claim_on_reactivation(self, mock_create_agent, db_manager, sample_workflow):
        from src.autopilot.orchestrator import _create_phase_task

        mock_create_agent.return_value = {"agent_id": "new-agent"}

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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_reactivated_phase_can_claim_after_completion(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """End-to-end version of the same regression: after reactivation via
        _create_phase_task, a fresh _claim_phase_task_creation call for that
        same phase must succeed -- proving the transition-evaluation claim
        isn't permanently blocked by the stale value from the prior cycle."""
        from src.autopilot.orchestrator import _claim_phase_task_creation, _create_phase_task

        mock_create_agent.return_value = {"agent_id": "new-agent"}

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "completed"
            exec1.task_creation_claimed_at = datetime(2020, 1, 1)

        _create_phase_task("wf-1", "phase-1", "requirements", "goto", MagicMock())

        with db_manager.session_scope() as session:
            assert _claim_phase_task_creation(session, "phase-1") is True

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
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
        from src.autopilot.orchestrator import _claim_phase_task_creation, _create_phase_task

        mock_create_agent.return_value = {"agent_id": "new-agent"}

        with db_manager.session_scope() as session:
            exec1 = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            exec1.status = "in_progress"
            exec1.task_creation_claimed_at = datetime.utcnow()

        result = _create_phase_task("wf-1", "phase-1", "requirements", "continue", MagicMock())

        assert result is True
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            assert _claim_phase_task_creation(session, "phase-1") is True


class TestFirePhaseTransition:
    """Tests for _fire_phase_transition function."""

    @patch("src.autopilot.orchestrator.PhaseManager")
    @patch("src.autopilot.orchestrator._create_phase_task")
    def test_fires_transition_successfully(self, mock_create, mock_pm_class, db_manager, sample_workflow):
        """Should fire phase transition and create next task."""
        from src.autopilot.orchestrator import _fire_phase_transition
        
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

    @patch("src.autopilot.orchestrator.PhaseManager")
    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.build_phase_output")
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
        from src.autopilot.orchestrator import _fire_phase_transition

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


class TestFirePhaseTransitionArbitrate:
    """Regression: the "arbitrate" action from PhaseManager was a dead-end
    TODO stub -- it logged a warning and did nothing else, leaving the
    phase's PhaseExecution.status="pending" forever with no agent ever
    dispatched to resolve it. Must now actually trigger arbitration."""

    @patch("src.autopilot.orchestrator._trigger_arbitration")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_arbitrate_action_triggers_arbitration(
        self, mock_pm_class, mock_trigger, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import _fire_phase_transition

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

    @patch("src.autopilot.orchestrator._trigger_arbitration")
    def test_exhausting_bound_triggers_arbitration_not_pause(
        self, mock_trigger, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import _create_phase_task

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
        from src.autopilot.orchestrator import _create_phase_task

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
            "src.autopilot.orchestrator.create_agent_for_task_direct",
            return_value={"agent_id": "a1"},
        ):
            result = _create_phase_task(
                "wf-1", "phase-1", "requirements", "goto", MagicMock()
            )

        assert result is True


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
            session.query(Phase).delete()
            session.query(PhaseExecution).delete()
            session.query(Task).delete()

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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_advance_phases_does_not_bypass_arbitrating_phase(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import _advance_phases, _trigger_arbitration

        self._seed_realistic_pipeline(db_manager)
        mock_create_agent.return_value = {"agent_id": "arb-agent"}
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_creates_task_and_dispatches_arbitration_agent(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import ARBITRATION_CREATED_BY, _trigger_arbitration

        mock_create_agent.return_value = {"agent_id": "arb-agent"}

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted 5 attempts", MagicMock()
        )

        assert result is True
        mock_create_agent.assert_called_once()
        _, kwargs = mock_create_agent.call_args
        assert kwargs["agent_type"] == "arbitration"
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_prompt_lists_valid_phase_names(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """An LLM-hallucinated/mis-cased target_phase makes goto silently
        fall back (see TestResolveArbitrationOutcome's unresolvable-target
        test) -- enumerating the real, exact names up front is the
        prevention half of that defense, not just the fallback."""
        from src.autopilot.orchestrator import _trigger_arbitration

        mock_create_agent.return_value = {"agent_id": "arb-agent"}

        _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted", MagicMock()
        )

        _, kwargs = mock_create_agent.call_args
        prompt = kwargs["enriched_data_override"]["validation_prompt"]
        assert "requirements" in prompt
        assert "implementation" in prompt  # sample_workflow's phase-2

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_caps_repeated_arbitration_and_fails_workflow(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """A persistently-confused arbiter that keeps choosing "goto" back
        into a phase that keeps re-exhausting its budget must not be able
        to cycle forever (5 real attempts, arbitrate, goto, 5 more,
        arbitrate again...). Past MAX_ARBITRATIONS_PER_PHASE, fail instead
        of spawning yet another arbitration agent -- "never pause for a
        human" doesn't mean "never terminate"."""
        from src.autopilot.orchestrator import ARBITRATION_CREATED_BY, _trigger_arbitration

        mock_create_agent.return_value = {"agent_id": "arb-agent"}

        # 3 prior arbitration tasks already exist for this phase.
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

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "still not converging", MagicMock()
        )

        assert result is False
        mock_create_agent.assert_not_called()
        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed"
            assert "requirements" in wf.status_reason
            assert "3 times" in wf.status_reason

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
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
        from src.autopilot.orchestrator import (
            OrchestratorLogger,
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

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_prompt_forbids_editing_files(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """The arbitration agent has full read/write/bash tool access in
        the shared worktree (same as any other phase agent) -- without an
        explicit boundary, an eager coding agent could "just fix it"
        directly instead of writing a decision, skipping the fix's own
        review/test cycle entirely."""
        from src.autopilot.orchestrator import _trigger_arbitration

        mock_create_agent.return_value = {"agent_id": "arb-agent"}

        _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "exhausted", MagicMock()
        )

        _, kwargs = mock_create_agent.call_args
        prompt = kwargs["enriched_data_override"]["validation_prompt"].lower()
        assert "do not edit" in prompt or "not edit" in prompt

    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_already_in_flight_skips_duplicate(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import _claim_phase_task_creation, _trigger_arbitration

        with db_manager.session_scope() as session:
            _claim_phase_task_creation(session, "phase-1")

        result = _trigger_arbitration(
            "wf-1", "phase-1", "requirements", "reason", MagicMock()
        )

        assert result is False
        mock_create_agent.assert_not_called()

    @patch("src.autopilot.orchestrator.PhaseManager")
    @patch("src.autopilot.orchestrator.create_agent_for_task_direct")
    def test_dispatch_failure_fails_workflow_instead_of_silent_pause(
        self, mock_create_agent, mock_pm_class, db_manager, sample_workflow
    ):
        """Regression scenario within a regression fix: if even spawning the
        arbitration agent fails, the phase must not end up silently
        re-claimed forever -- it fails loudly and immediately."""
        from src.autopilot.orchestrator import _trigger_arbitration

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


class TestReadArbitrationResult:
    def test_valid_file(self, tmp_path):
        from src.autopilot.orchestrator import _read_arbitration_result
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
        from src.autopilot.orchestrator import _read_arbitration_result

        assert _read_arbitration_result(str(tmp_path)) == (None, None, None)

    def test_no_working_directory(self):
        from src.autopilot.orchestrator import _read_arbitration_result

        assert _read_arbitration_result(None) == (None, None, None)

    def test_malformed_json(self, tmp_path):
        from src.autopilot.orchestrator import _read_arbitration_result
        from src.core.constants import CONTEXT_DIR_NAME

        d = tmp_path / CONTEXT_DIR_NAME
        d.mkdir()
        (d / "arbitration_result.json").write_text("not json")

        assert _read_arbitration_result(str(tmp_path)) == (None, None, None)

    def test_invalid_decision_value(self, tmp_path):
        from src.autopilot.orchestrator import _read_arbitration_result
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
            from src.autopilot.orchestrator import _claim_phase_task_creation

            _claim_phase_task_creation(session, "phase-1")

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_continue_dispatches_next_phase_task(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import _resolve_arbitration_outcome

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
            "wf-1", "phase-2", "implementation", "continue", ANY, feedback=None
        )
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status_reason is None

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_continue_at_last_phase_does_not_dispatch(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """Workflow-complete case: no next phase, so nothing to dispatch --
        must not crash or call _create_phase_task with a None target."""
        from src.autopilot.orchestrator import _resolve_arbitration_outcome

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_goto_dispatches_target_phase_task(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import _resolve_arbitration_outcome

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
            "wf-1", "phase-2", "implementation", "goto", ANY, feedback="fix x"
        )
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_goto_dispatch_failure_is_logged_loudly(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """If _create_phase_task itself fails (e.g. agent creation error),
        this must be logged as an error, not silently swallowed -- "surface
        errors better" applies to this failure mode too."""
        from src.autopilot.orchestrator import _resolve_arbitration_outcome

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_fail_calls_force_fail_clears_claim_and_sets_status_reason(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        from src.autopilot.orchestrator import _resolve_arbitration_outcome

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_goto_without_target_treated_as_fail(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """A malformed decision (goto with no target_phase) must not crash
        or hang -- falls back to fail rather than silently doing nothing."""
        from src.autopilot.orchestrator import _resolve_arbitration_outcome

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_goto_with_unresolvable_target_phase_is_surfaced_not_hidden(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow
    ):
        """An LLM-hallucinated or mis-cased target_phase (_find_phase_by_
        name_or_order does an EXACT string match) makes _handle_force_goto
        fall back to _advance_or_complete internally, returning an action
        that ISN'T "goto". The raw decision the arbiter wrote must not be
        trusted blindly for clearing status_reason -- check what actually
        happened."""
        from src.autopilot.orchestrator import _resolve_arbitration_outcome

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
            "wf-1", "phase-2", "next_real_phase", "continue", ANY, feedback=None
        )


class TestMaybeResolveArbitration:
    """End-to-end-ish: seeds a real arbitration Task + claimed
    PhaseExecution + arbitration_result.json on disk, and confirms the
    sweep-tick consumer resolves it."""

    def _seed_arbitration_in_flight(self, db_manager, task_status="done"):
        from src.autopilot.orchestrator import ARBITRATION_CREATED_BY, _claim_phase_task_creation

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_resolves_done_arbitration_with_valid_result(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        from src.core.constants import CONTEXT_DIR_NAME
        from src.autopilot.orchestrator import _maybe_resolve_arbitration

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
            "wf-1", "phase-2", "implementation", "continue", ANY, feedback=None
        )
        with db_manager.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-1").first()
            assert execution.task_creation_claimed_at is None

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_still_running_arbitration_is_left_alone(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        from src.autopilot.orchestrator import _maybe_resolve_arbitration

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_failed_arbitration_agent_resolves_as_fail(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        """The arbitration agent itself dying/failing must not leave the
        phase stuck forever -- resolves as a fail decision automatically."""
        from src.autopilot.orchestrator import ARBITRATION_CREATED_BY, _claim_phase_task_creation, _maybe_resolve_arbitration

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

    @patch("src.autopilot.orchestrator._create_phase_task")
    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_done_without_result_file_resolves_as_fail(
        self, mock_pm_class, mock_create_task, db_manager, sample_workflow, tmp_path
    ):
        """Regression backstop: an arbitration agent that calls
        update_task_status(done) without ever writing
        arbitration_result.json must not hang the phase forever."""
        from src.autopilot.orchestrator import _maybe_resolve_arbitration

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

    @patch("src.autopilot.orchestrator.PhaseManager")
    def test_no_arbitration_task_is_a_noop(
        self, mock_pm_class, db_manager, sample_workflow
    ):
        """A phase with a stale/unrelated claim but no arbitration task at
        all (e.g. a normal in-flight task-creation claim) must be left
        alone -- only genuine arbitration in-flight is acted on."""
        from src.autopilot.orchestrator import _claim_phase_task_creation, _maybe_resolve_arbitration

        with db_manager.session_scope() as session:
            _claim_phase_task_creation(session, "phase-1")

        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm

        _maybe_resolve_arbitration("wf-1", MagicMock())

        mock_pm.mark_phase_complete.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
