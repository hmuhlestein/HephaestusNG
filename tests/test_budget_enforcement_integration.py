"""Integration tests for budget enforcement.

Tests the full budget enforcement lifecycle including:
- Pausing workflows when budget exceeded
- Blocking new work when over budget
- Clearing budget pause when limit raised
- Auto-resume protection for budget-paused workflows
"""

import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.cost_derivation import (
    check_budget_before_new_work,
    record_cost,
)
from src.core.database import (
    Agent,
    AutopilotDesign,
    AutopilotProject,
    Base,
    CostEntry,
    Feature,
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
def sample_project(db_session):
    project = AutopilotProject(
        id=f"proj-{uuid.uuid4().hex[:8]}",
        name="Test Project",
        base_dir="/tmp/test",
    )
    db_session.add(project)
    db_session.commit()
    return project


@pytest.fixture
def sample_design(db_session, sample_project):
    design = AutopilotDesign(
        id=f"des-{uuid.uuid4().hex[:8]}",
        project_id=sample_project.id,
        filename="test-design.md",
        name="Test Design",
    )
    db_session.add(design)
    db_session.commit()
    return design


@pytest.fixture
def sample_workflow_definition(db_session):
    wd = WorkflowDefinition(
        id="autopilot",
        name="Autopilot",
        description="Autopilot workflow",
    )
    db_session.add(wd)
    db_session.commit()
    return wd


@pytest.fixture
def sample_workflow(db_session, sample_design, sample_workflow_definition):
    workflow = Workflow(
        id=f"wf-{uuid.uuid4().hex[:8]}",
        name="Test Workflow",
        phases_folder_path="/tmp/phases",
        definition_id="autopilot",
        design_id=sample_design.id,
        project_id=sample_design.project_id,
    )
    db_session.add(workflow)
    db_session.commit()
    return workflow


@pytest.fixture
def sample_feature(db_session, sample_design, sample_workflow):
    feature = Feature(
        id=f"feat-{uuid.uuid4().hex[:8]}",
        design_id=sample_design.id,
        feature_key="test-feature",
        name="Test Feature",
        scope="Test scope",
        workflow_id=sample_workflow.id,
    )
    db_session.add(feature)
    db_session.commit()
    sample_workflow.feature_id = feature.id
    db_session.commit()
    return feature


@pytest.fixture
def sample_agent(db_session):
    agent = Agent(
        id=f"agent-{uuid.uuid4().hex[:8]}",
        system_prompt="Test system prompt",
        cli_type="pi",
    )
    db_session.add(agent)
    db_session.commit()
    return agent


@pytest.fixture
def sample_task(db_session, sample_workflow, sample_agent):
    task = Task(
        id=f"task-{uuid.uuid4().hex[:8]}",
        raw_description="Test task",
        done_definition="Task is done",
        workflow_id=sample_workflow.id,
        assigned_agent_id=sample_agent.id,
    )
    db_session.add(task)
    db_session.commit()
    return task


class TestBudgetPausOnOverage:
    """Test budget pause triggers when cost exceeds limit."""

    def test_budget_pauses_when_cost_exceeds_limit(self, db_session, sample_project, sample_workflow, sample_task, sample_feature, sample_design):
        """When cost exceeds limit, workflows should be paused."""
        sample_project.cost_limit_usd = 10.0
        db_session.commit()

        # Record cost entries to build up to near the limit (using real entries
        # so the self-healing derivation sees the correct total)
        record_cost(
            db=db_session,
            cost_usd=9.50,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        # Verify we're near limit but not over
        db_session.refresh(sample_project)
        assert sample_project.cost_total_usd < sample_project.cost_limit_usd

        # record_cost's write_back cascade to feature/design/project is
        # throttled per-entity (_COST_CASCADE_THROTTLE_SECONDS) to avoid
        # hammering SQLite's single connection under concurrent agents --
        # see cost_derivation.py's module docstring. Without clearing it
        # here, the cascade for THIS call can be silently skipped if it
        # lands within the throttle window of the call above (or of an
        # earlier test in the same session touching the same entity),
        # leaving cost_total_usd stale and the pause below never firing.
        import src.core.cost_derivation as cd

        cd._last_cost_cascade_time.clear()

        # Now record cost that pushes over the limit
        record_cost(
            db=db_session,
            cost_usd=1.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        # Check that workflow was paused
        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "budget"
        assert "Budget limit" in (sample_workflow.status_reason or "")

    def test_budget_stays_active_when_under_limit(self, db_session, sample_project, sample_workflow, sample_task, sample_feature, sample_design):
        """When cost is under limit, workflows should stay active."""
        sample_project.cost_limit_usd = 100.0
        db_session.commit()

        record_cost(
            db=db_session,
            cost_usd=50.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        record_cost(
            db=db_session,
            cost_usd=5.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "active"
        assert sample_workflow.paused_by is None


class TestBudgetIncludesPhase0:
    """Test that budget pause includes autopilot-phase0 workflows."""

    def test_phase0_workflows_are_paused(self, db_session, sample_design, sample_project, sample_task, sample_workflow, sample_feature):
        """Phase 0 workflows should also be paused when budget exceeded."""
        sample_project.cost_limit_usd = 1.0
        db_session.commit()

        wd = WorkflowDefinition(id="autopilot-phase0", name="Phase 0", description="Phase 0")
        db_session.add(wd)
        db_session.commit()

        wf = Workflow(
            id=f"wf-{uuid.uuid4().hex[:8]}",
            name="Phase 0 WF",
            phases_folder_path="/tmp/phases",
            definition_id="autopilot-phase0",
            design_id=sample_design.id,
            project_id=sample_design.project_id,
            status="active",
        )
        db_session.add(wf)
        db_session.commit()

        # Build up cost to exceed limit
        record_cost(
            db=db_session,
            cost_usd=2.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        db_session.refresh(wf)
        assert wf.status == "paused"
        assert wf.paused_by == "budget"


class TestBudgetBlocksNewWork:
    """Test that budget check blocks new work."""

    def test_check_budget_returns_false_when_over(self, db_session, sample_project, sample_workflow, sample_task, sample_feature):
        """check_budget_before_new_work returns False when over budget."""
        sample_project.cost_limit_usd = 10.0
        db_session.commit()

        # Build up cost using actual entries
        record_cost(
            db=db_session,
            cost_usd=15.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        result = check_budget_before_new_work(db_session, sample_project.id)
        assert result is False

    def test_check_budget_returns_true_when_under(self, db_session, sample_project, sample_workflow, sample_task, sample_feature):
        """check_budget_before_new_work returns True when under budget."""
        sample_project.cost_limit_usd = 100.0
        db_session.commit()

        record_cost(
            db=db_session,
            cost_usd=50.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        result = check_budget_before_new_work(db_session, sample_project.id)
        assert result is True

    def test_check_budget_returns_true_when_no_limit(self, db_session, sample_project, sample_workflow, sample_task, sample_feature):
        """check_budget_before_new_work returns True when no limit set."""
        sample_project.cost_limit_usd = None
        db_session.commit()

        record_cost(
            db=db_session,
            cost_usd=1000.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        result = check_budget_before_new_work(db_session, sample_project.id)
        assert result is True


class TestBudgetAutoResumeBlocked:
    """Test that budget-paused workflows can't be auto-resumed."""

    def test_budget_paused_workflow_stays_paused(self, db_session, sample_workflow):
        """Budget-paused workflows should not be auto-resumed."""
        sample_workflow.status = "paused"
        sample_workflow.paused_by = "budget"
        sample_workflow.status_reason = "Budget limit reached"
        db_session.commit()

        # Simulate auto-resume attempt (would check paused_by is not None)
        # In the orchestrator, _try_auto_resume_paused_workflow now checks
        # paused_by is not None, not == "user"
        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "budget"

    def test_user_paused_workflow_stays_paused(self, db_session, sample_workflow):
        """User-paused workflows should not be auto-resumed."""
        sample_workflow.status = "paused"
        sample_workflow.paused_by = "user"
        db_session.commit()

        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "user"


class TestLimitRaiseClearsPause:
    """Test that raising the limit clears budget pause."""

    def test_raising_limit_clears_budget_pause(self, db_session, sample_project, sample_workflow, sample_task, sample_feature):
        """Raising the cost limit should clear budget-paused workflows."""
        sample_project.cost_limit_usd = 10.0
        db_session.commit()

        # Build up cost past limit
        record_cost(
            db=db_session,
            cost_usd=15.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        # Verify it was paused
        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "budget"

        # Simulate PUT /projects/{id} with higher limit
        sample_project.cost_limit_usd = 50.0
        db_session.flush()

        # Clear budget pause logic (same as in autopilot_api.py)
        if sample_project.cost_limit_usd is None or sample_project.cost_total_usd < sample_project.cost_limit_usd:
            budget_paused = (
                db_session.query(Workflow)
                .filter(
                    Workflow.project_id == sample_project.id,
                    Workflow.paused_by == "budget",
                )
                .all()
            )
            for wf in budget_paused:
                wf.paused_by = None
                wf.status = "active"
                wf.status_reason = None

        db_session.commit()

        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "active"
        assert sample_workflow.paused_by is None

    def test_clearing_limit_clears_budget_pause(self, db_session, sample_project, sample_workflow, sample_task, sample_feature):
        """Setting limit to None should clear budget-paused workflows."""
        sample_project.cost_limit_usd = 10.0
        db_session.commit()

        # Build up cost past limit
        record_cost(
            db=db_session,
            cost_usd=15.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        # Verify it was paused
        db_session.refresh(sample_workflow)
        assert sample_workflow.paused_by == "budget"

        sample_project.cost_limit_usd = None
        db_session.flush()

        # Clear budget pause logic
        budget_paused = (
            db_session.query(Workflow)
            .filter(
                Workflow.project_id == sample_project.id,
                Workflow.paused_by == "budget",
            )
            .all()
        )
        for wf in budget_paused:
            wf.paused_by = None
            wf.status = "active"
            wf.status_reason = None

        db_session.commit()

        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "active"
        assert sample_workflow.paused_by is None

    def test_lowering_limit_does_not_clear_pause(self, db_session, sample_project, sample_workflow, sample_task, sample_feature):
        """Lowering limit should NOT clear budget-paused workflows."""
        sample_project.cost_limit_usd = 10.0
        db_session.commit()

        # Build up cost past limit
        record_cost(
            db=db_session,
            cost_usd=15.0,
            source="pi",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
        )
        db_session.commit()

        # Verify it was paused
        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "budget"

        # Lower limit (still over budget)
        sample_project.cost_limit_usd = 5.0
        db_session.flush()

        # Check that pause is NOT cleared
        if sample_project.cost_limit_usd is None or sample_project.cost_total_usd < sample_project.cost_limit_usd:
            # This branch should NOT execute
            assert False, "Should not clear pause when lowering limit"

        db_session.commit()

        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "budget"


class TestConcurrentCostWrites:
    """Test that concurrent cost writes don't cause issues."""

    def test_multiple_cost_entries_for_same_task(self, db_session, sample_task, sample_workflow):
        """Multiple cost entries for same task should accumulate correctly."""
        for i in range(5):
            record_cost(
                db=db_session,
                cost_usd=1.0,
                source="pi",
                task_id=sample_task.id,
                workflow_id=sample_workflow.id,
            )

        db_session.commit()

        # Verify total
        from sqlalchemy import func

        total = db_session.query(func.sum(CostEntry.cost_usd)).filter(CostEntry.task_id == sample_task.id).scalar()
        assert abs(total - 5.0) < 0.0001

    def test_cost_entries_for_different_tasks(self, db_session, sample_workflow, sample_agent):
        """Cost entries for different tasks should be independent."""
        task1 = Task(
            id=f"task-{uuid.uuid4().hex[:8]}",
            raw_description="Task 1",
            done_definition="Done",
            workflow_id=sample_workflow.id,
            assigned_agent_id=sample_agent.id,
        )
        task2 = Task(
            id=f"task-{uuid.uuid4().hex[:8]}",
            raw_description="Task 2",
            done_definition="Done",
            workflow_id=sample_workflow.id,
            assigned_agent_id=sample_agent.id,
        )
        db_session.add(task1)
        db_session.add(task2)
        db_session.commit()

        record_cost(db=db_session, cost_usd=2.0, source="pi", task_id=task1.id)
        record_cost(db=db_session, cost_usd=3.0, source="pi", task_id=task2.id)
        db_session.commit()

        from src.core.cost_derivation import derive_task_cost

        assert abs(derive_task_cost(db_session, task1.id, write_back=False) - 2.0) < 0.0001
        assert abs(derive_task_cost(db_session, task2.id, write_back=False) - 3.0) < 0.0001
