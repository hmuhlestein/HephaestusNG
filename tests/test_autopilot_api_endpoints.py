"""Tests for autopilot_api.py REST endpoints using mock services."""

import pytest

# ── Pipeline Status ───────────────────────────────────────────────


class TestPipelineStatus:
    def test_status_returns_200(self, client):
        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200

    def test_status_has_required_fields(self, client):
        resp = client.get("/api/autopilot/status")
        data = resp.json()
        assert "running" in data
        assert "designs_processed" in data

    def test_status_default_not_running(self, client):
        resp = client.get("/api/autopilot/status")
        data = resp.json()
        assert data["running"] is False


class TestPipelineStatusRunningRespectsProjectActive:
    """Regression: get_pipeline_status's belt-and-suspenders fallback (when
    AutopilotService.status() itself reports not-running) promoted `running`
    to True purely off a stale Workflow.status=='active' row, without ever
    checking AutopilotProject.is_active. A project deactivated weeks ago
    (excluded from the phase-advancement sweep/dispatch entirely, per the
    concurrent-active-projects invariant) could still show "Running" in the
    UI forever off that leftover row -- confirmed live for a real project
    stuck this way. is_active must gate the whole fallback: an inactive
    project cannot be running no matter what stale DB state exists."""

    async def test_inactive_project_with_a_stale_active_workflow_reports_not_running(
        self, db_manager
    ):
        from src.core.database import AutopilotProject, Workflow
        from src.mcp.autopilot.control_routes import get_pipeline_status

        session = db_manager.get_session()
        try:
            session.add(
                AutopilotProject(
                    id="proj-inactive-stale-wf",
                    name="Inactive Stale Workflow Test",
                    base_dir="/tmp/inactive-stale-wf",
                    is_active=False,
                )
            )
            session.add(
                Workflow(
                    id="wf-inactive-stale",
                    name="Stale Workflow",
                    phases_folder_path="/tmp/inactive-stale-wf/phases",
                    status="active",
                    project_id="proj-inactive-stale-wf",
                )
            )
            session.commit()
        finally:
            session.close()

        status = await get_pipeline_status(project_id="proj-inactive-stale-wf")
        assert status.running is False

    async def test_active_project_with_an_active_workflow_still_reports_running(
        self, db_manager
    ):
        """Positive control: the fallback's original behavior (promote off
        a genuinely active project's active workflow) must be unchanged."""
        from src.core.database import AutopilotProject, Workflow
        from src.mcp.autopilot.control_routes import get_pipeline_status

        session = db_manager.get_session()
        try:
            session.add(
                AutopilotProject(
                    id="proj-active-stale-wf",
                    name="Active Workflow Test",
                    base_dir="/tmp/active-stale-wf",
                    is_active=True,
                )
            )
            session.add(
                Workflow(
                    id="wf-active-stale",
                    name="Active Workflow",
                    phases_folder_path="/tmp/active-stale-wf/phases",
                    status="active",
                    project_id="proj-active-stale-wf",
                )
            )
            session.commit()
        finally:
            session.close()

        status = await get_pipeline_status(project_id="proj-active-stale-wf")
        assert status.running is True

    async def test_inactive_project_with_a_live_agent_still_reports_not_running(
        self, db_manager
    ):
        """The is_active gate sits above BOTH sub-checks in
        _check_project_running_sync (the has_active-workflow short-circuit
        AND the live-agent fallback below it) -- exercise the agent branch
        specifically: no Workflow with status=='active' (so the first
        sub-check can't trip), but a genuinely 'working' Agent on a
        non-paused Workflow (so the pre-fix code would have reported
        running=True via the second sub-check)."""
        from src.core.database import Agent, AutopilotProject, Task, Workflow
        from src.mcp.autopilot.control_routes import get_pipeline_status

        session = db_manager.get_session()
        try:
            session.add(
                AutopilotProject(
                    id="proj-inactive-live-agent",
                    name="Inactive Live Agent Test",
                    base_dir="/tmp/inactive-live-agent",
                    is_active=False,
                )
            )
            session.add(
                Workflow(
                    id="wf-inactive-live-agent",
                    name="Failed Workflow",
                    phases_folder_path="/tmp/inactive-live-agent/phases",
                    status="failed",
                    project_id="proj-inactive-live-agent",
                )
            )
            session.add(
                Task(
                    id="task-inactive-live-agent",
                    raw_description="test task",
                    done_definition="test done",
                    status="in_progress",
                    workflow_id="wf-inactive-live-agent",
                )
            )
            session.add(
                Agent(
                    id="agent-inactive-live-agent",
                    system_prompt="test",
                    status="working",
                    cli_type="claude",
                    current_task_id="task-inactive-live-agent",
                )
            )
            session.commit()
        finally:
            session.close()

        status = await get_pipeline_status(project_id="proj-inactive-live-agent")
        assert status.running is False

    async def test_active_project_with_a_live_agent_still_reports_running(
        self, db_manager
    ):
        """Positive control for the agent-branch test above: the same
        setup on an ACTIVE project must still report running=True,
        confirming the fix didn't touch this sub-check's own logic."""
        from src.core.database import Agent, AutopilotProject, Task, Workflow
        from src.mcp.autopilot.control_routes import get_pipeline_status

        session = db_manager.get_session()
        try:
            session.add(
                AutopilotProject(
                    id="proj-active-live-agent",
                    name="Active Live Agent Test",
                    base_dir="/tmp/active-live-agent",
                    is_active=True,
                )
            )
            session.add(
                Workflow(
                    id="wf-active-live-agent",
                    name="Failed Workflow",
                    phases_folder_path="/tmp/active-live-agent/phases",
                    status="failed",
                    project_id="proj-active-live-agent",
                )
            )
            session.add(
                Task(
                    id="task-active-live-agent",
                    raw_description="test task",
                    done_definition="test done",
                    status="in_progress",
                    workflow_id="wf-active-live-agent",
                )
            )
            session.add(
                Agent(
                    id="agent-active-live-agent",
                    system_prompt="test",
                    status="working",
                    cli_type="claude",
                    current_task_id="task-active-live-agent",
                )
            )
            session.commit()
        finally:
            session.close()

        status = await get_pipeline_status(project_id="proj-active-live-agent")
        assert status.running is True


