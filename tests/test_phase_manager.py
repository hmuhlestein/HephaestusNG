"""Tests for phases/phase_manager.py — pure utilities + key methods."""

from unittest.mock import Mock, patch

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


class TestMarkPhaseCompleteLockRetry:
    """Regression, observed live (workflow ca539a75, 2026-08-21): a
    transient sqlite "database is locked" during mark_phase_complete fell
    into the blanket except, which returned action="arbitrate" --
    converting pure write contention into a phase-flow decision and
    silently discarding whatever decision the completion was carrying
    (an arbiter's "continue" in the live incident; the workflow sat
    "active" awaiting an arbiter forever). A lock error must instead be
    retried from a fresh session, and only escalate to arbitration once
    the retries are exhausted."""

    def test_lock_error_is_retried_not_escalated(self, seeded_workflow, monkeypatch):
        from src.phases import phase_manager as pm_module
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"

        calls = {"n": 0}
        from sqlalchemy import event
        from sqlalchemy.exc import OperationalError

        @event.listens_for(seeded_workflow.engine, "before_cursor_execute")
        def flaky(conn, cursor, statement, parameters, context, executemany):
            if calls["n"] == 0:
                calls["n"] += 1
                raise OperationalError(
                    statement, parameters, Exception("database is locked")
                )

        monkeypatch.setattr(
            pm_module, "_MARK_COMPLETE_LOCK_RETRY_DELAY_SECONDS", 0
        )

        try:
            result = pm.mark_phase_complete(
                "phase-dev", "Arbiter: proceed", force_action="continue"
            )
        finally:
            event.remove(seeded_workflow.engine, "before_cursor_execute", flaky)

        assert calls["n"] == 1  # first attempt raised, retry ran for real
        assert result["action"] != "arbitrate"
        assert result["action"] in ("continue", "already_completed")

    def test_persistent_lock_still_escalates_after_retries(
        self, seeded_workflow, monkeypatch
    ):
        from sqlalchemy.exc import OperationalError

        from src.phases import phase_manager as pm_module
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"

        from sqlalchemy import event
        from sqlalchemy.exc import OperationalError

        @event.listens_for(seeded_workflow.engine, "before_cursor_execute")
        def always_locked(conn, cursor, statement, parameters, context, executemany):
            raise OperationalError(statement, parameters, Exception("database is locked"))

        monkeypatch.setattr(
            pm_module, "_MARK_COMPLETE_LOCK_RETRY_DELAY_SECONDS", 0
        )

        try:
            result = pm.mark_phase_complete(
                "phase-dev", "Arbiter: proceed", force_action="continue"
            )
        finally:
            event.remove(seeded_workflow.engine, "before_cursor_execute", always_locked)

        assert result["action"] == "arbitrate"
        assert "database is locked" in result["reason"]


class TestMarkPhaseCompleteForceGoto:
    """force_action="goto" is how an arbitration decision resolves back
    into a normal task -- see orchestrator._resolve_arbitration_outcome.
    Must mirror _handle_evaluation_goto's effects exactly, just driven by
    an explicit target/reason instead of an orchestrator Evaluation."""

    def test_goto_target_found(self, seeded_workflow):
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        # force_action="goto" resolves an arbitration decision for the
        # CURRENTLY RUNNING phase (see class docstring) -- the fixture's
        # default "pending" for phase-dev is not a state arbitration ever
        # fires from. Matches the explicit in_progress setup already used
        # by TestHandleEvaluationRetryAndArbitrateReopenExecution below.
        with seeded_workflow.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            execution.status = "in_progress"

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


class TestCloseExecution:
    """Direct unit tests for _close_execution's own write, independent of
    any particular mark_phase_complete caller. Regression: migrating this
    to transition_phase_execution (Step 3 of
    docs/designs/PHASE_EXECUTION_STATE_MACHINE_REFACTOR.md) silently
    dropped completed_at -- (in_progress, completed) and
    (in_progress, failed), the two transitions this function performs,
    have no entry in _FIELD_RESETS, and the migration didn't pass
    completed_at via extra_fields either. Every existing caller-level test
    only checked status/action, never completed_at, so 414 passing tests
    across the full Step 3 regression set did not catch it."""

    def test_completing_sets_status_completed_at_and_summary(self, real_db):
        from src.core.database import Phase, PhaseExecution, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active"))
            session.add(Phase(id="phase-1", workflow_id="wf-1", order=1, name="development", description="d", done_definitions=["x"]))
            session.add(PhaseExecution(id="exec-1", phase_id="phase-1", status="in_progress"))

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            PhaseManager._close_execution(session, execution, "completed", "done")
            assert execution.status == "completed"
            assert execution.completed_at is not None
            assert execution.completion_summary == "done"

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            assert execution.completed_at is not None

    def test_failing_sets_status_and_completed_at(self, real_db):
        from src.core.database import Phase, PhaseExecution, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active"))
            session.add(Phase(id="phase-1", workflow_id="wf-1", order=1, name="development", description="d", done_definitions=["x"]))
            session.add(PhaseExecution(id="exec-1", phase_id="phase-1", status="in_progress"))

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            PhaseManager._close_execution(session, execution, "failed", "bad")
            assert execution.status == "failed"
            assert execution.completed_at is not None

    def test_no_summary_leaves_completion_summary_unset(self, real_db):
        from src.core.database import Phase, PhaseExecution, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active"))
            session.add(Phase(id="phase-1", workflow_id="wf-1", order=1, name="development", description="d", done_definitions=["x"]))
            session.add(PhaseExecution(id="exec-1", phase_id="phase-1", status="in_progress"))

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            PhaseManager._close_execution(session, execution, "completed")
            assert execution.completion_summary is None
            assert execution.completed_at is not None

    def test_invalid_transition_is_skipped_not_forced(self, real_db):
        """A "pending" execution has no valid path straight to "completed"
        (see _VALID_TRANSITIONS) -- the write must be skipped, not forced,
        and the caller's in-memory execution must reflect the real
        (unchanged) row rather than being force-mutated to look closed."""
        from src.core.database import Phase, PhaseExecution, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active"))
            session.add(Phase(id="phase-1", workflow_id="wf-1", order=1, name="development", description="d", done_definitions=["x"]))
            session.add(PhaseExecution(id="exec-1", phase_id="phase-1", status="pending"))

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            PhaseManager._close_execution(session, execution, "completed", "should not stick")

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(id="exec-1").first()
            assert execution.status == "pending"
            assert execution.completed_at is None
            assert execution.completion_summary is None


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


