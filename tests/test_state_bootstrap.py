"""Coverage for the startup steps extracted out of ServerState (SOLID 1.6).

migrate_is_active_column and load_active_project used to be ServerState
methods, touching only self.db_manager. Neither had any direct test before
this -- ServerState.initialize() exercises them, but only as a side effect of
constructing the whole server, which does not pin their own edge cases
(no projects yet, an is_default project, no is_default project).
"""

from types import SimpleNamespace

import pytest

from src.core.database import AutopilotProject, DatabaseManager
from src.mcp.server.state_bootstrap import load_active_project, migrate_is_active_column


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "bootstrap.db"))
    manager.create_tables()
    return manager


def _config():
    return SimpleNamespace(
        git=SimpleNamespace(main_repo_path=None),
        paths=SimpleNamespace(project_root=None),
    )


class TestMigrateIsActiveColumn:
    def test_is_idempotent(self, db):
        """create_tables() already includes is_active on a fresh DB, so this
        exercises the "column already exists" branch -- the common case on
        every subsequent startup, not just the first."""
        migrate_is_active_column(db)
        migrate_is_active_column(db)  # must not raise


class TestLoadActiveProject:
    def test_no_projects_leaves_config_untouched(self, db):
        config = _config()
        load_active_project(db, config)
        assert config.git.main_repo_path is None
        assert config.paths.project_root is None

    def test_an_already_active_project_is_applied_to_config(self, db):
        session = db.get_session()
        session.add(AutopilotProject(id="p1", name="proj", base_dir="/tmp/p1", is_active=True))
        session.commit()
        session.close()

        config = _config()
        load_active_project(db, config)

        assert str(config.git.main_repo_path) == "/tmp/p1"
        assert str(config.paths.project_root) == "/tmp/p1"

    def test_the_default_project_is_auto_activated_when_none_is_active(self, db):
        session = db.get_session()
        session.add(AutopilotProject(id="p1", name="not-default", base_dir="/tmp/p1"))
        session.add(
            AutopilotProject(id="p2", name="default", base_dir="/tmp/p2", is_default=True)
        )
        session.commit()
        session.close()

        config = _config()
        load_active_project(db, config)

        assert str(config.git.main_repo_path) == "/tmp/p2"
        session = db.get_session()
        try:
            assert session.query(AutopilotProject).filter_by(id="p2").first().is_active is True
            assert session.query(AutopilotProject).filter_by(id="p1").first().is_active is not True
        finally:
            session.close()

    def test_the_first_project_is_auto_activated_when_no_default_exists(self, db):
        session = db.get_session()
        session.add(AutopilotProject(id="p1", name="only", base_dir="/tmp/p1"))
        session.commit()
        session.close()

        config = _config()
        load_active_project(db, config)

        assert str(config.git.main_repo_path) == "/tmp/p1"

    def test_a_db_failure_is_swallowed_rather_than_raised(self, tmp_path):
        """Server startup must not fail over this -- it degrades to no
        project activated rather than crashing."""

        class ExplodingDB:
            def get_session(self):
                raise RuntimeError("db unavailable")

        config = _config()
        load_active_project(ExplodingDB(), config)  # must not raise
        assert config.git.main_repo_path is None
