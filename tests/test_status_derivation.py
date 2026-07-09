"""Tests for centralized status derivation functions.

Tests for src/core/status_derivation.py (H-3 fix).
"""


import pytest

from src.core.database import (
    AutopilotDesign,
    DatabaseManager,
    Feature,
    Task,
    Workflow,
)
from src.core.status_derivation import (
    derive_design_status,
    derive_feature_status,
    derive_workflow_status,
)


@pytest.fixture
def db_manager(tmp_path):
    """Create a test database manager."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _create_design(session, design_id="design-1", status="active"):
    """Helper to create an AutopilotDesign for tests."""
    from src.core.database import AutopilotProject
    
    # Create project first (required for design)
    project = AutopilotProject(
        id="project-1",
        name="Test Project",
        base_dir="/tmp/test-project",
        is_active=True,
    )
    session.add(project)
    
    design = AutopilotDesign(
        id=design_id,
        project_id="project-1",
        name="Test Design",
        filename="test.md",
        status=status,
    )
    session.add(design)
    return design


class TestDeriveFeatureStatus:
    """Tests for derive_feature_status function."""
    
    def test_returns_paused_when_feature_paused(self, db_manager):
        """Should return 'paused' when feature is explicitly paused."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="paused",
            )
            session.add(feature)
        
        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "paused"
    
    def test_returns_completed_when_all_tasks_done(self, db_manager):
        """Should return 'completed' when all tasks are done."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)
            
            for i in range(3):
                task = Task(
                    id=f"task-{i}",
                    workflow_id="wf-1",
                    raw_description=f"Task {i}",
                    done_definition="Done",
                    status="done",
                )
                session.add(task)
        
        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "completed"
    
    def test_returns_active_when_tasks_in_progress(self, db_manager):
        """Should return 'active' when some tasks are in progress."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)
            
            # Mix of done and in_progress tasks
            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task 1",
                done_definition="Done",
                status="done",
            )
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="Task 2",
                done_definition="Done",
                status="in_progress",
            )
            session.add(task1)
            session.add(task2)
        
        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "active"
    
    def test_returns_failed_when_all_tasks_failed(self, db_manager):
        """Should return 'failed' when all tasks are failed."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)
            
            for i in range(2):
                task = Task(
                    id=f"task-{i}",
                    workflow_id="wf-1",
                    raw_description=f"Task {i}",
                    done_definition="Done",
                    status="failed",
                )
                session.add(task)
        
        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "failed"
    
    def test_excludes_diagnostic_tasks(self, db_manager):
        """Should exclude DIAGNOSTIC: prefixed tasks from status derivation."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)
            
            # Regular done task
            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Regular task",
                done_definition="Done",
                status="done",
            )
            session.add(task1)
            
            # Diagnostic task (should be excluded)
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="DIAGNOSTIC: Health check",
                done_definition="Done",
                status="in_progress",  # Would make feature "active" if included
            )
            session.add(task2)
        
        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        # Should be completed because diagnostic task is excluded
        assert result == "completed"
    
    def test_self_heals_stale_status(self, db_manager):
        """Should update feature status when it disagrees with derived status."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",  # Stale - should be completed
            )
            session.add(feature)
            
            task = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task",
                done_definition="Done",
                status="done",
            )
            session.add(task)
        
        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1", write_back=True)
        
        assert result == "completed"
        
        # Verify status was self-healed
        with db_manager.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feat-1").first()
            assert feature.status == "completed"

    def test_pending_feature_with_no_workflow_ignores_unrelated_null_workflow_tasks(
        self, db_manager
    ):
        """Regression: Task.workflow_id == feature.workflow_id becomes
        Task.workflow_id IS NULL when a feature hasn't had its own
        workflow launched yet (workflow_id is None) -- that matches every
        OTHER task in the system with a null workflow_id, not "no tasks
        for this feature". Observed live: leftover SDK/API test tasks
        created without a workflow_id, all status="failed", made every
        not-yet-started feature derive (and self-heal write back) status
        "failed" before it had ever actually run -- a freshly decomposed
        design's features all showed "failed" immediately, with no
        workflow, no error, no started_at."""
        with db_manager.session_scope() as session:
            _create_design(session)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id=None,
                status="pending",
            )
            session.add(feature)

            # Unrelated stray task with no workflow_id -- must not be
            # attributed to this feature.
            session.add(
                Task(
                    id="stray-task",
                    workflow_id=None,
                    raw_description="Test task without a workflow",
                    done_definition="Done",
                    status="failed",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1", write_back=True)

        assert result == "pending"
        with db_manager.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feat-1").first()
            assert feature.status == "pending"


class TestDeriveWorkflowStatus:
    """Tests for derive_workflow_status function."""
    
    def test_returns_paused_when_workflow_paused(self, db_manager):
        """Should return 'paused' when workflow is explicitly paused."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="paused", phases_folder_path="/tmp/phases")
            session.add(wf)
        
        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "paused"
    
    def test_returns_completed_when_all_tasks_done(self, db_manager):
        """Should return 'completed' when all tasks are done."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            for i in range(3):
                task = Task(
                    id=f"task-{i}",
                    workflow_id="wf-1",
                    raw_description=f"Task {i}",
                    done_definition="Done",
                    status="done",
                )
                session.add(task)
        
        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "completed"
    
    def test_returns_active_when_tasks_mixed(self, db_manager):
        """Should return 'active' when tasks have mixed statuses."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task 1",
                done_definition="Done",
                status="done",
            )
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="Task 2",
                done_definition="Done",
                status="pending",
            )
            session.add(task1)
            session.add(task2)
        
        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "active"


class TestDeriveDesignStatus:
    """Tests for derive_design_status function."""
    
    def test_returns_pending_when_no_features(self, db_manager):
        """Should return design's DB status when no features exist."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1", status="pending")
        
        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "pending"
    
    def test_returns_completed_when_all_features_completed(self, db_manager):
        """Should return 'completed' when all features are completed."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1")
            
            wf = Workflow(id="wf-1", name="Test", status="completed", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="feature-1",
                name="Feature 1",
                scope="Scope 1",
                workflow_id="wf-1",
                status="completed",
            )
            session.add(feature)
            
            # Add done task so derive_feature_status returns completed
            task = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task",
                done_definition="Done",
                status="done",
            )
            session.add(task)
        
        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "completed"
    
    def test_returns_active_when_any_feature_active(self, db_manager):
        """Should return 'active' when any feature is active."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1")
            
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            
            # Completed feature
            feature1 = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="feature-1",
                name="Feature 1",
                scope="Scope 1",
                workflow_id="wf-1",
                status="completed",
            )
            session.add(feature1)
            
            # Active feature
            feature2 = Feature(
                id="feat-2",
                design_id="design-1",
                feature_key="feature-2",
                name="Feature 2",
                scope="Scope 2",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature2)
            
            # Tasks for completed feature
            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task 1",
                done_definition="Done",
                status="done",
            )
            session.add(task1)
            
            # Tasks for active feature
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="Task 2",
                done_definition="Done",
                status="in_progress",
            )
            session.add(task2)
        
        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "active"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
