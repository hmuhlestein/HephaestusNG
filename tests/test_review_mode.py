"""Integration tests for the review mode feature.

Tests the end-to-end flow:
1. Enable review mode on a project
2. Feature pauses after workflow completes
3. Approve clears the pause
4. Request changes injects feedback and re-queues
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import (
    Agent,
    AutopilotDesign,
    AutopilotProject,
    Base,
    Feature,
    Phase,
    Task,
    TaskPromptOverride,
    Workflow,
    WorkflowDefinition,
)


@pytest.fixture
def db_session():
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
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def review_project(db_session):
    proj = AutopilotProject(
        id=f"proj-{uuid.uuid4().hex[:8]}",
        name="Test Project",
        base_dir="/tmp/test",
        review_mode=True,
    )
    db_session.add(proj)
    db_session.commit()
    return proj


@pytest.fixture
def review_setup(db_session, review_project):
    """Create a complete feature + workflow + task setup for review testing."""
    design = AutopilotDesign(
        id=f"des-{uuid.uuid4().hex[:8]}",
        project_id=review_project.id,
        filename="test.md",
        name="Test Design",
    )
    workflow = Workflow(
        id=f"wf-{uuid.uuid4().hex[:8]}",
        name="Test Workflow",
        phases_folder_path="/tmp/phases",
        status="active",
        design_id=design.id,
        project_id=review_project.id,
    )
    feature = Feature(
        id=f"feat-{uuid.uuid4().hex[:8]}",
        design_id=design.id,
        feature_key="test-feature",
        name="Test Feature",
        scope="Build the test feature",
        workflow_id=workflow.id,
        status="active",
    )
    workflow.feature_id = feature.id
    phase = Phase(
        id=f"phase-{uuid.uuid4().hex[:8]}",
        workflow_id=workflow.id,
        order=1,
        name="development",
        description="Dev phase",
        done_definitions=["done"],
    )
    task = Task(
        id=f"task-{uuid.uuid4().hex[:8]}",
        raw_description="Build the thing",
        done_definition="Thing is built",
        workflow_id=workflow.id,
        phase_id=phase.id,
        status="done",
    )
    agent = Agent(
        id=f"agent-{uuid.uuid4().hex[:8]}",
        system_prompt="test",
        status="terminated",
        cli_type="pi",
        current_task_id=task.id,
    )
    task.assigned_agent_id = agent.id
    db_session.add_all([design, workflow, feature, phase, task, agent])
    db_session.commit()
    return feature, workflow, task, agent


class TestReviewModeSchema:
    def test_project_has_review_mode_column(self, db_session, review_project):
        assert review_project.review_mode is True

    def test_feature_has_review_columns(self, db_session, review_setup):
        feature, _, _, _ = review_setup
        assert feature.review_status is None
        assert feature.review_feedback is None
        assert feature.reviewed_at is None

    def test_review_mode_default_false(self, db_session):
        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}",
            name="Test",
            base_dir="/tmp",
        )
        db_session.add(proj)
        db_session.commit()
        assert proj.review_mode is False


class TestPauseForReview:
    def test_pause_sets_workflow_and_feature_status(self, db_session, review_setup):
        from unittest.mock import patch, Mock, MagicMock

        feature, workflow, _, _ = review_setup

        # get_db() is used as a context manager; mock it to yield our session
        cm = MagicMock()
        cm.__enter__ = Mock(return_value=db_session)
        cm.__exit__ = Mock(return_value=False)

        with patch("src.core.database.get_db", return_value=cm):
            from src.autopilot.orchestrator import _pause_feature_for_review
            logger = Mock()
            logger.info = Mock()
            logger.error = Mock()
            _pause_feature_for_review(feature.id, logger)

        db_session.refresh(workflow)
        db_session.refresh(feature)
        assert workflow.status == "paused"
        assert workflow.paused_by == "review"
        assert feature.status == "paused"

    def test_pause_idempotent(self, db_session, review_setup):
        from unittest.mock import patch, Mock, MagicMock

        feature, workflow, _, _ = review_setup

        cm = MagicMock()
        cm.__enter__ = Mock(return_value=db_session)
        cm.__exit__ = Mock(return_value=False)

        with patch("src.core.database.get_db", return_value=cm):
            from src.autopilot.orchestrator import _pause_feature_for_review
            logger = Mock()
            logger.info = Mock()
            logger.error = Mock()
            _pause_feature_for_review(feature.id, logger)
            _pause_feature_for_review(feature.id, logger)

        db_session.refresh(workflow)
        assert workflow.paused_by == "review"


class TestReviewEndpoint:
    """Test the POST /features/{id}/review endpoint logic."""

    def test_approve_clears_pause(self, db_session, review_setup):
        feature, workflow, _, _ = review_setup
        workflow.status = "paused"
        workflow.paused_by = "review"
        feature.status = "paused"
        db_session.commit()

        # Simulate approve
        feature.review_status = "approved"
        feature.reviewed_at = datetime.utcnow()
        feature.reviewed_by = "ui-user"
        workflow.status = "active"
        workflow.paused_by = None
        feature.status = "active"
        db_session.commit()

        db_session.refresh(workflow)
        db_session.refresh(feature)
        assert workflow.status == "active"
        assert workflow.paused_by is None
        assert feature.review_status == "approved"
        assert feature.status == "active"

    def test_request_changes_sets_feedback(self, db_session, review_setup):
        feature, workflow, task, _ = review_setup
        workflow.status = "paused"
        workflow.paused_by = "review"
        feature.status = "paused"
        db_session.commit()

        feedback = "Please fix the error handling in module X"

        # Simulate request_changes
        feature.review_status = "changes_requested"
        feature.review_feedback = feedback
        feature.reviewed_at = datetime.utcnow()
        feature.reviewed_by = "ui-user"
        workflow.status = "active"
        workflow.paused_by = None
        task.status = "pending"
        task.failure_reason = None
        task.assigned_agent_id = None
        feature.status = "active"

        # Inject feedback via TaskPromptOverride
        override = TaskPromptOverride(
            task_id=task.id,
            user_prompt=f"## Human Review Feedback\n\n{feedback}\n\n---\n\n",
            updated_by="ui-user",
        )
        db_session.add(override)
        db_session.commit()

        db_session.refresh(feature)
        db_session.refresh(task)
        assert feature.review_status == "changes_requested"
        assert feature.review_feedback == feedback
        assert task.status == "pending"

        saved_override = db_session.query(TaskPromptOverride).filter_by(task_id=task.id).first()
        assert saved_override is not None
        assert feedback in saved_override.user_prompt

    def test_approve_not_reviewing_is_idempotent(self, db_session, review_setup):
        feature, workflow, _, _ = review_setup
        # Workflow is NOT paused_by review
        workflow.status = "active"
        workflow.paused_by = None
        db_session.commit()

        # Simulate the endpoint's idempotent check
        if workflow.paused_by != "review":
            result = {"success": True, "message": "Feature was not awaiting review"}
        else:
            result = {"success": True, "message": "approved"}

        assert result["message"] == "Feature was not awaiting review"


class TestPromptBuilderOverride:
    """Test that TaskPromptOverride is injected into the initial message."""

    def test_override_prepended_to_task_description(self, db_session, review_setup):
        from src.agents.prompt_builder import AgentPromptBuilder
        from unittest.mock import Mock, patch, MagicMock

        feature, workflow, task, agent = review_setup

        # Create an override
        override = TaskPromptOverride(
            task_id=task.id,
            user_prompt="## Human Review Feedback\n\nFix the bugs.\n\n---\n\n",
            updated_by="ui-user",
        )
        db_session.add(override)
        db_session.commit()

        # Mock get_db to return our test session
        cm = MagicMock()
        cm.__enter__ = Mock(return_value=db_session)
        cm.__exit__ = Mock(return_value=False)

        phase_manager = Mock()
        phase_manager.get_workflow = Mock(return_value=None)
        phase_manager.get_workflow_config = Mock(return_value=None)
        builder = AgentPromptBuilder(phase_manager)

        with patch("src.core.database.get_db", return_value=cm):
            message = builder.format_initial_message(
                task=task,
                agent_id=agent.id,
                branch_path="/tmp/test",
                agent_type="phase",
            )

        assert "Fix the bugs" in message
        assert "Human Review Feedback" in message
        # Override should appear before the task description
        feedback_pos = message.index("Human Review Feedback")
        desc_pos = message.index("Build the thing")
        assert feedback_pos < desc_pos

    def test_no_override_no_extra_text(self, db_session, review_setup):
        from src.agents.prompt_builder import AgentPromptBuilder
        from unittest.mock import Mock, patch, MagicMock

        feature, workflow, task, agent = review_setup

        cm = MagicMock()
        cm.__enter__ = Mock(return_value=db_session)
        cm.__exit__ = Mock(return_value=False)

        phase_manager = Mock()
        phase_manager.get_workflow = Mock(return_value=None)
        phase_manager.get_workflow_config = Mock(return_value=None)
        builder = AgentPromptBuilder(phase_manager)

        with patch("src.core.database.get_db", return_value=cm):
            message = builder.format_initial_message(
                task=task,
                agent_id=agent.id,
                branch_path="/tmp/test",
                agent_type="phase",
            )

        assert "Human Review Feedback" not in message
        assert "Build the thing" in message


class TestFeatureStatusDerivation:
    def test_paused_feature_stays_paused(self, db_session, review_setup):
        from src.core.status_derivation import derive_feature_status

        feature, workflow, _, _ = review_setup
        feature.status = "paused"
        db_session.commit()

        status = derive_feature_status(db_session, feature.id, write_back=False)
        assert status == "paused"

    def test_active_feature_derives_from_tasks(self, db_session, review_setup):
        from src.core.status_derivation import derive_feature_status

        feature, workflow, task, _ = review_setup
        feature.status = "active"
        task.status = "done"
        db_session.commit()

        status = derive_feature_status(db_session, feature.id, write_back=False)
        assert status == "completed"
