"""Regression test for the "aborted create-launch still commits a stale
task mutation" bug in src/agents/launch_pipeline.py's _check_termination_race.

create_agent_for_task sets task.assigned_agent_id/status="in_progress"/
started_at on its in-memory `task` object right after pane.send_keys()
launches the tmux session -- before _check_termination_race runs, and long
before create_agent_for_task_direct's own session.commit() (its caller,
much further up the call stack) actually persists that mutation.

If _check_termination_race then detects the agent was terminated in the
meantime (e.g. a concurrent workflow pause found it via Agent.current_task_id
and killed it), it used to only kill the tmux session and return an abort
sentinel -- indistinguishable in shape from a real success (`AgentInfo`), so
the caller had no way to know NOT to commit. The already-mutated `task`
object still said status="in_progress", and that got persisted anyway: a
task pointing at an agent that was terminated seconds after creation,
invisible to every sweep until health_audit's 30-minute stuck-timeout
finally noticed, with a misleading "no agent activity for 30 minutes"
reason.

Confirmed live: task 1d27052e's workflow was paused_by="user" (paused_at
20:42:07.510) at almost the exact instant its agent was terminated
(terminated_at 20:42:07.557) -- 31 seconds after the agent was created,
while create_agent_for_task was still mid-launch. The task's started_at
wasn't committed until 20:42:37.665, 30 seconds after the agent was
already dead, because create_agent_for_task_direct's session.commit()
persisted the stale in-memory mutation regardless.
"""

import uuid
from datetime import datetime, timedelta

import pytest

from src.core.database import Agent, DatabaseManager, Task, Workflow


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def launch_pipeline(db_manager):
    from unittest.mock import MagicMock

    from src.agents.manager import AgentManager

    manager = AgentManager(
        db_manager=db_manager,
        llm_provider=MagicMock(),
        phase_manager=MagicMock(),
        tmux_server=MagicMock(),
    )
    return manager._launch


def _seed(db_manager, *, agent_status, task_status, assigned_agent_id, paused_by=None):
    task_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())
    with db_manager.session_scope() as session:
        session.add(
            Workflow(
                id="wf-1", name="wf-1", status="paused", phases_folder_path="/tmp",
                paused_by=paused_by,
            )
        )
        session.add(
            Task(
                id=task_id,
                workflow_id="wf-1",
                raw_description="r",
                done_definition="d",
                status=task_status,
                priority="medium",
                assigned_agent_id=assigned_agent_id,
            )
        )
        session.add(
            Agent(
                id=agent_id,
                system_prompt="p",
                status=agent_status,
                cli_type="claude",
                current_task_id=task_id,
            )
        )
    return task_id, agent_id


