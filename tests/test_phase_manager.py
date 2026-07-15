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


# ── mark_phase_complete force_action (arbitration resolution) ─────


@pytest.fixture
def real_db(tmp_path):
    """Real sqlite DB -- force_action="goto" queries Phase/PhaseExecution
    via real joins that a Mock session can't meaningfully stand in for."""
    from src.core.database import DatabaseManager as _DBM

    db = _DBM(str(tmp_path / "test.db"))
    db.create_tables()
    return db


@pytest.fixture
def seeded_workflow(real_db):
    """Two phases: 'development' (order 4) is where arbitration fires;
    'qa_validation' (order 8) is a valid goto target ahead of it, and
    'architecture_design' (order 3) is a valid target BEHIND it."""
    from src.core.database import Phase, PhaseExecution, Workflow

    with real_db.session_scope() as session:
        session.add(
            Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Phase(
                id="phase-arch", workflow_id="wf-1", order=3,
                name="architecture_design", description="d", done_definitions=["x"],
            )
        )
        session.add(
            Phase(
                id="phase-dev", workflow_id="wf-1", order=4,
                name="development", description="d", done_definitions=["x"],
            )
        )
        session.add(
            Phase(
                id="phase-qa", workflow_id="wf-1", order=8,
                name="qa_validation", description="d", done_definitions=["x"],
            )
        )
        session.add(
            PhaseExecution(
                id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
                status="pending",
            )
        )
        session.add(
            PhaseExecution(
                id="exec-qa", phase_id="phase-qa", workflow_execution_id="wf-1",
                status="in_progress",
            )
        )
    return real_db


class TestMarkPhaseCompleteForceGoto:
    """force_action="goto" is how an arbitration decision resolves back
    into a normal task -- see orchestrator._resolve_arbitration_outcome.
    Must mirror _handle_evaluation_goto's effects exactly, just driven by
    an explicit target/reason instead of an orchestrator Evaluation."""

    def test_goto_target_found(self, seeded_workflow):
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"

        result = pm.mark_phase_complete(
            "phase-dev",
            "Arbiter: proceed",
            force_action="goto",
            force_target_phase="qa_validation",
            force_reason="fix the stale test",
        )

        assert result["action"] == "goto"
        assert result["target_phase"] == "qa_validation"
        assert result["target_phase_id"] == "phase-qa"
        assert result["should_continue"] is True
        assert result["reason"] == "fix the stale test"

        with seeded_workflow.session_scope() as session:
            from src.core.database import PhaseExecution

            dev_exec = (
                session.query(PhaseExecution).filter_by(phase_id="phase-dev").first()
            )
            assert dev_exec.status == "completed"

    def test_goto_target_not_found_falls_back(self, seeded_workflow):
        """A bogus/misspelled target from a malformed arbitration_result.json
        must not crash -- falls back to _advance_or_complete like
        _handle_evaluation_goto's own else-branch."""
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"

        result = pm.mark_phase_complete(
            "phase-dev",
            "Arbiter: proceed",
            force_action="goto",
            force_target_phase="not_a_real_phase",
            force_reason="x",
        )

        assert result["action"] != "goto"

    def test_goto_resets_stale_intermediate_executions(self, seeded_workflow):
        """Same stale-reset _handle_evaluation_goto does: phases between the
        goto target and the current phase left "in_progress" from a prior
        pass must be closed out, or they block later evaluation forever."""
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        with seeded_workflow.session_scope() as session:
            arch_exec = PhaseExecution(
                id="exec-arch", phase_id="phase-arch", workflow_execution_id="wf-1",
                status="in_progress",
            )
            session.add(arch_exec)

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"
        pm.mark_phase_complete(
            "phase-dev",
            "Arbiter: proceed",
            force_action="goto",
            force_target_phase="architecture_design",
            force_reason="restart from architecture",
        )

        with seeded_workflow.session_scope() as session:
            arch_exec = (
                session.query(PhaseExecution).filter_by(phase_id="phase-arch").first()
            )
            # phase-arch IS the target (order 3, same as target), so the
            # ">= target.order, < current.order" stale-reset window is
            # empty here -- this just confirms mark_phase_complete didn't
            # crash touching it, not that it got reset (there's no phase
            # strictly between them in this fixture).
            assert arch_exec is not None

    def test_goto_reason_and_metadata_returned(self, seeded_workflow):
        """The reason must thread through unchanged -- this is what
        _create_phase_task embeds as "WHY YOU'RE HERE" in the next task's
        description (see orchestrator._fire_phase_transition's feedback
        extraction, reused for the arbitration-driven goto path too)."""
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"

        result = pm.mark_phase_complete(
            "phase-dev",
            "s",
            force_action="goto",
            force_target_phase="qa_validation",
            force_reason="fix test_anthropic_provider.py::test_x specifically",
        )

        assert result["reason"] == "fix test_anthropic_provider.py::test_x specifically"
        assert result["metadata"] == {}


