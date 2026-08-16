"""Tests for background_phase_advancement_sweep (src/mcp/server.py).

Regression: _advance_phases was only ever invoked from inside
run_single_workflow's own polling loop, which lives and dies with that
specific async call. A backend restart killed it, and nothing re-created a
poller for an already-launched workflow -- the startup resume path
(_resume_interrupted_workflows) only restarts orphaned agents, not phase
advancement. Observed live: a workflow's task finished successfully hours
before this fix, but its phase never advanced past it, because nothing was
polling _advance_phases for that workflow anymore.

This sweep is a generic, restart-safe safety net: it runs independently of
any specific run and periodically calls _advance_phases for every workflow
with status active/paused.
"""

import asyncio

import pytest

from src.core.database import AutopilotProject, DatabaseManager, Workflow


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_workflow(db_manager, wf_id, status):
    with db_manager.session_scope() as session:
        session.add(
            Workflow(
                id=wf_id,
                name="t",
                status=status,
                phases_folder_path="/tmp",
            )
        )


def _make_active_project_with_workflow(db_manager, project_id, workflow_id):
    with db_manager.session_scope() as session:
        session.add(
            AutopilotProject(
                id=project_id,
                name=project_id,
                base_dir=f"/tmp/{project_id}",
                is_active=True,
            )
        )
        session.add(
            Workflow(
                id=workflow_id,
                name=workflow_id,
                status="active",
                project_id=project_id,
                phases_folder_path="/tmp",
            )
        )


class TestBackgroundPhaseAdvancementSweep:
    @pytest.mark.asyncio
    async def test_advances_every_active_or_paused_workflow(self, db_manager, monkeypatch):
        from src.mcp import server

        _make_workflow(db_manager, "wf-active", "active")
        _make_workflow(db_manager, "wf-paused", "paused")
        _make_workflow(db_manager, "wf-completed", "completed")
        _make_workflow(db_manager, "wf-failed", "failed")

        monkeypatch.setattr(server.server_state, "db_manager", db_manager)
        server.server_state.shutdown_event = asyncio.Event()

        advanced_ids = []

        def fake_advance_phases(wf_id, logger):
            advanced_ids.append(wf_id)
            # Stop the sweep after this single pass over the DB snapshot.
            server.server_state.shutdown_event.set()

        monkeypatch.setattr(
            "src.autopilot.orchestrator.phase_transitions.py._advance_phases", fake_advance_phases
        )

        await server.background_phase_advancement_sweep()

        assert set(advanced_ids) == {"wf-active", "wf-paused"}

    @pytest.mark.asyncio
    async def test_one_workflow_error_does_not_block_others(self, db_manager, monkeypatch):
        """A single workflow raising must not stop the sweep from advancing
        the rest -- one bad workflow shouldn't take down every other
        workflow's advancement for that cycle."""
        from src.mcp import server

        _make_workflow(db_manager, "wf-broken", "active")
        _make_workflow(db_manager, "wf-fine", "active")

        monkeypatch.setattr(server.server_state, "db_manager", db_manager)
        server.server_state.shutdown_event = asyncio.Event()

        advanced_ids = []

        def fake_advance_phases(wf_id, logger):
            if wf_id == "wf-broken":
                server.server_state.shutdown_event.set()
                raise RuntimeError("boom")
            advanced_ids.append(wf_id)

        monkeypatch.setattr(
            "src.autopilot.orchestrator.phase_transitions.py._advance_phases", fake_advance_phases
        )

        await server.background_phase_advancement_sweep()

        assert "wf-fine" in advanced_ids