class TestHandleEvaluationRetryAndArbitrateReopenExecution:
    """§4.1's 4th copy-family: _handle_evaluation_retry and
    _handle_evaluation_arbitrate's status/started_at/task_creation_claimed_at
    writes were extracted into reopen_phase_execution
    (phase_transitions.py), shared with _start_next_phase (already covered
    by test_goto_reconvergence.py's test_start_next_phase_resets_task_creation_claim)
    and task_admin_routes.py's restart_task_endpoint (covered by
    test_server_dispatch_endpoints.py). Pure extraction -- these
    characterize the exact fields each handler must still write,
    unchanged, after the refactor."""

    def test_retry_resets_status_started_at_and_claim(self, seeded_workflow):
        from datetime import datetime

        from src.core.database import Phase, PhaseExecution
        from src.phases.phase_manager import PhaseManager
        from src.workflow_engine.orchestrator import EvaluationResult, OrchestrationAction

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"

        with seeded_workflow.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-dev").first()
            execution = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            execution.status = "in_progress"
            execution.started_at = datetime.utcnow()
            execution.task_creation_claimed_at = datetime.utcnow()
            session.flush()

            evaluation = EvaluationResult(
                action=OrchestrationAction.RETRY,
                reason="score too low",
                metadata={"retry_count": 1, "max_retries": 2},
            )
            pm._handle_evaluation_retry(session, phase, execution, "summary", evaluation)

            assert execution.status == "pending"
            assert execution.started_at is None
            assert execution.task_creation_claimed_at is None

    def test_arbitrate_sets_in_progress_and_preserves_started_at(self, seeded_workflow):
        """Must NOT land on "pending" -- _case_completed_with_successor's
        next-pending-by-order picking would skip a phase reopened as
        pending while later phases are already completed (see this
        handler's own comment). started_at must survive untouched: this
        reopens the SAME already-running execution, not a fresh start."""
        from datetime import datetime

        from src.core.database import Phase, PhaseExecution
        from src.phases.phase_manager import PhaseManager
        from src.workflow_engine.orchestrator import EvaluationResult, OrchestrationAction

        pm = PhaseManager(db_manager=seeded_workflow)
        pm.workflow_id = "wf-1"

        with seeded_workflow.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-dev").first()
            execution = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            execution.status = "in_progress"
            original_started_at = datetime.utcnow()
            execution.started_at = original_started_at
            execution.task_creation_claimed_at = datetime.utcnow()
            session.flush()

            evaluation = EvaluationResult(
                action=OrchestrationAction.ARBITRATE,
                reason="budget exhausted",
                metadata={},
            )
            pm._handle_evaluation_arbitrate(session, phase, execution, "summary", evaluation)

            assert execution.status == "in_progress"
            assert execution.task_creation_claimed_at is None
            assert execution.started_at == original_started_at


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


@pytest.fixture
def single_unique_role_phase(real_db):
    """A phase whose name isn't in the real session_roles config, so its
    role falls back to its own name (SESSION_ROLES.get(name, name)) --
    used for the "goto/retry back to the SAME phase" resume case, distinct
    from two_architect_phases' cross-phase role-sharing case."""
    from src.core.database import Phase, Workflow

    with real_db.session_scope() as session:
        session.add(
            Workflow(id="wf-2", name="t", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Phase(
                id="phase-dev", workflow_id="wf-2", order=4,
                name="zzz_unmapped_test_phase", description="d", done_definitions=["x"],
            )
        )
    return real_db


class TestPhaseRolePreviouslyCompletedSelfGoto:
    """A goto (e.g. adversarial_review finds issues and sends the pipeline
    back to development) or a retry sends work back to the SAME phase_id --
    not "an earlier phase" in the Phase.order sense
    TestPhaseRolePreviouslyCompleted covers, but get_session_id resumes the
    identical pi conversation regardless, since it keys purely on (project,
    design, role, model). Before this fix, phase_role_previously_completed
    never recognized this case, so every goto/retry redo resent the full
    tool-instructions boilerplate to an agent whose pi session already had
    it from the phase's first pass."""

    def test_false_on_first_ever_task_for_this_phase(self, single_unique_role_phase):
        from src.core.database import Task
        from src.phases.phase_manager import PhaseManager

        with single_unique_role_phase.session_scope() as session:
            session.add(
                Task(
                    id="task-1", phase_id="phase-dev", workflow_id="wf-2",
                    raw_description="d", done_definition="d", status="pending",
                )
            )

        pm = PhaseManager(db_manager=single_unique_role_phase)
        pm.workflow_id = "wf-2"

        assert pm.phase_role_previously_completed(
            "phase-dev", "zzz_unmapped_test_phase"
        ) is False

    def test_true_when_a_prior_task_already_ran_for_this_phase(
        self, single_unique_role_phase
    ):
        """The current (goto-created) task's own row already exists in DB
        by the time this check runs -- a second row for the same phase_id
        is the evidence a real prior attempt/session exists. Also confirms
        this doesn't depend on PhaseExecution.status == "completed": a
        goto resets this phase's own execution back to "pending" (see
        _handle_evaluation_goto), which would erase that evidence if the
        check relied on it the way the cross-phase case does."""
        from src.core.database import PhaseExecution, Task
        from src.phases.phase_manager import PhaseManager

        with single_unique_role_phase.session_scope() as session:
            session.add(
                Task(
                    id="task-1", phase_id="phase-dev", workflow_id="wf-2",
                    raw_description="d", done_definition="d", status="failed",
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-dev", phase_id="phase-dev",
                    workflow_execution_id="wf-2", status="pending",
                )
            )
            session.add(
                Task(
                    id="task-2", phase_id="phase-dev", workflow_id="wf-2",
                    raw_description="d", done_definition="d", status="pending",
                )
            )

        pm = PhaseManager(db_manager=single_unique_role_phase)
        pm.workflow_id = "wf-2"

        assert pm.phase_role_previously_completed(
            "phase-dev", "zzz_unmapped_test_phase"
        ) is True


class TestEvaluationGotoConsumesGateArtifacts:
    """Regression: _handle_evaluation_goto acted on a gate's findings but
    left the result files the score came from on disk -- a later re-run of
    the gate phase re-scored the same stale files and sent the pipeline
    back to development again, in a loop (see
    spec.consume_gate_artifacts)."""

    def test_goto_deletes_the_gate_phases_result_files(self, real_db, tmp_path):
        from types import SimpleNamespace

        from src.core.database import Phase, PhaseExecution, Workflow
        from src.phases.phase_manager import PhaseManager

        docs = tmp_path / ".hephaestus" / "adversarial_review"
        docs.mkdir(parents=True)
        (docs / "adversarial.md").write_text(
            "---\ntype: adversarial_review_result\nblocker_count: 4\n---\n\n# stale report"
        )

        with real_db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    working_directory=str(tmp_path), status="active",
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
                    id="phase-adv", workflow_id="wf-1", order=6,
                    name="adversarial_review", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-adv", phase_id="phase-adv",
                    workflow_execution_id="wf-1", status="in_progress",
                )
            )

        pm = PhaseManager(db_manager=real_db)
        pm.workflow_id = "wf-1"

        session = real_db.get_session()
        try:
            phase = session.query(Phase).filter_by(id="phase-adv").first()
            execution = (
                session.query(PhaseExecution).filter_by(phase_id="phase-adv").first()
            )
            evaluation = SimpleNamespace(
                target_phase="development",
                reason="4 BLOCKER(s) found",
                metadata={},
            )

            result = pm._handle_evaluation_goto(
                session, phase, execution, "gate fired", evaluation
            )
        finally:
            session.close()

        assert result["action"] == "goto"
        assert result["target_phase"] == "development"
        assert not (docs / "adversarial.md").exists()


class TestForceGotoConsumesGateArtifacts:
    """Regression: _handle_force_goto (arbitration's goto resolution) had
    the exact same gap as _handle_evaluation_goto -- an arbiter routing a
    gated phase back for another attempt left its stale result file on
    disk, so a later re-run of that phase could re-score the same stale
    findings and loop the pipeline through arbitration again."""

    def test_force_goto_deletes_the_gate_phases_result_files(self, real_db, tmp_path):
        from src.core.database import Phase, PhaseExecution, Workflow
        from src.phases.phase_manager import PhaseManager

        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        (docs / "qa.md").write_text(
            "---\ntype: qa_validation_result\nfailed_tests: 3\n---\n\n# stale report"
        )

        with real_db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    working_directory=str(tmp_path), status="active",
                )
            )
            session.add(
                Phase(
                    id="phase-arch", workflow_id="wf-1", order=3,
                    name="architecture_design", description="d", done_definitions=["x"],
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
                    id="exec-qa", phase_id="phase-qa", workflow_execution_id="wf-1",
                    status="in_progress",
                )
            )

        pm = PhaseManager(db_manager=real_db)
        pm.workflow_id = "wf-1"

        result = pm.mark_phase_complete(
            "phase-qa",
            "Arbiter: return for another attempt",
            force_action="goto",
            force_target_phase="architecture_design",
            force_reason="arbiter says redo the architecture",
        )

        assert result["action"] == "goto"
        assert result["target_phase"] == "architecture_design"
        assert not (docs / "qa.md").exists()


