"""Tests for two /create_task guards added after a live incident:

1. Content-aware dedup: the phase+workflow dedup used to match on phase_id
   alone, silently discarding genuinely different task descriptions submitted
   while one task was already active in that phase (an agent believed it
   created 5 distinct tasks; only 1 was ever actually recorded).
2. Own-phase guard: an agent creating the FIRST task for a phase other than
   its own now gets rejected — this is what let a scope_review agent
   mistakenly file full implementation work under an architecture_design
   phase, since nothing validated the phase number it guessed.
"""

import uuid

import pytest

from src.core.database import Agent, DatabaseManager, Phase, Task, Workflow


@pytest.fixture
def task_env(tmp_path, monkeypatch):
    """Seed a real sqlite DB and point server_state.db_manager at it."""
    from src.mcp.server._shared import server_state

    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    monkeypatch.setattr(server_state, "db_manager", manager)

    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    session = manager.get_session()
    try:
        session.add(
            Workflow(
                id=workflow_id,
                name="Test Workflow",
                phases_folder_path="/tmp",
                status="active",
                definition_id="autopilot",
            )
        )
        phase_a_id = f"phase-{uuid.uuid4().hex[:8]}"
        phase_a_order = 2
        phase_b_id = f"phase-{uuid.uuid4().hex[:8]}"
        phase_b_order = 3
        session.add(
            Phase(
                id=phase_a_id,
                workflow_id=workflow_id,
                order=phase_a_order,
                name="scope_review",
                description="scope review",
                done_definitions=["done"],
            )
        )
        session.add(
            Phase(
                id=phase_b_id,
                workflow_id=workflow_id,
                order=phase_b_order,
                name="architecture_design",
                description="architecture design",
                done_definitions=["done"],
            )
        )
        session.commit()

        agent_id = f"agent-{uuid.uuid4().hex[:8]}"
        own_task_id = str(uuid.uuid4())
        session.add(
            Task(
                id=own_task_id,
                raw_description="review the scope",
                done_definition="done",
                status="in_progress",
                workflow_id=workflow_id,
                phase_id=phase_a_id,
                assigned_agent_id=agent_id,
            )
        )
        session.add(
            Agent(
                id=agent_id,
                system_prompt="test",
                status="working",
                cli_type="pi",
                current_task_id=own_task_id,
            )
        )
        session.commit()
    finally:
        session.close()

    return {
        "manager": manager,
        "workflow_id": workflow_id,
        "phase_a_order": phase_a_order,
        "phase_b_id": phase_b_id,
        "phase_b_order": phase_b_order,
        "agent_id": agent_id,
    }


def _make_request(**overrides):
    from src.mcp.server._shared import CreateTaskRequest

    defaults = dict(
        task_description="do some work",
        done_definition="done when complete",
        ai_agent_id="test-agent",
        workflow_id="wf-x",
        phase_order=1,
    )
    defaults.update(overrides)
    return CreateTaskRequest(**defaults)


