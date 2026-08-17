"""Tests for FrontendAPI.get_workflow_info's workflow-selection logic.

Regression coverage for a real bug found live: get_workflow_info() picked
its workflow via `session.query(Workflow).first()` with no ORDER BY.
Workflow.id is a UUID-string primary key, so this returned whatever row
SQLite's B-tree happened to store first for that key -- effectively an
arbitrary workflow, unrelated to "current" or "selected". This made the
Overview dashboard's "Active Phase Distribution" card show a random
workflow's phases (sometimes a single-phase Phase 0 run, sometimes an
unrelated pipeline) with no way to pick which, and selectedExecutionId
(tracked in the frontend) was never actually sent to the backend at all.
"""

from datetime import datetime, timedelta

import pytest

from src.core.database import DatabaseManager, Phase, Workflow
from src.mcp.frontend._shared import FrontendAPI


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


@pytest.fixture
def frontend_api(db):
    return FrontendAPI(db_manager=db, agent_manager=None)


def _make_workflow(db, workflow_id, status, created_at, phase_name="Some Phase"):
    session = db.get_session()
    session.add(
        Workflow(
            id=workflow_id,
            name=f"wf-{workflow_id}",
            phases_folder_path="/tmp",
            status=status,
            created_at=created_at,
        )
    )
    session.add(
        Phase(
            id=f"phase-{workflow_id}",
            workflow_id=workflow_id,
            order=1,
            name=phase_name,
            description="d",
            done_definitions=["done"],
        )
    )
    session.commit()
    session.close()


class TestGetWorkflowInfoSelection:
    @pytest.mark.asyncio
    async def test_explicit_workflow_id_is_honored(self, db, frontend_api):
        now = datetime.utcnow()
        _make_workflow(db, "wf-old", "active", now - timedelta(hours=1), "Old Phase")
        _make_workflow(db, "wf-new", "active", now, "New Phase")

        result = await frontend_api.get_workflow_info(workflow_id="wf-old")

        assert result["id"] == "wf-old"
        assert result["phases"][0]["name"] == "Old Phase"

    @pytest.mark.asyncio
    async def test_no_workflow_id_defaults_to_most_recent_active(
        self, db, frontend_api
    ):
        """Regression: must not be arbitrary UUID-order .first() -- must
        deterministically prefer the most recently created active workflow."""
        now = datetime.utcnow()
        _make_workflow(db, "aaa-older", "active", now - timedelta(minutes=10))
        _make_workflow(db, "zzz-newer", "active", now)

        result = await frontend_api.get_workflow_info()

        assert result["id"] == "zzz-newer"

    @pytest.mark.asyncio
    async def test_falls_back_to_most_recent_when_none_active(
        self, db, frontend_api
    ):
        now = datetime.utcnow()
        _make_workflow(db, "wf-completed-old", "completed", now - timedelta(hours=2))
        _make_workflow(db, "wf-completed-new", "completed", now)

        result = await frontend_api.get_workflow_info()

        assert result["id"] == "wf-completed-new"

    @pytest.mark.asyncio
    async def test_no_workflows_at_all_returns_empty_state(self, db, frontend_api):
        result = await frontend_api.get_workflow_info()

        assert result["id"] is None
        assert result["phases"] == []

    @pytest.mark.asyncio
    async def test_phase0_workflow_is_selectable_by_id(self, db, frontend_api):
        """The original symptom: a Phase 0 workflow (single 'Feature
        Architect' phase) must be viewable when explicitly selected, not
        just whichever workflow happened to win an arbitrary ordering."""
        now = datetime.utcnow()
        _make_workflow(
            db, "phase0-wf", "active", now - timedelta(minutes=5), "Feature Architect"
        )
        _make_workflow(db, "feature-wf", "active", now, "product_requirements")

        result = await frontend_api.get_workflow_info(workflow_id="phase0-wf")

        assert result["phases"][0]["name"] == "Feature Architect"