class TestTerminationRaceRevertsStaleTaskMutation:
    @pytest.mark.asyncio
    async def test_agent_terminated_mid_launch_reverts_in_memory_task(
        self, db_manager, launch_pipeline
    ):
        """Mirrors task 1d27052e: agent was terminated (by a concurrent
        workflow pause) while its DB row was still "pending", so a
        terminated-agent abort must copy that "pending"/no-agent state back
        onto the in-memory task -- not leave it holding the speculative
        "in_progress" write the create path set before this check ran."""
        task_id, agent_id = _seed(
            db_manager, agent_status="terminated", task_status="pending",
            assigned_agent_id=None,
        )

        # The exact speculative mutation create_agent_for_task performs on
        # its in-memory `task` object right after pane.send_keys(), well
        # before _check_termination_race or the caller's eventual commit.
        stale_task = Task(id=task_id)
        stale_task.assigned_agent_id = agent_id
        stale_task.status = "in_progress"
        stale_task.started_at = datetime.utcnow()

        launch_pipeline._agent_manager.tmux_server.has_session.return_value = False

        result = await launch_pipeline._check_termination_race(
            agent_id, task_id, "some-session", agent_id_to_return=agent_id,
            task=stale_task,
        )

        assert result is not None, "expected an abort (AgentInfo) result"
        assert stale_task.status == "pending", (
            f"in-memory task still says {stale_task.status!r} -- the "
            "caller's later session.commit() will persist this stale "
            "'in_progress' write over the real 'pending' state, stranding "
            "the task on a corpse agent"
        )
        assert stale_task.assigned_agent_id is None

    @pytest.mark.asyncio
    async def test_agent_terminated_by_user_pause_stamps_user_terminated_reason(
        self, db_manager, launch_pipeline
    ):
        """pause_project_workflows's own tasks_to_reset pass stamps this
        same message, but it and this check both key off the just-
        terminated Agent row -- either can win the race to act on a given
        task first. When this path wins (task-reset side of the pause
        hasn't committed yet), _check_termination_race must independently
        attribute the termination via Workflow.paused_by, which is set in
        the exact same commit as the agent termination it just detected."""
        task_id, agent_id = _seed(
            db_manager, agent_status="terminated", task_status="pending",
            assigned_agent_id=None, paused_by="user",
        )

        stale_task = Task(id=task_id)
        stale_task.assigned_agent_id = agent_id
        stale_task.status = "in_progress"
        stale_task.started_at = datetime.utcnow()

        launch_pipeline._agent_manager.tmux_server.has_session.return_value = False

        await launch_pipeline._check_termination_race(
            agent_id, task_id, "some-session", agent_id_to_return=agent_id,
            task=stale_task,
        )

        assert stale_task.failure_reason == "User terminated: workflow was paused"

    @pytest.mark.asyncio
    async def test_agent_terminated_without_user_pause_leaves_reason_alone(
        self, db_manager, launch_pipeline
    ):
        """Only a "user" pause gets this specific message -- an unrelated
        termination (e.g. mechanical_recovery killing a frozen session)
        must not be mislabeled as a pause the user never triggered."""
        task_id, agent_id = _seed(
            db_manager, agent_status="terminated", task_status="pending",
            assigned_agent_id=None, paused_by=None,
        )

        stale_task = Task(id=task_id)
        stale_task.assigned_agent_id = agent_id
        stale_task.status = "in_progress"
        stale_task.started_at = datetime.utcnow()

        launch_pipeline._agent_manager.tmux_server.has_session.return_value = False

        await launch_pipeline._check_termination_race(
            agent_id, task_id, "some-session", agent_id_to_return=agent_id,
            task=stale_task,
        )

        assert stale_task.failure_reason != "User terminated: workflow was paused"

    @pytest.mark.asyncio
    async def test_task_reassigned_mid_launch_reverts_in_memory_task(
        self, db_manager, launch_pipeline
    ):
        """The other abort trigger: the task was reassigned to a different
        agent (or went terminal) while this launch was mid-flight."""
        other_agent_id = str(uuid.uuid4())
        task_id, agent_id = _seed(
            db_manager, agent_status="working", task_status="in_progress",
            assigned_agent_id=other_agent_id,
        )

        stale_task = Task(id=task_id)
        stale_task.assigned_agent_id = agent_id
        stale_task.status = "in_progress"
        stale_task.started_at = datetime.utcnow() - timedelta(seconds=5)

        launch_pipeline._agent_manager.tmux_server.has_session.return_value = False

        result = await launch_pipeline._check_termination_race(
            agent_id, task_id, "some-session", agent_id_to_return=agent_id,
            task=stale_task,
        )

        assert result is not None
        assert stale_task.assigned_agent_id == other_agent_id, (
            "in-memory task still points at the aborted agent, not the "
            "one it was actually reassigned to"
        )

    @pytest.mark.asyncio
    async def test_no_race_leaves_task_untouched(self, db_manager, launch_pipeline):
        """The common case: no concurrent termination/reassignment. Must
        not touch `task` or report an abort."""
        task_id, agent_id = _seed(
            db_manager, agent_status="working", task_status="in_progress",
            assigned_agent_id=None,
        )

        live_task = Task(id=task_id)
        live_task.assigned_agent_id = agent_id
        live_task.status = "in_progress"
        marker = datetime.utcnow()
        live_task.started_at = marker

        launch_pipeline._agent_manager.tmux_server.has_session.return_value = False

        result = await launch_pipeline._check_termination_race(
            agent_id, task_id, "some-session", agent_id_to_return=agent_id,
            task=live_task,
        )

        assert result is None
        assert live_task.status == "in_progress"
        assert live_task.assigned_agent_id == agent_id
        assert live_task.started_at == marker


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
