"""Tests for phases/phase_manager.py — pure utilities + key methods."""

from unittest.mock import Mock

import pytest

from src.core.database import (
    DatabaseManager,
)

# ── Pure utility functions ────────────────────────────────────────


class TestSubstituteParams:
    def test_basic_replacement(self):
        from src.phases.phase_manager import substitute_params

        result = substitute_params("Hello {name}", {"name": "World"})
        assert result == "Hello World"

    def test_multiple_params(self):
        from src.phases.phase_manager import substitute_params

        result = substitute_params("{a} and {b}", {"a": "X", "b": "Y"})
        assert result == "X and Y"

    def test_no_params(self):
        from src.phases.phase_manager import substitute_params

        result = substitute_params("No placeholders", {"a": "X"})
        assert result == "No placeholders"

    def test_empty_text(self):
        from src.phases.phase_manager import substitute_params

        assert substitute_params("", {"a": "X"}) == ""

    def test_none_text(self):
        from src.phases.phase_manager import substitute_params

        assert substitute_params(None, {"a": "X"}) is None

    def test_empty_params(self):
        from src.phases.phase_manager import substitute_params

        assert substitute_params("Hello {name}", {}) == "Hello {name}"

    def test_none_params(self):
        from src.phases.phase_manager import substitute_params

        assert substitute_params("Hello {name}", None) == "Hello {name}"

    def test_none_value(self):
        from src.phases.phase_manager import substitute_params

        result = substitute_params("Hello {name}", {"name": None})
        assert result == "Hello "

    def test_numeric_value(self):
        from src.phases.phase_manager import substitute_params

        result = substitute_params("Count: {n}", {"n": 42})
        assert result == "Count: 42"


class TestSubstituteParamsInList:
    def test_basic(self):
        from src.phases.phase_manager import substitute_params_in_list

        result = substitute_params_in_list(["{a}", "{b}"], {"a": "X", "b": "Y"})
        assert result == ["X", "Y"]

    def test_empty_list(self):
        from src.phases.phase_manager import substitute_params_in_list

        assert substitute_params_in_list([], {"a": "X"}) == []

    def test_none_list(self):
        from src.phases.phase_manager import substitute_params_in_list

        assert substitute_params_in_list(None, {"a": "X"}) is None

    def test_no_params(self):
        from src.phases.phase_manager import substitute_params_in_list

        assert substitute_params_in_list(["{a}"], {}) == ["{a}"]

    def test_mixed(self):
        from src.phases.phase_manager import substitute_params_in_list

        result = substitute_params_in_list(["No placeholder", "{x}"], {"x": "val"})
        assert result == ["No placeholder", "val"]


# ── PhaseManager methods ──────────────────────────────────────────


@pytest.fixture
def mock_db():
    return Mock(spec=DatabaseManager)


@pytest.fixture
def phase_manager(mock_db):
    from src.phases.phase_manager import PhaseManager

    return PhaseManager(db_manager=mock_db)


class TestPhaseManagerInit:
    def test_init(self, phase_manager, mock_db):
        assert phase_manager.db_manager is mock_db
        assert phase_manager.workflow_id is None
        assert phase_manager.active_executions == {}


class TestGetWorkflowStatus:
    def test_no_workflow(self, phase_manager):
        result = phase_manager.get_workflow_status()
        assert result == {"error": "No active workflow"}

    def test_workflow_not_found(self, phase_manager, mock_db):
        phase_manager.workflow_id = "wf-1"
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        result = phase_manager.get_workflow_status()
        assert result == {"error": "Workflow not found"}

    def test_with_workflow(self, phase_manager, mock_db):
        phase_manager.workflow_id = "wf-1"
        session = Mock()
        wf = Mock(status="active", name="Test WF")
        session.query.return_value.filter_by.return_value.first.return_value = wf
        session.query.return_value.filter_by.return_value.order_by.return_value.all.return_value = []
        mock_db.get_session.return_value = session

        result = phase_manager.get_workflow_status()
        assert result["workflow_status"] == "active"
        assert result["phases"] == []


class TestRegisterDefinition:
    def test_register(self, phase_manager, mock_db):
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        phase_manager.register_definition(
            definition_id="def-1",
            name="Test Def",
            description="A test",
            phases_config=[{"order": 1, "name": "P1"}],
        )
        session.add.assert_called()
        session.commit.assert_called()

    def test_register_with_config(self, phase_manager, mock_db):
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        phase_manager.register_definition(
            definition_id="def-2",
            name="With Config",
            workflow_config={"has_result": True, "result_criteria": "works"},
        )
        session.add.assert_called()


class TestListDefinitions:
    def test_empty(self, phase_manager, mock_db):
        session = Mock()
        session.query.return_value.all.return_value = []
        mock_db.get_session.return_value = session

        result = phase_manager.list_definitions()
        assert result == []


