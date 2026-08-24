"""Feature.repo_id set/backfill + create_task mismatch rejection -- REQ-19.

_resolve_task_repo_id (src/mcp/server/_create_task_steps.py) is what makes
"every Feature bound to exactly one repo" a WRITE-time-enforced invariant:
an explicit repo_id is validated to belong to the task's own project before
the Task row is persisted (WARNING-1), and a task/Feature repo_id mismatch
is rejected with HTTPException(400) before the write reaches the Task
table (BLOCKER).
"""

import uuid

import pytest
from fastapi import HTTPException

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Feature,
    ProjectRepo,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


def _seed_multi_repo_project(db_manager, tmp_path):
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-backend", project_id="proj-1", label="backend", path=str(backend), is_primary=True))
        session.add(ProjectRepo(id="repo-frontend", project_id="proj-1", label="frontend", path=str(frontend)))
        session.add(AutopilotDesign(id="design-1", project_id="proj-1", filename="d.md", name="d"))
    return backend, frontend


def _seed_workflow_for_feature(db_manager, feature_id, workflow_id="wf-1"):
    with db_manager.session_scope() as session:
        session.add(Workflow(id=workflow_id, name="w", status="active", phases_folder_path="/tmp", project_id="proj-1", feature_id=feature_id))
    return workflow_id


def _make_request(workflow_id=None, repo_id=None, cwd=None):
    from src.mcp.server._shared import CreateTaskRequest

    return CreateTaskRequest(
        task_description="do work",
        done_definition="done",
        ai_agent_id="agent-1",
        workflow_id=workflow_id,
        repo_id=repo_id,
        cwd=cwd,
    )


class TestResolveTaskRepoId:
    def test_no_workflow_id_returns_explicit_repo_id_unchanged(self, db_manager):
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        with db_manager.session_scope() as session:
            assert _resolve_task_repo_id(session, _make_request(repo_id="repo-x")) == "repo-x"

    def test_explicit_repo_id_validated_and_persisted(self, db_manager, tmp_path):
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        _seed_multi_repo_project(db_manager, tmp_path)
        _seed_workflow_for_feature(db_manager, feature_id=None)

        with db_manager.session_scope() as session:
            resolved = _resolve_task_repo_id(session, _make_request(workflow_id="wf-1", repo_id="repo-frontend"))
        assert resolved == "repo-frontend"

    def test_explicit_repo_id_from_a_different_project_is_rejected(self, db_manager, tmp_path):
        """WARNING-1: an explicit repo_id must belong to the task's OWN
        project -- checked synchronously before the Task row is persisted."""
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        _seed_multi_repo_project(db_manager, tmp_path)
        _seed_workflow_for_feature(db_manager, feature_id=None)
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-2", name="p2", base_dir=str(other_dir)))
            session.add(ProjectRepo(id="repo-other-project", project_id="proj-2", label="other", path=str(other_dir), is_primary=True))

        with db_manager.session_scope() as session:
            with pytest.raises(HTTPException) as exc_info:
                _resolve_task_repo_id(session, _make_request(workflow_id="wf-1", repo_id="repo-other-project"))
        assert exc_info.value.status_code == 400

    def test_repo_id_inferred_from_cwd_when_not_explicit(self, db_manager, tmp_path):
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        backend, frontend = _seed_multi_repo_project(db_manager, tmp_path)
        _seed_workflow_for_feature(db_manager, feature_id=None)

        with db_manager.session_scope() as session:
            resolved = _resolve_task_repo_id(
                session, _make_request(workflow_id="wf-1", cwd=str(frontend / "src" / "App.tsx"))
            )
        assert resolved == "repo-frontend"

    def test_task_under_feature_with_matching_repo_id_is_allowed(self, db_manager, tmp_path):
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        _seed_multi_repo_project(db_manager, tmp_path)
        with db_manager.session_scope() as session:
            session.add(Feature(id="feat-1", design_id="design-1", feature_key="fe", name="FE", scope="s", repo_id="repo-frontend"))
        _seed_workflow_for_feature(db_manager, feature_id="feat-1")

        with db_manager.session_scope() as session:
            resolved = _resolve_task_repo_id(session, _make_request(workflow_id="wf-1", repo_id="repo-frontend"))
        assert resolved == "repo-frontend"

    def test_task_under_feature_with_conflicting_repo_id_is_rejected(self, db_manager, tmp_path):
        """BLOCKER: a task explicitly assigned a DIFFERENT repo than its
        Feature must be rejected before the Task row is written -- not
        logged, not silently allowed."""
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        _seed_multi_repo_project(db_manager, tmp_path)
        with db_manager.session_scope() as session:
            session.add(Feature(id="feat-2", design_id="design-1", feature_key="fe", name="FE", scope="s", repo_id="repo-frontend"))
        _seed_workflow_for_feature(db_manager, feature_id="feat-2")

        with db_manager.session_scope() as session:
            with pytest.raises(HTTPException) as exc_info:
                _resolve_task_repo_id(session, _make_request(workflow_id="wf-1", repo_id="repo-backend"))
        assert exc_info.value.status_code == 400
        assert "conflicts" in exc_info.value.detail

    def test_task_inherits_features_repo_id_when_unset_on_task(self, db_manager, tmp_path):
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        _seed_multi_repo_project(db_manager, tmp_path)
        with db_manager.session_scope() as session:
            session.add(Feature(id="feat-3", design_id="design-1", feature_key="fe", name="FE", scope="s", repo_id="repo-frontend"))
        _seed_workflow_for_feature(db_manager, feature_id="feat-3")

        with db_manager.session_scope() as session:
            resolved = _resolve_task_repo_id(session, _make_request(workflow_id="wf-1"))
        assert resolved == "repo-frontend"

    def test_first_task_backfills_unset_feature_repo_id_and_second_mismatched_task_is_rejected(
        self, db_manager, tmp_path
    ):
        """The ONLY case where "first task wins": Feature.repo_id is unset
        (inference at feature-creation time had nothing to work with) --
        the first task with a concrete repo_id backfills it, and every
        subsequent task is validated against that now-set value."""
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        _seed_multi_repo_project(db_manager, tmp_path)
        with db_manager.session_scope() as session:
            session.add(Feature(id="feat-4", design_id="design-1", feature_key="fe", name="FE", scope="s", repo_id=None))
        _seed_workflow_for_feature(db_manager, feature_id="feat-4")

        with db_manager.session_scope() as session:
            resolved_first = _resolve_task_repo_id(session, _make_request(workflow_id="wf-1", repo_id="repo-frontend"))
        assert resolved_first == "repo-frontend"

        with db_manager.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feat-4").first()
            assert feature.repo_id == "repo-frontend"

        with db_manager.session_scope() as session:
            with pytest.raises(HTTPException):
                _resolve_task_repo_id(session, _make_request(workflow_id="wf-1", repo_id="repo-backend"))

    def test_single_repo_project_byte_identical_no_repo_id_anywhere(self, db_manager, tmp_path):
        """No ProjectRepo rows / no repo_id anywhere involved -- regression
        guard that the single-repo path is untouched by this change."""
        from src.mcp.server._create_task_steps import _resolve_task_repo_id

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id="proj-single", name="p", base_dir=str(tmp_path)))
            session.add(Workflow(id="wf-single", name="w", status="active", phases_folder_path="/tmp", project_id="proj-single"))

        with db_manager.session_scope() as session:
            resolved = _resolve_task_repo_id(session, _make_request(workflow_id="wf-single"))
        assert resolved is None