class TestOwnPhaseGuard:
    @pytest.mark.asyncio
    async def test_rejects_task_for_a_different_phase(self, task_env):
        """The exact incident: an agent working phase order 2 tries to seed
        the first task for phase order 3 — must be rejected."""
        from fastapi import HTTPException

        from src.mcp.server.agent_task_routes import create_task

        request = _make_request(
            workflow_id=task_env["workflow_id"],
            phase_id=task_env["phase_b_id"],
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_task(request, agent_id=task_env["agent_id"])
        assert exc_info.value.status_code == 400
        assert "architecture_design" in exc_info.value.detail
        assert "scope_review" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_allows_task_for_own_phase(self, task_env):
        """A subtask within the agent's own current phase must not be
        blocked by the guard (it may still fail later without LLM mocks —
        we only assert it gets past the guard, not full success)."""
        from fastapi import HTTPException

        from src.mcp.server.agent_task_routes import create_task

        request = _make_request(
            workflow_id=task_env["workflow_id"],
            phase_order=task_env["phase_a_order"],
        )
        try:
            await create_task(request, agent_id=task_env["agent_id"])
        except HTTPException as e:
            # Anything but the own-phase-guard's 400 is fine here — this
            # request may still fail deeper in the function (LLM enrichment
            # isn't mocked in this lightweight test).
            assert "Refusing to create a task for phase" not in str(e.detail)


class TestAutoResolvedPhaseNamedInDescription:
    """The gap the own-phase guard above didn't cover: an agent that OMITS
    phase_id/phase_order (rather than guessing a wrong one) gets it silently
    auto-resolved to its own current phase -- own_phase.order != target_
    phase.order can never be true in that case, since both sides come from
    the same lookup. The exact incident this guards against: a development-
    phase agent created a task titled "Adversarial review of ChatPanel.tsx"
    with no phase_id -- it got filed under development (whose hard floor
    requires a commit), the agent correctly treated it as pure verification
    and made none, and a later retry failed with "No commit was made
    during this development task"."""

    @pytest.mark.asyncio
    async def test_rejects_omitted_phase_id_naming_another_real_phase(self, task_env):
        from fastapi import HTTPException

        from src.mcp.server.agent_task_routes import create_task

        # References "architecture design" -- the fixture's other real
        # phase (phase_b, name "architecture_design") -- the same way the
        # live incident's task named "adversarial review" (a different
        # real phase of that workflow) in its own description.
        request = _make_request(
            task_description="Architecture design review: verify the component boundaries",
            workflow_id=task_env["workflow_id"],
            phase_id=None,
            phase_order=None,
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_task(request, agent_id=task_env["agent_id"])
        assert exc_info.value.status_code == 400
        assert "architecture_design" in exc_info.value.detail
        assert "scope_review" in exc_info.value.detail
        assert "create_ticket" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_allows_omitted_phase_id_for_ordinary_own_phase_work(self, task_env):
        """Sanity check the fix isn't overbroad: a genuine same-phase
        subtask with no phase_id, whose description doesn't name a
        different phase, must still pass (the M-6 fix's actual intent)."""
        from fastapi import HTTPException

        from src.mcp.server.agent_task_routes import create_task

        request = _make_request(
            task_description="Implement the pagination helper for the scope list",
            workflow_id=task_env["workflow_id"],
            phase_id=None,
            phase_order=None,
        )
        try:
            await create_task(request, agent_id=task_env["agent_id"])
        except HTTPException as e:
            assert "Refusing to create this task" not in str(e.detail)
            assert "Refusing to create a task for phase" not in str(e.detail)


class TestContentAwareDedup:
    @pytest.mark.asyncio
    async def test_similar_description_is_deduped(self, task_env):
        """A near-identical resubmission for the same phase should return
        the existing task rather than creating a new one."""
        from src.mcp.server.agent_task_routes import create_task

        manager = task_env["manager"]
        session = manager.get_session()
        existing_id = str(uuid.uuid4())
        try:
            session.add(
                Task(
                    id=existing_id,
                    raw_description="Implement the input models module",
                    done_definition="done",
                    status="pending",
                    workflow_id=task_env["workflow_id"],
                    phase_id=task_env["phase_b_id"],
                )
            )
            session.commit()
        finally:
            session.close()

        request = _make_request(
            task_description="Implement the input models module",
            workflow_id=task_env["workflow_id"],
            phase_id=task_env["phase_b_id"],
        )
        # Use the agent whose own phase IS phase_b so the own-phase guard
        # doesn't interfere with isolating the dedup behavior.
        session = manager.get_session()
        try:
            other_agent_id = f"agent-{uuid.uuid4().hex[:8]}"
            session.add(
                Agent(
                    id=other_agent_id,
                    system_prompt="test",
                    status="working",
                    cli_type="pi",
                )
            )
            session.commit()
        finally:
            session.close()

        result = await create_task(request, agent_id=other_agent_id)
        assert result.task_id == existing_id

    @pytest.mark.asyncio
    async def test_different_description_is_not_deduped(self, task_env):
        """The exact incident: 5 genuinely different task descriptions for
        the same phase used to collapse into 1 — a different description
        must not be silently matched to the unrelated existing task."""
        from fastapi import HTTPException

        from src.mcp.server.agent_task_routes import create_task

        manager = task_env["manager"]
        session = manager.get_session()
        existing_id = str(uuid.uuid4())
        try:
            session.add(
                Task(
                    id=existing_id,
                    raw_description="Implement the input models module",
                    done_definition="done",
                    status="pending",
                    workflow_id=task_env["workflow_id"],
                    phase_id=task_env["phase_b_id"],
                )
            )
            session.commit()

            other_agent_id = f"agent-{uuid.uuid4().hex[:8]}"
            session.add(
                Agent(
                    id=other_agent_id,
                    system_prompt="test",
                    status="working",
                    cli_type="pi",
                )
            )
            session.commit()
        finally:
            session.close()

        request = _make_request(
            task_description=(
                "Set up custom exception hierarchy and configuration "
                "management with YAML/JSON support"
            ),
            workflow_id=task_env["workflow_id"],
            phase_id=task_env["phase_b_id"],
        )
        try:
            result = await create_task(request, agent_id=other_agent_id)
        except HTTPException:
            # Fine if it fails deeper (no LLM mock) — the point is it must
            # NOT silently return the unrelated existing task's id.
            return
        assert result.task_id != existing_id
