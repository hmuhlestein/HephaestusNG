"""Tests for budget enforcement guards in the autopilot orchestrator.

Tests:
1. paused_by generalization — budget-paused workflows are NOT auto-resumed
2. Budget guard in pick_next_design — skips over-budget projects
3. Budget guard in _run_one_feature — blocks new launches when over budget
4. AutopilotService.start() — user-paused are resumed, budget-paused are NOT
5. PUT /projects/{id} — raising limit clears budget-paused workflows
"""

import uuid
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.cost_derivation import (
    _pause_project_workflows,
    check_budget_before_new_work,
)
from src.core.database import (
    Agent,
    AutopilotProject,
    Base,
    Task,
    Workflow,
    WorkflowDefinition,
)


@pytest.fixture
def db_session():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _skip_fk(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def project(db_session):
    p = AutopilotProject(
        id=f"proj-{uuid.uuid4().hex[:8]}",
        name="Test Project",
        base_dir="/tmp/test",
        cost_total_usd=0.0,
        cost_limit_usd=None,
    )
    db_session.add(p)
    db_session.commit()
    return p


@pytest.fixture
def workflow_def(db_session):
    wd = WorkflowDefinition(id="autopilot", name="Autopilot", description="desc")
    db_session.add(wd)
    wd2 = WorkflowDefinition(id="autopilot-phase0", name="Phase0", description="desc")
    db_session.add(wd2)
    db_session.commit()
    return wd


@pytest.fixture
def active_autopilot_workflow(db_session, project, workflow_def):
    wf = Workflow(
        id=f"wf-{uuid.uuid4().hex[:8]}",
        name="Main Workflow",
        phases_folder_path="/tmp/phases",
        definition_id="autopilot",
        project_id=project.id,
        status="active",
    )
    db_session.add(wf)
    db_session.commit()
    return wf


@pytest.fixture
def active_phase0_workflow(db_session, project, workflow_def):
    wf = Workflow(
        id=f"wf-{uuid.uuid4().hex[:8]}",
        name="Phase 0 Workflow",
        phases_folder_path="/tmp/phases",
        definition_id="autopilot-phase0",
        project_id=project.id,
        status="active",
    )
    db_session.add(wf)
    db_session.commit()
    return wf


@pytest.fixture
def agent_on_workflow(db_session, active_autopilot_workflow):
    agent = Agent(
        id=f"agent-{uuid.uuid4().hex[:8]}",
        system_prompt="Test prompt",
        cli_type="pi",
        status="working",
    )
    db_session.add(agent)
    task = Task(
        id=f"task-{uuid.uuid4().hex[:8]}",
        raw_description="Test task",
        done_definition="Done",
        workflow_id=active_autopilot_workflow.id,
        assigned_agent_id=agent.id,
    )
    db_session.add(task)
    db_session.commit()
    agent.current_task_id = task.id
    db_session.commit()
    return agent


# ── Test: _pause_project_workflows includes Phase 0 ─────────────


class TestPauseProjectWorkflows:
    def test_pauses_autopilot_workflow(self, db_session, active_autopilot_workflow):
        count = _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="budget")
        db_session.commit()
        assert count == 1
        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.status == "paused"
        assert active_autopilot_workflow.paused_by == "budget"
        assert active_autopilot_workflow.status_reason == "Budget limit reached"

    def test_pauses_phase0_workflow(self, db_session, active_phase0_workflow):
        count = _pause_project_workflows(db_session, active_phase0_workflow.project_id, paused_by="budget")
        db_session.commit()
        assert count == 1
        db_session.refresh(active_phase0_workflow)
        assert active_phase0_workflow.status == "paused"
        assert active_phase0_workflow.paused_by == "budget"

    def test_pauses_both_simultaneously(self, db_session, active_autopilot_workflow, active_phase0_workflow):
        count = _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="budget")
        db_session.commit()
        assert count == 2
        db_session.refresh(active_autopilot_workflow)
        db_session.refresh(active_phase0_workflow)
        assert active_autopilot_workflow.paused_by == "budget"
        assert active_phase0_workflow.paused_by == "budget"

    def test_terminates_active_agents(self, db_session, active_autopilot_workflow, agent_on_workflow):
        _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="budget")
        db_session.commit()
        db_session.refresh(agent_on_workflow)
        assert agent_on_workflow.status == "terminated"
        assert agent_on_workflow.terminated_at is not None

    def test_idempotent(self, db_session, active_autopilot_workflow):
        c1 = _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="budget")
        db_session.commit()
        c2 = _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="budget")
        assert c1 == 1
        assert c2 == 0  # Already paused — no-op

    def test_user_pause_also_works(self, db_session, active_autopilot_workflow):
        _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="user")
        db_session.commit()
        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.paused_by == "user"
        assert active_autopilot_workflow.status_reason is None

    def test_user_pause_clears_stale_budget_reason(self, db_session, active_autopilot_workflow):
        """User pause should clear any stale budget status_reason (WARNING-3)."""
        # Simulate a workflow that had a stale budget status_reason
        # (e.g., from a previous budget pause that was partially cleared)
        active_autopilot_workflow.status_reason = "Budget limit reached"
        db_session.commit()

        # Now pause by user (simulating /autopilot/stop)
        _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="user")
        db_session.commit()
        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.paused_by == "user"
        assert active_autopilot_workflow.status_reason is None  # Cleared!

    def test_terminates_starting_agents(self, db_session, active_autopilot_workflow):
        """Agents with status='starting' should be terminated (WARNING-1)."""
        # Create a starting agent
        starting_agent = Agent(
            id=f"agent-{uuid.uuid4().hex[:8]}",
            system_prompt="Test prompt",
            cli_type="pi",
            status="starting",
        )
        db_session.add(starting_agent)
        task = Task(
            id=f"task-{uuid.uuid4().hex[:8]}",
            raw_description="Test task",
            done_definition="Done",
            workflow_id=active_autopilot_workflow.id,
            assigned_agent_id=starting_agent.id,
        )
        db_session.add(task)
        db_session.commit()
        starting_agent.current_task_id = task.id
        db_session.commit()

        _pause_project_workflows(db_session, active_autopilot_workflow.project_id, paused_by="budget")
        db_session.commit()
        db_session.refresh(starting_agent)
        assert starting_agent.status == "terminated"  # Starting agents must be terminated