@pytest.fixture
def two_architect_phases(real_db):
    """architecture_design (order 3) and architectural_review (order 5)
    both map to the "architect" session role in workflow.yaml. No
    PhaseExecution rows are seeded here -- individual tests add them to
    control completion status."""
    from src.core.database import Phase, Workflow

    with real_db.session_scope() as session:
        session.add(
            Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Phase(
                id="phase-arch-design", workflow_id="wf-1", order=3,
                name="architecture_design", description="d", done_definitions=["x"],
            )
        )
        session.add(
            Phase(
                id="phase-arch-review", workflow_id="wf-1", order=5,
                name="architectural_review", description="d", done_definitions=["x"],
            )
        )
    return real_db


class TestPhaseRolePreviouslyCompleted:
    """Regression: the resumed-session warning shown to agents used to be
    driven by a static check ("does this role appear more than once in the
    pipeline config?"), which is true for BOTH architecture_design and
    architectural_review -- so the very first phase to use a shared role
    was wrongly told its session was "previously used" and "already
    complete". The real question is whether an earlier-ordered phase
    sharing the role has actually completed in THIS workflow.
    """

    def test_false_for_first_occurrence_with_no_earlier_phase_run(
        self, two_architect_phases
    ):
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=two_architect_phases)
        pm.workflow_id = "wf-1"

        assert pm.phase_role_previously_completed(
            "phase-arch-design", "architect"
        ) is False

    def test_false_when_earlier_same_role_phase_exists_but_not_completed(
        self, two_architect_phases
    ):
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        with two_architect_phases.session_scope() as session:
            session.add(
                PhaseExecution(
                    id="exec-arch-design", phase_id="phase-arch-design",
                    workflow_execution_id="wf-1", status="in_progress",
                )
            )

        pm = PhaseManager(db_manager=two_architect_phases)
        pm.workflow_id = "wf-1"

        assert pm.phase_role_previously_completed(
            "phase-arch-review", "architect"
        ) is False

    def test_true_when_earlier_same_role_phase_completed(self, two_architect_phases):
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        with two_architect_phases.session_scope() as session:
            session.add(
                PhaseExecution(
                    id="exec-arch-design", phase_id="phase-arch-design",
                    workflow_execution_id="wf-1", status="completed",
                )
            )

        pm = PhaseManager(db_manager=two_architect_phases)
        pm.workflow_id = "wf-1"

        assert pm.phase_role_previously_completed(
            "phase-arch-review", "architect"
        ) is True

    def test_false_for_a_completed_phase_that_is_not_earlier(
        self, two_architect_phases
    ):
        """A LATER phase with the same role completing must not make an
        EARLIER phase's invocation claim reuse -- only order < current
        counts."""
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        with two_architect_phases.session_scope() as session:
            session.add(
                PhaseExecution(
                    id="exec-arch-review", phase_id="phase-arch-review",
                    workflow_execution_id="wf-1", status="completed",
                )
            )

        pm = PhaseManager(db_manager=two_architect_phases)
        pm.workflow_id = "wf-1"

        assert pm.phase_role_previously_completed(
            "phase-arch-design", "architect"
        ) is False
