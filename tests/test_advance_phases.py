"""Tests for orchestrator._advance_phases and related phase transition functions.

These tests address the critical test coverage gap identified in ARCHITECTURE_REVIEW.md:
"_advance_phases has no test referencing it anywhere in tests/"
"""

from datetime import datetime, timedelta
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
        (MANUAL_ONLY_PHASES, i.e. git_commit_push) is waiting on a human --
        it must not freeze every OTHER in-progress phase too. Before this
        fix, the top-level `if wf.status == "paused": return False` gate
        short-circuited _advance_phases entirely regardless of paused_by,
        so a workflow paused for git_commit_push approval silently stopped
        retrying/self-healing every unrelated phase as well. Observed
        live: task a1efdda6 (an adversarial_review-phase task, nothing to
        do with git_commit_push) sat orphaned and was never retried while
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
            wf.status_reason = "git_commit_push is manual-only; human approval is required"

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


class TestMaybeRetryFailedTasks:
    """Tests for _maybe_retry_failed_tasks function."""

    def test_manual_git_phase_pauses_without_retrying_in_review_mode(self, db_manager, sample_workflow):
        """The human-only hand-off must not consume agent retries or spawn
        rejected agents, otherwise it starves the global design queue --
        but only when the project is actually in review mode (see the
        companion full-autopilot test below)."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp/proj-1", review_mode=True))
            workflow = session.query(Workflow).filter_by(id="wf-1").first()
            workflow.project_id = "proj-1"
            phase = session.query(Phase).filter_by(id="phase-1").first()
            phase.name = "git_commit_push"
            session.add(Task(
                id="task-manual-git",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="Human Git hand-off",
                done_definition="Human approval",
                status="failed",
                failure_reason="manual-only",
            ))

        logger = MagicMock()
        with patch("src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct") as dispatch:
            with db_manager.session_scope() as session:
                phase = session.query(Phase).filter_by(id="phase-1").first()
                assert _maybe_retry_failed_tasks(session, phase, logger) is None
                dispatch.assert_not_called()

        with db_manager.session_scope() as session:
            workflow = session.query(Workflow).filter_by(id="wf-1").first()
            assert workflow.status == "paused"
            assert workflow.paused_by == "review"
            assert "manual-only" in workflow.status_reason

    def test_manual_git_phase_retries_normally_in_full_autopilot(self, db_manager, sample_workflow):
        """Regression: full autopilot (no project, or review_mode off) must
        retain the original autonomous git_commit_push behavior -- a real
        agent commits, pushes, and opens the PR, same as any other phase.
        Only review_mode actually asks for a human in the loop."""
        from src.autopilot.orchestrator.phase_transitions import _maybe_retry_failed_tasks

        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            phase.name = "git_commit_push"
            session.add(Task(
                id="task-manual-git",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="Human Git hand-off",
                done_definition="Human approval",
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

    @patch("src.autopilot.orchestrator.arbitration.create_agent_for_task_direct")
    def test_caps_repeated_arbitration_and_fails_workflow(
        self, mock_create_agent, db_manager, sample_workflow
    ):
        """A persistently-confused arbiter that keeps choosing "goto" back
        into a phase that keeps re-exhausting its budget must not be able
        to cycle forever (5 real attempts, arbitrate, goto, 5 more,
        arbitrate again...). Past MAX_ARBITRATIONS_PER_PHASE, fail instead
        of spawning yet another arbitration agent -- "never pause for a
        human" doesn't mean "never terminate"."""
        from src.autopilot.orchestrator.phase_transitions import ARBITRATION_CREATED_BY
        from src.autopilot.orchestrator.phase_transitions import _trigger_arbitration

        mock_create_agent.side_effect = _agent_row_side_effect("arb-agent")

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

        with patch("src.autopilot.orchestrator.phase_transitions.GATED_PHASES", ("qa_validation",)):
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestManualHandoffPausesBeforeOrphanSweep:
    """Regression test: a manual-only phase (git_commit_push) under review
    mode must pause the workflow immediately, not after the generic
    orphan-staleness sweep mislabels its undispatched task as failed.

    Found live 2026-08-19 investigating task 2ffbcab0-b07e-4aa7-8515-
    b06d857bf48a: create_agent_for_task_direct converts git_commit_push's
    intentional PermissionError (review mode blocks autonomous commit/
    push) into an ordinary "dispatch failed" None return, so the task just
    sits "pending" like any other undispatched task -- indistinguishable
    from a genuinely abandoned one to the orphan-staleness sweep a few
    lines below. That sweep marked it failed with the generic "Orphaned:
    never dispatched to an agent" reason after its 1-minute cutoff, and
    only THEN -- once failed_count > 0 let a later sweep pass reach
    _maybe_retry_failed_tasks's own manual-only check -- did the workflow
    actually pause for review. The pause happened either way, but the
    operator saw a misleading "orphaned" failure first, and it took an
    extra sweep cycle to happen. The fix checks MANUAL_ONLY_PHASES +
    _manual_handoff_required before the orphan-staleness block, not after
    it, in _case_in_progress_complete.
    """

    def _seed_git_commit_push_phase(self, db_manager, *, with_stale_pending_task):
        with db_manager.session_scope() as session:
            session.add(
                Workflow(id="wf-gcp", name="w", status="active", phases_folder_path="/tmp")
            )
            session.add(
                Phase(
                    id="phase-gcp", workflow_id="wf-gcp", name="git_commit_push", order=1,
                    description="d", done_definitions=["done"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-gcp", phase_id="phase-gcp", workflow_execution_id="wf-gcp",
                    status="in_progress", started_at=datetime.utcnow() - timedelta(minutes=10),
                )
            )
            if with_stale_pending_task:
                session.add(
                    Task(
                        id="task-gcp-1", workflow_id="wf-gcp", phase_id="phase-gcp",
                        raw_description="r", done_definition="d", status="pending",
                        created_at=datetime.utcnow() - timedelta(minutes=5),
                    )
                )

    @patch("src.autopilot.orchestrator.phase_transitions._manual_handoff_required", return_value=True)
    def test_pauses_immediately_with_no_task_yet(self, mock_required, db_manager):
        """The phase is in_progress but hasn't even created a task yet --
        the pause must not wait for one to exist and go stale first."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_git_commit_push_phase(db_manager, with_stale_pending_task=False)

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-gcp")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-gcp", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-gcp").first()
            assert wf.status == "paused"
            assert wf.paused_by == "review"

    @patch("src.autopilot.orchestrator.phase_transitions._manual_handoff_required", return_value=True)
    def test_a_stale_pending_task_is_not_mislabeled_orphaned(self, mock_required, db_manager):
        """The exact live bug: a task already sitting stale-pending in this
        phase must be left alone -- paused for review, not marked failed
        with the generic, misleading orphan reason."""
        from src.autopilot.orchestrator.phase_transitions import _case_in_progress_complete, _get_phase_statuses

        self._seed_git_commit_push_phase(db_manager, with_stale_pending_task=True)

        with db_manager.session_scope() as session:
            phase_statuses = _get_phase_statuses(session, "wf-gcp")
            in_progress = [p for p in phase_statuses if p["status"] == "in_progress"]
            _case_in_progress_complete(session, "wf-gcp", in_progress, MagicMock())

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-gcp").first()
            assert wf.status == "paused"
            assert wf.paused_by == "review"

            task = session.query(Task).filter_by(id="task-gcp-1").first()
            assert task.status == "pending", (
                "the task must be left untouched for the human to act on, "
                "not failed out from under them with a misleading reason"
            )
            assert task.failure_reason is None