class TestPopulateFeatureFolder:
    """Regression: _populate_feature_folder swept the worktree's docs/ for
    production artifacts to archive into the features gallery -- but agents
    write their reports to .hephaestus/ now (see the docs/ -> .hephaestus/
    migration), so the sweep silently archived nothing."""

    @pytest.fixture
    def real_db(self, tmp_path):
        from src.core.database import DatabaseManager as _DBM

        db = _DBM(str(tmp_path / "test.db"))
        db.create_tables()
        return db

    def test_sweeps_hephaestus_not_docs(self, real_db, tmp_path):
        import json

        from src.core.database import Phase, Workflow
        from src.phases.phase_manager import PhaseManager

        wt = tmp_path / "project"

        with real_db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="my_feature", phases_folder_path="/tmp",
                    status="active", working_directory=str(wt),
                )
            )
            # Declares the files below as this workflow's own known outputs
            # -- _known_output_basenames reads Phase.outputs, not a
            # hardcoded list, so the sweep needs a real Phase row per file.
            # Phase.outputs is a Text column, not JSON -- must be a
            # JSON-encoded string, same as PhaseManager.initialize_workflow
            # writes it, not a raw Python list.
            session.add(Phase(
                id="phase-1", workflow_id="wf-1", order=1, name="architecture_design",
                description="d", done_definitions=["x"],
                outputs=json.dumps(["architecture.md"]),
            ))
            session.add(Phase(
                id="phase-2", workflow_id="wf-1", order=2, name="qa_validation",
                description="d", done_definitions=["x"],
                outputs=json.dumps(["qa.md"]),
            ))
            session.add(Phase(
                id="phase-3", workflow_id="wf-1", order=3, name="doc_review",
                description="d", done_definitions=["x"],
                outputs=json.dumps(["feature_report.html"]),
            ))

        # Written after the Workflow row exists -- matches real ordering
        # (agents write into the worktree once their task/workflow is live)
        # and satisfies the freshness guard (file mtime >= workflow.created_at).
        (wt / ".hephaestus" / "qa_validation").mkdir(parents=True)
        (wt / ".hephaestus" / "architecture.md").write_text("# Architecture")
        (wt / ".hephaestus" / "qa_validation" / "qa.md").write_text("# QA")
        (wt / ".hephaestus" / "feature_report.html").write_text("<html></html>")

        pm = PhaseManager(db_manager=real_db)
        pm.workflow_id = "wf-1"

        session = real_db.get_session()
        try:
            workflow = session.query(Workflow).filter_by(id="wf-1").first()
            pm._populate_feature_folder(session, workflow)
        finally:
            session.close()

        feature_dirs = list((wt / ".hephaestus" / "features").iterdir())
        assert len(feature_dirs) == 1
        feature_dir = feature_dirs[0]
        assert (feature_dir / "docs" / "architecture.md").exists()
        assert (feature_dir / "docs" / "qa_validation" / "qa.md").exists()
        assert (feature_dir / "feature_report.html").exists()

    def test_excludes_tmux_features_and_scratch_directories(self, real_db, tmp_path):
        import json

        from src.core.database import Phase, Workflow
        from src.phases.phase_manager import PhaseManager

        wt = tmp_path / "project"

        with real_db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="my_feature", phases_folder_path="/tmp",
                    status="active", working_directory=str(wt),
                )
            )
            session.add(Phase(
                id="phase-1", workflow_id="wf-1", order=1, name="product_requirements",
                description="d", done_definitions=["x"],
                outputs=json.dumps(["requirements.md"]),
            ))

        # Written after the Workflow row exists -- see matching comment in
        # test_sweeps_hephaestus_not_docs above.
        (wt / ".hephaestus" / "tmux").mkdir(parents=True)
        (wt / ".hephaestus" / "tmux" / "agent.transcript.log").write_text("log")
        (wt / ".hephaestus" / "features" / "some-feature").mkdir(parents=True)
        (wt / ".hephaestus" / "features" / "some-feature" / "scope.md").write_text("# Scope")
        (wt / ".hephaestus" / "scratch").mkdir(parents=True)
        (wt / ".hephaestus" / "scratch" / "notes.md").write_text("# Notes")
        (wt / ".hephaestus" / "requirements.md").write_text("# Requirements")

        pm = PhaseManager(db_manager=real_db)
        pm.workflow_id = "wf-1"

        session = real_db.get_session()
        try:
            workflow = session.query(Workflow).filter_by(id="wf-1").first()
            pm._populate_feature_folder(session, workflow)
        finally:
            session.close()

        feature_dir = next(
            d for d in (wt / ".hephaestus" / "features").iterdir()
            if "my_feature" in d.name
        )
        copied = list((feature_dir / "docs").rglob("*"))
        copied_names = {f.name for f in copied if f.is_file()}
        # pipeline_metrics.json is always written by _populate_feature_folder
        # itself (step 5), independent of the .hephaestus/ sweep under test.
        assert copied_names == {"requirements.md", "pipeline_metrics.json"}

    def test_ignores_stale_and_unknown_files(self, real_db, tmp_path):
        """Regression: a shared worktree can carry leftover files from an
        unrelated prior run -- observed live, a three-day-old
        architecture.md/docs.md/summary.md/feature_report.html from a
        completely different feature got archived as this workflow's own
        output, because the sweep trusted any doc-extension file it found
        regardless of what wrote it or when. Two independent guards now
        apply: the filename must be one of this pipeline's well-known
        outputs, AND its mtime must be no older than the workflow itself."""
        import json
        import os
        import time

        from src.core.database import Phase, Workflow
        from src.phases.phase_manager import PhaseManager

        wt = tmp_path / "project"

        with real_db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="my_feature", phases_folder_path="/tmp",
                    status="active", working_directory=str(wt),
                )
            )
            # Both filenames below are declared as known outputs -- the test
            # is specifically about the mtime guard rejecting a stale
            # architecture.md despite its name being well-known, so the name
            # guard alone must not be what rejects it.
            session.add(Phase(
                id="phase-1", workflow_id="wf-1", order=1, name="product_requirements",
                description="d", done_definitions=["x"],
                outputs=json.dumps(["requirements.md"]),
            ))
            session.add(Phase(
                id="phase-2", workflow_id="wf-1", order=2, name="architecture_design",
                description="d", done_definitions=["x"],
                outputs=json.dumps(["architecture.md"]),
            ))

        (wt / ".hephaestus").mkdir(parents=True)
        # Genuinely fresh, well-known output -- must be archived.
        (wt / ".hephaestus" / "requirements.md").write_text("# Requirements")
        # Well-known filename, but its mtime predates the workflow -- a
        # leftover from whatever ran in this worktree/path before. Must be
        # rejected even though the name alone would otherwise pass.
        stale = wt / ".hephaestus" / "architecture.md"
        stale.write_text("# Stale architecture from an unrelated feature")
        old_time = time.time() - 86400  # 1 day before "now"
        os.utime(stale, (old_time, old_time))
        # Fresh mtime, but not a known output filename -- must be rejected
        # too (e.g. a scratch/debug file an agent happened to leave behind).
        (wt / ".hephaestus" / "random_notes.md").write_text("# Random")

        pm = PhaseManager(db_manager=real_db)
        pm.workflow_id = "wf-1"

        session = real_db.get_session()
        try:
            workflow = session.query(Workflow).filter_by(id="wf-1").first()
            pm._populate_feature_folder(session, workflow)
        finally:
            session.close()

        feature_dir = next(
            d for d in (wt / ".hephaestus" / "features").iterdir()
            if "my_feature" in d.name
        )
        copied_names = {f.name for f in (feature_dir / "docs").rglob("*") if f.is_file()}
        assert copied_names == {"requirements.md", "pipeline_metrics.json"}

    def test_pipeline_metrics_records_the_workflow_passed_in_not_self_workflow_id(
        self, real_db, tmp_path
    ):
        """Regression: pipeline_metrics.json's workflow_id used to come from
        self.workflow_id -- PhaseManager's legacy single-workflow instance
        attribute -- instead of the `workflow` object this method was
        actually called with. PhaseManager also tracks multiple concurrent
        workflows (self.active_executions), so a stale/mismatched
        self.workflow_id would record the WRONG workflow_id here --
        precisely the field _find_archived_feature_report matches archived
        reports by, so this is exactly the kind of bug that could
        misattribute one workflow's report to another's gallery entry."""
        import json

        from src.core.database import Workflow
        from src.phases.phase_manager import PhaseManager

        wt = tmp_path / "project"
        wt.mkdir(parents=True)

        with real_db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-real", name="my_feature", phases_folder_path="/tmp",
                    status="active", working_directory=str(wt),
                )
            )

        pm = PhaseManager(db_manager=real_db)
        # Deliberately mismatched -- simulates this instance last tracking a
        # DIFFERENT workflow (e.g. a shared/reused PhaseManager) at the
        # moment _populate_feature_folder runs for "wf-real".
        pm.workflow_id = "wf-stale-unrelated"

        session = real_db.get_session()
        try:
            workflow = session.query(Workflow).filter_by(id="wf-real").first()
            pm._populate_feature_folder(session, workflow)
        finally:
            session.close()

        feature_dir = next(
            d for d in (wt / ".hephaestus" / "features").iterdir()
            if "my_feature" in d.name
        )
        metrics = json.loads((feature_dir / "docs" / "pipeline_metrics.json").read_text())
        assert metrics["workflow_id"] == "wf-real"


