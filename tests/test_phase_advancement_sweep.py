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

from src.core.database import DatabaseManager, Workflow


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
            "src.autopilot.orchestrator._advance_phases", fake_advance_phases
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
            "src.autopilot.orchestrator._advance_phases", fake_advance_phases
        )

        await server.background_phase_advancement_sweep()

        assert "wf-fine" in advanced_ids
