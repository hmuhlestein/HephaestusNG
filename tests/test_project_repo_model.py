"""ProjectRepo model: unique constraints (project_id, path) and
(project_id, label), and repo_id columns on Task/Ticket/TicketCommit/
AgentWorktree/Feature. REQ-01, REQ-02, REQ-03."""

import pytest
from sqlalchemy.exc import IntegrityError

from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_project(db_manager, project_id="proj-1", base_dir="/tmp/repo-a"):
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id=project_id, name="p", base_dir=base_dir))


def test_project_repo_path_is_absolute_and_optional_under_base_dir(db_manager):
    _seed_project(db_manager)
    with db_manager.session_scope() as session:
        session.add(
            ProjectRepo(id="repo-1", project_id="proj-1", label="backend", path="/elsewhere/backend", is_primary=True)
        )
    with db_manager.session_scope() as session:
        repo = session.query(ProjectRepo).filter_by(id="repo-1").first()
        assert repo.path == "/elsewhere/backend"


def test_duplicate_path_within_project_raises_integrity_error(db_manager):
    _seed_project(db_manager)
    with db_manager.session_scope() as session:
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="backend", path="/tmp/repo-a", is_primary=True))
    with pytest.raises(IntegrityError):
        with db_manager.session_scope() as session:
            session.add(ProjectRepo(id="repo-2", project_id="proj-1", label="frontend", path="/tmp/repo-a"))


def test_duplicate_label_within_project_raises_integrity_error(db_manager):
    _seed_project(db_manager)
    with db_manager.session_scope() as session:
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="backend", path="/tmp/a", is_primary=True))
    with pytest.raises(IntegrityError):
        with db_manager.session_scope() as session:
            session.add(ProjectRepo(id="repo-2", project_id="proj-1", label="backend", path="/tmp/b"))


def test_same_label_across_different_projects_is_allowed(db_manager):
    _seed_project(db_manager, project_id="proj-1", base_dir="/tmp/a")
    _seed_project(db_manager, project_id="proj-2", base_dir="/tmp/b")
    with db_manager.session_scope() as session:
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="backend", path="/tmp/a", is_primary=True))
        session.add(ProjectRepo(id="repo-2", project_id="proj-2", label="backend", path="/tmp/b", is_primary=True))
    with db_manager.session_scope() as session:
        assert session.query(ProjectRepo).count() == 2


def test_project_repos_relationship_cascade_deletes(db_manager):
    _seed_project(db_manager)
    with db_manager.session_scope() as session:
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="backend", path="/tmp/a", is_primary=True))
    with db_manager.session_scope() as session:
        project = session.query(AutopilotProject).filter_by(id="proj-1").first()
        assert len(project.repos) == 1
        session.delete(project)
    with db_manager.session_scope() as session:
        assert session.query(ProjectRepo).count() == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