class TestPersistNewTaskWritesRepoId:
    @pytest.mark.asyncio
    async def test_task_row_persisted_with_resolved_repo_id(self, db_manager, tmp_path, monkeypatch):
        from src.core.database import Task
        from src.mcp.server._create_task_steps import _persist_new_task
        from src.mcp.server._shared import server_state

        monkeypatch.setattr(server_state, "db_manager", db_manager)
        _seed_multi_repo_project(db_manager, tmp_path)
        _seed_workflow_for_feature(db_manager, feature_id=None)

        task_id = str(uuid.uuid4())
        _persist_new_task("agent-1", _make_request(workflow_id="wf-1", repo_id="repo-frontend"), task_id)

        with db_manager.session_scope() as session:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.repo_id == "repo-frontend"

    @pytest.mark.asyncio
    async def test_conflicting_repo_id_never_reaches_the_task_table(self, db_manager, tmp_path, monkeypatch):
        from src.core.database import Task
        from src.mcp.server._create_task_steps import _persist_new_task
        from src.mcp.server._shared import server_state

        monkeypatch.setattr(server_state, "db_manager", db_manager)
        _seed_multi_repo_project(db_manager, tmp_path)
        with db_manager.session_scope() as session:
            session.add(Feature(id="feat-5", design_id="design-1", feature_key="fe", name="FE", scope="s", repo_id="repo-frontend"))
        _seed_workflow_for_feature(db_manager, feature_id="feat-5")

        task_id = str(uuid.uuid4())
        with pytest.raises(HTTPException):
            _persist_new_task("agent-1", _make_request(workflow_id="wf-1", repo_id="repo-backend"), task_id)

        with db_manager.session_scope() as session:
            assert session.query(Task).filter_by(id=task_id).first() is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
