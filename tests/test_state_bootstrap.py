"""Coverage for the startup steps extracted out of ServerState (SOLID 1.6).

migrate_is_active_column and load_active_project used to be ServerState
methods, touching only self.db_manager. Neither had any direct test before
this -- ServerState.initialize() exercises them, but only as a side effect of
constructing the whole server, which does not pin their own edge cases
(no projects yet, an is_default project, no is_default project).
"""

from types import SimpleNamespace

import pytest

from src.core.database import (
    AutopilotProject,
    DatabaseManager,
    Phase,
    PhaseExecution,
    Workflow,
)
from src.mcp.server.state_bootstrap import (
    load_active_project,
    migrate_is_active_column,
    resync_incomplete_phase_prompts_from_yaml,
)


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


class _FakePhase:
    """Minimal stand-in for sdk.models.Phase -- only the fields
    resync_incomplete_phase_prompts_from_yaml actually reads."""

    def __init__(self, name, description, done_definitions=None, additional_notes=None, outputs=None, next_steps=None):
        self.name = name
        self.description = description
        self.done_definitions = done_definitions or []
        self.additional_notes = additional_notes
        self.outputs = outputs
        self.next_steps = next_steps


class _FakeDefinition:
    def __init__(self, id, phases):
        self.id = id
        self.phases = phases


class TestResyncIncompletePhasePromptsFromYaml:
    """Phase.description/additional_notes/etc. are snapshotted from YAML
    once, at workflow-creation time, and never re-read afterward -- a
    prompt fix landed in config/workflows/*.yaml has zero effect on any
    already-created workflow. This resync closes that gap on every
    startup, mirroring migrate_is_active_column's own convention."""

    def _seed(self, db, *, execution_status="pending", launch_params=None, definition_id="fake_wf", workflow_status="active"):
        session = db.get_session()
        try:
            session.add(
                Workflow(
                    id="wf1",
                    name="Test Workflow",
                    phases_folder_path="/tmp/wf1",
                    definition_id=definition_id,
                    launch_params=launch_params,
                    status=workflow_status,
                )
            )
            session.add(
                Phase(
                    id="phase1",
                    workflow_id="wf1",
                    order=1,
                    name="my_phase",
                    description="OLD description",
                    done_definitions=["old criterion"],
                    additional_notes="OLD notes",
                    outputs="[\"old.md\"]",
                    next_steps="[\"old next\"]",
                )
            )
            if execution_status is not None:
                session.add(
                    PhaseExecution(
                        id="exec1",
                        phase_id="phase1",
                        workflow_execution_id="wf1",
                        status=execution_status,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _fresh_phase(self, **overrides):
        defaults = dict(
            name="my_phase",
            description="NEW description",
            done_definitions=["new criterion"],
            additional_notes="NEW notes",
            outputs=["new.md"],
            next_steps=["new next"],
        )
        defaults.update(overrides)
        return _FakePhase(**defaults)

    def _get_phase(self, db):
        session = db.get_session()
        try:
            return session.query(Phase).filter_by(id="phase1").first()
        finally:
            session.close()

    def test_resyncs_a_pending_phase_from_current_yaml(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="pending")
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase()])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)

        phase = self._get_phase(db)
        assert phase.description == "NEW description"
        assert phase.done_definitions == ["new criterion"]
        assert phase.additional_notes == "NEW notes"
        assert phase.outputs == '["new.md"]'
        assert phase.next_steps == '["new next"]'

    def test_completed_phase_is_left_untouched(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="completed")
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase()])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)

        phase = self._get_phase(db)
        assert phase.description == "OLD description"
        assert phase.additional_notes == "OLD notes"

    def test_skipped_phase_is_left_untouched(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="skipped")
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase()])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)

        phase = self._get_phase(db)
        assert phase.description == "OLD description"

    def test_a_phase_with_no_execution_row_is_still_resynced(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status=None)
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase()])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)

        phase = self._get_phase(db)
        assert phase.description == "NEW description"

    def test_launch_params_are_reapplied(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="pending", launch_params={"project_name": "Sotto"})
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase(description="Building {project_name}")])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)

        phase = self._get_phase(db)
        assert phase.description == "Building Sotto"

    def test_unknown_definition_id_is_skipped_without_raising(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="pending", definition_id="does_not_exist")
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase()])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)  # must not raise

        phase = self._get_phase(db)
        assert phase.description == "OLD description"

    def test_is_idempotent(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="pending")
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase()])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)
        resync_incomplete_phase_prompts_from_yaml(db)  # must not raise or change anything further

        phase = self._get_phase(db)
        assert phase.description == "NEW description"

    def test_a_completed_workflows_phases_are_never_scanned(self, db, monkeypatch):
        """The workflow-level status!='completed' filter is a performance
        optimization (skip querying phases for workflows that can't have
        anything left to resync), not just a per-phase check -- lock in
        that it still behaves correctly even in the edge case of a
        completed workflow with a stray non-terminal PhaseExecution row."""
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="pending", workflow_status="completed")
        monkeypatch.setattr(
            workflow_registry,
            "get_all_workflow_definitions",
            lambda: [_FakeDefinition("fake_wf", [self._fresh_phase()])],
        )

        resync_incomplete_phase_prompts_from_yaml(db)

        phase = self._get_phase(db)
        assert phase.description == "OLD description"

    def test_a_definitions_load_failure_is_swallowed_rather_than_raised(self, db, monkeypatch):
        import src.workflow_registry as workflow_registry

        self._seed(db, execution_status="pending")

        def _explode():
            raise RuntimeError("yaml parse error")

        monkeypatch.setattr(workflow_registry, "get_all_workflow_definitions", _explode)

        resync_incomplete_phase_prompts_from_yaml(db)  # must not raise

        phase = self._get_phase(db)
        assert phase.description == "OLD description"
