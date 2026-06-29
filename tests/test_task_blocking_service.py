"""Tests for task_blocking_service.py — blocking logic."""

from unittest.mock import Mock, patch


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