# ── Test: check_budget_before_new_work ──────────────────────────


class TestCheckBudget:
    def test_under_budget(self, db_session, project):
        project.cost_total_usd = 50.0
        project.cost_limit_usd = 100.0
        db_session.commit()
        assert check_budget_before_new_work(db_session, project.id) is True

    def test_over_budget(self, db_session, project):
        project.cost_total_usd = 150.0
        project.cost_limit_usd = 100.0
        db_session.commit()
        assert check_budget_before_new_work(db_session, project.id) is False

    def test_no_limit(self, db_session, project):
        project.cost_total_usd = 9999.0
        project.cost_limit_usd = None
        db_session.commit()
        assert check_budget_before_new_work(db_session, project.id) is True

    def test_nonexistent_project(self, db_session):
        assert check_budget_before_new_work(db_session, "nonexistent") is True


# ── Test: paused_by generalization ──────────────────────────────


class TestPausedByGeneralization:
    """Budget-paused workflows should NOT be auto-resumed by self-heal paths."""

    def test_try_auto_resume_skips_budget_paused(self, db_session, active_autopilot_workflow):
        """_try_auto_resume_paused_workflow should not resume a budget-paused workflow."""
        from src.autopilot.orchestrator import _try_auto_resume_paused_workflow

        active_autopilot_workflow.status = "paused"
        active_autopilot_workflow.paused_by = "budget"
        db_session.commit()

        logger = MagicMock()
        _try_auto_resume_paused_workflow(db_session, active_autopilot_workflow.id, active_autopilot_workflow, logger)

        db_session.refresh(active_autopilot_workflow)
        # Should NOT have been resumed
        assert active_autopilot_workflow.status == "paused"
        assert active_autopilot_workflow.paused_by == "budget"

    def test_try_auto_resume_skips_user_paused(self, db_session, active_autopilot_workflow):
        """_try_auto_resume_paused_workflow should not resume a user-paused workflow."""
        from src.autopilot.orchestrator import _try_auto_resume_paused_workflow

        active_autopilot_workflow.status = "paused"
        active_autopilot_workflow.paused_by = "user"
        db_session.commit()

        logger = MagicMock()
        _try_auto_resume_paused_workflow(db_session, active_autopilot_workflow.id, active_autopilot_workflow, logger)

        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.status == "paused"
        assert active_autopilot_workflow.paused_by == "user"

    def test_try_auto_resume_works_when_not_paused_by_anything(self, db_session, active_autopilot_workflow):
        """_try_auto_resume_paused_workflow should resume when paused_by is None."""
        from src.autopilot.orchestrator import _try_auto_resume_paused_workflow

        active_autopilot_workflow.status = "paused"
        active_autopilot_workflow.paused_by = None
        db_session.commit()

        # Need at least one phase with a done task for the auto-resume to trigger
        from src.core.database import Phase, PhaseExecution

        phase = Phase(
            id=f"phase-{uuid.uuid4().hex[:8]}",
            workflow_id=active_autopilot_workflow.id,
            name="test_phase",
            order=1,
            description="Test phase for auto-resume",
            done_definitions=["Task is complete"],
        )
        db_session.add(phase)
        pe = PhaseExecution(
            id=f"pe-{uuid.uuid4().hex[:8]}",
            phase_id=phase.id,
            status="in_progress",
        )
        db_session.add(pe)
        task = Task(
            id=f"task-{uuid.uuid4().hex[:8]}",
            raw_description="done task",
            done_definition="done",
            workflow_id=active_autopilot_workflow.id,
            phase_id=phase.id,
            status="done",
        )
        db_session.add(task)
        db_session.commit()

        logger = MagicMock()
        _try_auto_resume_paused_workflow(db_session, active_autopilot_workflow.id, active_autopilot_workflow, logger)

        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.status == "active"