class TestSweepSelfHealing:
    """Regression: dead-agent cleanup (_clean_stale_assigned_tasks) and
    failed-task retry (_retry_failed_tasks) used to run only once, at
    pipeline-startup, for whichever single workflow happened to be the
    last-tracked current_workflow_id (attempt_recovery's only caller, in
    run_continuous_pipeline). Any other in-flight workflow -- parallel
    feature runs, or one resumed outside that one startup check -- never
    got either: a task whose agent died mid-work just sat "assigned"/
    "in_progress" forever. Wiring both into the generic per-tick sweep
    makes self-healing universal instead of tied to one specific caller."""

    @pytest.mark.asyncio
    async def test_runs_self_healing_for_active_workflows_only(
        self, db_manager, monkeypatch
    ):
        from src.mcp import server

        _make_workflow(db_manager, "wf-active", "active")
        _make_workflow(db_manager, "wf-paused", "paused")

        monkeypatch.setattr(server.server_state, "db_manager", db_manager)
        server.server_state.shutdown_event = asyncio.Event()

        cleaned_ids = []
        retried_ids = []

        def fake_advance_phases(wf_id, logger):
            if wf_id == "wf-paused":
                server.server_state.shutdown_event.set()

        def fake_clean(wf_id, logger):
            cleaned_ids.append(wf_id)

        def fake_retry(wf_id, logger):
            retried_ids.append(wf_id)
            return []

        monkeypatch.setattr(
            "src.autopilot.orchestrator.phase_transitions.py._advance_phases", fake_advance_phases
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.features.py._clean_stale_assigned_tasks", fake_clean
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.phase_transitions.py._retry_failed_tasks", fake_retry
        )

        await server.background_phase_advancement_sweep()

        assert cleaned_ids == ["wf-active", "wf-paused"]
        assert retried_ids == ["wf-active"]


class TestSweepMultiProjectScoping:
    """Part of the multi-project concurrency fix: the sweep used to scope
    itself to a single is_active=True project
    (.filter_by(is_active=True).first()), starving every OTHER active
    project's workflows -- observed live: applitnator's workflows sat idle
    for days because HephaestusNG was the sole active project. It must now
    process every currently-active project's workflows, not just one."""

    @pytest.mark.asyncio
    async def test_sweeps_workflows_across_all_active_projects(
        self, db_manager, monkeypatch
    ):
        from src.mcp import server

        _make_active_project_with_workflow(db_manager, "proj-a", "wf-a")
        _make_active_project_with_workflow(db_manager, "proj-b", "wf-b")

        monkeypatch.setattr(server.server_state, "db_manager", db_manager)
        server.server_state.shutdown_event = asyncio.Event()

        advanced_ids = []

        def fake_advance_phases(wf_id, logger):
            advanced_ids.append(wf_id)
            if len(advanced_ids) >= 2:
                server.server_state.shutdown_event.set()

        monkeypatch.setattr(
            "src.autopilot.orchestrator.phase_transitions.py._advance_phases", fake_advance_phases
        )

        await server.background_phase_advancement_sweep()

        assert set(advanced_ids) == {"wf-a", "wf-b"}

    @pytest.mark.asyncio
    async def test_does_not_sweep_workflows_of_an_inactive_project(
        self, db_manager, monkeypatch
    ):
        from src.mcp import server

        _make_active_project_with_workflow(db_manager, "proj-active", "wf-in-scope")
        with db_manager.session_scope() as session:
            session.add(
                AutopilotProject(
                    id="proj-inactive",
                    name="proj-inactive",
                    base_dir="/tmp/proj-inactive",
                    is_active=False,
                )
            )
            session.add(
                Workflow(
                    id="wf-out-of-scope",
                    name="wf-out-of-scope",
                    status="active",
                    project_id="proj-inactive",
                    phases_folder_path="/tmp",
                )
            )

        monkeypatch.setattr(server.server_state, "db_manager", db_manager)
        server.server_state.shutdown_event = asyncio.Event()

        advanced_ids = []

        def fake_advance_phases(wf_id, logger):
            advanced_ids.append(wf_id)
            server.server_state.shutdown_event.set()

        monkeypatch.setattr(
            "src.autopilot.orchestrator.phase_transitions.py._advance_phases", fake_advance_phases
        )

        await server.background_phase_advancement_sweep()

        assert advanced_ids == ["wf-in-scope"]