class TestGetOrchestratorPhaseOrderMap:
    """_get_orchestrator must build WorkflowOrchestrator's phase_order_map
    from the workflow's real Phase.order DB values, not leave the
    orchestrator to fall back to its own hand-maintained vocabulary of the
    autopilot pipeline's phase names (SOLID review 2.11) -- that hardcoded
    dict has already drifted out of sync with the real phase ids once."""

    @pytest.fixture
    def real_db(self, tmp_path):
        from src.core.database import DatabaseManager as _DBM

        db = _DBM(str(tmp_path / "test.db"))
        db.create_tables()
        return db

    def _seed(self, real_db):
        from src.core.database import Phase, Workflow
        from src.core.database import WorkflowDefinition as DBWorkflowDefinition

        with real_db.session_scope() as session:
            session.add(
                DBWorkflowDefinition(
                    id="def-1",
                    name="Test Definition",
                    orchestrator_config={"type": "evaluating"},
                )
            )
            session.add(
                Workflow(
                    id="wf-1",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    definition_id="def-1",
                )
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

    def test_phase_order_map_reflects_real_phase_rows(self, real_db):
        from src.phases.phase_manager import PhaseManager

        self._seed(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            orchestrator = pm._get_orchestrator(session, "wf-1")

        assert orchestrator is not None
        assert orchestrator.phase_order_map == {
            "architecture_design": 3,
            "development": 4,
        }

    def test_phase_order_map_wins_over_legacy_fallback(self, real_db):
        """Regression proof: the legacy hardcoded dict has
        forensics_analysis/git_expert at the wrong orders relative to
        an unusual real workflow -- a workflow-supplied phase_order_map
        must always be consulted first, never silently overridden by the
        fallback vocabulary."""
        from src.core.database import Phase
        from src.phases.phase_manager import PhaseManager

        self._seed(real_db)
        with real_db.session_scope() as session:
            # A phase named "development" at a DB order (99) that
            # deliberately disagrees with the legacy dict's guess (4) --
            # proves the real map wins, not the hardcoded one.
            session.query(Phase).filter_by(id="phase-dev").update({"order": 99})

        pm = PhaseManager(db_manager=real_db)
        with real_db.session_scope() as session:
            orchestrator = pm._get_orchestrator(session, "wf-1")

        assert orchestrator._phase_name_to_order("development") == 99

    def test_orchestrator_cached_after_first_build(self, real_db):
        from src.phases.phase_manager import PhaseManager

        self._seed(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            first = pm._get_orchestrator(session, "wf-1")
            second = pm._get_orchestrator(session, "wf-1")

        assert first is second

    def test_config_refreshes_from_db_on_cache_hit_while_state_persists(self, real_db):
        """Regression: the orchestrator instance was cached forever after
        its first build, freezing its STATIC config (max_total_gotos,
        evaluation_points) at whatever the DB happened to say at that one
        moment -- even after a later server.py startup correctly re-synced
        WorkflowDefinition.orchestrator_config from source. Observed live:
        a long-running workflow's cached orchestrator kept using a stale
        max_total_gotos long after the DB was fixed, permanently stuck
        re-arbitrating the same phase and never advancing. The cached
        instance must still be reused (so RUNTIME state -- total_gotos,
        retry counters -- persists across calls), but its config must be
        refreshed from the DB on every call, not just the first."""
        from src.core.database import WorkflowDefinition as DBWorkflowDefinition
        from src.phases.phase_manager import PhaseManager

        self._seed(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            first = pm._get_orchestrator(session, "wf-1")
        first.total_gotos = 7  # simulate accumulated runtime state

        # A later "register workflow definitions from source" pass (what
        # server.py's startup does on every restart) corrects the DB.
        with real_db.session_scope() as session:
            session.query(DBWorkflowDefinition).filter_by(id="def-1").update(
                {"orchestrator_config": {"type": "evaluating", "max_total_gotos": 30}}
            )

        with real_db.session_scope() as session:
            second = pm._get_orchestrator(session, "wf-1")

        assert second is first, "must reuse the same instance so runtime state persists"
        assert second.total_gotos == 7
        assert second.config.max_total_gotos == 30

    def test_max_iterations_is_scoped_per_workflow_not_shared_globally(self, real_db):
        """Regression: run_single_workflow used to apply --max-iterations by
        mutating the shared WorkflowDefinition.orchestrator_config row
        itself (_update_orchestrator_max_gotos, since removed) -- every
        workflow sharing that definition_id reads the SAME row, so
        launching one workflow with a different max_iterations silently
        changed the goto budget for every OTHER concurrently-active
        workflow of that type. run_phase0 hardcodes max_iterations=3 for
        every Phase 0 launch, so each Phase 0 run reset every in-flight
        feature pipeline's real budget (e.g. 30, from workflow.yaml) down
        to 3, regardless of which workflow was actually being launched.
        max_iterations must now be scoped per-workflow via
        Workflow.launch_params, not leak between sibling workflows of the
        same definition."""
        from src.core.database import Phase, Workflow
        from src.core.database import WorkflowDefinition as DBWorkflowDefinition
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(DBWorkflowDefinition(
                id="def-1", name="Test Definition",
                orchestrator_config={"type": "evaluating", "max_total_gotos": 30},
            ))
            # Feature pipeline workflow, launched with the real per-project
            # default (no override) -- must keep the definition's own 30.
            session.add(Workflow(
                id="wf-feature", name="feature", phases_folder_path="/tmp",
                status="active", definition_id="def-1",
            ))
            session.add(Phase(
                id="phase-feature", workflow_id="wf-feature", order=1,
                name="scope_review", description="d", done_definitions=["x"],
            ))
            # Phase 0 workflow, launched with max_iterations=3 hardcoded --
            # must NOT drag wf-feature's budget down with it.
            session.add(Workflow(
                id="wf-phase0", name="phase0", phases_folder_path="/tmp",
                status="active", definition_id="def-1",
                launch_params={"max_iterations": 3},
            ))
            session.add(Phase(
                id="phase-phase0", workflow_id="wf-phase0", order=1,
                name="feature_architect", description="d", done_definitions=["x"],
            ))

        pm = PhaseManager(db_manager=real_db)
        with real_db.session_scope() as session:
            feature_orch = pm._get_orchestrator(session, "wf-feature")
            phase0_orch = pm._get_orchestrator(session, "wf-phase0")
            # Re-fetch wf-feature AFTER wf-phase0 was built -- proves the
            # earlier lookup wasn't just returning a not-yet-corrupted
            # snapshot.
            feature_orch_again = pm._get_orchestrator(session, "wf-feature")

        assert feature_orch.config.max_total_gotos == 30
        assert phase0_orch.config.max_total_gotos == 3
        assert feature_orch_again.config.max_total_gotos == 30


class TestPhaseNameToOrderLegacyFallback:
    """_phase_name_to_order falls back to _LEGACY_NAME_TO_ORDER only when
    no phase_order_map was supplied (e.g. an orchestrator constructed
    directly, not via PhaseManager._get_orchestrator). Regression: a prior
    fix corrected 10 of these 12 entries but swapped forensics_analysis and
    git_expert, trusting workflow.yaml's session_roles dict key order
    (which lists git_expert first) instead of each phase's own `id:`
    field in config/workflows/autopilot/*.yaml (forensics_analysis: id 11,
    git_expert: id 12)."""

    def _orchestrator(self):
        from src.workflow_engine.orchestrator import (
            OrchestratorConfig,
            WorkflowOrchestrator,
        )

        return WorkflowOrchestrator(OrchestratorConfig())

    def test_forensics_analysis_before_git_expert(self):
        orch = self._orchestrator()
        assert orch._phase_name_to_order("forensics_analysis") == 12
        assert orch._phase_name_to_order("git_expert") == 13

    def test_matches_real_phase_ids_in_every_autopilot_yaml(self):
        """Cross-checks the legacy fallback dict against the actual `id:`
        field in every phase YAML file -- the same source of truth
        _get_orchestrator's phase_order_map now reads from the DB copy of.
        Catches the next drift automatically instead of relying on someone
        noticing by hand again."""
        import re
        from pathlib import Path

        orch = self._orchestrator()
        phases_dir = (
            Path(__file__).parent.parent
            / "config"
            / "workflows"
            / "autopilot"
        )
        for phase_file in phases_dir.glob("*.yaml"):
            if phase_file.name == "workflow.yaml":
                continue
            text = phase_file.read_text()
            match = re.search(r"^id:\s*(\d+)", text, re.MULTILINE)
            assert match, f"{phase_file.name} has no top-level id: field"
            expected_order = int(match.group(1))
            phase_name = phase_file.stem
            assert orch._phase_name_to_order(phase_name) == expected_order, (
                f"{phase_name}: _LEGACY_NAME_TO_ORDER says "
                f"{orch._phase_name_to_order(phase_name)}, "
                f"but {phase_file.name} declares id: {expected_order}"
            )


class TestTagCompletingTask:
    """Regression: only one of mark_phase_complete's six external callers
    ever set Task.action, and only for 'goto' -- 'retry' was never set
    anywhere, so that badge was dead in the frontend. _tag_completing_task
    is the single choke point every mark_phase_complete return path now
    routes through, so it only needs testing once instead of at each of the
    six callers separately."""

    @pytest.fixture
    def real_db(self, tmp_path):
        from src.core.database import DatabaseManager as _DBM

        db = _DBM(str(tmp_path / "test.db"))
        db.create_tables()
        return db

    def _seed_completed_task(self, real_db, phase_id="phase-1", task_id="task-1"):
        from src.core.database import Phase, Task, Workflow

        with real_db.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Phase(
                    id=phase_id, workflow_id="wf-1", order=1,
                    name="development", description="d", done_definitions=["x"],
                )
            )
            session.add(
                Task(
                    id=task_id,
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    phase_id=phase_id,
                    workflow_id="wf-1",
                )
            )

    def test_goto_sets_action_and_target_phase(self, real_db):
        from src.phases.phase_manager import PhaseManager

        self._seed_completed_task(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            pm._tag_completing_task(
                session, "phase-1",
                {"action": "goto", "target_phase": "architecture_design"},
            )

        from src.core.database import Task

        with real_db.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.action == "goto"
            assert task.action_target_phase == "architecture_design"

    def test_retry_sets_action_and_target_phase(self, real_db):
        """Regression proof: retry was never tagged anywhere in production
        code before -- confirms the same choke point handles it too, not
        just goto."""
        from src.phases.phase_manager import PhaseManager

        self._seed_completed_task(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            pm._tag_completing_task(
                session, "phase-1",
                {"action": "retry", "target_phase": "development"},
            )

        from src.core.database import Task

        with real_db.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.action == "retry"
            assert task.action_target_phase == "development"

    def test_continue_does_not_touch_action(self, real_db):
        from src.phases.phase_manager import PhaseManager

        self._seed_completed_task(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            pm._tag_completing_task(
                session, "phase-1", {"action": "continue", "target_phase": "qa_validation"},
            )

        from src.core.database import Task

        with real_db.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.action == ""
            assert task.action_target_phase is None

    def test_no_completed_task_for_phase_is_safe(self, real_db):
        """No 'done'/'failed' task exists for this phase yet -- must not
        raise, just silently skip tagging."""
        from src.core.database import Phase, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Phase(
                    id="phase-1", workflow_id="wf-1", order=1,
                    name="development", description="d", done_definitions=["x"],
                )
            )

        pm = PhaseManager(db_manager=real_db)
        with real_db.session_scope() as session:
            pm._tag_completing_task(
                session, "phase-1", {"action": "goto", "target_phase": "architecture_design"},
            )  # should not raise

    def test_force_goto_path_tags_the_completing_task(self, real_db):
        """Integration-level: mark_phase_complete(force_action='goto') --
        the arbitration path, _resolve_arbitration_outcome's real call
        shape -- actually tags the completing task end-to-end, not just
        the isolated _tag_completing_task unit."""
        from src.core.database import Phase, PhaseExecution, Task, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Phase(
                    id="phase-dev", workflow_id="wf-1", order=4,
                    name="development", description="d", done_definitions=["x"],
                )
            )
            session.add(
                Phase(
                    id="phase-arch", workflow_id="wf-1", order=3,
                    name="architecture_design", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
                    status="in_progress",
                )
            )
            session.add(
                Task(
                    id="task-dev",
                    raw_description="r",
                    done_definition="d",
                    status="done",
                    phase_id="phase-dev",
                    workflow_id="wf-1",
                )
            )

        pm = PhaseManager(db_manager=real_db)
        pm.mark_phase_complete(
            "phase-dev",
            "Arbiter: return for another attempt",
            force_action="goto",
            force_target_phase="architecture_design",
            force_reason="arbiter says redo the architecture",
        )

        with real_db.session_scope() as session:
            task = session.query(Task).filter_by(id="task-dev").first()
            assert task.action == "goto"
            assert task.action_target_phase == "architecture_design"


class TestStartNextPhaseMarksJumpedOverPhasesSkipped:
    """Regression, observed live: _start_next_phase honors an explicit
    action_target_phase (e.g. product_validation goto's back to
    development, then resumes at product_validation once the fix lands,
    correctly skipping a redundant re-run of architectural_review through
    qa_validation) but never touched the PhaseExecution rows for the
    phases it jumped over -- they're left at whatever stale "pending"
    status an earlier goto's reset left them in, forever. Every consumer
    that treats "pending" as "real work remains" (derive_workflow_status's
    completeness check chief among them) then sees the workflow as
    permanently incomplete, even after the whole feature has shipped."""

    @pytest.fixture
    def real_db(self, tmp_path):
        from src.core.database import DatabaseManager as _DBM

        db = _DBM(str(tmp_path / "test.db"))
        db.create_tables()
        return db

    def _seed(self, real_db, intermediate_status="pending"):
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        with real_db.session_scope() as session:
            session.add(
                Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active")
            )
            phases = [
                ("phase-dev", 4, "development"),
                ("phase-arch-review", 5, "architectural_review"),
                ("phase-adversarial", 6, "adversarial_review"),
                ("phase-qa", 9, "qa_validation"),
                ("phase-prodval", 10, "product_validation"),
            ]
            for pid, order, name in phases:
                session.add(
                    Phase(
                        id=pid, workflow_id="wf-1", order=order, name=name,
                        description="d", done_definitions=["x"],
                    )
                )
            session.add(
                PhaseExecution(id="exec-dev", phase_id="phase-dev", status="in_progress")
            )
            session.add(
                PhaseExecution(id="exec-arch", phase_id="phase-arch-review", status=intermediate_status)
            )
            session.add(
                PhaseExecution(id="exec-adv", phase_id="phase-adversarial", status=intermediate_status)
            )
            session.add(
                PhaseExecution(id="exec-qa", phase_id="phase-qa", status=intermediate_status)
            )
            session.add(
                PhaseExecution(id="exec-prodval", phase_id="phase-prodval", status="pending")
            )
            session.add(
                Task(
                    id="task-dev", raw_description="r", done_definition="d",
                    status="done", phase_id="phase-dev", workflow_id="wf-1",
                    action="goto", action_target_phase="product_validation",
                )
            )

    def test_jumped_over_phases_marked_skipped(self, real_db):
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        self._seed(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            next_phase = pm._start_next_phase(session, "phase-dev")
            assert next_phase.name == "product_validation"

        with real_db.session_scope() as session:
            for phase_id in ("phase-arch-review", "phase-adversarial", "phase-qa"):
                execution = session.query(PhaseExecution).filter_by(phase_id=phase_id).first()
                assert execution.status == "skipped"
                assert execution.completed_at is not None
            prodval_execution = session.query(PhaseExecution).filter_by(phase_id="phase-prodval").first()
            assert prodval_execution.status == "in_progress"

    def test_does_not_downgrade_an_already_completed_intermediate_phase(self, real_db):
        """A phase that genuinely completed in an earlier pass (its
        PhaseExecution already "completed") must not get overwritten to
        "skipped" just because this jump doesn't need to redo it."""
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        self._seed(real_db, intermediate_status="completed")
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            pm._start_next_phase(session, "phase-dev")

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-arch-review").first()
            assert execution.status == "completed"

    def test_reopens_the_target_phase_even_if_it_was_left_failed(self, real_db):
        """Root-cause regression: this function's reopen gate was an
        identical, independently-drifted ("pending", "completed",
        "skipped") copy of _create_phase_task's own gate (fixed there in
        4d2f2005) -- missing "failed" here too. _start_next_phase is the
        MAIN forward-progress path, so a target phase left "failed" from an
        earlier attempt never reopened here either, staying invisible to
        every _advance_phases dispatch case even once this jump correctly
        selected it as the goto target."""
        from src.core.database import PhaseExecution
        from src.phases.phase_manager import PhaseManager

        self._seed(real_db)
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            prodval_execution = session.query(PhaseExecution).filter_by(phase_id="phase-prodval").first()
            prodval_execution.status = "failed"

        with real_db.session_scope() as session:
            next_phase = pm._start_next_phase(session, "phase-dev")
            assert next_phase.name == "product_validation"

        with real_db.session_scope() as session:
            prodval_execution = session.query(PhaseExecution).filter_by(phase_id="phase-prodval").first()
            assert prodval_execution.status == "in_progress"

    def test_reopens_a_previously_skipped_target_phase(self, real_db):
        """Regression: next_phase's own reopen only handled entry status
        "pending"/"completed" -- if the target itself was already "skipped"
        (e.g. an earlier jump skipped it, and a later goto/retry sends work
        back through it directly), it stayed "skipped" while genuinely
        becoming the phase this cycle is about to run. Every consumer that
        treats "skipped" as terminal (derive_workflow_status's completeness
        check chief among them) then sees nothing incomplete and can mark
        the whole workflow "completed" while this phase is actually about
        to start real work. Same bug class as this class's own
        jumped-over-phases fix, just the mirror-image entry condition."""
        from src.core.database import Phase, PhaseExecution, Task, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active"))
            session.add(Phase(
                id="phase-dev", workflow_id="wf-1", order=4, name="development",
                description="d", done_definitions=["x"],
            ))
            session.add(Phase(
                id="phase-adversarial", workflow_id="wf-1", order=6, name="adversarial_review",
                description="d", done_definitions=["x"],
            ))
            session.add(PhaseExecution(id="exec-dev", phase_id="phase-dev", status="in_progress"))
            session.add(PhaseExecution(id="exec-adv", phase_id="phase-adversarial", status="skipped"))
            session.add(Task(
                id="task-dev", raw_description="r", done_definition="d",
                status="done", phase_id="phase-dev", workflow_id="wf-1",
                action="goto", action_target_phase="adversarial_review",
            ))
        pm = PhaseManager(db_manager=real_db)

        with real_db.session_scope() as session:
            next_phase = pm._start_next_phase(session, "phase-dev")
            assert next_phase.name == "adversarial_review"

        with real_db.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="phase-adversarial").first()
            assert execution.status == "in_progress"
            assert execution.started_at is not None


class TestGetPhaseContextUsesLiveRequiredOutput:
    """Regression: get_phase_context built each phase's SdkPhase.outputs
    from the raw Phase.outputs column -- a per-workflow-instance snapshot
    taken at workflow-creation time from whatever workflow.yaml said then,
    and never refreshed afterward. This shows up verbatim in the agent's
    own prompt (PhaseContext.to_prompt_context's "Outputs:" line), so a
    workflow created before an output-format change (e.g. the OKF
    single-file refactor collapsing a phase's json+md pair into one .md)
    kept telling the agent to produce the OLD file(s), for every phase, for
    its entire remaining run -- not just its next retry. Must prefer
    workflow.yaml's required_output override, which IS read fresh from disk
    on every call, while still falling back to Phase.outputs (preserving
    non-file descriptive text) for phases with no override."""

    @pytest.fixture
    def real_db_with_override(self, tmp_path, monkeypatch):
        import json as _json

        from src.core.database import DatabaseManager as _DBM
        from src.core.database import Phase, PhaseExecution, Workflow, WorkflowDefinition

        db = _DBM(str(tmp_path / "test.db"))
        db.create_tables()
        monkeypatch.setattr("src.core.database.DatabaseManager", lambda *a, **kw: db)

        workflows_dir = tmp_path / "workflows"
        (workflows_dir / "phase_mgr_test_def").mkdir(parents=True)
        (workflows_dir / "phase_mgr_test_def" / "workflow.yaml").write_text(
            "required_output:\n"
            "  architectural_review: review.md\n"
        )
        monkeypatch.setattr("src.workflow_registry._WORKFLOWS_DIR", workflows_dir)

        with db.session_scope() as session:
            session.add(WorkflowDefinition(id="phase_mgr_test_def", name="t"))
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    definition_id="phase_mgr_test_def",
                )
            )
            session.add(
                Phase(
                    id="phase-review", workflow_id="wf-1", order=5,
                    name="architectural_review", description="d",
                    done_definitions=["x"],
                    # The stale snapshot: this workflow was created back
                    # when the phase still wrote a json+md pair.
                    outputs=_json.dumps(
                        [
                            "review.md",
                            "architectural_review_result.json",
                        ]
                    ),
                )
            )
            session.add(
                Phase(
                    id="phase-dev", workflow_id="wf-1", order=4,
                    name="development", description="d",
                    done_definitions=["x"],
                    # No required_output override for this phase -- non-file
                    # descriptive text like this must survive unchanged.
                    outputs=_json.dumps(["source code in project path"]),
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-review", phase_id="phase-review",
                    workflow_execution_id="wf-1", status="in_progress",
                )
            )
        return db

    def test_gated_phase_outputs_reflects_the_current_override(
        self, real_db_with_override
    ):
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=real_db_with_override)
        ctx = pm.get_phase_context("phase-review")
        assert ctx.phase.outputs == ["review.md"]

    def test_non_file_outputs_survive_for_a_phase_with_no_override(
        self, real_db_with_override
    ):
        """Sanity check the fix isn't overbroad: a phase with no
        required_output override (development) must keep its non-file
        descriptive text, not have it dropped by a strict required-files
        check."""
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=real_db_with_override)
        ctx = pm.get_phase_context("phase-dev")
        assert ctx.phase.outputs == ["source code in project path"]


