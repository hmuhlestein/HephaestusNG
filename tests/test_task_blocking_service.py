"""Tests for task_blocking_service.py — blocking logic."""

from unittest.mock import Mock, patch

import pytest


class TestCheckTaskBlocked:
    def test_task_not_found(self):
        from src.services.task_blocking_service import TaskBlockingService

        with patch("src.services.task_blocking_service.get_db") as mock_db:
            mock_session = Mock()
            mock_session.query.return_value.filter_by.return_value.first.return_value = None
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = TaskBlockingService.check_task_blocked("nonexistent")
            assert result["is_blocked"] is False
            assert result.get("error") == "Task not found"

    def test_task_no_ticket(self):
        from src.services.task_blocking_service import TaskBlockingService

        with patch("src.services.task_blocking_service.get_db") as mock_db:
            mock_session = Mock()
            mock_task = Mock(ticket_id=None)
            mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = TaskBlockingService.check_task_blocked("task1")
            assert result["is_blocked"] is False

    def test_task_not_blocked(self):
        from src.services.task_blocking_service import TaskBlockingService

        with patch("src.services.task_blocking_service.get_db") as mock_db:
            mock_session = Mock()
            mock_task = Mock(ticket_id="ticket-1")
            mock_ticket = Mock(blocked_by_ticket_ids=[])
            # First query returns task, second returns ticket
            mock_session.query.return_value.filter_by.return_value.first.side_effect = [
                mock_task,
                mock_ticket,
            ]
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = TaskBlockingService.check_task_blocked("task1")
            assert result["is_blocked"] is False

    def test_task_blocked(self):
        from src.services.task_blocking_service import TaskBlockingService

        with patch("src.services.task_blocking_service.get_db") as mock_db:
            mock_session = Mock()
            mock_task = Mock(ticket_id="ticket-1")
            mock_ticket = Mock(blocked_by_ticket_ids=["ticket-2"])
            mock_blocker = Mock(
                id="ticket-2",
                title="Blocker",
                status="open",
                priority="high",
                is_resolved=False,
            )
            # First query: task, second: ticket, third: blockers
            mock_session.query.return_value.filter_by.return_value.first.side_effect = [
                mock_task,
                mock_ticket,
            ]
            mock_session.query.return_value.filter.return_value.all.return_value = [
                mock_blocker
            ]
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = TaskBlockingService.check_task_blocked("task1")
            assert result["is_blocked"] is True
            assert "ticket-2" in result["blocking_ticket_ids"]


class TestBlockTask:
    def test_blocks_task(self):
        from src.services.task_blocking_service import TaskBlockingService

        with patch("src.services.task_blocking_service.get_db") as mock_db:
            mock_session = Mock()
            mock_task = Mock(status="pending")
            mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = TaskBlockingService.block_task("task1", "blocking")
            assert result["success"] is True
            assert result["new_status"] == "blocked"


class TestUnblockTask:
    def test_unblocks_task(self):
        from src.services.task_blocking_service import TaskBlockingService

        with patch("src.services.task_blocking_service.get_db") as mock_db:
            mock_session = Mock()
            mock_task = Mock(status="blocked")
            mock_session.query.return_value.filter_by.return_value.first.return_value = mock_task
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = TaskBlockingService.unblock_task("task1")
            assert result["success"] is True
            assert result["new_status"] == "queued"


class TestGetAllBlockedTasks:
    def test_returns_empty_list(self):
        from src.services.task_blocking_service import TaskBlockingService

        with patch("src.services.task_blocking_service.get_db") as mock_db:
            mock_session = Mock()
            mock_session.query.return_value.filter_by.return_value.all.return_value = []
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = TaskBlockingService.get_all_blocked_tasks()
            assert isinstance(result, list)
            assert len(result) == 0


class TestSyncTaskBlockingStatusSessionLifecycle:
    """Regression: sync_task_blocking_status held its own get_db() session
    open for the ENTIRE per-task loop, while check_task_blocked/block_task/
    unblock_task each opened a SECOND, independent get_db() session per
    task -- needlessly serializing N+1 SQLite connections against each
    other for the whole sync. The outer session is only ever used to fetch
    the task list; it must be released before the loop starts."""

    @pytest.fixture
    def blocked_task(self, db_manager):
        from src.core.database import Agent, Task, Ticket, Workflow

        session = db_manager.get_session()
        try:
            session.add(Workflow(
                id="wf-1", name="Test Workflow",
                phases_folder_path="/test/phases", status="active",
            ))
            session.add(Agent(
                id="agent-1", system_prompt="x", status="working", cli_type="claude",
            ))
            session.add(Ticket(
                id="ticket-blocker", workflow_id="wf-1", created_by_agent_id="agent-1",
                title="Blocker", description="x", ticket_type="task",
                priority="medium", status="backlog",
            ))
            session.add(Ticket(
                id="ticket-1", workflow_id="wf-1", created_by_agent_id="agent-1",
                title="Blocked ticket", description="x", ticket_type="task",
                priority="medium", status="backlog",
                blocked_by_ticket_ids=["ticket-blocker"],
            ))
            session.add(Task(
                id="task-1", raw_description="x", enriched_description="x",
                done_definition="x", status="pending", priority="medium",
                ticket_id="ticket-1",
            ))
            session.commit()
        finally:
            session.close()
        return "task-1"

    def test_outer_session_closed_before_per_task_loop(self, db_manager, blocked_task):
        import contextlib

        from src.core.database import get_db as real_get_db
        from src.services.task_blocking_service import TaskBlockingService

        open_count = 0
        max_open = 0

        @contextlib.contextmanager
        def tracking_get_db(*args, **kwargs):
            nonlocal open_count, max_open
            with real_get_db(*args, **kwargs) as db:
                open_count += 1
                max_open = max(max_open, open_count)
                try:
                    yield db
                finally:
                    open_count -= 1

        with patch(
            "src.services.task_blocking_service.get_db", side_effect=tracking_get_db
        ):
            result = TaskBlockingService.sync_task_blocking_status()

        assert result["success"] is True
        assert result["tasks_blocked"] == 1
        assert max_open == 1
