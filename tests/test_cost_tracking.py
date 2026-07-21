"""Tests for cost tracking schema and cost derivation module."""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.cost_derivation import (
    _check_budget_enforcement,
    _pause_project_workflows,
    check_budget_before_new_work,
    derive_design_cost,
    derive_feature_cost,
    derive_project_cost,
    derive_task_cost,
    derive_workflow_cost,
    record_cost,
)
from src.core.database import (
    Agent,
    AutopilotDesign,
    AutopilotProject,
    Base,
    CostEntry,
    Feature,
    SessionCostCheckpoint,
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

    # Disable FK enforcement for test simplicity
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
    """Create a sample project for testing."""
    project = AutopilotProject(
        id=f"proj-{uuid.uuid4().hex[:8]}",
        name="Test Project",
        base_dir="/tmp/test-project",
    )
    db_session.add(project)
    db_session.commit()
    return project


@pytest.fixture
def sample_design(db_session, sample_project):
    """Create a sample design for testing."""
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
    """Create a sample workflow definition for testing."""
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
    """Create a sample workflow for testing."""
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
    """Create a sample feature for testing."""
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

    # Update workflow with feature_id
    sample_workflow.feature_id = feature.id
    db_session.commit()

    return feature


@pytest.fixture
def sample_agent(db_session):
    """Create a sample agent for testing."""
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
    """Create a sample task for testing."""
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


class TestCostEntryModel:
    """Test the CostEntry model."""

    def test_cost_entry_creation(self, db_session, sample_task, sample_agent, sample_workflow):
        """Test creating a CostEntry."""
        entry = CostEntry(
            id=f"cost-{uuid.uuid4().hex[:8]}",
            task_id=sample_task.id,
            agent_id=sample_agent.id,
            workflow_id=sample_workflow.id,
            source="pi",
            model="anthropic/claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cache_write_tokens=100,
            reasoning_tokens=50,
            cost_usd=0.05,
            recorded_at=datetime.utcnow(),
            raw_usage={"test": "data"},
        )
        db_session.add(entry)
        db_session.commit()

        # Verify it was saved
        saved = db_session.query(CostEntry).filter_by(id=entry.id).first()
        assert saved is not None
        assert saved.task_id == sample_task.id
        assert saved.agent_id == sample_agent.id
        assert saved.workflow_id == sample_workflow.id
        assert saved.source == "pi"
        assert saved.model == "anthropic/claude-sonnet-4"
        assert saved.input_tokens == 1000
        assert saved.output_tokens == 500
        assert saved.cache_read_tokens == 200
        assert saved.cache_write_tokens == 100
        assert saved.reasoning_tokens == 50
        assert saved.cost_usd == 0.05
        assert saved.raw_usage == {"test": "data"}

    def test_cost_entry_nullable_fields(self, db_session):
        """Test CostEntry with nullable fields."""
        entry = CostEntry(
            id=f"cost-{uuid.uuid4().hex[:8]}",
            source="openrouter_direct",
            cost_usd=0.01,
        )
        db_session.add(entry)
        db_session.commit()

        saved = db_session.query(CostEntry).filter_by(id=entry.id).first()
        assert saved is not None
        assert saved.task_id is None
        assert saved.agent_id is None
        assert saved.workflow_id is None
        assert saved.model is None
        assert saved.input_tokens == 0
        assert saved.output_tokens == 0
        assert saved.cache_read_tokens == 0
        assert saved.cache_write_tokens == 0
        assert saved.reasoning_tokens == 0
        assert saved.raw_usage is None

    def test_cost_entry_indexes(self, db_session):
        """Test that cost_entries indexes exist."""
        # This test verifies the indexes were created
        # In SQLite, we can check via PRAGMA index_list
        with db_session.bind.connect() as conn:
            result = conn.execute(text("PRAGMA index_list(cost_entries)"))
            indexes = [row[1] for row in result]

        assert "ix_cost_entries_task_id" in indexes
        assert "ix_cost_entries_workflow_id" in indexes
        assert "ix_cost_entries_recorded_at" in indexes


class TestSessionCostCheckpointModel:
    """Test the SessionCostCheckpoint model."""

    def test_checkpoint_creation(self, db_session):
        """Test creating a SessionCostCheckpoint."""
        checkpoint = SessionCostCheckpoint(
            session_id="test-session-123",
            lines_processed=42,
            updated_at=datetime.utcnow(),
        )
        db_session.add(checkpoint)
        db_session.commit()

        saved = db_session.query(SessionCostCheckpoint).filter_by(session_id="test-session-123").first()
        assert saved is not None
        assert saved.session_id == "test-session-123"
        assert saved.lines_processed == 42

    def test_checkpoint_defaults(self, db_session):
        """Test SessionCostCheckpoint defaults."""
        checkpoint = SessionCostCheckpoint(
            session_id="test-session-456",
        )
        db_session.add(checkpoint)
        db_session.commit()

        saved = db_session.query(SessionCostCheckpoint).filter_by(session_id="test-session-456").first()
        assert saved is not None
        assert saved.lines_processed == 0


class TestCostColumnsOnExistingModels:
    """Test cost_total_usd columns on existing models."""

    def test_task_cost_column(self, db_session, sample_task):
        """Test Task.cost_total_usd column."""
        assert sample_task.cost_total_usd == 0.0

        sample_task.cost_total_usd = 1.50
        db_session.commit()

        saved = db_session.query(Task).filter_by(id=sample_task.id).first()
        assert saved.cost_total_usd == 1.50

    def test_feature_cost_column(self, db_session, sample_feature):
        """Test Feature.cost_total_usd column."""
        assert sample_feature.cost_total_usd == 0.0

        sample_feature.cost_total_usd = 2.75
        db_session.commit()

        saved = db_session.query(Feature).filter_by(id=sample_feature.id).first()
        assert saved.cost_total_usd == 2.75

    def test_design_cost_column(self, db_session, sample_design):
        """Test AutopilotDesign.cost_total_usd column."""
        assert sample_design.cost_total_usd == 0.0

        sample_design.cost_total_usd = 5.25
        db_session.commit()

        saved = db_session.query(AutopilotDesign).filter_by(id=sample_design.id).first()
        assert saved.cost_total_usd == 5.25

    def test_project_cost_columns(self, db_session, sample_project):
        """Test AutopilotProject.cost_total_usd and cost_limit_usd columns."""
        assert sample_project.cost_total_usd == 0.0
        assert sample_project.cost_limit_usd is None

        sample_project.cost_total_usd = 10.50
        sample_project.cost_limit_usd = 100.00
        db_session.commit()

        saved = db_session.query(AutopilotProject).filter_by(id=sample_project.id).first()
        assert saved.cost_total_usd == 10.50
        assert saved.cost_limit_usd == 100.00


class TestRecordCost:
    """Test the record_cost function."""

    def test_record_cost_basic(self, db_session, sample_task, sample_agent, sample_workflow):
        """Test basic cost recording."""
        entry = record_cost(
            db=db_session,
            cost_usd=0.05,
            source="pi",
            task_id=sample_task.id,
            agent_id=sample_agent.id,
            workflow_id=sample_workflow.id,
            model="anthropic/claude-sonnet-4",
            input_tokens=1000,
            output_tokens=500,
        )

        assert entry is not None
        assert entry.cost_usd == 0.05
        assert entry.source == "pi"

        # Verify task cost was updated
        db_session.refresh(sample_task)
        assert sample_task.cost_total_usd == 0.05

    def test_record_cost_auto_derives_workflow(self, db_session, sample_task, sample_agent):
        """Test that workflow_id is auto-derived from task."""
        entry = record_cost(
            db=db_session,
            cost_usd=0.03,
            source="claude_code",
            task_id=sample_task.id,
            agent_id=sample_agent.id,
        )

        assert entry.workflow_id == sample_task.workflow_id

    def test_record_cost_multiple_entries(self, db_session, sample_task, sample_agent):
        """Test recording multiple cost entries."""
        record_cost(
            db=db_session,
            cost_usd=0.05,
            source="pi",
            task_id=sample_task.id,
        )
        record_cost(
            db=db_session,
            cost_usd=0.03,
            source="pi",
            task_id=sample_task.id,
        )
        record_cost(
            db=db_session,
            cost_usd=0.02,
            source="openrouter_direct",
            task_id=sample_task.id,
        )

        # Verify task cost was summed
        db_session.refresh(sample_task)
        assert abs(sample_task.cost_total_usd - 0.10) < 0.0001

    def test_record_cost_without_task(self, db_session, sample_workflow):
        """Test recording cost without a task_id (overhead)."""
        entry = record_cost(
            db=db_session,
            cost_usd=0.01,
            source="openrouter_direct",
            workflow_id=sample_workflow.id,
            model="openai/gpt-4o",
        )

        assert entry.task_id is None
        assert entry.workflow_id == sample_workflow.id


class TestDeriveTaskCost:
    """Test the derive_task_cost function."""

    def test_derive_task_cost_no_entries(self, db_session, sample_task):
        """Test deriving cost for a task with no entries."""
        cost = derive_task_cost(db_session, sample_task.id)
        assert cost == 0.0

    def test_derive_task_cost_with_entries(self, db_session, sample_task):
        """Test deriving cost for a task with entries."""
        # Add some cost entries
        for i in range(3):
            entry = CostEntry(
                id=f"cost-{uuid.uuid4().hex[:8]}",
                task_id=sample_task.id,
                source="pi",
                cost_usd=0.01 * (i + 1),
            )
            db_session.add(entry)
        db_session.commit()

        cost = derive_task_cost(db_session, sample_task.id)
        assert abs(cost - 0.06) < 0.0001  # 0.01 + 0.02 + 0.03

    def test_derive_task_cost_self_heal(self, db_session, sample_task):
        """Test that self-healing updates the task."""
        # Set incorrect cost on task
        sample_task.cost_total_usd = 999.99
        db_session.commit()

        # Add actual cost entries
        entry = CostEntry(
            id=f"cost-{uuid.uuid4().hex[:8]}",
            task_id=sample_task.id,
            source="pi",
            cost_usd=0.05,
        )
        db_session.add(entry)
        db_session.commit()

        cost = derive_task_cost(db_session, sample_task.id, write_back=True)
        db_session.commit()  # Caller must commit

        # Verify cost was derived correctly
        assert abs(cost - 0.05) < 0.0001

        # Verify task was self-healed
        db_session.refresh(sample_task)
        assert abs(sample_task.cost_total_usd - 0.05) < 0.0001

    def test_derive_task_cost_nonexistent(self, db_session):
        """Test deriving cost for a nonexistent task."""
        cost = derive_task_cost(db_session, "nonexistent-task")
        assert cost == 0.0


class TestDeriveWorkflowCost:
    """Test the derive_workflow_cost function."""

    def test_derive_workflow_cost(self, db_session, sample_workflow, sample_task):
        """Test deriving workflow cost."""
        # Add cost entries
        for i in range(2):
            entry = CostEntry(
                id=f"cost-{uuid.uuid4().hex[:8]}",
                task_id=sample_task.id,
                workflow_id=sample_workflow.id,
                source="pi",
                cost_usd=0.05,
            )
            db_session.add(entry)
        db_session.commit()

        cost = derive_workflow_cost(db_session, sample_workflow.id)
        assert abs(cost - 0.10) < 0.0001


class TestDeriveFeatureCost:
    """Test the derive_feature_cost function."""

    def test_derive_feature_cost(self, db_session, sample_feature, sample_workflow, sample_task):
        """Test deriving feature cost."""
        # Add cost entries
        entry = CostEntry(
            id=f"cost-{uuid.uuid4().hex[:8]}",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
            source="pi",
            cost_usd=0.15,
        )
        db_session.add(entry)
        db_session.commit()

        cost = derive_feature_cost(db_session, sample_feature.id)
        db_session.commit()  # Caller must commit
        assert abs(cost - 0.15) < 0.0001

        # Verify feature was updated
        db_session.refresh(sample_feature)
        assert abs(sample_feature.cost_total_usd - 0.15) < 0.0001


class TestDeriveDesignCost:
    """Test the derive_design_cost function."""

    def test_derive_design_cost(self, db_session, sample_design, sample_feature, sample_workflow, sample_task):
        """Test deriving design cost."""
        # Add cost entries
        entry = CostEntry(
            id=f"cost-{uuid.uuid4().hex[:8]}",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
            source="pi",
            cost_usd=0.25,
        )
        db_session.add(entry)
        db_session.commit()

        cost = derive_design_cost(db_session, sample_design.id)
        db_session.commit()  # Caller must commit
        assert abs(cost - 0.25) < 0.0001

        # Verify design was updated
        db_session.refresh(sample_design)
        assert abs(sample_design.cost_total_usd - 0.25) < 0.0001


class TestDeriveProjectCost:
    """Test the derive_project_cost function."""

    def test_derive_project_cost(self, db_session, sample_project, sample_design, sample_feature, sample_workflow, sample_task):
        """Test deriving project cost."""
        # Add cost entries
        entry = CostEntry(
            id=f"cost-{uuid.uuid4().hex[:8]}",
            task_id=sample_task.id,
            workflow_id=sample_workflow.id,
            source="pi",
            cost_usd=0.50,
        )
        db_session.add(entry)
        db_session.commit()

        cost = derive_project_cost(db_session, sample_project.id)
        db_session.commit()  # Caller must commit
        assert abs(cost - 0.50) < 0.0001

        # Verify project was updated
        db_session.refresh(sample_project)
        assert abs(sample_project.cost_total_usd - 0.50) < 0.0001


class TestBudgetEnforcement:
    """Test budget enforcement logic."""

    def test_check_budget_under_limit(self, db_session, sample_project):
        """Test budget check when under limit."""
        sample_project.cost_total_usd = 50.0
        sample_project.cost_limit_usd = 100.0
        db_session.commit()

        result = check_budget_before_new_work(db_session, sample_project.id)
        assert result is True

    def test_check_budget_over_limit(self, db_session, sample_project):
        """Test budget check when over limit."""
        sample_project.cost_total_usd = 150.0
        sample_project.cost_limit_usd = 100.0
        db_session.commit()

        result = check_budget_before_new_work(db_session, sample_project.id)
        assert result is False

    def test_check_budget_no_limit(self, db_session, sample_project):
        """Test budget check when no limit is set."""
        sample_project.cost_total_usd = 1000.0
        sample_project.cost_limit_usd = None
        db_session.commit()

        result = check_budget_before_new_work(db_session, sample_project.id)
        assert result is True

    def test_check_budget_nonexistent_project(self, db_session):
        """Test budget check for nonexistent project."""
        result = check_budget_before_new_work(db_session, "nonexistent-project")
        assert result is True

    def test_pause_project_workflows(self, db_session, sample_workflow):
        """Test pausing project workflows."""
        sample_workflow.status = "active"
        db_session.commit()

        paused = _pause_project_workflows(db_session, sample_workflow.project_id, paused_by="budget")
        db_session.commit()  # Caller must commit

        assert paused == 1

        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "budget"
        assert sample_workflow.status_reason == "Budget limit reached"

    def test_pause_project_workflows_idempotent(self, db_session, sample_workflow):
        """Test that pausing is idempotent."""
        sample_workflow.status = "active"
        db_session.commit()

        # First pause
        _pause_project_workflows(db_session, sample_workflow.project_id, paused_by="budget")
        db_session.commit()  # Caller must commit

        # Second pause (should be no-op)
        paused2 = _pause_project_workflows(db_session, sample_workflow.project_id, paused_by="budget")
        assert paused2 == 0

    def test_budget_enforcement_triggers_pause(self, db_session, sample_project, sample_workflow):
        """Test that budget enforcement triggers workflow pause."""
        sample_project.cost_limit_usd = 1.0
        sample_workflow.status = "active"
        db_session.commit()

        # Simulate going over budget
        sample_project.cost_total_usd = 1.50
        db_session.commit()

        _check_budget_enforcement(db_session, sample_project)

        db_session.refresh(sample_workflow)
        assert sample_workflow.status == "paused"
        assert sample_workflow.paused_by == "budget"


class TestMigration:
    """Test that migrations work correctly."""

    def test_cost_columns_exist(self, db_session):
        """Test that cost columns exist on all relevant tables."""
        with db_session.bind.connect() as conn:
            # Check tasks table
            result = conn.execute(text("PRAGMA table_info(tasks)"))
            columns = {row[1] for row in result}
            assert "cost_total_usd" in columns

            # Check features table
            result = conn.execute(text("PRAGMA table_info(features)"))
            columns = {row[1] for row in result}
            assert "cost_total_usd" in columns

            # Check autopilot_designs table
            result = conn.execute(text("PRAGMA table_info(autopilot_designs)"))
            columns = {row[1] for row in result}
            assert "cost_total_usd" in columns

            # Check autopilot_projects table
            result = conn.execute(text("PRAGMA table_info(autopilot_projects)"))
            columns = {row[1] for row in result}
            assert "cost_total_usd" in columns
            assert "cost_limit_usd" in columns

    def test_cost_entries_table_exists(self, db_session):
        """Test that cost_entries table exists."""
        with db_session.bind.connect() as conn:
            result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='cost_entries'"))
            assert result.fetchone() is not None

    def test_session_cost_checkpoints_table_exists(self, db_session):
        """Test that session_cost_checkpoints table exists."""
        with db_session.bind.connect() as conn:
            result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name='session_cost_checkpoints'"))
            assert result.fetchone() is not None