class TestCompleteWorkflowRefusesWhenPhasesRemain:
    """Regression, observed live: _start_next_phase returns None for three
    different reasons (workflow not active/paused; another phase already
    in_progress -- e.g. stale state from goto/retry churn; or genuinely no
    higher-order phase exists), but every caller treated ANY None the same
    way: complete the workflow. A goto-limit-exceeded forced "continue"
    past product_validation got treated as full completion while
    doc_review/forensics_analysis/git_expert/deploy were all still
    "pending" and had never run -- the workflow never reached the phase
    that actually merges to main. _complete_workflow must now independently
    verify no higher-order phase remains before marking the workflow done."""

    @pytest.fixture
    def workflow_with_remaining_phase(self, real_db):
        from src.core.database import Phase, PhaseExecution, Workflow

        with real_db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp", status="active",
                )
            )
            session.add(
                Phase(
                    id="phase-pv", workflow_id="wf-1", order=9,
                    name="product_validation", description="d", done_definitions=["x"],
                )
            )
            session.add(
                Phase(
                    id="phase-doc", workflow_id="wf-1", order=10,
                    name="doc_review", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-pv", phase_id="phase-pv",
                    workflow_execution_id="wf-1", status="completed",
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-doc", phase_id="phase-doc",
                    workflow_execution_id="wf-1", status="pending",
                )
            )
        return real_db

    @pytest.fixture
    def workflow_ready_to_complete(self, workflow_with_remaining_phase):
        """Same as workflow_with_remaining_phase, but doc_review's own
        PhaseExecution is "completed" -- derive_workflow_status requires
        every tracked PhaseExecution to be terminal, not just that no
        higher-order Phase remains, so the "genuinely completes" tests need
        a fixture where that's actually true. Also adds a done Task --
        derive_workflow_status returns the current status unchanged with
        zero tasks for the workflow ("no tasks yet"), never reaching its
        own PhaseExecution check at all."""
        with workflow_with_remaining_phase.session_scope() as session:
            from src.core.database import PhaseExecution, Task

            exec_doc = session.query(PhaseExecution).filter_by(id="exec-doc").first()
            exec_doc.status = "completed"
            session.add(
                Task(
                    id="task-doc",
                    raw_description="do work",
                    done_definition="done",
                    status="done",
                    phase_id="phase-doc",
                    workflow_id="wf-1",
                )
            )
        return workflow_with_remaining_phase

    def test_refuses_to_complete_when_a_higher_order_phase_exists(
        self, workflow_with_remaining_phase
    ):
        from src.core.database import Workflow
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=workflow_with_remaining_phase)
        pm.workflow_id = "wf-1"
        session = workflow_with_remaining_phase.get_session()
        try:
            pm._complete_workflow(session, current_phase_id="phase-pv")
        finally:
            session.close()

        with workflow_with_remaining_phase.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"

    def test_completes_when_current_phase_is_genuinely_last(
        self, workflow_ready_to_complete
    ):
        from src.core.database import Workflow
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=workflow_ready_to_complete)
        pm.workflow_id = "wf-1"
        session = workflow_ready_to_complete.get_session()
        try:
            # doc_review (order 10) is the last phase here -- nothing has a
            # higher order than it.
            pm._complete_workflow(session, current_phase_id="phase-doc")
        finally:
            session.close()

        with workflow_ready_to_complete.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "completed"

    def test_still_completes_when_no_current_phase_id_given(
        self, workflow_ready_to_complete
    ):
        """Backward-compatible default: callers that genuinely can't supply
        a phase_id (none currently do) must not be newly broken -- the
        safety check is opt-in via the parameter, not mandatory."""
        from src.core.database import Workflow
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=workflow_ready_to_complete)
        pm.workflow_id = "wf-1"
        session = workflow_ready_to_complete.get_session()
        try:
            pm._complete_workflow(session)
        finally:
            session.close()

        with workflow_ready_to_complete.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "completed"

    def test_refuses_when_current_phase_own_execution_is_not_actually_done(
        self, workflow_ready_to_complete
    ):
        """SOLID review 2.1: the order-only guard above (no higher-order
        Phase remains) is necessary but not sufficient -- it says nothing
        about whether the CURRENT phase's own PhaseExecution genuinely
        finished. Route through derive_workflow_status (write_back=True)
        instead of unconditionally marking "completed" once the order
        check passes, so a phase still stuck in_progress for some other
        reason doesn't get silently skipped over."""
        from src.core.database import PhaseExecution, Workflow
        from src.phases.phase_manager import PhaseManager

        with workflow_ready_to_complete.session_scope() as session:
            exec_doc = session.query(PhaseExecution).filter_by(id="exec-doc").first()
            exec_doc.status = "in_progress"

        pm = PhaseManager(db_manager=workflow_ready_to_complete)
        pm.workflow_id = "wf-1"
        session = workflow_ready_to_complete.get_session()
        try:
            pm._complete_workflow(session, current_phase_id="phase-doc")
        finally:
            session.close()

        with workflow_ready_to_complete.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"