class TestListActiveExecutions:
    def test_empty(self, phase_manager, mock_db):
        session = Mock()
        session.query.return_value.options.return_value.order_by.return_value.all.return_value = []
        mock_db.get_session.return_value = session

        result = phase_manager.list_active_executions()
        assert result == []

    def test_with_workflows(self, phase_manager, mock_db):
        session = Mock()
        wf1 = Mock(id="wf-1")
        session.query.return_value.options.return_value.order_by.return_value.all.return_value = [
            wf1
        ]
        mock_db.get_session.return_value = session

        result = phase_manager.list_active_executions()
        assert len(result) == 1


class TestGetWorkflow:
    def test_not_found(self, phase_manager, mock_db):
        session = Mock()
        session.query.return_value.options.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        result = phase_manager.get_workflow("nonexistent")
        assert result is None


class TestGetPhaseForTask:
    def test_by_phase_id(self, phase_manager, mock_db):
        # If phase_id is provided, it's returned directly
        result = phase_manager.get_phase_for_task(phase_id="p1")
        assert result == "p1"

    def test_by_order(self, phase_manager, mock_db):
        phase_manager.workflow_id = "wf-1"
        session = Mock()
        phase = Mock(id="p2")
        session.query.return_value.filter_by.return_value.first.return_value = phase
        mock_db.get_session.return_value = session

        result = phase_manager.get_phase_for_task(order=2)
        assert result == "p2"

    def test_by_order_not_found(self, phase_manager, mock_db):
        phase_manager.workflow_id = "wf-1"
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        result = phase_manager.get_phase_for_task(order=99)
        assert result is None

    def test_defaults_to_current(self, phase_manager, mock_db):
        result = phase_manager.get_phase_for_task()
        # No workflow_id set → returns None
        assert result is None


class TestGetCurrentPhaseId:
    def test_no_workflow(self, phase_manager):
        result = phase_manager.get_current_phase_id()
        assert result is None

    def test_with_workflow(self, phase_manager, mock_db):
        phase_manager.workflow_id = "wf-1"
        session = Mock()
        execution = Mock(phase_id="p1")
        # Mock the complex query chain: query().join().filter().order_by().first()
        session.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value = execution
        mock_db.get_session.return_value = session

        result = phase_manager.get_current_phase_id()
        assert result == "p1"

    def test_no_active_phase(self, phase_manager, mock_db):
        phase_manager.workflow_id = "wf-1"
        session = Mock()
        session.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        result = phase_manager.get_current_phase_id()
        assert result is None


class TestCheckPhaseCompletion:
    def test_completed(self, phase_manager, mock_db):
        session = Mock()
        # Mock phase query
        phase = Mock(id="p1")
        session.query.return_value.filter_by.return_value.first.return_value = phase
        # Mock Task queries - first returns 0 incomplete, second returns 2 completed
        task_query = Mock()
        task_query.filter_by.return_value.filter.return_value.count.return_value = (
            0  # incomplete = 0
        )
        task_query.filter_by.return_value.count.return_value = 2  # completed = 2
        # Return phase for Phase query, task_query for Task query
        session.query.side_effect = [
            Mock(filter_by=Mock(return_value=Mock(first=Mock(return_value=phase)))),
            task_query,
            task_query,
        ]
        mock_db.get_session.return_value = session

        result = phase_manager.check_phase_completion("p1")
        assert result is True

    def test_incomplete(self, phase_manager, mock_db):
        session = Mock()
        phase = Mock(id="p1")
        task_query = Mock()
        task_query.filter_by.return_value.filter.return_value.count.return_value = (
            1  # incomplete = 1
        )
        session.query.side_effect = [
            Mock(filter_by=Mock(return_value=Mock(first=Mock(return_value=phase)))),
            task_query,
        ]
        mock_db.get_session.return_value = session

        result = phase_manager.check_phase_completion("p1")
        assert result is False

    def test_no_tasks(self, phase_manager, mock_db):
        session = Mock()
        phase = Mock(id="p1")
        task_query = Mock()
        task_query.filter_by.return_value.filter.return_value.count.return_value = (
            0  # incomplete = 0
        )
        task_query.filter_by.return_value.count.return_value = 0  # completed = 0
        session.query.side_effect = [
            Mock(filter_by=Mock(return_value=Mock(first=Mock(return_value=phase)))),
            task_query,
            task_query,
        ]
        mock_db.get_session.return_value = session

        result = phase_manager.check_phase_completion("p1")
        assert result is False  # No completed tasks = not complete

    def test_phase_not_found(self, phase_manager, mock_db):
        session = Mock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        mock_db.get_session.return_value = session

        result = phase_manager.check_phase_completion("missing")
        assert result is False
