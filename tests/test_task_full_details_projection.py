"""Regression tests for get_task_full_details's parameterized task
projection (SOLID review 1.10) -- child_tasks/parent_task/duplicated_tasks/
related_tasks_details all route through _task_summary_dict now instead of
four hand-rolled dict literals (parent_task's two branches previously
duplicated the same literal twice)."""

from unittest.mock import Mock

import pytest

from src.core.database import Agent, DatabaseManager, Task
from src.mcp.frontend.task_service import TaskService, _task_summary_dict


@pytest.fixture
def db_manager(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


@pytest.fixture
def task_service(db_manager):
    return TaskService(db_manager=db_manager, agent_manager=Mock())


def _add_task(session, **kwargs):
    defaults = {
        "raw_description": "do the thing",
        "done_definition": "it is done",
        "status": "pending",
    }
    defaults.update(kwargs)
    task = Task(**defaults)
    session.add(task)
    return task


class TestTaskSummaryDict:
    def test_selects_only_requested_fields(self):
        task = Task(
            id="t1",
            raw_description="raw",
            enriched_description="enriched text here",
            done_definition="done",
            status="in_progress",
            priority="high",
        )
        result = _task_summary_dict(task, ("id", "status"))
        assert result == {"id": "t1", "status": "in_progress"}

    def test_prefers_enriched_description_and_truncates_to_100(self):
        task = Task(
            id="t1",
            raw_description="raw",
            enriched_description="x" * 150,
            done_definition="done",
        )
        result = _task_summary_dict(task, ("description",))
        assert result == {"description": "x" * 100}

    def test_falls_back_to_raw_description_when_no_enrichment(self):
        task = Task(id="t1", raw_description="raw text", done_definition="done")
        result = _task_summary_dict(task, ("description",))
        assert result == {"description": "raw text"}

    def test_override_replaces_the_row_own_column(self):
        """related_tasks_details needs a computed cosine similarity, not
        the row's own similarity_score column -- **overrides must win."""
        task = Task(
            id="t1", raw_description="raw", done_definition="done", similarity_score=0.1
        )
        result = _task_summary_dict(task, ("similarity_score",), similarity_score=0.87)
        assert result == {"similarity_score": 0.87}


class TestGetTaskFullDetailsProjectionShapes:
    @pytest.mark.asyncio
    async def test_child_tasks_include_priority(self, task_service, db_manager):
        session = db_manager.get_session()
        try:
            agent = Agent(id="agent-1", system_prompt="sys", cli_type="claude")
            session.add(agent)
            parent = _add_task(
                session, id="parent", assigned_agent_id="agent-1"
            )
            _add_task(
                session,
                id="child",
                created_by_agent_id="agent-1",
                priority="low",
                status="done",
            )
            session.commit()
        finally:
            session.close()

        result = await task_service.get_task_full_details("parent")

        assert result["child_tasks"] == [
            {
                "id": "child",
                "description": "do the thing",
                "status": "done",
                "priority": "low",
                "created_at": result["child_tasks"][0]["created_at"],
            }
        ]

    @pytest.mark.asyncio
    async def test_parent_task_via_explicit_parent_id(self, task_service, db_manager):
        session = db_manager.get_session()
        try:
            _add_task(session, id="parent", status="done")
            _add_task(session, id="child", parent_task_id="parent")
            session.commit()
        finally:
            session.close()

        result = await task_service.get_task_full_details("child")

        assert result["parent_task"]["id"] == "parent"
        assert result["parent_task"]["status"] == "done"
        assert set(result["parent_task"].keys()) == {
            "id", "description", "status", "created_at",
        }

    @pytest.mark.asyncio
    async def test_parent_task_via_inferred_creator_agent(self, task_service, db_manager):
        """No explicit parent_task_id -- parent is inferred as the task
        assigned to the agent that created this task. Must produce the same
        shape as the explicit-parent_task_id branch above."""
        session = db_manager.get_session()
        try:
            agent = Agent(id="agent-1", system_prompt="sys", cli_type="claude")
            session.add(agent)
            _add_task(session, id="parent", assigned_agent_id="agent-1", status="done")
            _add_task(session, id="child", created_by_agent_id="agent-1")
            session.commit()
        finally:
            session.close()

        result = await task_service.get_task_full_details("child")

        assert result["parent_task"]["id"] == "parent"
        assert result["parent_task"]["status"] == "done"
        assert set(result["parent_task"].keys()) == {
            "id", "description", "status", "created_at",
        }

    @pytest.mark.asyncio
    async def test_duplicated_tasks_include_created_by_agent_id_not_priority(
        self, task_service, db_manager
    ):
        session = db_manager.get_session()
        try:
            _add_task(session, id="original")
            _add_task(
                session,
                id="dup1",
                status="duplicated",
                duplicate_of_task_id="original",
                created_by_agent_id="agent-9",
                similarity_score=0.95,
            )
            session.commit()
        finally:
            session.close()

        result = await task_service.get_task_full_details("original")

        assert result["duplicated_tasks"] == [
            {
                "id": "dup1",
                "description": "do the thing",
                "similarity_score": 0.95,
                "created_at": result["duplicated_tasks"][0]["created_at"],
                "created_by_agent_id": "agent-9",
            }
        ]

    @pytest.mark.asyncio
    async def test_related_tasks_details_uses_computed_similarity_override(
        self, task_service, db_manager
    ):
        """related_task.similarity_score itself is unset (None) -- the
        dict's similarity_score must come from related_task_ids' stored
        score via the override, not the row's own column."""
        session = db_manager.get_session()
        try:
            _add_task(session, id="related-1", status="in_progress")
            _add_task(
                session,
                id="main",
                related_task_ids=[{"id": "related-1", "similarity": 0.42}],
            )
            session.commit()
        finally:
            session.close()

        result = await task_service.get_task_full_details("main")

        assert result["related_tasks_details"] == [
            {
                "id": "related-1",
                "description": "do the thing",
                "status": "in_progress",
                "similarity_score": 0.42,
                "created_at": result["related_tasks_details"][0]["created_at"],
            }
        ]