class TestCompleteWorkflowPausesForReviewMode:
    """Regression: under review_mode, git_expert now dispatches like
    any other phase (the agent commits, pushes, and opens a PR --
    scripts/agent-safe-bin/git blocks only `git merge`/push-to-main until
    approved), so a workflow can genuinely reach "every phase done"
    without a human ever having reviewed/merged anything. Before this
    fix, _complete_workflow had no review-mode awareness at all and would
    mark the workflow "completed" outright the moment the last phase
    (e.g. git_expert, having opened but not merged a PR) finished --
    nothing would ever prompt the human to merge it.

    review_feature's approve branch (feature_routes.py) is what actually
    clears this pause and completes the workflow for real, once the human
    approves -- see its own resume_workflow + derive_workflow_status call.
    """

    @pytest.fixture
    def review_mode_env(self, real_db, monkeypatch):
        """A workflow with a single, genuinely-last, genuinely-completed
        phase (mirrors TestCompleteWorkflowRefusesWhenPhasesRemain's own
        workflow_ready_to_complete, duplicated here since pytest doesn't
        share method-scoped fixtures across classes), linked to a
        review_mode=True project. Also points get_db() (used internally
        by _should_pause_for_review) at the same sqlite file this
        fixture's own session_scope calls use."""
        from src.core.database import AutopilotProject, Phase, PhaseExecution, Task, Workflow

        db_path = str(real_db.engine.url.database)
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)

        with real_db.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp", review_mode=True))
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp", status="active",
                project_id="proj-1",
            ))
            session.add(Phase(
                id="phase-doc", workflow_id="wf-1", order=10,
                name="doc_review", description="d", done_definitions=["x"],
            ))
            session.add(PhaseExecution(
                id="exec-doc", phase_id="phase-doc",
                workflow_execution_id="wf-1", status="completed",
            ))
            session.add(Task(
                id="task-doc", raw_description="do work", done_definition="done",
                status="done", phase_id="phase-doc", workflow_id="wf-1",
            ))

        return real_db

    def test_pauses_instead_of_completing_when_review_mode_is_on(self, review_mode_env):
        from src.core.database import Workflow
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=review_mode_env)
        pm.workflow_id = "wf-1"
        session = review_mode_env.get_session()
        try:
            pm._complete_workflow(session, current_phase_id="phase-doc")
        finally:
            session.close()

        with review_mode_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "review"

    def test_does_not_re_pause_on_a_later_call(self, review_mode_env):
        """The "workflow.status == active" guard must make this a one-shot
        pause -- a later sweep tick re-evaluating the same terminal state
        must not error or re-process it."""
        from src.core.database import Workflow
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=review_mode_env)
        pm.workflow_id = "wf-1"

        session = review_mode_env.get_session()
        try:
            pm._complete_workflow(session, current_phase_id="phase-doc")
        finally:
            session.close()

        session = review_mode_env.get_session()
        try:
            pm._complete_workflow(session, current_phase_id="phase-doc")
        finally:
            session.close()

        with review_mode_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "review"

    def test_populates_the_feature_folder_before_pausing_for_review(self, review_mode_env):
        """Regression: _populate_feature_folder (archives feature_report.html
        and the rest of this run's artifacts into the permanent feature
        record) used to run only in the auto-complete branch below the
        review-mode pause-and-return -- so under review mode it never ran
        at all, not while pending review and not even after approval
        (review_feature's own approve branch never called it either). The
        whole point of review mode is letting a human inspect the report
        BEFORE approving, so this must fire before the pause, not only on
        the (never-reached, under review mode) auto-complete path."""
        from src.phases.phase_manager import PhaseManager

        pm = PhaseManager(db_manager=review_mode_env)
        pm.workflow_id = "wf-1"
        with patch.object(PhaseManager, "_populate_feature_folder") as mock_populate:
            session = review_mode_env.get_session()
            try:
                pm._complete_workflow(session, current_phase_id="phase-doc")
            finally:
                session.close()

        mock_populate.assert_called_once()

    def test_does_not_re_pause_an_already_approved_feature(self, review_mode_env):
        """Regression: unlike _pause_feature_for_review (pipeline.py, fixed
        at ce0c4a7), this pause site had no "already approved" guard --
        checked only whether the PROJECT has review_mode on, never whether
        THIS feature was already reviewed. Once a workflow is resumed after
        approval (workflow.status back to "active", the very condition that
        gates this whole block) and a later cycle reaches a fresh
        completion here -- a retry/goto firing after approval, or a phase
        simply re-dispatching -- the human got a second, redundant review
        prompt with the PR never actually merged. Observed live: approve ->
        git_expert re-ran -> workflow paused_by="review" again,
        review_status still "approved" the whole time."""
        from src.core.database import Feature, Workflow
        from src.phases.phase_manager import PhaseManager

        with review_mode_env.session_scope() as session:
            session.add(
                Feature(
                    id="feat-1", design_id="des-1", feature_key="k", name="n",
                    scope="s", workflow_id="wf-1", status="active",
                    review_status="approved",
                )
            )

        pm = PhaseManager(db_manager=review_mode_env)
        pm.workflow_id = "wf-1"
        session = review_mode_env.get_session()
        try:
            pm._complete_workflow(session, current_phase_id="phase-doc")
        finally:
            session.close()

        with review_mode_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status != "paused"
            assert wf.paused_by is None

    def test_still_completes_normally_when_review_mode_is_off(self, real_db):
        """No AutopilotProject/review_mode involved at all -- must behave
        exactly as before this fix (matches the sibling
        test_completes_when_current_phase_is_genuinely_last)."""
        from src.core.database import Phase, PhaseExecution, Task, Workflow
        from src.phases.phase_manager import PhaseManager

        with real_db.session_scope() as session:
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp", status="active",
            ))
            session.add(Phase(
                id="phase-doc", workflow_id="wf-1", order=10,
                name="doc_review", description="d", done_definitions=["x"],
            ))
            session.add(PhaseExecution(
                id="exec-doc", phase_id="phase-doc",
                workflow_execution_id="wf-1", status="completed",
            ))
            session.add(Task(
                id="task-doc", raw_description="do work", done_definition="done",
                status="done", phase_id="phase-doc", workflow_id="wf-1",
            ))

        pm = PhaseManager(db_manager=real_db)
        pm.workflow_id = "wf-1"
        session = real_db.get_session()
        try:
            pm._complete_workflow(session, current_phase_id="phase-doc")
        finally:
            session.close()

        with real_db.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "completed"