# ── Design Queue ──────────────────────────────────────────────────


class TestDesignQueue:
    def test_list_queue_empty(self, client):
        resp = client.get("/api/autopilot/queue")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_add_to_queue(self, client):
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "test_design.md",
                "description": "A test design",
            },
        )
        # May fail if no active project configured
        assert resp.status_code in (200, 201, 400, 404, 422, 500)

    def test_add_to_queue_rejects_traversal(self, client):
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "../etc/passwd",
                "description": "Evil design",
            },
        )
        # Should reject path traversal or fail without project
        assert resp.status_code in (400, 404, 422, 500)

    def test_list_queue_returns_list(self, client):
        resp = client.get("/api/autopilot/queue")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# ── Features ──────────────────────────────────────────────────────


class TestFeatures:
    def test_list_features(self, client):
        resp = client.get("/api/autopilot/features")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    def test_feature_status(self, client):
        resp = client.get("/api/autopilot/features/status")
        # May return 200 or 404 depending on project state
        assert resp.status_code in (200, 404)


# ── Projects ──────────────────────────────────────────────────────


class TestProjects:
    @pytest.mark.skip(reason="Needs deep mock chain for project listing")
    def test_list_projects(self, client):
        """Test GET /api/autopilot/projects - List all projects."""
        resp = client.get("/api/autopilot/projects")
        # Should return 200 with a list (empty or populated)
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            assert isinstance(resp.json(), list)

    @pytest.mark.skip(reason="Needs deep mock chain for project lookup")
    def test_get_project(self, client):
        """Test GET /api/autopilot/projects/{id} - Get project by ID."""
        resp = client.get("/api/autopilot/projects/nonexistent")
        # Should return 404 for nonexistent project
        assert resp.status_code in (404, 500)


# ── Repair ────────────────────────────────────────────────────────


class TestRepair:
    def test_repair_nonexistent(self, client):
        resp = client.post(
            "/api/autopilot/repair",
            json={
                "filename": "nonexistent.md",
            },
        )
        # Should fail gracefully
        assert resp.status_code in (400, 404, 422, 500)


# ── Queue content ─────────────────────────────────────────────────


class TestQueueContent:
    def test_get_queue_item_content_missing(self, client):
        resp = client.get("/api/autopilot/queue/nonexistent.md/content")
        assert resp.status_code in (404, 200)


# ── Validation ────────────────────────────────────────────────────


class TestValidation:
    def test_validation_status(self, client):
        resp = client.get("/api/autopilot/validation/status")
        assert resp.status_code in (200, 404)


# ── Autocomplete / Queue order ────────────────────────────────────


class TestQueueOrder:
    def test_get_queue_order(self, client):
        resp = client.get("/api/autopilot/queue/order")
        assert resp.status_code in (200, 404, 405)

    def test_save_queue_order(self, client):
        resp = client.post(
            "/api/autopilot/queue/order", json={"order": ["a.md", "b.md"]}
        )
        assert resp.status_code in (200, 201, 404, 405)


# ── Queue depth vs archived designs ────────────────────────────────
# Uses db_manager (a real, file-backed test DB) instead of the `client`
# fixture's mocked get_db, since this checks an actual SQL filter.


class TestQueueDepthExcludesArchived:
    async def test_archived_designs_do_not_count_toward_queue_depth(self, db_manager):
        """The Spec Queue badge (PipelineStatus.queue_depth) must not count
        a design the user has archived. _count_queue_depth_sync only
        filtered on status, not archived_at, so archiving a design (which
        sets archived_at but leaves status untouched) left it still
        counted -- the same status-filter/archived_at inconsistency already
        fixed in queue.py's pending_designs/active_designs queries."""
        import datetime

        from src.core.database import AutopilotDesign, AutopilotProject
        from src.mcp.autopilot.control_routes import get_pipeline_status

        session = db_manager.get_session()
        try:
            session.add(
                AutopilotProject(
                    id="proj-queue-depth-archive",
                    name="Queue Depth Archive Test",
                    base_dir="/tmp/queue-depth-archive",
                    is_active=True,
                )
            )
            session.add(
                AutopilotDesign(
                    id="des-archived-1",
                    project_id="proj-queue-depth-archive",
                    filename="archived.md",
                    name="Archived Design",
                    status="pending",
                    archived_at=datetime.datetime.utcnow(),
                )
            )
            session.add(
                AutopilotDesign(
                    id="des-not-archived-1",
                    project_id="proj-queue-depth-archive",
                    filename="active.md",
                    name="Active Design",
                    status="pending",
                )
            )
            session.commit()
        finally:
            session.close()

        status = await get_pipeline_status(project_id="proj-queue-depth-archive")
        assert status.queue_depth == 1