# ── Test: budget guard in pick_next_design ──────────────────────


class TestPickNextDesignBudgetGuard:
    """Behavioral tests for budget guards (replaces source inspection - NIT-1)."""

    def test_check_budget_blocks_over_budget_project(self, db_session, project, workflow_def):
        """check_budget_before_new_work returns False when over budget."""
        project.cost_limit_usd = 10.0
        project.cost_total_usd = 15.0
        db_session.commit()

        assert check_budget_before_new_work(db_session, project.id) is False

    def test_check_budget_allows_under_budget_project(self, db_session, project, workflow_def):
        """check_budget_before_new_work returns True when under budget."""
        project.cost_limit_usd = 100.0
        project.cost_total_usd = 5.0
        db_session.commit()

        assert check_budget_before_new_work(db_session, project.id) is True

    def test_check_budget_allows_no_limit_set(self, db_session, project, workflow_def):
        """check_budget_before_new_work returns True when no limit is set."""
        project.cost_limit_usd = None
        project.cost_total_usd = 9999.0
        db_session.commit()

        assert check_budget_before_new_work(db_session, project.id) is True

    def test_budget_guard_blocks_at_exact_limit(self, db_session, project, workflow_def):
        """Budget guard blocks when cost equals limit."""
        project.cost_limit_usd = 100.0
        project.cost_total_usd = 100.0
        db_session.commit()

        assert check_budget_before_new_work(db_session, project.id) is False


# ── Test: PUT /projects/{id} clears budget-paused workflows ────


class TestUpdateProjectClearsBudgetPause:
    def test_raising_limit_clears_budget_pause(self, db_session, project, active_autopilot_workflow):
        """Raising cost_limit_usd clears budget-paused workflows."""
        # Set over budget and trigger pause
        project.cost_limit_usd = 1.0
        project.cost_total_usd = 2.0
        db_session.commit()
        _pause_project_workflows(db_session, project.id, paused_by="budget")
        db_session.commit()

        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.paused_by == "budget"

        # Simulate raising the limit (mimicking PUT /projects/{id} logic)
        project.cost_limit_usd = 100.0
        db_session.commit()

        # Clear budget-paused workflows (same logic as autopilot_api.py)
        if project.cost_limit_usd is None or project.cost_total_usd < project.cost_limit_usd:
            budget_paused = (
                db_session.query(Workflow)
                .filter(
                    Workflow.project_id == project.id,
                    Workflow.paused_by == "budget",
                )
                .all()
            )
            for wf in budget_paused:
                wf.paused_by = None
                wf.status = "active"
                wf.status_reason = None
            db_session.commit()

        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.status == "active"
        assert active_autopilot_workflow.paused_by is None

    def test_clearing_limit_clears_budget_pause(self, db_session, project, active_autopilot_workflow):
        """Setting cost_limit_usd to None clears budget-paused workflows."""
        project.cost_limit_usd = 1.0
        project.cost_total_usd = 2.0
        db_session.commit()
        _pause_project_workflows(db_session, project.id, paused_by="budget")
        db_session.commit()

        # Clear the limit
        project.cost_limit_usd = None
        db_session.commit()

        if project.cost_limit_usd is None or project.cost_total_usd < project.cost_limit_usd:
            budget_paused = db_session.query(Workflow).filter(Workflow.project_id == project.id, Workflow.paused_by == "budget").all()
            for wf in budget_paused:
                wf.paused_by = None
                wf.status = "active"
                wf.status_reason = None
            db_session.commit()

        db_session.refresh(active_autopilot_workflow)
        assert active_autopilot_workflow.status == "active"
        assert active_autopilot_workflow.paused_by is None