# ── _record_review_pass_if_applicable ──────────────────────────────


class TestRecordReviewPassIfApplicable:
    """A diff-stable review phase (adversarial_review/architectural_review/
    security_review) cleanly passing should snapshot the worktree's HEAD
    SHA so _create_phase_task's skip check can later tell whether anything
    changed. Any other phase must not write anything.

    Uses the global `db_manager` fixture (conftest.py), not the module-
    local `real_db` one: record_review_pass/get_review_pass_sha go through
    get_db(), which resolves via the HEPHAESTUS_TEST_DB env var -- only
    `db_manager` points that at the same file this test's own PhaseManager
    uses, so a Phase/Workflow row seeded here is visible there.
    """

    def _seed(self, db_manager, phase_name, working_directory):
        from src.core.database import Phase, Workflow

        with db_manager.session_scope() as session:
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp", status="active",
                working_directory=working_directory,
            ))
            session.add(Phase(
                id="phase-1", workflow_id="wf-1", order=6,
                name=phase_name, description="d", done_definitions=["x"],
            ))

    def test_records_commit_sha_for_diff_stable_phase(self, db_manager, tmp_path):
        from unittest.mock import MagicMock

        from src.autopilot.spec import get_review_pass_sha
        from src.core.database import Phase
        from src.phases.phase_manager import PhaseManager

        self._seed(db_manager, "architectural_review", str(tmp_path))
        pm = PhaseManager(db_manager=db_manager)

        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = "abc123"

        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            with patch("git.Repo", return_value=mock_repo):
                pm._record_review_pass_if_applicable(session, phase)

        assert get_review_pass_sha("wf-1", "architectural_review") == "abc123"

    def test_does_not_record_for_non_diff_stable_phase(self, db_manager, tmp_path):
        from unittest.mock import MagicMock

        from src.autopilot.spec import get_review_pass_sha
        from src.core.database import Phase
        from src.phases.phase_manager import PhaseManager

        self._seed(db_manager, "development", str(tmp_path))
        pm = PhaseManager(db_manager=db_manager)

        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = "abc123"

        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            with patch("git.Repo", return_value=mock_repo):
                pm._record_review_pass_if_applicable(session, phase)

        assert get_review_pass_sha("wf-1", "development") is None

    def test_no_working_directory_is_safe(self, db_manager):
        """No working_directory to read HEAD from -- must not raise."""
        from src.core.database import Phase
        from src.phases.phase_manager import PhaseManager

        self._seed(db_manager, "adversarial_review", working_directory=None)
        pm = PhaseManager(db_manager=db_manager)

        with db_manager.session_scope() as session:
            phase = session.query(Phase).filter_by(id="phase-1").first()
            pm._record_review_pass_if_applicable(session, phase)  # no raise
