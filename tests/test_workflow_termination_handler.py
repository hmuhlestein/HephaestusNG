"""Coverage for WorkflowTerminationHandler.terminate_workflow (SOLID review 2.14).

This path had no real test coverage at all -- the one test that referenced it
(test_result_submission_flow.py) patches terminate_workflow out entirely, so
nothing exercised the actual teardown. It fires when a result validator
confirms a workflow's output, i.e. on the success path of a whole design.

The finding is that the four sub-steps (terminate agents / cancel tasks /
clean up resources / mark the workflow failed) are not atomic. They are
written as one try/except with a rollback, which reads as transactional but
is not: the sub-steps commit independently, so a failure in a later step
leaves the earlier ones durably applied.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.database import (
    Agent,
    AgentBranch,
    DatabaseManager,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
)
from src.workflow.termination_handler import WorkflowTerminationHandler

WORKFLOW_ID = "wf-1"


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "term.db"))
    manager.create_tables()
    return manager


@pytest.fixture
def agent_manager():
    am = MagicMock()
    am.terminate_agent = AsyncMock(return_value=True)
    return am


@pytest.fixture
def handler(db, agent_manager):
    return WorkflowTerminationHandler(db_manager=db, agent_manager=agent_manager)


def _seed(
    db,
    task_status="in_progress",
    agent_type="phase",
    agent_status="working",
    with_worktree=False,
    phase_execution_status=None,
    extra_task_statuses=(),
):
    session = db.get_session()
    session.add(
        Workflow(
            id=WORKFLOW_ID,
            name="wf",
            phases_folder_path="/tmp",
            status="active",
            created_at=datetime.utcnow(),
        )
    )
    session.add(
        Phase(
            id="ph-1",
            workflow_id=WORKFLOW_ID,
            order=1,
            name="development",
            description="d",
            done_definitions=["done"],
        )
    )
    session.add(
        Agent(
            id="agent-1",
            system_prompt="p",
            status=agent_status,
            cli_type="test",
            agent_type=agent_type,
            current_task_id="task-1",
        )
    )
    session.add(
        Task(
            id="task-1",
            raw_description="do it",
            done_definition="done",
            status=task_status,
            workflow_id=WORKFLOW_ID,
            assigned_agent_id="agent-1",
            phase_id="ph-1",
        )
    )
    for i, status in enumerate(extra_task_statuses):
        session.add(
            Task(
                id=f"task-extra-{i}",
                raw_description="x",
                done_definition="done",
                status=status,
                workflow_id=WORKFLOW_ID,
                phase_id="ph-1",
            )
        )
    if phase_execution_status:
        session.add(
            PhaseExecution(
                id="pe-1",
                phase_id="ph-1",
                workflow_execution_id=WORKFLOW_ID,
                status=phase_execution_status,
            )
        )
    if with_worktree:
        session.add(
            AgentBranch(
                agent_id="agent-1",
                worktree_path="/tmp/wt",
                branch_name="agent-1-branch",
                parent_commit_sha="abc",
                base_commit_sha="def",
                merge_status="active",
            )
        )
    session.commit()
    session.close()


def _read(db, model, **filters):
    session = db.get_session()
    try:
        return session.query(model).filter_by(**filters).first()
    finally:
        session.close()


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_terminates_agents_fails_tasks_and_marks_workflow_failed(
        self, handler, db, agent_manager
    ):
        _seed(db)

        results = await handler.terminate_workflow(WORKFLOW_ID)

        agent_manager.terminate_agent.assert_awaited_once_with("agent-1")
        assert _read(db, Task, id="task-1").status == "failed"
        workflow = _read(db, Workflow, id=WORKFLOW_ID)
        assert workflow.status == "failed"
        assert workflow.completed_by_result is True
        assert results["workflow_id"] == WORKFLOW_ID
        assert results["errors"] == []

    @pytest.mark.asyncio
    async def test_missing_workflow_raises(self, handler, db):
        _seed(db)
        with pytest.raises(ValueError, match="Workflow not found"):
            await handler.terminate_workflow("nope")

    @pytest.mark.asyncio
    async def test_result_validator_agents_are_spared(
        self, handler, db, agent_manager
    ):
        """Terminating the validator would kill the very agent that just
        produced the validated result this teardown is reacting to."""
        _seed(db, agent_type="result_validator")

        await handler.terminate_workflow(WORKFLOW_ID)

        agent_manager.terminate_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_termination_failure_is_recorded_without_aborting(
        self, handler, db, agent_manager
    ):
        """One agent refusing to die must not abandon the rest of teardown."""
        _seed(db)
        agent_manager.terminate_agent.side_effect = RuntimeError("tmux gone")

        results = await handler.terminate_workflow(WORKFLOW_ID)

        assert any("terminate_agent" in e["context"] for e in results["errors"])
        # Teardown still completed.
        assert _read(db, Workflow, id=WORKFLOW_ID).status == "failed"


class TestPhaseExecutionOutcomes:
    @pytest.mark.asyncio
    async def test_in_progress_with_completed_work_and_nothing_pending_completes(
        self, handler, db
    ):
        _seed(db, task_status="done", phase_execution_status="in_progress")

        await handler.terminate_workflow(WORKFLOW_ID)

        assert _read(db, PhaseExecution, id="pe-1").status == "completed"

    @pytest.mark.asyncio
    async def test_pending_tasks_do_not_keep_the_phase_in_progress(self, handler, db):
        """Pins actual behavior, which contradicts the code's own comment.

        _cleanup_workflow_resources has an `elif pending_tasks > 0` branch
        commented "Keep in_progress so pending tasks can be dispatched" -- but
        _cancel_workflow_tasks runs first and marks every pending/assigned/
        in_progress task failed, so that count is always zero by the time it
        is read. The branch is unreachable and the phase completes instead.
        Left in place rather than removed (pre-existing dead code is out of
        scope for this change); recorded here so the next reader does not
        mistake the comment for behavior.
        """
        _seed(
            db,
            task_status="done",
            phase_execution_status="in_progress",
            extra_task_statuses=("pending",),
        )

        await handler.terminate_workflow(WORKFLOW_ID)

        assert _read(db, PhaseExecution, id="pe-1").status == "completed"

    @pytest.mark.asyncio
    async def test_in_progress_with_no_completed_work_fails(self, handler, db):
        _seed(db, task_status="under_review", phase_execution_status="in_progress")

        await handler.terminate_workflow(WORKFLOW_ID)

        assert _read(db, PhaseExecution, id="pe-1").status == "failed"

    @pytest.mark.asyncio
    async def test_never_started_phase_fails(self, handler, db):
        _seed(db, phase_execution_status="pending")

        await handler.terminate_workflow(WORKFLOW_ID)

        assert _read(db, PhaseExecution, id="pe-1").status == "failed"


class TestWorktreeCleanupReporting:
    @pytest.mark.asyncio
    async def test_abandoned_worktree_is_reported_as_success(self, handler, db):
        """Regression: the cleanup record read worktree.branch_path, but the
        column is worktree_path (AgentBranch is an alias of AgentWorktree).
        Every worktree therefore raised AttributeError, was swallowed by the
        per-item except, and got reported "success": False -- while the
        merge_status write on the preceding line was still committed. Callers
        inspecting cleanup_actions to decide whether teardown worked were told
        it failed every single time, for work that had actually succeeded.
        """
        _seed(db, with_worktree=True)

        results = await handler.terminate_workflow(WORKFLOW_ID)

        abandons = [
            a for a in results["cleanup_actions"] if a["action"] == "abandon_worktree"
        ]
        assert abandons, "expected an abandon_worktree cleanup record"
        assert all(a["success"] for a in abandons), abandons
        assert _read(db, AgentBranch, agent_id="agent-1").merge_status == "abandoned"


class TestAtomicity:
    @pytest.mark.asyncio
    async def test_a_failure_in_the_final_step_rolls_back_task_cancellation(
        self, handler, db, monkeypatch, agent_manager
    ):
        """The sub-steps must share one transaction.

        Written as one try/except with a rollback, this function reads as
        transactional -- but each sub-step used to commit on its own, so a
        failure in a later step left the earlier ones durably applied. The
        resulting state (every task failed, workflow still "active") is the
        stale-active-workflow shape the orchestrator has separate escalation
        machinery to clean up, and it blocks the design queue until that
        fires.
        """
        _seed(db)

        real_cleanup = handler._cleanup_workflow_resources

        async def exploding_cleanup(workflow_id, session, results):
            await real_cleanup(workflow_id, session, results)
            raise RuntimeError("cleanup blew up after cancelling tasks")

        monkeypatch.setattr(handler, "_cleanup_workflow_resources", exploding_cleanup)

        with pytest.raises(RuntimeError):
            await handler.terminate_workflow(WORKFLOW_ID)

        # Nothing the failed teardown touched may survive.
        assert _read(db, Task, id="task-1").status == "in_progress"
        assert _read(db, Workflow, id=WORKFLOW_ID).status == "active"
        # ...except the one step that cannot be rolled back. Killing a tmux
        # session is irreversible and terminate_agent runs its own
        # transaction, so the agent stays terminated. Pinned because
        # terminate_workflow's docstring makes this promise explicitly: the
        # DB half is atomic, the process half is not.
        agent_manager.terminate_agent.assert_awaited_once_with("agent-1")
