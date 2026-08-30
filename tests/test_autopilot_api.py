"""Tests for autopilot API endpoints."""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock, patch

import pytest


@pytest.fixture
def autopilot_dirs(tmp_path):
    """Create temp queue, features, and state directories."""
    queue_dir = tmp_path / "queue"
    features_dir = tmp_path / "features"
    state_dir = tmp_path / "state"
    queue_dir.mkdir()
    features_dir.mkdir()
    state_dir.mkdir()

    from src.autopilot import repair_service as repair_service_mod
    from src.mcp.autopilot import _shared as api_mod
    from src.mcp.autopilot import intervention_routes

    api_mod._cache.clear()

    # AUTOPILOT_STATE_DIR is imported into each module that reads it, so the
    # rebind must fan out to every reader's module namespace. queue_routes
    # no longer reads it directly -- rerun/repair moved into
    # repair_service.py (SOLID review 1.11), which reads it instead.
    state_modules = (api_mod, intervention_routes, repair_service_mod)
    old_state = {m: m.AUTOPILOT_STATE_DIR for m in state_modules}
    old_queue = api_mod.DESIGN_QUEUE_DIR
    old_features = api_mod.FEATURES_DIR

    for m in state_modules:
        m.AUTOPILOT_STATE_DIR = str(state_dir)
    api_mod.configure_autopilot_api(
        design_queue_dir=str(queue_dir),
        features_dir=str(features_dir),
    )

    yield {
        "queue": queue_dir,
        "features": features_dir,
        "state": state_dir,
    }

    for m in state_modules:
        m.AUTOPILOT_STATE_DIR = old_state[m]
    api_mod.DESIGN_QUEUE_DIR = old_queue
    api_mod.FEATURES_DIR = old_features
    api_mod._cache.clear()


@pytest.fixture
def client(autopilot_dirs, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.core.database import DatabaseManager
    from src.mcp.autopilot import _shared as autopilot_api
    from src.mcp.autopilot import control_routes
    from src.mcp.autopilot import router

    # Route handlers that call DatabaseManager(None) (e.g. list_features'
    # _scan_features) read HEPHAESTUS_TEST_DB fresh on every call -- point
    # it at a real file, not the session-wide ":memory:" default (conftest.py),
    # so a test's own DB writes and the route handler's own DatabaseManager(None)
    # instantiation see the same data. Each ":memory:" DatabaseManager gets its
    # own isolated, uncached connection (see DatabaseManager's own docstring),
    # so two separate ":memory:" instances never share rows.
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    DatabaseManager(str(db_path)).create_tables()

    app = FastAPI()
    app.include_router(router)

    # Mock _get_active_project_id to return None (no DB needed) — patch it in
    # every module that resolves it from its own globals
    monkeypatch.setattr(autopilot_api, "_get_active_project_id", lambda: None)
    monkeypatch.setattr(control_routes, "_get_active_project_id", lambda: None)

    # _cache is a module-level dict shared by every test that imports this
    # module -- an earlier test's cached "features"/etc. entry (still within
    # its TTL) would otherwise leak into this test's response.
    autopilot_api._cache.clear()

    return TestClient(app)


# ── Path Traversal ───────────────────────────────────────────────


class TestPathTraversal:
    def test_queue_delete_rejects_traversal(self, client):
        resp = client.delete("/api/autopilot/queue/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_queue_content_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/queue/../../etc/passwd/content")
        assert resp.status_code in (400, 404)

    def test_feature_detail_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/features/../../etc")
        assert resp.status_code in (400, 404)

    def test_feature_report_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/features/../../etc/report")
        assert resp.status_code in (400, 404)

    def test_feature_artifact_rejects_traversal(self, client):
        resp = client.get("/api/autopilot/features/foo/../../etc/passwd")
        assert resp.status_code in (400, 404)

    def test_queue_add_rejects_path_in_name(self, client):
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "../../etc/cron",
                "content": "test",
            },
        )
        if resp.status_code == 200:
            data = resp.json()
            assert ".." not in data["filename"]


# ── Design Queue ─────────────────────────────────────────────────


class TestDesignQueue:
    def test_empty_queue(self, client):
        resp = client.get("/api/autopilot/queue")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_add_and_list(self, client):
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "Test Feature",
                "content": "# Design\nHello world",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Feature"
        assert data["extension"] == ".md"

        resp = client.get("/api/autopilot/queue")
        assert len(resp.json()) == 1
        assert resp.json()[0]["filename"] == data["filename"]

    def test_add_duplicate_rejects(self, client):
        client.post(
            "/api/autopilot/queue",
            json={
                "name": "Dup Test",
                "content": "content",
            },
        )
        resp = client.post(
            "/api/autopilot/queue",
            json={
                "name": "Dup Test",
                "content": "content",
            },
        )
        assert resp.status_code == 409

    def test_delete(self, client, autopilot_dirs):
        f = autopilot_dirs["queue"] / "to_delete.md"
        f.write_text("content")

        resp = client.delete("/api/autopilot/queue/to_delete.md")
        assert resp.status_code == 200
        assert not f.exists()

    def test_delete_nonexistent(self, client):
        resp = client.delete("/api/autopilot/queue/nope.md")
        assert resp.status_code == 404

    def test_content(self, client, autopilot_dirs):
        f = autopilot_dirs["queue"] / "readme.md"
        f.write_text("# Hello")

        resp = client.get("/api/autopilot/queue/readme.md/content")
        assert resp.status_code == 200
        assert resp.json()["content"] == "# Hello"

    def test_reorder(self, client, autopilot_dirs):
        for name in ["a.md", "b.md", "c.md"]:
            (autopilot_dirs["queue"] / name).write_text(name)

        resp = client.post(
            "/api/autopilot/queue/reorder", json={"filenames": ["c.md", "a.md", "b.md"]}
        )
        assert resp.status_code == 200

        resp = client.get("/api/autopilot/queue")
        filenames = [i["filename"] for i in resp.json()]
        assert filenames == ["c.md", "a.md", "b.md"]

    def test_reorder_rejects_unknown(self, client, autopilot_dirs):
        (autopilot_dirs["queue"] / "a.md").write_text("a")

        resp = client.post(
            "/api/autopilot/queue/reorder", json={"filenames": ["a.md", "ghost.md"]}
        )
        assert resp.status_code == 400


class TestQueueRerun:
    """Rerun must drive the in-process AutopilotService singleton (the same
    one the play/pause button uses), not spawn/kill a separate
    `python -m src.autopilot.orchestrator` subprocess. The subprocess path
    could run concurrently with the in-process service -- both independently
    calling run_phase0 -- which was the root cause of design docs getting
    copied twice.
    """

    def test_rerun_uses_in_process_service_not_subprocess(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        import subprocess as subprocess_mod

        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "specs").mkdir(parents=True)
        design_file = project_dir / ".hephaestus" / "specs" / "my_design.md"
        design_file.write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        popen_mock = Mock(side_effect=AssertionError("subprocess.Popen must not be called by rerun"))
        monkeypatch.setattr(subprocess_mod, "Popen", popen_mock)

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 200, resp.text
        popen_mock.assert_not_called()
        fake_service.start.assert_called_once()
        call_kwargs = fake_service.start.call_args.kwargs
        assert call_kwargs["project_path"] == str(project_dir)
        assert not (Path(autopilot_dirs["state"]) / "orchestrator.pid").exists()

    def test_rerun_stops_running_service_before_restarting(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "specs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "specs" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = True  # already running -- rerun must stop it first
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 200, resp.text
        fake_service.stop.assert_called_once()
        fake_service.start.assert_called_once()

    def test_rerun_maps_runtime_error_to_409_not_400(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        """service.start() raising RuntimeError means "already running" (its
        own docstring: "Raises: RuntimeError: If pipeline is already
        running") -- matches /start's own convention of mapping that to 409,
        not the generic 400 a first pass at this used. Real scenario: another
        request races in and starts something else between Step 1's stop()
        and Step 6's start()."""
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "specs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "specs" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(
            side_effect=RuntimeError("Pipeline is already running")
        )
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 409, resp.text

    def test_rerun_maps_value_error_to_400(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        """service.start() raising ValueError means bad input (e.g. project
        path isn't a git repo) -- matches /start's own convention of 400."""
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "specs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "specs" / "my_design.md").write_text("# Design")

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(
            side_effect=ValueError("Project path is not a git repository")
        )
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 400, resp.text

    def test_rerun_rejects_when_over_concurrency_cap(
        self, client, autopilot_dirs, monkeypatch, tmp_path
    ):
        """Regression: rerun_design used to call service.start() with no
        concurrency-cap check at all, unlike POST /start -- a "rerun" on a
        brand-new, not-yet-running project could silently exceed
        max_concurrent_projects even while POST /start would reject the
        identical project with a 409."""
        project_dir = tmp_path / "project"
        (project_dir / ".hephaestus" / "specs").mkdir(parents=True)
        (project_dir / ".hephaestus" / "specs" / "my_design.md").write_text("# Design")

        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._resolve_project_id", lambda project_path: None
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            lambda project_path: "proj-over-cap",
        )
        fake_registry = Mock()
        fake_registry.try_reserve.return_value = (
            False,
            "Max concurrent projects (2) reached: proj-a, proj-b. "
            "Stop one before starting another.",
        )
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "my_design.md", "project_path": str(project_dir)},
        )

        assert resp.status_code == 409, resp.text
        assert "proj-a" in resp.text

    def test_rerun_cleans_up_old_worktree_before_returning(
        self, project_client, monkeypatch, tmp_path
    ):
        """Regression: rerun deleted a design's Workflow rows (a "clean
        slate") but never removed the worktree directory on disk.
        _create_integration_worktree's per-design path is deterministic and
        unchanged by rerun, and only creates fresh `if not wt_path.exists()`
        -- so the next run silently reused the OLD worktree's stale commits
        instead of actually starting over. This must synchronously clean up
        the worktree as part of rerun, not rely on the best-effort
        background branch sweep (Step 3), which races the orchestrator's
        own worktree creation for the freshly-reset design."""
        client, dirs = project_client
        project_dir = dirs["project_dir"]
        design_file = dirs["design_dir"] / "01-auth.md"

        worktree = project_dir / ".worktrees" / "wt_feature-auth"
        worktree.mkdir(parents=True)
        (worktree / ".git").mkdir()

        from src.core.database import Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-rerun-1", name="autopilot", phases_folder_path="/tmp",
                    status="failed", definition_id="autopilot",
                    working_directory=str(worktree),
                    launch_params={
                        "design_document": str(design_file),
                        "project_path": str(project_dir),
                    },
                )
            )

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._resolve_project_id", lambda project_path: "proj-fixed"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            lambda project_path: "proj-fixed",
        )

        with patch("src.autopilot.orchestrator.worktree_integration._cleanup_worktree") as mock_cleanup:
            resp = client.post(
                "/api/autopilot/queue/rerun",
                json={"filename": "01-auth.md", "project_path": str(project_dir)},
            )

        assert resp.status_code == 200, resp.text
        mock_cleanup.assert_called_once()
        call_args = mock_cleanup.call_args[0]
        assert call_args[0] == worktree
        assert str(call_args[2]) == str(project_dir)

    def test_rerun_does_not_touch_an_unrelated_designs_agents(
        self, project_client, monkeypatch, tmp_path
    ):
        """Regression: Step 2 used to query Agent/Workflow with NO scoping
        at all -- db.query(Agent).filter(Agent.status.in_(["working",
        "starting", "idle"])) terminated every active agent and paused
        every active workflow SYSTEM-WIDE, across every other project and
        design, every time anyone reran any one design. It also never
        reset the Task rows those agents were working on -- only the
        Agent row -- leaving them "assigned"/"in_progress" pointing at a
        now-terminated agent, indistinguishable from one still genuinely
        working. Observed live: rerunning one stuck design's Phase 0
        silently killed a healthy, unrelated feature's adversarial_review
        agent mid-review, after it had already written a complete, correct
        report -- the agent couldn't report completion (correctly rejected
        as already-terminated) and the feature's workflow burned through
        its entire retry budget and failed with no visible cause.

        Verifies both halves: an agent/task/workflow belonging to a
        completely unrelated design must survive untouched, while an
        agent/task/workflow belonging to a FEATURE spawned from the design
        actually being rerun (not just its Phase 0 workflow, which Step 2b
        deletes wholesale) must still be stopped and properly reset."""
        client, dirs = project_client
        project_dir = dirs["project_dir"]

        from src.core.database import (
            Agent,
            AutopilotDesign,
            AutopilotProject,
            Task,
            Workflow,
            get_db,
        )

        with get_db() as db:
            target_proj = AutopilotProject(id="proj-target", name="target", base_dir=str(project_dir))
            db.add(target_proj)
            target_design = AutopilotDesign(
                id="des-target", project_id="proj-target", filename="01-auth.md", name="Auth", status="active",
            )
            db.add(target_design)
            # A FEATURE workflow spawned from the design being rerun -- not
            # a Phase 0 workflow, so Step 2b's own hard-delete (scoped to
            # DESIGN_WORKFLOW_DEFINITION_IDS) never touches it. Step 2 must
            # stop it directly.
            db.add(Workflow(
                id="wf-target-feature", name="autopilot", phases_folder_path="/tmp",
                status="active", definition_id="autopilot", design_id="des-target",
            ))
            db.add(Task(
                id="task-target", workflow_id="wf-target-feature", phase_id=None,
                raw_description="x", done_definition="x", status="in_progress", assigned_agent_id="agent-target",
            ))
            db.add(Agent(id="agent-target", status="working", current_task_id="task-target", cli_type="claude", system_prompt="x"))

            # A completely unrelated project/design -- must survive.
            other_proj = AutopilotProject(id="proj-other", name="other", base_dir=str(tmp_path / "otherproject"))
            db.add(other_proj)
            other_design = AutopilotDesign(
                id="des-other", project_id="proj-other", filename="other.md", name="Other", status="active",
            )
            db.add(other_design)
            db.add(Workflow(
                id="wf-other-feature", name="autopilot", phases_folder_path="/tmp",
                status="active", definition_id="autopilot", design_id="des-other",
            ))
            db.add(Task(
                id="task-other", workflow_id="wf-other-feature", phase_id=None,
                raw_description="x", done_definition="x", status="in_progress", assigned_agent_id="agent-other",
            ))
            db.add(Agent(id="agent-other", status="working", current_task_id="task-other", cli_type="claude", system_prompt="x"))

        fake_service = Mock()
        fake_service.running = False
        fake_service.start = AsyncMock(return_value={"started": True})
        fake_service.stop = AsyncMock(return_value={"stopped": True})
        monkeypatch.setattr(
            "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._resolve_project_id", lambda project_path: "proj-target"
        )
        monkeypatch.setattr(
            "src.autopilot.orchestrator.state._get_or_create_project_id",
            lambda project_path: "proj-target",
        )

        resp = client.post(
            "/api/autopilot/queue/rerun",
            json={"filename": "01-auth.md", "project_path": str(project_dir)},
        )
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            other_agent = db.query(Agent).filter_by(id="agent-other").first()
            other_task = db.query(Task).filter_by(id="task-other").first()
            other_wf = db.query(Workflow).filter_by(id="wf-other-feature").first()
            assert other_agent.status == "working", "unrelated design's agent must not be touched"
            assert other_task.status == "in_progress"
            assert other_task.assigned_agent_id == "agent-other"
            assert other_wf.status == "active"

            target_agent = db.query(Agent).filter_by(id="agent-target").first()
            target_task = db.query(Task).filter_by(id="task-target").first()
            target_wf = db.query(Workflow).filter_by(id="wf-target-feature").first()
            assert target_agent.status == "terminated"
            assert target_task.status == "pending", "task must be reset, not left dangling"
            assert target_task.assigned_agent_id is None
            assert target_wf.status == "paused"


# ── Caching ──────────────────────────────────────────────────────


class TestCaching:
    def test_queue_caching(self, client, autopilot_dirs):
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        (autopilot_dirs["queue"] / "cached.md").write_text("x")

        resp1 = client.get("/api/autopilot/queue")
        assert len(resp1.json()) == 1

        (autopilot_dirs["queue"] / "new.md").write_text("y")
        resp2 = client.get("/api/autopilot/queue")
        assert len(resp2.json()) == 1  # cached

    def test_add_invalidates_cache(self, client, autopilot_dirs):
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        client.get("/api/autopilot/queue")

        client.post(
            "/api/autopilot/queue",
            json={
                "name": "Cache Test",
                "content": "x",
            },
        )

        resp = client.get("/api/autopilot/queue")
        assert len(resp.json()) == 1


# ── Multi-project queue-dir scoping ────────────────────────────────


@pytest.fixture
def two_project_client(tmp_path, monkeypatch):
    """Test client wired to a real DB with two distinct AutopilotProject
    rows, each with its own design-queue directory -- for verifying
    _get_effective_queue_dir(project_id) resolves the RIGHT project's
    directory instead of silently falling back to whichever one is
    is_active."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)

    from src.core.database import AutopilotProject, DatabaseManager

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    proj_a_dir = tmp_path / "proj-a"
    proj_b_dir = tmp_path / "proj-b"
    proj_a_dir.mkdir()
    proj_b_dir.mkdir()

    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-a", name="proj-a", base_dir=str(proj_a_dir), is_active=True))
        session.add(AutopilotProject(id="proj-b", name="proj-b", base_dir=str(proj_b_dir), is_active=True))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    from src.mcp.autopilot import _shared as api_mod

    api_mod._cache.clear()
    api_mod._queue_dir_by_project.clear()
    api_mod._features_dir_by_project.clear()

    yield client, proj_a_dir, proj_b_dir

    api_mod._cache.clear()
    api_mod._queue_dir_by_project.clear()
    api_mod._features_dir_by_project.clear()


class TestQueueDirProjectScoping:
    """Regression: with two projects both is_active, _get_effective_queue_dir()
    (no project_id) silently resolved EVERY caller against whichever ONE
    project happened to be picked by is_active's .first() -- these prove
    project_id, once passed, resolves that project specifically and keeps
    the two projects' queues fully isolated."""

    def test_add_and_list_are_isolated_per_project(self, two_project_client):
        client, proj_a_dir, proj_b_dir = two_project_client

        resp = client.post(
            "/api/autopilot/queue",
            json={"name": "Design A", "content": "a", "project_id": "proj-a"},
        )
        assert resp.status_code == 200, resp.text

        resp = client.post(
            "/api/autopilot/queue",
            json={"name": "Design B", "content": "b", "project_id": "proj-b"},
        )
        assert resp.status_code == 200, resp.text

        queue_a = client.get("/api/autopilot/queue?project_id=proj-a").json()
        queue_b = client.get("/api/autopilot/queue?project_id=proj-b").json()

        assert [d["name"] for d in queue_a] == ["Design A"]
        assert [d["name"] for d in queue_b] == ["Design B"]

        # File actually landed under the RIGHT project's own directory.
        assert any(proj_a_dir.rglob("Design_A.md"))
        assert any(proj_b_dir.rglob("Design_B.md"))
        assert not any(proj_a_dir.rglob("Design_B.md"))
        assert not any(proj_b_dir.rglob("Design_A.md"))

    def test_remove_only_affects_its_own_project(self, two_project_client):
        client, proj_a_dir, proj_b_dir = two_project_client

        client.post(
            "/api/autopilot/queue",
            json={"name": "Shared Name", "content": "a", "project_id": "proj-a"},
        )
        client.post(
            "/api/autopilot/queue",
            json={"name": "Shared Name", "content": "b", "project_id": "proj-b"},
        )

        resp = client.delete("/api/autopilot/queue/Shared_Name.md?project_id=proj-a")
        assert resp.status_code == 200, resp.text

        assert client.get("/api/autopilot/queue?project_id=proj-a").json() == []
        assert len(client.get("/api/autopilot/queue?project_id=proj-b").json()) == 1


# ── Features ─────────────────────────────────────────────────────


class TestFeatures:
    def test_empty_features(self, client):
        resp = client.get("/api/autopilot/features")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_features(self, client, autopilot_dirs, tmp_path):
        """list_features reads from the Feature/Workflow DB tables (via
        _scan_features), not from FEATURES_DIR on disk -- that filesystem
        path is get_feature_detail's job, a different route entirely. Seed
        the DB rows the implementation actually reads. "completed" (not
        the old test's "validated", which the Feature.status CHECK
        constraint doesn't even permit) is what the current status model
        actually produces."""
        from types import SimpleNamespace

        import src.core.app_context as app_context
        from src.core.app_context import set_app_state
        from src.core.constants import CONTEXT_DIR_NAME
        from src.core.database import DatabaseManager, Feature, Workflow

        working_dir = tmp_path / "wf-working-dir"
        (working_dir / CONTEXT_DIR_NAME).mkdir(parents=True)
        (working_dir / CONTEXT_DIR_NAME / "feature_report.html").write_text(
            "<html>report</html>"
        )

        db = DatabaseManager(None)
        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-list-features",
                    name="t",
                    phases_folder_path="/tmp",
                    status="completed",
                    working_directory=str(working_dir),
                )
            )
            session.add(
                Feature(
                    id="feat-list-features",
                    design_id="design-list-features",
                    feature_key="my_feature",
                    name="my_feature",
                    scope="test feature",
                    status="completed",
                    workflow_id="wf-list-features",
                )
            )

        # _scan_features reads get_app_state().db_manager, not a fresh
        # DatabaseManager(None) -- register a fake state pointing at this
        # test's own db, restoring whatever was there before so this
        # doesn't leak into later tests in the session (see
        # test_broadcast_scoping_round2.py's fake_state fixture for the
        # same leak this mirrors).
        previous_state = app_context._app_state
        set_app_state(SimpleNamespace(db_manager=db))
        try:
            resp = client.get("/api/autopilot/features")
        finally:
            app_context._app_state = previous_state
        assert len(resp.json()) == 1
        assert resp.json()[0]["status"] == "completed"
        assert resp.json()[0]["has_report"] is True

    def test_list_features_scoped_to_project(self, client, tmp_path):
        """?project_id= returns only that project's features. The Completed
        tab is per-project; without the filter it showed every project's
        features (and counted them in the tab badge)."""
        from types import SimpleNamespace

        import src.core.app_context as app_context
        from src.core.app_context import set_app_state
        from src.core.database import (
            AutopilotDesign,
            AutopilotProject,
            DatabaseManager,
            Feature,
        )

        db = DatabaseManager(None)
        with db.session_scope() as session:
            for n in ("a", "b"):
                session.add(
                    AutopilotProject(
                        id=f"proj-{n}", name=f"project {n}", base_dir=str(tmp_path / n)
                    )
                )
                session.add(
                    AutopilotDesign(
                        id=f"design-{n}", project_id=f"proj-{n}", name=f"design {n}"
                    )
                )
                session.add(
                    Feature(
                        id=f"feat-{n}",
                        design_id=f"design-{n}",
                        feature_key=f"feature_{n}",
                        name=f"feature_{n}",
                        scope="test feature",
                        status="completed",
                    )
                )

        previous_state = app_context._app_state
        set_app_state(SimpleNamespace(db_manager=db))
        try:
            scoped = client.get("/api/autopilot/features?project_id=proj-a").json()
            unscoped = client.get("/api/autopilot/features").json()
        finally:
            app_context._app_state = previous_state

        assert [f["name"] for f in scoped] == ["feature_a"]
        # The Completed tab groups by spec, so every feature must carry its
        # parent design's identity, not just its own.
        assert scoped[0]["design_id"] == "design-a"
        assert scoped[0]["design_name"] == "design a"
        assert {f["name"] for f in unscoped} == {"feature_a", "feature_b"}

    def test_feature_detail_resolves_a_db_feature_row(self, client, tmp_path):
        """Every id /features lists is a Feature row id (feat-<uuid>), but
        get_feature_detail only resolved FEATURES_DIR directory names, so
        clicking any feature in the gallery 404'd -- a spinner, then an
        empty modal titled with the raw id."""
        from datetime import datetime

        from src.core.database import (
            AutopilotDesign,
            DatabaseManager,
            Feature,
            Workflow,
        )

        working_dir = tmp_path / "wt"
        (working_dir / "docs").mkdir(parents=True)
        (working_dir / "docs" / "architecture.md").write_text("the architecture")

        db = DatabaseManager(None)
        with db.session_scope() as session:
            session.add(
                AutopilotDesign(id="des-d", project_id="proj-d", name="My Spec")
            )
            session.add(
                Workflow(
                    id="wf-d",
                    name="t",
                    phases_folder_path="/tmp",
                    status="completed",
                    working_directory=str(working_dir),
                )
            )
            session.add(
                Feature(
                    id="feat-detail",
                    design_id="des-d",
                    feature_key="k",
                    name="My Feature",
                    scope="s",
                    status="completed",
                    workflow_id="wf-d",
                    started_at=datetime(2026, 1, 1, 0, 0, 0),
                    completed_at=datetime(2026, 1, 1, 0, 1, 30),
                )
            )

        body = client.get("/api/autopilot/features/feat-detail").json()
        assert body["name"] == "My Feature"
        assert body["design_name"] == "My Spec"
        assert body["architecture_summary"] == "the architecture"
        assert body["total_time_seconds"] == 90
        assert [d["name"] for d in body["docs"]] == ["architecture.md"]

    def test_feature_docs_come_from_the_archived_record_not_the_project(
        self, client, tmp_path
    ):
        """Once a completed feature's worktree is gone, its docs live in the
        archived features gallery. Falling straight through to the project
        path listed the whole project's docs/ instead -- which is also why
        the modal's Report tab found no report for a feature that has one."""
        import json

        from src.core.constants import CONTEXT_DIR_NAME
        from src.core.database import DatabaseManager, Feature, Workflow

        project = tmp_path / "proj"
        (project / "docs").mkdir(parents=True)
        (project / "docs" / "unrelated-project-doc.md").write_text("not the feature's")

        archived = project / CONTEXT_DIR_NAME / "features" / "20260101_my_spec"
        (archived / "docs").mkdir(parents=True)
        (archived / "docs" / "pipeline_metrics.json").write_text(
            json.dumps({"workflow_id": "wf-archived"})
        )
        (archived / "docs" / "feature_report.html").write_text("<html>report</html>")

        db = DatabaseManager(None)
        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-archived",
                    name="t",
                    phases_folder_path="/tmp",
                    status="completed",
                    working_directory=None,
                    launch_params={"project_path": str(project)},
                )
            )
            session.add(
                Feature(
                    id="feat-archived",
                    design_id="des-archived",
                    feature_key="k",
                    name="Archived Feature",
                    scope="s",
                    status="completed",
                    workflow_id="wf-archived",
                )
            )

        docs = client.get(
            "/api/autopilot/feature-records/feat-archived/docs"
        ).json()["docs"]
        names = [d["name"] for d in docs]
        assert "feature_report.html" in names
        assert "unrelated-project-doc.md" not in names

    def test_feature_report_comes_from_whichever_design_folder_has_it(
        self, client, tmp_path
    ):
        """doc_review files a feature's report under the design folder of the
        run that produced it, but every run makes a new folder and
        Feature.feature_record_path/AutopilotDesign.designs_folder still name
        an older one -- so the report existed and the modal showed nothing."""
        from src.core.database import AutopilotDesign, DatabaseManager, Feature

        specs = tmp_path / ".hephaestus" / "specs"
        recorded = specs / "20260101-000000_my_spec_des-x"
        later_run = specs / "20260102-000000_my_spec_des-x"
        (recorded / "features" / "k").mkdir(parents=True)
        (later_run / "features" / "k").mkdir(parents=True)
        (later_run / "features" / "k" / "feature_report-abc12345.html").write_text(
            "<html>the report</html>"
        )

        db = DatabaseManager(None)
        with db.session_scope() as session:
            session.add(
                AutopilotDesign(
                    id="des-x",
                    project_id="proj-x",
                    name="My Spec",
                    designs_folder=str(recorded),
                )
            )
            session.add(
                Feature(
                    id="feat-report",
                    design_id="des-x",
                    feature_key="k",
                    name="F",
                    scope="s",
                    status="completed",
                )
            )

        docs = client.get("/api/autopilot/feature-records/feat-report/docs").json()
        assert "feature_report.html" in [d["name"] for d in docs["docs"]]

        doc = client.get(
            "/api/autopilot/feature-records/feat-report/docs/feature_report.html"
        ).json()
        assert doc["content"] == "<html>the report</html>"

        raw = client.get("/api/autopilot/feature-records/feat-report/report")
        assert raw.status_code == 200
        assert "the report" in raw.text

    def test_feature_detail(self, client, autopilot_dirs):
        feature_dir = autopilot_dirs["features"] / "20260101-120000_detail_test"
        feature_dir.mkdir()
        docs = feature_dir / "docs"
        docs.mkdir()
        (docs / "pipeline_metrics.json").write_text(
            json.dumps(
                {
                    "product_validated": False,
                    "stop_reason": "max_iterations",
                    "qa_passed": False,
                }
            )
        )
        (docs / "qa.md").write_text("# QA Report\nSome content here")

        resp = client.get("/api/autopilot/features/20260101-120000_detail_test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_review"
        assert data["qa_passed"] is False
        assert len(data["qa_summary"]) > 0

    def test_feature_not_found(self, client):
        resp = client.get("/api/autopilot/features/nonexistent")
        assert resp.status_code == 404

    def test_feature_doc(self, client, autopilot_dirs):
        feature_dir = autopilot_dirs["features"] / "20260101-000000_art"
        feature_dir.mkdir()
        docs = feature_dir / "docs"
        docs.mkdir()
        (docs / "test.md").write_text("# Test document")

        resp = client.get("/api/autopilot/features/20260101-000000_art/docs/test.md")
        assert resp.status_code == 200
        assert resp.json()["content"] == "# Test document"


# ── Human Input ──────────────────────────────────────────────────


class TestHumanInput:
    def test_no_pending_input(self, client):
        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        assert resp.json() is None

    def test_submit_and_read(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_abc123.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "abc123",
                    "reason": "Test impasse",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": ["c", "s", "q"],
                    "labels": {"c": "Continue", "s": "Skip", "q": "Quit"},
                }
            )
        )

        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "abc123"
        assert data["reason"] == "Test impasse"

    def test_read_surfaces_workflow_id_and_decision_context(self, client, autopilot_dirs):
        """Regression: HumanInputRequest didn't declare workflow_id/phase_id,
        so BaseModel silently dropped them from the response even though
        arbitration.py's escalation request file always writes them --
        the frontend's row-correlation (which design/workflow is this
        request for?) never received them."""
        state_dir = autopilot_dirs["state"]
        (state_dir / "input_request_wf1.json").write_text(
            json.dumps({
                "id": "wf1", "reason": "r",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "options": ["c", "s"], "labels": {},
                "workflow_id": "wf-abc", "phase_id": "phase-xyz",
                "decision_context": {"phase_name": "design_review", "attempts": [], "distinct_options": []},
            })
        )

        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        data = resp.json()
        assert data["workflow_id"] == "wf-abc"
        assert data["phase_id"] == "phase-xyz"
        assert data["decision_context"]["phase_name"] == "design_review"

    def test_submit_response(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_def456.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "def456",
                    "reason": "Credits exhausted",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": ["c", "s", "q"],
                    "labels": {},
                }
            )
        )

        resp = client.post(
            "/api/autopilot/input",
            json={
                "request_id": "def456",
                "choice": "c",
            },
        )
        assert resp.status_code == 200

        response_file = state_dir / "input_response_def456.json"
        assert response_file.exists()
        data = json.loads(response_file.read_text())
        assert data["choice"] == "c"

    def test_submit_goto_choice_requires_target_phase(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        (state_dir / "input_request_g1.json").write_text(
            json.dumps({
                "id": "g1", "reason": "r",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "options": ["c", "s"], "labels": {},
            })
        )

        resp = client.post(
            "/api/autopilot/input",
            json={"request_id": "g1", "choice": "g"},
        )
        assert resp.status_code == 400

    def test_submit_goto_choice_with_target_phase_persists_it(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        (state_dir / "input_request_g2.json").write_text(
            json.dumps({
                "id": "g2", "reason": "r",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "options": ["c", "s"], "labels": {},
            })
        )

        resp = client.post(
            "/api/autopilot/input",
            json={"request_id": "g2", "choice": "g", "target_phase": "architecture_design"},
        )
        assert resp.status_code == 200

        response_file = state_dir / "input_response_g2.json"
        data = json.loads(response_file.read_text())
        assert data["choice"] == "g"
        assert data["target_phase"] == "architecture_design"

    def test_submit_invalid_choice(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        (state_dir / "input_request_x.json").write_text(
            json.dumps(
                {
                    "id": "x",
                    "reason": "r",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": [],
                    "labels": {},
                }
            )
        )

        resp = client.post(
            "/api/autopilot/input",
            json={
                "request_id": "x",
                "choice": "invalid",
            },
        )
        assert resp.status_code == 400

    def test_submit_to_missing_request(self, client):
        resp = client.post(
            "/api/autopilot/input",
            json={
                "request_id": "nonexistent",
                "choice": "c",
            },
        )
        assert resp.status_code == 404

    def test_dismiss(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]
        request_file = state_dir / "input_request_dismiss_me.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "dismiss_me",
                    "reason": "test",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "options": [],
                    "labels": {},
                }
            )
        )

        resp = client.delete("/api/autopilot/input/dismiss_me")
        assert resp.status_code == 200
        assert not request_file.exists()

    def test_stale_request_cleanup(self, client, autopilot_dirs):
        state_dir = autopilot_dirs["state"]

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        request_file = state_dir / "input_request_stale.json"
        request_file.write_text(
            json.dumps(
                {
                    "id": "stale",
                    "reason": "old request",
                    "timestamp": old_ts,
                    "options": [],
                    "labels": {},
                }
            )
        )

        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        assert resp.json() is None
        assert not request_file.exists()

    def test_arbitration_escalation_is_never_cleaned_up_as_stale(self, client, autopilot_dirs):
        """Regression: arbitration.py's _escalate_arbitration_deadlock_to_
        human docstring is explicit that it "deliberately does NOT time
        out" -- auto-continuing past an unresolved arbitration deadlock
        with no actual human decision defeats the point of escalating it.
        Before this fix, the generic 1-hour staleness cleanup below
        deleted an arbitration escalation's request file regardless of
        kind, and the orchestrator sweep treats a missing request file
        with no response as an explicit dismissal -- silently force-
        continuing a deadlocked phase nobody ever actually decided on."""
        state_dir = autopilot_dirs["state"]

        old_ts = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        request_file = state_dir / "input_request_arb1.json"
        request_file.write_text(
            json.dumps({
                "id": "arb1", "reason": "design_review deadlocked",
                "timestamp": old_ts, "options": ["c", "s"], "labels": {},
                "kind": "arbitration_escalation",
                "workflow_id": "wf-1", "phase_id": "phase-1",
            })
        )

        resp = client.get("/api/autopilot/input")
        assert resp.status_code == 200
        assert resp.json() is not None
        assert resp.json()["id"] == "arb1"
        assert request_file.exists()


# ── Pipeline Status ──────────────────────────────────────────────


class TestPipelineStatus:
    def test_status_default(self, client):
        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running"] is False
        assert data["designs_processed"] == 0

    def test_status_with_state(self, client, autopilot_dirs):
        """Pipeline state is DB-backed (PersistentPipelineState), not a
        state.json file -- control_routes.py's /status reads it directly."""
        from src.autopilot.orchestrator.state import PersistentPipelineState, PipelineState
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        state = PipelineState(
            designs_processed=5,
            designs_succeeded=4,
            designs_failed=1,
            current_design="My Feature",
            total_elapsed=3600,
        )
        PersistentPipelineState(project_id=None).save_state_only(state)

        resp = client.get("/api/autopilot/status")
        data = resp.json()
        assert data["designs_processed"] == 5
        assert data["current_design"] == "My Feature"

    def test_status_reports_which_project_is_running(self, client, autopilot_dirs, monkeypatch):
        """The (single, global) AutopilotService's running project must be
        surfaced so the frontend can tell the user what's actually running
        on a 409 conflict, instead of a generic "another project" message
        that's equally confusing whether it's a genuine cross-project
        conflict or the caller's own just-started run (a self-conflict from
        status-polling lag)."""
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        class FakeService:
            running = True

            def status(self):
                return {
                    "running": True,
                    "project_path": "/Users/test/some-project",
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        # No project_id given (global status) -- the handler asks the
        # registry for whatever's running, not a single global service.
        fake_registry = Mock()
        fake_registry.running.return_value = [FakeService()]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["running_project_path"] == "/Users/test/some-project"
        # No AutopilotProject DB row registered for this path (no DB wired
        # in this test) -- falls back to the directory basename.
        assert data["running_project_name"] == "some-project"

    def test_status_sums_design_counts_across_concurrently_running_projects(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Regression: with no project_id given, the global status endpoint
        used to report running_services[0].status() outright -- whichever
        project happened to be first in registry dict-iteration order.
        Concurrent projects are only possible at all since the multi-project
        concurrency diff; before that, there was only ever one service to
        ask. designs_processed/succeeded/failed must be summed across every
        running project, not just the first one's."""
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, processed, succeeded, failed):
                self.running = True
                self._processed = processed
                self._succeeded = succeeded
                self._failed = failed

            def status(self):
                return {
                    "running": True,
                    "project_path": "/Users/test/project-a",
                    "current_design": None,
                    "designs_processed": self._processed,
                    "designs_succeeded": self._succeeded,
                    "designs_failed": self._failed,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService(5, 4, 1),
            FakeService(3, 2, 1),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["designs_processed"] == 8
        assert data["designs_succeeded"] == 6
        assert data["designs_failed"] == 2

    def test_status_reports_every_running_project_not_just_the_first(
        self, client, autopilot_dirs, monkeypatch
    ):
        """running_project_path/running_project_name are singular fields
        that can't represent more than one concurrently running project --
        running_projects must list every one of them (id/name/base_dir),
        so a caller hitting the concurrency cap can identify and stop
        exactly the project(s) blocking it instead of a bare stop-all call
        that would also kill an unrelated project it was never told about."""
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, project_id, project_path):
                self.running = True
                self.project_id = project_id
                self._project_path = project_path

            def status(self):
                return {
                    "running": True,
                    "project_path": self._project_path,
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService("proj-a", "/Users/test/project-a"),
            FakeService("proj-b", "/Users/test/project-b"),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        running_projects = resp.json()["running_projects"]
        assert len(running_projects) == 2
        assert {p["id"] for p in running_projects} == {"proj-a", "proj-b"}
        assert {p["base_dir"] for p in running_projects} == {
            "/Users/test/project-a",
            "/Users/test/project-b",
        }

    def test_status_running_projects_survives_missing_project_id_attr(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Must not crash if a service object doesn't expose project_id
        (defensive -- real AutopilotService always sets it, but this
        endpoint shouldn't 500 on a test double or future refactor that
        doesn't)."""
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        class FakeService:
            running = True

            def status(self):
                return {
                    "running": True,
                    "project_path": "/Users/test/some-project",
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [FakeService()]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get("/api/autopilot/status")
        assert resp.status_code == 200
        assert resp.json()["running_projects"][0]["id"] is None

    def test_self_conflict_detected_even_when_not_first_in_running_list(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Regression: is_self_conflict only compared project_path against
        running_project_path, which (with no project_id given, exactly how
        the frontend's self-conflict check calls this endpoint) is just
        running_services[0]'s path -- arbitrary registry order. A caller
        whose own project IS running but isn't index 0 in that list got
        is_self_conflict=False, so the UI's 409 handler treated it as a
        genuine cross-project conflict and offered to "stop X and start X"."""
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, project_id, project_path):
                self.running = True
                self.project_id = project_id
                self._project_path = project_path

            def status(self):
                return {
                    "running": True,
                    "project_path": self._project_path,
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService("proj-a", "/Users/test/project-a"),
            FakeService("proj-b", "/Users/test/project-b"),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get(
            "/api/autopilot/status",
            params={"project_path": "/Users/test/project-b"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_self_conflict"] is True

    def test_self_conflict_false_for_a_genuinely_different_project(
        self, client, autopilot_dirs, monkeypatch
    ):
        """Sanity check the fix isn't overbroad: a project_path that matches
        none of the running projects must still report is_self_conflict=False."""
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        class FakeService:
            def __init__(self, project_id, project_path):
                self.running = True
                self.project_id = project_id
                self._project_path = project_path

            def status(self):
                return {
                    "running": True,
                    "project_path": self._project_path,
                    "current_design": None,
                    "designs_processed": 0,
                    "designs_succeeded": 0,
                    "designs_failed": 0,
                    "elapsed_seconds": 0,
                    "error": None,
                }

        fake_registry = Mock()
        fake_registry.running.return_value = [
            FakeService("proj-a", "/Users/test/project-a"),
            FakeService("proj-b", "/Users/test/project-b"),
        ]
        monkeypatch.setattr(
            "src.autopilot.service.get_registry", lambda: fake_registry
        )

        resp = client.get(
            "/api/autopilot/status",
            params={"project_path": "/Users/test/project-c"},
        )
        assert resp.status_code == 200
        assert resp.json()["is_self_conflict"] is False


# ── Messages ─────────────────────────────────────────────────────


class TestMessages:
    def test_messages_empty(self, client, autopilot_dirs):
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        resp = client.get("/api/autopilot/messages")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_messages_with_events(self, client, autopilot_dirs):
        """Events are DB-backed (AutopilotPipelineEvent rows), not an
        events.jsonl file -- message_routes.py's /messages reads them
        directly, most recent first, so the message list (and the pulsing
        "Waiting on you" badge for a fresh human_input_required row) shows
        newest activity at the top without the frontend needing its own
        sort."""
        from datetime import datetime

        from src.core.database import AutopilotPipelineEvent, get_db
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        with get_db() as db:
            db.add(
                AutopilotPipelineEvent(
                    event_type="design_started",
                    data={"name": "A"},
                    created_at=datetime(2026, 1, 1, 0, 0, 0),
                )
            )
            db.add(
                AutopilotPipelineEvent(
                    event_type="design_completed",
                    data={"name": "A"},
                    created_at=datetime(2026, 1, 1, 0, 1, 0),
                )
            )

        resp = client.get("/api/autopilot/messages?limit=10")
        assert len(resp.json()) == 2
        assert resp.json()[0]["type"] == "design_completed"
        assert resp.json()[1]["type"] == "design_started"


# ── Logs ─────────────────────────────────────────────────────────


class TestLogs:
    def test_logs_empty(self, client, autopilot_dirs):
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        resp = client.get("/api/autopilot/logs")
        assert resp.status_code == 200
        assert resp.json()["lines"] == []

    def test_logs_with_content(self, client, autopilot_dirs):
        from src.mcp.autopilot import _shared as api_mod

        api_mod._cache.clear()

        state_dir = autopilot_dirs["state"]
        run_dir = state_dir / "run-20260101"
        run_dir.mkdir(parents=True)
        (run_dir / "orchestrator.log").write_text(
            "Line 1\nLine 2\nLine 3\nLine 4\nLine 5\n"
        )

        resp = client.get("/api/autopilot/logs?lines=3")
        assert len(resp.json()["lines"]) == 3
        assert resp.json()["lines"][0] == "Line 3"


# ── Projects ─────────────────────────────────────────────────────


@pytest.fixture
def project_dirs(tmp_path):
    """Create a project directory with .hephaestus/specs containing test files."""
    project_dir = tmp_path / "myproject"
    design_dir = project_dir / ".hephaestus" / "specs"
    design_dir.mkdir(parents=True)
    # A project directory has to be a git repository -- creating one on a
    # plain directory is refused now (see _validate_base_dir), because
    # autopilot cannot make the worktree every phase runs in without it.
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)

    (design_dir / "01-auth.md").write_text("# Auth Design\nImplement OAuth2.")
    (design_dir / "02-payments.md").write_text("# Payments\nStripe integration.")
    (design_dir / "readme.txt").write_text("General readme.")

    return {
        "project_dir": project_dir,
        "design_dir": design_dir,
    }


@pytest.fixture
def project_client(tmp_path, project_dirs, monkeypatch):
    """Test client with a temporary database for project tests."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)

    from src.core.database import DatabaseManager

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app, headers={"X-Agent-ID": "system"})

    from src.mcp.autopilot import _shared as api_mod

    api_mod._cache.clear()

    yield client, project_dirs

    api_mod._cache.clear()


class TestProjects:
    def test_create_project(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test Project",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "Test Project"
        assert data["design_count"] == 3
        assert data["id"].startswith("proj-")

    def test_create_project_auto_syncs_designs(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = resp.json()["id"]

        resp = client.get(f"/api/autopilot/projects/{project_id}/designs")
        assert resp.status_code == 200
        designs = resp.json()
        assert len(designs) == 3
        # Ordinal-prefixed files come first
        assert designs[0]["filename"] == "01-auth.md"
        assert designs[0]["ordinal"] == 1
        assert designs[1]["filename"] == "02-payments.md"
        assert designs[1]["ordinal"] == 2

    def test_create_project_nonexistent_dir(self, project_client):
        client, _ = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Bad",
                "base_dir": "/nonexistent/path",
            },
        )
        assert resp.status_code == 400

    def test_start_rejects_a_non_git_project_directory(self, project_client, tmp_path):
        """Same check on the other entry point: /start is where an existing
        project with a since-broken base_dir shows up, and it used to reserve
        a concurrency slot and launch a pipeline that could only fail."""
        client, _ = project_client
        plain_dir = tmp_path / "start-not-a-repo"
        plain_dir.mkdir()

        resp = client.post(f"/api/autopilot/start?project_path={plain_dir}")
        assert resp.status_code == 400
        assert "not a git repository" in resp.json()["detail"]

    def test_create_project_rejects_a_non_git_directory(self, project_client, tmp_path):
        """Without a repo there is no worktree, so no phase can ever run --
        and activation already refuses the same directory. Rejecting at
        creation reports it where the directory is actually chosen, instead
        of as an unrelated-looking failure deep inside Phase 0."""
        client, _ = project_client
        plain_dir = tmp_path / "not-a-repo"
        plain_dir.mkdir()

        resp = client.post(
            "/api/autopilot/projects",
            json={"name": "Plain", "base_dir": str(plain_dir)},
        )
        assert resp.status_code == 400
        assert "not a git repository" in resp.json()["detail"]
        assert "git init" in resp.json()["detail"]

    def test_create_project_duplicate_dir(self, project_client):
        client, dirs = project_client
        client.post(
            "/api/autopilot/projects",
            json={
                "name": "First",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Second",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        assert resp.status_code == 409

    def test_create_project_offloads_codegraph_init(self, project_client):
        """Regression: codegraph init ran subprocess.run(...) with a 120s
        timeout directly on the event loop -- blocking every other
        in-flight request for up to that long on a single POST /projects
        call. Must go through run_in_executor instead."""
        from unittest.mock import AsyncMock, MagicMock, patch

        client, dirs = project_client

        fake_loop = MagicMock()
        fake_loop.run_in_executor = AsyncMock(return_value=None)

        with patch("asyncio.get_event_loop", return_value=fake_loop):
            resp = client.post(
                "/api/autopilot/projects",
                json={"name": "Test", "base_dir": str(dirs["project_dir"])},
            )

        assert resp.status_code == 200
        fake_loop.run_in_executor.assert_called_once()
        executor_arg, func_arg = fake_loop.run_in_executor.call_args.args[:2]
        assert executor_arg is None
        assert func_arg.__name__ == "_init_codegraph_index"

    def test_list_projects(self, project_client):
        client, dirs = project_client
        client.post(
            "/api/autopilot/projects",
            json={
                "name": "P1",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        resp = client.get("/api/autopilot/projects")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["name"] == "P1"

    def test_get_project(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.get(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == project_id

    def test_get_project_not_found(self, project_client):
        client, _ = project_client
        resp = client.get("/api/autopilot/projects/nonexistent")
        assert resp.status_code == 404

    def test_update_project_name(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Old Name",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.put(
            f"/api/autopilot/projects/{project_id}",
            json={
                "name": "New Name",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"

    def test_update_project_is_default(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.put(
            f"/api/autopilot/projects/{project_id}",
            json={
                "is_default": True,
            },
        )
        assert resp.status_code == 200
        assert resp.json()["is_default"] is True

    def test_delete_project(self, project_client):
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={
                "name": "To Delete",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        project_id = create.json()["id"]

        resp = client.delete(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 200

        resp = client.get(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 404

    def test_delete_project_not_found(self, project_client):
        client, _ = project_client
        resp = client.delete("/api/autopilot/projects/nonexistent")
        assert resp.status_code == 404


    def test_delete_project_with_repo_scoped_task_does_not_500(self, project_client, tmp_path):
        """BLOCKER regression (adversarial review): a project with a
        non-primary ProjectRepo that has a Task scoped to it (repo_id set)
        must still be deletable -- previously this raised an uncaught
        sqlite3 FOREIGN KEY constraint failure (Task.repo_id has no
        ondelete clause) that left the project permanently stuck."""
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={"name": "Multi-repo project", "base_dir": str(dirs["project_dir"])},
        )
        project_id = create.json()["id"]

        second_repo_dir = tmp_path / "second-repo"
        second_repo_dir.mkdir()
        (second_repo_dir / ".git").mkdir()
        add_repo = client.post(
            f"/api/autopilot/projects/{project_id}/repos",
            json={"label": "frontend", "path": str(second_repo_dir)},
        )
        assert add_repo.status_code == 200
        repo_id = add_repo.json()["id"]

        import os

        from src.core.database import DatabaseManager, Task

        db_manager = DatabaseManager(os.environ["HEPHAESTUS_TEST_DB"])
        session = db_manager.get_session()
        try:
            task = Task(
                id="task-repo-scoped",
                raw_description="scoped to non-primary repo",
                done_definition="n/a",
                status="pending",
                repo_id=repo_id,
            )
            session.add(task)
            session.commit()
        finally:
            session.close()

        resp = client.delete(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 200

        session = db_manager.get_session()
        try:
            reloaded = session.query(Task).filter_by(id="task-repo-scoped").first()
            assert reloaded is not None
            assert reloaded.repo_id is None
        finally:
            session.close()

    def test_delete_project_surfaces_integrity_error_as_409_not_500(self, project_client, monkeypatch):
        """BLOCKER fix, defense-in-depth: delete_project's try/except around
        db.flush() must turn ANY IntegrityError into a clean 409, not an
        unhandled 500 -- not just the repo_id case the pre-emptive null-out
        above already prevents from ever reaching this handler."""
        client, dirs = project_client
        create = client.post(
            "/api/autopilot/projects",
            json={"name": "To Delete", "base_dir": str(dirs["project_dir"])},
        )
        project_id = create.json()["id"]

        import sqlalchemy.orm
        from sqlalchemy.exc import IntegrityError

        def _raise_integrity_error(self, *args, **kwargs):
            raise IntegrityError("DELETE ...", {}, Exception("FOREIGN KEY constraint failed"))

        monkeypatch.setattr(sqlalchemy.orm.Session, "flush", _raise_integrity_error)

        resp = client.delete(f"/api/autopilot/projects/{project_id}")
        assert resp.status_code == 409
        assert "cannot be deleted" in resp.json()["detail"].lower()


class TestProjectDesigns:
    def _create_project(self, client, dirs):
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        return resp.json()["id"]

    def test_list_designs(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.get(f"/api/autopilot/projects/{pid}/designs")
        assert resp.status_code == 200
        assert len(resp.json()) == 3

    def test_list_designs_includes_a_directory_sourced_design(self, project_client):
        """A Spec-Kit directory-sourced design has filename=NULL (source_dir
        is set instead, per NFR-02) -- DesignItem.filename used to be a
        required `str`, so building one for such a row raised a pydantic
        ValidationError, 500ing this endpoint for the WHOLE project (every
        design, not just the directory-sourced one). Observed live."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, get_db

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-dir-sourced",
                    project_id=pid,
                    filename=None,
                    name="001-conversation-history",
                    source_dir="/tmp/specs/001-conversation-history",
                    status="pending",
                )
            )
            db.commit()

        resp = client.get(f"/api/autopilot/projects/{pid}/designs")
        assert resp.status_code == 200
        by_id = {d["id"]: d for d in resp.json()}
        assert by_id["des-dir-sourced"]["filename"] is None

    def test_designs_sorted_by_ordinal(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.get(f"/api/autopilot/projects/{pid}/designs")
        designs = resp.json()
        ordinals = [d["ordinal"] for d in designs]
        assert ordinals == sorted(ordinals)

    def test_add_design(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "New Feature",
                "content": "# New Feature\nDescription here.",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "New Feature"
        assert data["extension"] == ".md"

        # Verify file was created on disk
        design_dir = dirs["design_dir"]
        assert (design_dir / "New_Feature.md").exists()

    def test_add_design_explicit_workflow_type_is_stored_verbatim(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Add dark mode",
                "content": "# Add dark mode\nUsers want a dark theme.",
                "workflow_type": "bugfix",
            },
        )
        assert resp.status_code == 200
        # Explicit selection overrides the heuristic even though this design
        # reads like a feature request -- the manual dropdown always wins.
        assert resp.json()["workflow_type"] == "bugfix"

        from src.core.database import AutopilotDesign, get_db

        with get_db() as db:
            d = db.query(AutopilotDesign).filter_by(project_id=pid, name="Add dark mode").first()
            assert d.workflow_type == "bugfix"

    def test_add_design_auto_detects_workflow_type_when_omitted(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Fix login crash",
                "content": "Login throws an error and crashes for some users.",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["workflow_type"] == "bugfix"

    def test_add_design_duplicate(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Dup Test",
                "content": "first",
            },
        )
        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Dup Test",
                "content": "second",
            },
        )
        assert resp.status_code == 409

    def test_add_design_reselecting_the_same_remote_file_returns_it_instead_of_409(
        self, project_client
    ):
        """Regression: LoadDesignModal's "Load from Remote" file picker
        re-submits the exact file it just read back to its own folder --
        previously a guaranteed 409 on every such re-submission, since the
        file (and its design row) already exists. source_remote_path lets
        the client say "this is still exactly the file I picked, unedited"
        -- the endpoint recognizes the match and returns the existing
        design instead of erroring or inserting a duplicate queue entry."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        first = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={"name": "Reselect Test", "content": "original"},
        )
        assert first.status_code == 200
        first_id = first.json()["id"]
        first_ordinal = first.json()["ordinal"]

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Reselect Test",
                "content": "original",
                "source_remote_path": "some/folder/Reselect_Test.md",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == first_id
        assert resp.json()["ordinal"] == first_ordinal

    def test_add_design_source_remote_path_naming_a_different_file_still_409s(
        self, project_client
    ):
        """source_remote_path must name THIS SAME file -- a stale or
        mismatched value (e.g. left over from a previous selection) must
        not accidentally suppress a genuine name collision against an
        unrelated existing design."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={"name": "Collision Test", "content": "first"},
        )

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Collision Test",
                "content": "second",
                "source_remote_path": "some/folder/some_other_file.md",
            },
        )
        assert resp.status_code == 409

    def test_add_design_with_docs_destination_writes_to_docs_dir(self, project_client):
        """Locally-uploaded designs (destination="docs") persist as a
        real, git-tracked file under docs/ instead of the hidden
        .hephaestus/specs/ staging dir."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Uploaded Feature",
                "content": "# Uploaded Feature\nFrom the user's machine.",
                "destination": "docs",
            },
        )
        assert resp.status_code == 200

        docs_file = dirs["project_dir"] / "docs" / "Uploaded_Feature.md"
        assert docs_file.exists()
        assert docs_file.read_text() == "# Uploaded Feature\nFrom the user's machine."
        # Must NOT also land in the hidden staging dir
        assert not (dirs["design_dir"] / "Uploaded_Feature.md").exists()

    def test_add_design_with_docs_destination_rejects_conflict(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        docs_dir = dirs["project_dir"] / "docs"
        docs_dir.mkdir(parents=True, exist_ok=True)
        (docs_dir / "Existing.md").write_text("already here")

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Existing",
                "content": "new content",
                "destination": "docs",
            },
        )
        assert resp.status_code == 409
        # Original content must be untouched
        assert (docs_dir / "Existing.md").read_text() == "already here"

    def test_add_design_docs_destination_sets_file_path_for_pickup(self, project_client):
        """AutopilotDesign.file_path must point at the docs/ location --
        pick_next_design (queue.py) resolves this before falling back to
        its DESIGN_CONTEXT_SUBDIR-based reconstruction, which would look
        in the wrong directory for a docs/-stored design."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Path Check",
                "content": "content",
                "destination": "docs",
            },
        )
        assert resp.status_code == 200
        design_id = resp.json()["id"]

        import os

        from src.core.database import AutopilotDesign, DatabaseManager

        db_manager = DatabaseManager(os.environ["HEPHAESTUS_TEST_DB"])
        session = db_manager.get_session()
        try:
            design = session.query(AutopilotDesign).filter_by(id=design_id).first()
            assert design.file_path == str(dirs["project_dir"] / "docs" / "Path_Check.md")
        finally:
            session.close()

    def test_add_design_accepts_an_arbitrary_nested_destination_folder(self, project_client):
        """destination isn't limited to the "queue"/"docs" literals -- any
        other value is a real folder path relative to the project root,
        used verbatim (e.g. the New Feature/Report Bug flows' docs/spec
        and docs/bugfix defaults, or a folder the user picked)."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Login Crash",
                "content": "# Login Crash\nRepro steps.",
                "destination": "docs/bugfix",
            },
        )
        assert resp.status_code == 200

        bugfix_file = dirs["project_dir"] / "docs" / "bugfix" / "Login_Crash.md"
        assert bugfix_file.exists()
        assert bugfix_file.read_text() == "# Login Crash\nRepro steps."

    def test_add_design_rejects_a_destination_that_escapes_the_project_root(self, project_client):
        """destination is client-supplied (typed or browsed) for any
        non-"queue" value, unlike the old hardcoded "docs" literal -- it
        must be validated to stay within the project root, not trusted
        verbatim."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={
                "name": "Escape Attempt",
                "content": "content",
                "destination": "../../etc",
            },
        )
        assert resp.status_code == 400

    def test_ensure_folder_creates_a_not_yet_existing_destination(self, project_client):
        """The New Feature/Report Bug destination-folder field defaults to
        docs/spec or docs/bugfix, which may not exist yet on a project
        that's never had one -- ensure-folder makes it real immediately
        (rather than only once a design is actually submitted), so a
        browse/select round-trip against it works right away."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        target = dirs["project_dir"] / "docs" / "bugfix"
        assert not target.exists()

        resp = client.post(
            f"/api/autopilot/projects/{pid}/ensure-folder",
            json={"path": "docs/bugfix"},
        )
        assert resp.status_code == 200
        assert target.is_dir()

    def test_ensure_folder_rejects_a_path_that_escapes_the_project_root(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(
            f"/api/autopilot/projects/{pid}/ensure-folder",
            json={"path": "../../etc"},
        )
        assert resp.status_code == 400

    def test_get_design_content_resolves_docs_destination(self, project_client):
        """content/status/delete all resolved unconditionally against
        .hephaestus/specs/ (_get_design_queue_dir), ignoring
        AutopilotDesign.file_path -- 404s for every docs/-stored design
        even though the file exists on disk. Only pick_next_design
        (queue.py) checked file_path first."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={"name": "Docs Test", "content": "hello from docs", "destination": "docs"},
        )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/Docs_Test.md/content")
        assert resp.status_code == 200
        assert resp.json()["content"] == "hello from docs"

    def test_get_design_status_resolves_docs_destination(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={"name": "Docs Status", "content": "status body", "destination": "docs"},
        )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/Docs_Status.md/status")
        assert resp.status_code == 200

    def test_remove_design_resolves_docs_destination(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={"name": "Docs Remove", "content": "remove me", "destination": "docs"},
        )
        docs_file = dirs["project_dir"] / "docs" / "Docs_Remove.md"
        assert docs_file.exists()

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/Docs_Remove.md")
        assert resp.status_code == 200
        assert not docs_file.exists()
        # Must not have gone looking in (or deleted anything from) the
        # unrelated .hephaestus/specs/ staging dir
        assert not (dirs["design_dir"] / "Docs_Remove.md").exists()

    def test_content_status_and_delete_resolve_an_arbitrary_nested_destination(self, project_client):
        """The docs-destination fix above (file_path-based resolution)
        must generalize to ANY non-"queue" destination -- not just the
        literal "docs" string -- since destination now accepts arbitrary
        folder paths (the New Feature/Report Bug flows' docs/spec and
        docs/bugfix defaults, or a user-picked folder)."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        client.post(
            f"/api/autopilot/projects/{pid}/designs",
            json={"name": "Nested Bug", "content": "repro steps", "destination": "docs/bugfix"},
        )
        nested_file = dirs["project_dir"] / "docs" / "bugfix" / "Nested_Bug.md"
        assert nested_file.exists()

        content_resp = client.get(f"/api/autopilot/projects/{pid}/designs/Nested_Bug.md/content")
        assert content_resp.status_code == 200
        assert content_resp.json()["content"] == "repro steps"

        status_resp = client.get(f"/api/autopilot/projects/{pid}/designs/Nested_Bug.md/status")
        assert status_resp.status_code == 200

        delete_resp = client.delete(f"/api/autopilot/projects/{pid}/designs/Nested_Bug.md")
        assert delete_resp.status_code == 200
        assert not nested_file.exists()

    def test_remove_design(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/01-auth.md")
        assert resp.status_code == 200

        # Verify file was deleted
        design_dir = dirs["design_dir"]
        assert not (design_dir / "01-auth.md").exists()

        # Verify DB record was deleted
        resp = client.get(f"/api/autopilot/projects/{pid}/designs")
        assert len(resp.json()) == 2

    def test_remove_design_not_found(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/nonexistent.md")
        assert resp.status_code == 404

    def test_get_design_content(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/01-auth.md/content")
        assert resp.status_code == 200
        assert "OAuth2" in resp.json()["content"]

    def test_reorder_designs(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        designs = client.get(f"/api/autopilot/projects/{pid}/designs").json()
        reversed_ids = [d["id"] for d in reversed(designs)]

        resp = client.put(
            f"/api/autopilot/projects/{pid}/designs/reorder",
            json={
                "design_ids": reversed_ids,
            },
        )
        assert resp.status_code == 200

        reordered = client.get(f"/api/autopilot/projects/{pid}/designs").json()
        assert reordered[0]["id"] == reversed_ids[0]

    def test_reorder_invalid_id(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.put(
            f"/api/autopilot/projects/{pid}/designs/reorder",
            json={
                "design_ids": ["nonexistent-id"],
            },
        )
        assert resp.status_code == 400

    def test_sync_project(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        # Add a new file to the filesystem
        design_dir = dirs["design_dir"]
        (design_dir / "03-api.md").write_text("# API Design")

        resp = client.post(f"/api/autopilot/projects/{pid}/sync")
        assert resp.status_code == 200
        designs = resp.json()
        assert len(designs) == 4
        filenames = [d["filename"] for d in designs]
        assert "03-api.md" in filenames

    def test_sync_auto_detects_workflow_type_for_filesystem_discovered_designs(self, project_client):
        """A design that entered the queue by landing in the filesystem
        directly (e.g. "Load from Remote", or manually dropped in) instead
        of through POST /designs never got detect_workflow_type() applied
        -- _sync_project_designs created its AutopilotDesign row with only
        the column default "feature", silently skipping bugfix detection
        for every design that didn't go through the add-design API."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        design_dir = dirs["design_dir"]
        (design_dir / "04-fix-crash.md").write_text(
            "# Fix Crash\nThe app crashes and returns the wrong error on login."
        )

        resp = client.post(f"/api/autopilot/projects/{pid}/sync")
        assert resp.status_code == 200
        by_filename = {d["filename"]: d for d in resp.json()}
        assert by_filename["04-fix-crash.md"]["workflow_type"] == "bugfix"

    def test_sync_removes_deleted_files(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        # Delete a file from filesystem
        design_dir = dirs["design_dir"]
        (design_dir / "01-auth.md").unlink()

        resp = client.post(f"/api/autopilot/projects/{pid}/sync")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_sync_logs_deleted_design(self, project_client, caplog):
        """Regression test: _sync_project_designs used to silently
        db.delete() a queue-scoped row whenever its file went missing from
        .hephaestus/specs/ -- with zero logging. That silence is what let a
        real, in-use design row (des-2acb39c6378d, referenced by 7 live
        Feature rows) vanish without a trace, since its source file lived
        outside the queue dir and every sync treated that as "deleted"."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        design_dir = dirs["design_dir"]
        (design_dir / "01-auth.md").unlink()

        with caplog.at_level("INFO"):
            resp = client.post(f"/api/autopilot/projects/{pid}/sync")
        assert resp.status_code == 200
        assert any(
            "01-auth.md" in r.message and "Removing design" in r.message
            for r in caplog.records
        )

    def test_design_not_found_project(self, project_client):
        client, _ = project_client
        resp = client.get("/api/autopilot/projects/nonexistent/designs")
        assert resp.status_code == 404

    def test_remove_design_cascades_orphaned_workflow_with_no_design_id(
        self, project_client
    ):
        """Regression test: observed live in production that a completed
        Phase 0 workflow (and its first per-feature workflow) can end up with
        Workflow.design_id left NULL and Feature.workflow_id never linked
        (both cascade lookups miss it), so deleting the design that spawned
        them left the workflows permanently orphaned -- they survived the
        delete and their tasks/phase executions kept accumulating. The fix
        adds a fallback match on launch_params (design_id or
        design_document filename), the same way _relink_features_to_workflows
        already matches Feature.workflow_id when the direct link is missing.
        """
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import (
            AutopilotDesign,
            Workflow,
            WorkflowDefinition,
            get_db,
        )

        design_id = "des-test-orphan"
        with get_db() as db:
            # Workflow.definition_id is a foreign key to workflow_definitions
            # -- these rows must exist for the Workflow inserts below to
            # satisfy PRAGMA foreign_keys=ON.
            db.add(WorkflowDefinition(id="feature_architect", name="Feature Architect"))
            db.add(WorkflowDefinition(id="autopilot", name="Autopilot"))

            db.add(
                AutopilotDesign(
                    id=design_id,
                    project_id=pid,
                    filename="orphan-design.md",
                    name="Orphan Design",
                    ordinal=10,
                    size_bytes=10,
                    extension=".md",
                    status="pending",
                )
            )

            # Orphaned Phase 0 workflow: design_id column never got set, but
            # launch_params carries the real design_id.
            db.add(
                Workflow(
                    id="wf-orphan-phase0",
                    name="feature_architect",
                    description="Phase 0: Feature Architect for Orphan",
                    definition_id="feature_architect",
                    design_id=None,
                    phases_folder_path=".",
                    status="completed",
                    launch_params={
                        "design_document": str(dirs["design_dir"] / "orphan-design.md"),
                        "project_path": str(dirs["project_dir"]),
                        "design_id": design_id,
                    },
                )
            )
            # Orphaned per-feature workflow: neither design_id nor
            # Feature.workflow_id got linked, but launch_params still points
            # at the same design document.
            db.add(
                Workflow(
                    id="wf-orphan-feature",
                    name="autopilot",
                    description="Autopilot: Orphan - Feature: Core",
                    definition_id="autopilot",
                    design_id=None,
                    phases_folder_path=".",
                    status="failed",
                    launch_params={
                        "design_document": str(dirs["design_dir"] / "orphan-design.md"),
                        "project_path": str(dirs["project_dir"]),
                        "feature_id": "core",
                    },
                )
            )
            db.commit()

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/orphan-design.md")
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            remaining = (
                db.query(Workflow)
                .filter(Workflow.id.in_(["wf-orphan-phase0", "wf-orphan-feature"]))
                .all()
            )
            assert remaining == []

    def test_remove_design_with_cost_history_does_not_500(self, project_client):
        """Regression: CostEntry.task_id/workflow_id are enforced foreign
        keys (PRAGMA foreign_keys=ON) that this cleanup never deleted --
        removing a design whose workflow/tasks had ever recorded real LLM
        cost (the common case, not the exception, now that cost tracking
        exists) raised an unhandled IntegrityError instead of succeeding."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, CostEntry, Task, Workflow, get_db

        with get_db() as db:
            design = db.query(AutopilotDesign).filter_by(project_id=pid, filename="01-auth.md").first()
            db.add(
                Workflow(
                    id="wf-cost-1", name="autopilot", phases_folder_path="/tmp",
                    status="failed", definition_id="autopilot", design_id=design.id,
                )
            )
            db.add(
                Task(
                    id="task-cost-1", workflow_id="wf-cost-1", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="failed",
                )
            )
            db.add(
                CostEntry(
                    id="cost-1", task_id="task-cost-1", workflow_id="wf-cost-1",
                    source="pi", cost_usd=0.05,
                )
            )
            db.commit()

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/01-auth.md")
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            assert db.query(Workflow).filter_by(id="wf-cost-1").first() is None
            assert db.query(CostEntry).filter_by(id="cost-1").first() is None

    def test_design_status_surfaces_failure_reason(self, project_client):
        """Regression: AutopilotDesign had no column to store *why* a design
        failed -- orchestrator.py's run_phase0 always passed error=... to
        _update_design_status, but it was silently dropped ("unknown field
        'error'") and the design detail modal had nothing to show, even for
        a specific, actionable reason. The status endpoint must surface it
        once the design is actually failed."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "failed-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-failed",
                    project_id=pid,
                    filename="failed-design.md",
                    name="Failed Design",
                    ordinal=10,
                    size_bytes=10,
                    extension=".md",
                    status="failed",
                    error="Invalid features.json: features array must have at least 1 entry, got 0",
                )
            )
            db.commit()

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/failed-design.md/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "failed"
        assert body["error"] == (
            "Invalid features.json: features array must have at least 1 entry, got 0"
        )

    def test_design_status_includes_cost_total(self, project_client):
        """cost_total_usd must be surfaced per-feature and summed at the
        design level so the UI can show a budget indicator without an extra
        network call."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Feature, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "cost-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-cost",
                    project_id=pid,
                    filename="cost-design.md",
                    name="Cost Design",
                    ordinal=13,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-cost-1",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="active",
                )
            )

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-cost-1",
                    design_id="des-test-cost",
                    feature_key="core",
                    name="Core",
                    scope="s",
                    status="active",
                    workflow_id="wf-cost-1",
                    cost_total_usd=1.5,
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/cost-design.md/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["features"][0]["cost_total_usd"] == 1.5
        assert body["cost_total_usd"] == 1.5

    def test_design_status_task_timestamps_are_utc_marked_and_include_cli_type(
        self, project_client
    ):
        """Regression: task created_at/completed_at were serialized via
        plain datetime.isoformat() -- a naive-but-UTC datetime with no
        timezone marker at all. The frontend's `new Date(iso_string)` then
        parses it as LOCAL time, not UTC; on a host whose local timezone
        trails UTC, the parsed timestamp looks HOURS in the future relative
        to real now(), producing a large negative "elapsed" display (e.g.
        "-21263s"). Also verifies cli_type is now surfaced per task so the
        UI can show which CLI (pi/claude/codex) ran it."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import (
            Agent,
            AgentLog,
            AutopilotDesign,
            Feature,
            Task,
            Workflow,
            get_db,
        )

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "cli-design.md").write_text("# Design")

        with get_db() as db:
            db.add(AutopilotDesign(
                id="des-test-cli", project_id=pid, filename="cli-design.md", name="CLI Design",
                ordinal=14, size_bytes=10, extension=".md", status="active",
            ))
            db.add(Workflow(id="wf-cli-1", name="autopilot", phases_folder_path="/tmp", status="active"))
            db.add(Agent(id="agent-cli-1", status="working", cli_type="pi", system_prompt="x"))

        with get_db() as db:
            db.add(Feature(
                id="feat-cli-1", design_id="des-test-cli", feature_key="core", name="Core",
                scope="s", status="active", workflow_id="wf-cli-1",
            ))
            db.add(Task(
                id="task-cli-1", workflow_id="wf-cli-1", raw_description="x", done_definition="x",
                status="in_progress", assigned_agent_id="agent-cli-1",
            ))
            db.add(AgentLog(
                agent_id="agent-cli-1", log_type="created", message="x",
                details={"cli_type": "pi", "task_id": "task-cli-1"},
            ))

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/cli-design.md/status")
        assert resp.status_code == 200, resp.text
        task = resp.json()["features"][0]["tasks"][0]
        assert task["created_at"].endswith("Z"), "must carry an explicit UTC marker"
        assert task["cli_type"] == "pi"

    def test_design_status_ignores_stale_active_workflow_whose_feature_is_done(
        self, project_client
    ):
        """Regression, observed live on BACKEND_DESIGN.md: matching_workflows
        is deliberately broad (LIKE-matched on the bare design filename), so
        it also catches every OTHER feature's workflow that happened to
        originate from the same design document -- a design gets re-run
        once per decomposed feature, and each feature's own workflow
        references the same design_document path in its launch_params. One
        such sibling workflow (Credit Management System) had its Feature
        row correctly flip to "completed" but its own Workflow.status was
        never cleaned up from "active" -- the endpoint's "any workflow
        active wins" rule then made the WHOLE design look permanently
        "Active" in the UI, forever, even after every feature (including
        this design's actual current one) had genuinely finished."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Feature, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "stale-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-stale",
                    project_id=pid,
                    filename="stale-design.md",
                    name="Stale Design",
                    ordinal=14,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-stale-active",
                    name="autopilot",
                    definition_id="autopilot",
                    phases_folder_path="/tmp",
                    status="active",  # stale -- never cleaned up
                    launch_params={
                        "design_document": str(design_dir / "stale-design.md"),
                        "project_path": str(dirs["project_dir"]),
                    },
                )
            )

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-stale-1",
                    design_id="des-test-stale",
                    feature_key="credit-system",
                    name="Credit Management System",
                    scope="s",
                    status="completed",
                    workflow_id="wf-stale-active",
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/stale-design.md/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] != "active"

    def test_design_status_ignores_an_orphaned_paused_workflow_with_no_linked_feature(
        self, project_client
    ):
        """Regression: matching_workflows' exclusion filter only handled a
        workflow whose LINKED feature was already completed/skipped
        (_feature_status_by_wf.get(wf.id) not in (...)) -- a workflow with
        NO Feature row pointing to it at ALL (an orphaned duplicate from a
        prior bootstrap race, e.g. before _relink_features_to_workflows'
        own bugfix-typed-feature fix) got `None not in (...)`, which is
        True -- the OPPOSITE of this filter's intent, so an orphaned
        "paused" workflow got counted right alongside genuinely live ones.
        Observed live: a feature's canonical workflow completed while two
        superseded duplicate workflows (no Feature linking to either)
        still sat "paused" -- the design's top-level status reported
        "paused" indefinitely."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Feature, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "orphan-design.md").write_text("# Design")

        launch_params = {
            "design_document": str(design_dir / "orphan-design.md"),
            "project_path": str(dirs["project_dir"]),
        }

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-orphan",
                    project_id=pid,
                    filename="orphan-design.md",
                    name="Orphan Design",
                    ordinal=15,
                    size_bytes=10,
                    extension=".md",
                    status="completed",
                )
            )
            # The orphaned duplicate: paused, and nothing links to it.
            db.add(
                Workflow(
                    id="wf-orphan-paused",
                    name="bugfix",
                    definition_id="bugfix",
                    phases_folder_path="/tmp",
                    status="paused",
                    paused_by="system",
                    launch_params=launch_params,
                )
            )
            # The canonical, currently-linked workflow: genuinely done.
            db.add(
                Workflow(
                    id="wf-canonical-completed",
                    name="bugfix",
                    definition_id="bugfix",
                    phases_folder_path="/tmp",
                    status="completed",
                    launch_params=launch_params,
                )
            )

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-orphan-1",
                    design_id="des-test-orphan",
                    feature_key="the-fix",
                    name="The Fix",
                    scope="s",
                    status="completed",
                    workflow_id="wf-canonical-completed",
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/orphan-design.md/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] != "paused"

    def test_design_status_finds_report_after_worktree_cleanup(self, project_client):
        """Regression, observed live: has_report only ever checked
        Workflow.working_directory for feature_report.html. _cleanup_worktree
        nulls out working_directory once a feature's worktree is removed on
        full completion -- which is exactly when a report is most likely to
        exist. PhaseManager._populate_feature_folder already archives a
        durable copy to <project>/.hephaestus/features/<timestamp>_<design>/
        before that happens, keyed to the workflow via that folder's own
        pipeline_metrics.json (folder names are timestamp+design-name only,
        not feature-specific -- can't be matched by name alone when a design
        has more than one feature). Without checking that archive, the
        report button permanently disappears the moment a feature finishes."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Feature, Phase, Task, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "report-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-report",
                    project_id=pid,
                    filename="report-design.md",
                    name="Report Design",
                    ordinal=15,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-report-done",
                    name="autopilot",
                    definition_id="autopilot",
                    phases_folder_path="/tmp",
                    status="completed",
                    working_directory=None,  # worktree already cleaned up
                    launch_params={
                        "design_document": str(design_dir / "report-design.md"),
                        "project_path": str(dirs["project_dir"]),
                    },
                )
            )

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-report-1",
                    design_id="des-test-report",
                    feature_key="core",
                    name="Core",
                    scope="s",
                    status="completed",
                    workflow_id="wf-report-done",
                )
            )
            db.add(
                Phase(
                    id="phase-report-doc-review",
                    workflow_id="wf-report-done",
                    order=10,
                    name="doc_review",
                    description="Review documentation.",
                    done_definitions=["x"],
                )
            )
            db.add(
                Task(
                    id="task-report-doc-review",
                    raw_description="Execute doc_review",
                    enriched_description="Execute doc_review",
                    done_definition="d",
                    status="done",
                    phase_id="phase-report-doc-review",
                    workflow_id="wf-report-done",
                )
            )

        # The archived feature-folder copy _populate_feature_folder leaves
        # behind, matched to the workflow via pipeline_metrics.json.
        gallery_dir = dirs["project_dir"] / ".hephaestus" / "features" / "20260101_000000_Report_Design"
        (gallery_dir / "docs").mkdir(parents=True)
        (gallery_dir / "docs" / "pipeline_metrics.json").write_text(
            json.dumps({"workflow_id": "wf-report-done"})
        )
        (gallery_dir / "docs" / "feature_report.html").write_text("<html>report</html>")

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/report-design.md/status")
        assert resp.status_code == 200, resp.text
        features = resp.json()["features"]
        feat = next(f for f in features if f["id"] == "feat-report-1")
        assert feat["has_report"] is True

    def test_design_status_surfaces_budget_pause_reason(self, project_client):
        """A budget-triggered pause must be distinguishable from a plain
        user pause: the design-status endpoint (polled by DesignQueuePanel)
        has to surface paused_by/status_reason, same as WorkflowCard already
        does for the workflow-list page, otherwise a budget pause renders
        as an indistinguishable generic 'Paused' badge."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "budget-paused-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-budget-paused",
                    project_id=pid,
                    filename="budget-paused-design.md",
                    name="Budget Paused Design",
                    ordinal=14,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-budget-paused-1",
                    name="autopilot",
                    definition_id="autopilot",
                    phases_folder_path="/tmp",
                    status="paused",
                    paused_by="budget",
                    status_reason="Budget limit reached",
                    launch_params={
                        "design_document": str(design_dir / "budget-paused-design.md"),
                        "project_path": str(dirs["project_dir"]),
                    },
                )
            )

        resp = client.get(
            f"/api/autopilot/projects/{pid}/designs/budget-paused-design.md/status"
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "paused"
        assert body["paused_by"] == "budget"
        assert body["status_reason"] == "Budget limit reached"
        assert body["workflows"][0]["paused_by"] == "budget"
        assert body["workflows"][0]["status_reason"] == "Budget limit reached"

    def test_design_status_omits_error_when_not_failed(self, project_client):
        """The error field shouldn't leak a stale message from a previous
        failed attempt once the design is no longer in a failed state."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "ok-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-ok",
                    project_id=pid,
                    filename="ok-design.md",
                    name="OK Design",
                    ordinal=11,
                    size_bytes=10,
                    extension=".md",
                    status="pending",
                    error="stale error from a previous run",
                )
            )
            db.commit()

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/ok-design.md/status")
        assert resp.status_code == 200, resp.text
        assert resp.json()["error"] is None

    def test_task_row_shows_config_description_and_goto_reason(self, project_client):
        """Regression: the queue's task rows used to show the raw task
        prompt verbatim ("Execute development: ...\\n\\nWHY YOU'RE HERE:
        ..."), truncated to 200 chars -- noisy, and a long phase
        description could push the actual goto reason past the truncation
        point entirely. phase_description must come from the phase's own
        config-sourced Phase.description (not re-parsed from the task
        text), and goto_reason must be the clean text after
        GOTO_REASON_PREFIX, parsed from the FULL untruncated description."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.constants import GOTO_REASON_PREFIX
        from src.core.database import (
            AutopilotDesign,
            Feature,
            Phase,
            Task,
            Workflow,
            get_db,
        )

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "goto-design.md").write_text("# Design")

        # Separate get_db() blocks, in FK dependency order (Workflow before
        # Feature/Phase, both before Task) -- each commits before the next
        # starts, sidestepping any reliance on the ORM's automatic flush
        # ordering for a same-transaction multi-insert.
        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-test-goto",
                    project_id=pid,
                    filename="goto-design.md",
                    name="Goto Design",
                    ordinal=12,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-goto-1",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="active",
                )
            )

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-goto-1",
                    design_id="des-test-goto",
                    feature_key="core",
                    name="Core",
                    scope="s",
                    status="active",
                    workflow_id="wf-goto-1",
                )
            )
            db.add(
                Phase(
                    id="phase-goto-1",
                    workflow_id="wf-goto-1",
                    order=4,
                    name="development",
                    description="Implement all components according to the architecture.",
                    done_definitions=["x"],
                )
            )

        with get_db() as db:
            # A padded-length base description, long enough that a naive
            # 200-char truncation of the combined text would cut off the
            # goto reason below it -- pins down that goto_reason is parsed
            # from the FULL description, not the truncated `description`
            # field.
            padding = "x" * 250
            full_description = (
                f"Execute development: {padding}\n\n"
                f"{GOTO_REASON_PREFIX}6 BLOCKER findings in adversarial review\n"
                "Address this specifically -- this is not a fresh implementation "
                "pass, it's a return from review with a concrete issue to fix."
            )
            db.add(
                Task(
                    id="task-goto-1",
                    raw_description=full_description,
                    enriched_description=full_description,
                    done_definition="d",
                    status="pending",
                    phase_id="phase-goto-1",
                    workflow_id="wf-goto-1",
                    action="goto",
                    action_target_phase="development",
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/goto-design.md/status")
        assert resp.status_code == 200, resp.text
        tasks = resp.json()["features"][0]["tasks"]
        assert len(tasks) == 1
        task = tasks[0]
        assert task["phase_description"] == (
            "Implement all components according to the architecture."
        )
        assert task["goto_reason"] == "6 BLOCKER findings in adversarial review"
        assert task["action"] == "goto"
        assert task["action_target_phase"] == "development"


class TestArchiveProjectDesign:
    """Archive hides a design from the default queue list (and its own
    file/tasks/workflows/features untouched) without the destructive
    cascade remove_project_design does -- see design_file_routes.py's
    _set_design_archived."""

    def _create_project(self, client, dirs):
        resp = client.post(
            "/api/autopilot/projects",
            json={"name": "Test", "base_dir": str(dirs["project_dir"])},
        )
        return resp.json()["id"]

    def test_archive_hides_from_default_list_but_not_archived_list(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(f"/api/autopilot/projects/{pid}/designs/01-auth.md/archive")
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_at"] is not None

        default_list = client.get(f"/api/autopilot/projects/{pid}/designs").json()
        assert "01-auth.md" not in [d["filename"] for d in default_list]
        assert len(default_list) == 2

        archived_list = client.get(f"/api/autopilot/projects/{pid}/designs", params={"archived": True}).json()
        assert [d["filename"] for d in archived_list] == ["01-auth.md"]

    def test_unarchive_restores_to_default_list(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        client.post(f"/api/autopilot/projects/{pid}/designs/01-auth.md/archive")

        resp = client.post(f"/api/autopilot/projects/{pid}/designs/01-auth.md/unarchive")
        assert resp.status_code == 200, resp.text
        assert resp.json()["archived_at"] is None

        default_list = client.get(f"/api/autopilot/projects/{pid}/designs").json()
        assert "01-auth.md" in [d["filename"] for d in default_list]
        archived_list = client.get(f"/api/autopilot/projects/{pid}/designs", params={"archived": True}).json()
        assert archived_list == []

    def test_reload_excludes_archived_designs(self, project_client):
        """Regression (live incident): reload_project_designs calls
        list_project_designs(project_id) directly as a plain function
        rather than through route dispatch, so its archived: bool =
        Query(False) default stayed the literal Query(...) sentinel object
        -- which is truthy -- instead of resolving to False. Reload
        returned ONLY archived designs (dropping every real one), and the
        frontend's Reload button wrote that straight into the visible
        list, flashing archived designs back in."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        client.post(f"/api/autopilot/projects/{pid}/designs/01-auth.md/archive")

        resp = client.post(f"/api/autopilot/projects/{pid}/designs/reload")
        assert resp.status_code == 200, resp.text
        filenames = [d["filename"] for d in resp.json()]
        assert "01-auth.md" not in filenames
        assert "02-payments.md" in filenames

    def test_archive_unknown_design_404s(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        resp = client.post(f"/api/autopilot/projects/{pid}/designs/nonexistent.md/archive")
        assert resp.status_code == 404


class TestWorkflowFeatureReport:
    """GET /workflows/{workflow_id}/feature_report -- serves the same
    report get_project_design_status's has_report flag advertises."""

    def _create_project(self, client, dirs):
        resp = client.post(
            "/api/autopilot/projects",
            json={"name": "Test", "base_dir": str(dirs["project_dir"])},
        )
        return resp.json()["id"]

    def test_serves_from_live_worktree(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import Workflow, get_db

        worktree_dir = dirs["project_dir"] / "worktree"
        (worktree_dir / ".hephaestus").mkdir(parents=True)
        (worktree_dir / ".hephaestus" / "feature_report.html").write_text("<html>live</html>")

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-live-report",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="active",
                    project_id=pid,
                    working_directory=str(worktree_dir),
                )
            )

        resp = client.get("/api/autopilot/workflows/wf-live-report/feature_report")
        assert resp.status_code == 200
        assert "live" in resp.text

    def test_falls_back_to_archived_report_after_worktree_cleanup(self, project_client):
        """Regression, observed live: once a feature fully completes,
        _cleanup_worktree removes the worktree and nulls
        Workflow.working_directory -- this endpoint used to 404 forever
        from that point on ("Workflow not found or has no working
        directory"), even though PhaseManager._populate_feature_folder had
        already archived a durable copy before the worktree was removed."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-archived-report",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="completed",
                    project_id=pid,
                    working_directory=None,  # worktree already cleaned up
                )
            )

        gallery_dir = dirs["project_dir"] / ".hephaestus" / "features" / "20260101_000000_Some_Design"
        (gallery_dir / "docs").mkdir(parents=True)
        (gallery_dir / "docs" / "pipeline_metrics.json").write_text(
            json.dumps({"workflow_id": "wf-archived-report"})
        )
        (gallery_dir / "docs" / "feature_report.html").write_text("<html>archived</html>")

        resp = client.get("/api/autopilot/workflows/wf-archived-report/feature_report")
        assert resp.status_code == 200
        assert "archived" in resp.text

    def test_404_when_no_report_anywhere(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-no-report",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="completed",
                    project_id=pid,
                    working_directory=None,
                )
            )

        resp = client.get("/api/autopilot/workflows/wf-no-report/feature_report")
        assert resp.status_code == 404

    def test_404_when_workflow_does_not_exist(self, project_client):
        client, dirs = project_client
        self._create_project(client, dirs)

        resp = client.get("/api/autopilot/workflows/nonexistent/feature_report")
        assert resp.status_code == 404

    def test_serves_phase0_synopsis_from_designs_folder(self, project_client):
        """Phase 0 (Feature Architect) has no features-gallery entry --
        its archived report lives in the design's own designs_folder
        instead (see run_phase0's synopsis_src copy)."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Workflow, get_db

        designs_folder = dirs["project_dir"] / ".hephaestus" / "specs" / "run1"
        designs_folder.mkdir(parents=True)
        (designs_folder / "feature_report.html").write_text("<html>phase0 synopsis</html>")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-phase0-report",
                    project_id=pid,
                    filename="d.md",
                    name="D",
                    designs_folder=str(designs_folder),
                )
            )
            db.add(
                Workflow(
                    id="wf-phase0-report",
                    name="Phase 0",
                    definition_id="feature_architect",
                    phases_folder_path="/tmp",
                    status="paused",
                    project_id=pid,
                    design_id="des-phase0-report",
                    working_directory=None,  # worktree still holds it while
                    # paused for review in practice, but this test exercises
                    # the fallback specifically
                )
            )

        resp = client.get("/api/autopilot/workflows/wf-phase0-report/feature_report")
        assert resp.status_code == 200
        assert "phase0 synopsis" in resp.text


class TestFeatureRecordReport:
    """GET /feature-records/{feature_id}/report -- regression: this
    endpoint's candidate paths didn't include .hephaestus/doc_review/,
    where doc_review actually writes feature_report.html, even though
    the workflow-scoped sibling endpoint (get_workflow_feature_report)
    and design_status_service.py's has_report computation both already
    checked it. A real feature's review modal correctly showed the
    Report tab (feature.has_report was True) but its iframe 404'd,
    rendering blank -- the report existed and was reachable directly via
    /workflows/{id}/feature_report, just not through this endpoint."""

    def _create_project(self, client, dirs):
        resp = client.post(
            "/api/autopilot/projects",
            json={"name": "Test", "base_dir": str(dirs["project_dir"])},
        )
        return resp.json()["id"]

    def test_serves_report_from_doc_review_subdirectory(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Feature, Workflow, get_db

        worktree_dir = dirs["project_dir"] / "worktree"
        (worktree_dir / ".hephaestus" / "doc_review").mkdir(parents=True)
        (worktree_dir / ".hephaestus" / "doc_review" / "feature_report.html").write_text(
            "<html>doc_review report</html>"
        )

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-feat-report",
                    project_id=pid,
                    filename="d.md",
                    name="D",
                )
            )
            db.add(
                Workflow(
                    id="wf-feat-report",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="active",
                    project_id=pid,
                    working_directory=str(worktree_dir),
                )
            )
            db.add(
                Feature(
                    id="feat-report-test",
                    design_id="des-feat-report",
                    workflow_id="wf-feat-report",
                    feature_key="commit-resolution",
                    name="Commit Resolution",
                    scope="Test scope",
                    status="paused",
                )
            )

        resp = client.get("/api/autopilot/feature-records/feat-report-test/report")
        assert resp.status_code == 200
        assert "doc_review report" in resp.text

    def test_404_when_no_report_anywhere(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Feature, Workflow, get_db

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-feat-no-report",
                    project_id=pid,
                    filename="d.md",
                    name="D",
                )
            )
            db.add(
                Workflow(
                    id="wf-feat-no-report",
                    name="autopilot",
                    phases_folder_path="/tmp",
                    status="active",
                    project_id=pid,
                    working_directory=None,
                )
            )
            db.add(
                Feature(
                    id="feat-no-report-test",
                    design_id="des-feat-no-report",
                    workflow_id="wf-feat-no-report",
                    feature_key="no-report",
                    name="No Report",
                    scope="Test scope",
                    status="paused",
                )
            )

        resp = client.get("/api/autopilot/feature-records/feat-no-report-test/report")
        assert resp.status_code == 404


class TestPhase0PseudoFeatureReviewFields:
    """The synthetic "Feature Architect" row get_project_design_status
    inserts for Phase 0 -- must report review_pending/has_report/status
    the same way a real Feature row does, so the frontend's review-mode
    amber highlight and Resume-as-approve flow both work for it too."""

    def _create_project(self, client, dirs):
        resp = client.post(
            "/api/autopilot/projects",
            json={"name": "Test", "base_dir": str(dirs["project_dir"])},
        )
        return resp.json()["id"]

    def test_paused_for_review_reports_paused_status_and_review_pending(
        self, project_client
    ):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Task, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "phase0-design.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-phase0-paused",
                    project_id=pid,
                    filename="phase0-design.md",
                    name="Phase0 Design",
                    ordinal=20,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-phase0-paused",
                    name="Phase 0",
                    definition_id="feature_architect",
                    phases_folder_path="/tmp",
                    status="paused",
                    paused_by="review",
                    launch_params={
                        "design_document": str(design_dir / "phase0-design.md"),
                        "project_path": str(dirs["project_dir"]),
                    },
                )
            )
            db.add(
                Task(
                    id="task-phase0-paused",
                    raw_description="Execute feature_review",
                    enriched_description="Execute feature_review",
                    done_definition="d",
                    status="done",
                    workflow_id="wf-phase0-paused",
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/phase0-design.md/status")
        assert resp.status_code == 200, resp.text
        features = resp.json()["features"]
        phase0 = next(f for f in features if f["id"] == "phase0-wf-phase0-paused")

        # Every task is "done" -- without the paused_by override this would
        # read "completed", indistinguishable from a design that skipped
        # review entirely.
        assert phase0["status"] == "paused"
        assert phase0["review_pending"] is True

    def test_not_paused_reports_review_pending_false(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Task, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "phase0-active.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-phase0-active",
                    project_id=pid,
                    filename="phase0-active.md",
                    name="Phase0 Active",
                    ordinal=21,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-phase0-active",
                    name="Phase 0",
                    definition_id="feature_architect",
                    phases_folder_path="/tmp",
                    status="completed",
                    launch_params={
                        "design_document": str(design_dir / "phase0-active.md"),
                        "project_path": str(dirs["project_dir"]),
                    },
                )
            )
            db.add(
                Task(
                    id="task-phase0-active",
                    raw_description="Execute feature_review",
                    enriched_description="Execute feature_review",
                    done_definition="d",
                    status="done",
                    workflow_id="wf-phase0-active",
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/phase0-active.md/status")
        assert resp.status_code == 200, resp.text
        features = resp.json()["features"]
        phase0 = next(f for f in features if f["id"] == "phase0-wf-phase0-active")

        assert phase0["status"] == "completed"
        assert phase0["review_pending"] is False

    def test_orphaned_failed_task_does_not_show_phase0_as_failed(
        self, project_client
    ):
        """Regression: a task marked failed with an "Orphaned:"-prefixed
        failure_reason is self-heal's own transient artifact
        (_create_phase_task marks a stale task failed, then immediately
        creates a fresh replacement in the same pass) -- not a genuine
        failure. Same class of bug fixed in status_derivation.py's
        derive_workflow_status/derive_feature_status: a status poll
        landing in that split-second gap must not show this design's
        Feature Architect card as "failed" for a task that's already
        being replaced."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import AutopilotDesign, Task, Workflow, get_db

        design_dir = dirs["project_dir"] / ".hephaestus" / "specs"
        design_dir.mkdir(parents=True, exist_ok=True)
        (design_dir / "phase0-orphaned.md").write_text("# Design")

        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id="des-phase0-orphaned",
                    project_id=pid,
                    filename="phase0-orphaned.md",
                    name="Phase0 Orphaned",
                    ordinal=22,
                    size_bytes=10,
                    extension=".md",
                    status="active",
                )
            )
            db.add(
                Workflow(
                    id="wf-phase0-orphaned",
                    name="Phase 0",
                    definition_id="feature_architect",
                    phases_folder_path="/tmp",
                    status="active",
                    launch_params={
                        "design_document": str(design_dir / "phase0-orphaned.md"),
                        "project_path": str(dirs["project_dir"]),
                    },
                )
            )
            db.add(
                Task(
                    id="task-phase0-orphaned",
                    raw_description="Execute product_requirements",
                    enriched_description="Execute product_requirements",
                    done_definition="d",
                    status="failed",
                    failure_reason="Orphaned: never dispatched to an agent",
                    workflow_id="wf-phase0-orphaned",
                )
            )

        resp = client.get(f"/api/autopilot/projects/{pid}/designs/phase0-orphaned.md/status")
        assert resp.status_code == 200, resp.text
        features = resp.json()["features"]
        phase0 = next(f for f in features if f["id"] == "phase0-wf-phase0-orphaned")

        assert phase0["status"] != "failed"


class TestPhase0ReviewAction:
    """POST /features/{feature_id}/review for a "phase0-{workflow_id}"
    pseudo-feature -- approve clears the review pause the same way the
    Resume action does; request_changes creates a redo task on the
    feature_architect phase and leaves the workflow paused for a second
    look, mirroring the real-feature request_changes flow."""

    def _create_project(self, client, dirs):
        resp = client.post(
            "/api/autopilot/projects",
            json={"name": "Test", "base_dir": str(dirs["project_dir"])},
        )
        return resp.json()["id"]

    def _seed_paused_phase0(self, pid, dirs, workflow_id):
        from src.core.database import Phase, Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id=workflow_id,
                    name="Phase 0",
                    definition_id="feature_architect",
                    phases_folder_path="/tmp",
                    status="paused",
                    paused_by="review",
                    project_id=pid,
                    launch_params={"project_path": str(dirs["project_dir"])},
                )
            )
            db.add(
                Phase(
                    id=f"{workflow_id}-arch",
                    workflow_id=workflow_id,
                    order=1,
                    name="feature_architect",
                    description="Decompose the design.",
                    done_definitions=["x"],
                )
            )

    def test_approve_clears_pause(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        self._seed_paused_phase0(pid, dirs, "wf-phase0-approve")

        resp = client.post(
            "/api/autopilot/features/phase0-wf-phase0-approve/review",
            json={"action": "approve"},
        )
        assert resp.status_code == 200, resp.text

        from src.core.database import Workflow, get_db

        with get_db() as db:
            wf = db.query(Workflow).filter_by(id="wf-phase0-approve").first()
            assert wf.status == "active"
            assert wf.paused_by is None

    def test_approve_rejected_while_redo_task_in_flight(self, project_client):
        """Approving while a request_changes redo agent is still working
        would let run_phase0 read a half-written features.json and then
        delete the worktree out from under that agent -- must be blocked
        until the redo settles."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        self._seed_paused_phase0(pid, dirs, "wf-phase0-approve-race")

        from src.core.database import Task, get_db

        with get_db() as db:
            db.add(
                Task(
                    id="task-phase0-redo-in-flight",
                    workflow_id="wf-phase0-approve-race",
                    phase_id="wf-phase0-approve-race-arch",
                    raw_description="Execute feature_architect",
                    enriched_description="Execute feature_architect",
                    done_definition="d",
                    status="in_progress",
                )
            )

        resp = client.post(
            "/api/autopilot/features/phase0-wf-phase0-approve-race/review",
            json={"action": "approve"},
        )
        assert resp.status_code == 409, resp.text

        from src.core.database import Workflow

        with get_db() as db:
            wf = db.query(Workflow).filter_by(id="wf-phase0-approve-race").first()
            # Still paused -- the blocked approve must not have cleared it.
            assert wf.paused_by == "review"

    def test_request_changes_requires_feedback(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        self._seed_paused_phase0(pid, dirs, "wf-phase0-nofeedback")

        resp = client.post(
            "/api/autopilot/features/phase0-wf-phase0-nofeedback/review",
            json={"action": "request_changes"},
        )
        assert resp.status_code == 400

    def test_request_changes_creates_redo_task_and_keeps_paused(self, project_client, monkeypatch):
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        self._seed_paused_phase0(pid, dirs, "wf-phase0-redo")

        from src.mcp.autopilot import feature_review_routes as autopilot_api

        spawn_mock = AsyncMock()
        monkeypatch.setattr(autopilot_api, "_spawn_agent_for_task", spawn_mock)

        resp = client.post(
            "/api/autopilot/features/phase0-wf-phase0-redo/review",
            json={"action": "request_changes", "feedback": "Split the auth feature further"},
        )
        assert resp.status_code == 200, resp.text

        from src.core.database import Task, TaskPromptOverride, Workflow, get_db

        with get_db() as db:
            wf = db.query(Workflow).filter_by(id="wf-phase0-redo").first()
            # Still awaiting review -- request_changes doesn't clear the
            # pause, the human must approve again after the redo.
            assert wf.paused_by == "review"

            tasks = db.query(Task).filter_by(workflow_id="wf-phase0-redo").all()
            assert len(tasks) == 1
            new_task = tasks[0]
            assert new_task.status == "pending"
            assert new_task.phase_id == "wf-phase0-redo-arch"

            override = db.query(TaskPromptOverride).filter_by(task_id=new_task.id).first()
            assert override is not None
            assert "Split the auth feature further" in override.user_prompt

        spawn_mock.assert_called_once()

    def test_request_changes_reuses_still_pending_redo_task(self, project_client, monkeypatch):
        """A second request_changes call before the first redo agent has
        even been dispatched (still 'pending') must update that task in
        place, not create a second one -- otherwise two agents can end up
        racing on the same worktree's features.json."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        self._seed_paused_phase0(pid, dirs, "wf-phase0-redo2")

        from src.mcp.autopilot import feature_review_routes as autopilot_api

        spawn_mock = AsyncMock()
        monkeypatch.setattr(autopilot_api, "_spawn_agent_for_task", spawn_mock)

        resp1 = client.post(
            "/api/autopilot/features/phase0-wf-phase0-redo2/review",
            json={"action": "request_changes", "feedback": "First round of feedback"},
        )
        assert resp1.status_code == 200, resp1.text

        resp2 = client.post(
            "/api/autopilot/features/phase0-wf-phase0-redo2/review",
            json={"action": "request_changes", "feedback": "Second round of feedback"},
        )
        assert resp2.status_code == 200, resp2.text

        from src.core.database import Task, TaskPromptOverride, get_db

        with get_db() as db:
            tasks = db.query(Task).filter_by(workflow_id="wf-phase0-redo2").all()
            assert len(tasks) == 1, "second request_changes must reuse the still-pending task, not add a new one"
            reused_task = tasks[0]
            assert reused_task.status == "pending"

            override = db.query(TaskPromptOverride).filter_by(task_id=reused_task.id).first()
            assert override is not None
            # Prefixed, not replaced -- mirrors the real-feature restartable
            # path, so an earlier round's feedback isn't silently dropped.
            assert "Second round of feedback" in override.user_prompt
            assert "First round of feedback" in override.user_prompt
            assert override.user_prompt.index("Second round of feedback") < override.user_prompt.index("First round of feedback")

        assert spawn_mock.call_count == 2

    def test_request_changes_does_not_restart_task_with_live_agent(self, project_client, monkeypatch):
        """If the prior redo task is 'in_progress' with a still-live
        (non-terminated) agent, a second request_changes must not touch it
        -- creating a competing task would race the live agent on the same
        worktree."""
        client, dirs = project_client
        pid = self._create_project(client, dirs)
        self._seed_paused_phase0(pid, dirs, "wf-phase0-live-agent")

        from src.core.database import Agent, Task, get_db

        with get_db() as db:
            db.add(
                Agent(
                    id="agent-phase0-live",
                    system_prompt="p",
                    status="working",
                    cli_type="claude",
                )
            )
            db.add(
                Task(
                    id="task-phase0-in-progress",
                    workflow_id="wf-phase0-live-agent",
                    phase_id="wf-phase0-live-agent-arch",
                    raw_description="Execute feature_architect",
                    enriched_description="Execute feature_architect",
                    done_definition="d",
                    status="in_progress",
                    assigned_agent_id="agent-phase0-live",
                )
            )

        from src.mcp.autopilot import feature_review_routes as autopilot_api

        spawn_mock = AsyncMock()
        monkeypatch.setattr(autopilot_api, "_spawn_agent_for_task", spawn_mock)

        resp = client.post(
            "/api/autopilot/features/phase0-wf-phase0-live-agent/review",
            json={"action": "request_changes", "feedback": "More feedback"},
        )
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            tasks = db.query(Task).filter_by(workflow_id="wf-phase0-live-agent").all()
            assert len(tasks) == 2, "a new task must be created rather than restarting the live-agent one"
            live_task = next(t for t in tasks if t.id == "task-phase0-in-progress")
            assert live_task.status == "in_progress"
            assert live_task.assigned_agent_id == "agent-phase0-live"

    def test_approve_on_non_review_pause_is_idempotent(self, project_client):
        client, dirs = project_client
        pid = self._create_project(client, dirs)

        from src.core.database import Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-phase0-not-paused",
                    name="Phase 0",
                    definition_id="feature_architect",
                    phases_folder_path="/tmp",
                    status="active",
                    project_id=pid,
                )
            )

        resp = client.post(
            "/api/autopilot/features/phase0-wf-phase0-not-paused/review",
            json={"action": "approve"},
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True


class TestProjectPathTraversal:
    def test_design_content_rejects_traversal(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        pid = resp.json()["id"]

        resp = client.get(
            f"/api/autopilot/projects/{pid}/designs/../../etc/passwd/content"
        )
        assert resp.status_code in (400, 404)

    def test_design_remove_rejects_traversal(self, project_client):
        client, dirs = project_client
        resp = client.post(
            "/api/autopilot/projects",
            json={
                "name": "Test",
                "base_dir": str(dirs["project_dir"]),
            },
        )
        pid = resp.json()["id"]

        resp = client.delete(f"/api/autopilot/projects/{pid}/designs/../../etc/passwd")
        assert resp.status_code in (400, 404)


class TestCleanupBranchesProjectScoping:
    """POST /cleanup-branches used to construct WorktreeManager with no
    .reload(), so it silently operated on whatever project happened to be
    config.main_repo_path's current global default -- wrong project as
    soon as more than one exists. Live impact: a real cleanup run swept
    zero of ~25 stale worktrees sitting in a different project's repo,
    with no error surfaced anywhere. Fixed by passing repo_path directly
    to the constructor (WorktreeManager.__init__ opens the repo from it
    inline -- functionally identical to reload(), just not a separate
    call), so these check the constructor's repo_path kwarg, not .reload()."""

    def test_explicit_project_path_is_used(self, project_client, monkeypatch):
        client, dirs = project_client
        target = str(dirs["project_dir"])

        with patch("src.core.worktree_manager.WorktreeManager") as MockWtMgr:
            mock_instance = MockWtMgr.return_value
            mock_instance.cleanup_all_stale_branches.return_value = {
                "cleaned": 0,
                "merged": 0,
                "failed": 0,
                "worktrees_cleaned": 0,
                "branches": [],
            }
            resp = client.post(
                "/api/autopilot/cleanup-branches",
                params={"project_path": target},
            )

        assert resp.status_code == 200
        MockWtMgr.assert_called_once_with(ANY, repo_path=target)

    def test_falls_back_to_active_project_when_omitted(self, project_client):
        client, dirs = project_client
        target = str(dirs["project_dir"])

        from src.core.database import AutopilotProject, get_db

        with get_db() as db:
            proj = AutopilotProject(
                id="proj-active-1", name="Active", base_dir=target, is_active=True
            )
            db.add(proj)

        with patch("src.core.worktree_manager.WorktreeManager") as MockWtMgr:
            mock_instance = MockWtMgr.return_value
            mock_instance.cleanup_all_stale_branches.return_value = {
                "cleaned": 0,
                "merged": 0,
                "failed": 0,
                "worktrees_cleaned": 0,
                "branches": [],
            }
            resp = client.post("/api/autopilot/cleanup-branches")

        assert resp.status_code == 200
        MockWtMgr.assert_called_once_with(ANY, repo_path=target)

    def test_no_project_path_and_no_active_project_is_rejected(self, project_client):
        client, dirs = project_client

        resp = client.post("/api/autopilot/cleanup-branches")

        assert resp.status_code == 400


class TestCostEntryAgentBinding:
    """ticket-5a75167a: POST /cost-entries authenticated a caller's identity
    but never bound it to the entry being written -- a caller authenticated
    as one real agent could supply a *different* agent_id in the body and
    post a cost entry impersonating another agent's task. System/SDK
    identities have no single agent to bind to (they post cost entries on
    behalf of whichever agent/task they're servicing), so only a real
    per-agent UUID caller is bound to its own authenticated identity."""

    def _mock_cost_stack(self, monkeypatch):
        from contextlib import contextmanager
        from unittest.mock import MagicMock

        import src.core.cost_derivation as cost_derivation_mod
        import src.core.database as database_mod

        recorded = {}

        def fake_record_cost(**kwargs):
            recorded.update(kwargs)
            entry = MagicMock()
            entry.id = "entry-1"
            entry.cost_usd = kwargs["cost_usd"]
            return entry

        monkeypatch.setattr(cost_derivation_mod, "record_cost", fake_record_cost)

        @contextmanager
        def fake_get_db():
            yield MagicMock()

        monkeypatch.setattr(database_mod, "get_db", fake_get_db)
        return recorded

    def test_real_agent_cannot_claim_a_different_agent_id(self, client, monkeypatch):
        from src.mcp.autopilot import cost_routes as api_mod

        monkeypatch.setattr(
            api_mod, "verify_agent_authentication", AsyncMock(return_value=True)
        )
        self._mock_cost_stack(monkeypatch)

        resp = client.post(
            "/api/autopilot/cost-entries",
            json={
                "task_id": "someone-elses-task",
                "agent_id": "someone-elses-agent",
                "source": "pi",
                "cost_usd": 0.01,
            },
            headers={"X-Agent-ID": "11111111-1111-1111-1111-111111111111"},
        )
        assert resp.status_code == 403

    def test_real_agent_id_matching_header_is_allowed(self, client, monkeypatch):
        from src.mcp.autopilot import cost_routes as api_mod

        monkeypatch.setattr(
            api_mod, "verify_agent_authentication", AsyncMock(return_value=True)
        )
        recorded = self._mock_cost_stack(monkeypatch)

        own_id = "11111111-1111-1111-1111-111111111111"
        resp = client.post(
            "/api/autopilot/cost-entries",
            json={
                "task_id": "my-task",
                "agent_id": own_id,
                "source": "pi",
                "cost_usd": 0.01,
            },
            headers={"X-Agent-ID": own_id},
        )
        assert resp.status_code == 200
        assert recorded["agent_id"] == own_id

    def test_system_identity_may_post_on_behalf_of_any_agent(self, client, monkeypatch):
        from src.mcp.autopilot import cost_routes as api_mod

        monkeypatch.setattr(
            api_mod, "verify_agent_authentication", AsyncMock(return_value=True)
        )
        recorded = self._mock_cost_stack(monkeypatch)

        resp = client.post(
            "/api/autopilot/cost-entries",
            json={
                "task_id": "some-task",
                "agent_id": "some-other-real-agent",
                "source": "pi",
                "cost_usd": 0.01,
            },
            headers={"X-Agent-ID": "orchestrator"},
        )
        assert resp.status_code == 200
        assert recorded["agent_id"] == "some-other-real-agent"


class TestDeleteFeature:
    """DELETE /features/{feature_id}: an old/stuck feature (dead-end
    workflow, no path back to "done") had no way to actually disappear
    from the queue -- pause/stop/resume/rerun all assume the work is still
    salvageable. This removes the feature, its workflow, its tasks, and
    dependent records outright."""

    def test_deletes_feature_with_no_workflow(self, project_client):
        client, dirs = project_client
        from src.core.database import Feature, get_db

        with get_db() as db:
            db.add(
                Feature(
                    id="feat-1", design_id="does-not-matter", feature_key="x",
                    name="X", scope="s", status="pending",
                )
            )

        resp = client.delete("/api/autopilot/features/feat-1")
        assert resp.status_code == 200
        assert resp.json() == {"success": True, "feature_id": "feat-1"}

        with get_db() as db:
            assert db.query(Feature).filter_by(id="feat-1").first() is None

    def test_deletes_feature_workflow_and_tasks(self, project_client):
        client, dirs = project_client
        from src.core.database import Feature, Task, Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-del-1", name="t", phases_folder_path="/tmp",
                    status="active", definition_id="autopilot",
                )
            )
            db.add(
                Feature(
                    id="feat-2", design_id="does-not-matter", feature_key="y",
                    name="Y", scope="s", status="active", workflow_id="wf-del-1",
                )
            )
            db.add(
                Task(
                    id="task-del-1", workflow_id="wf-del-1", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="pending",
                )
            )

        resp = client.delete("/api/autopilot/features/feat-2")
        assert resp.status_code == 200

        with get_db() as db:
            assert db.query(Feature).filter_by(id="feat-2").first() is None
            assert db.query(Workflow).filter_by(id="wf-del-1").first() is None
            assert db.query(Task).filter_by(id="task-del-1").first() is None

    def test_deletes_feature_with_cost_history(self, project_client):
        """CostEntry.task_id/workflow_id are also enforced FKs -- a feature
        that ever recorded real LLM cost (the common case, not the
        exception, now that cost tracking exists) would otherwise fail to
        delete with an IntegrityError."""
        client, dirs = project_client
        from src.core.database import CostEntry, Feature, Task, Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-del-cost", name="t", phases_folder_path="/tmp",
                    status="active", definition_id="autopilot",
                )
            )
            db.add(
                Feature(
                    id="feat-cost", design_id="does-not-matter", feature_key="c",
                    name="C", scope="s", status="active", workflow_id="wf-del-cost",
                )
            )
            db.add(
                Task(
                    id="task-del-cost", workflow_id="wf-del-cost", phase_id="phase-1",
                    raw_description="r", done_definition="d", status="done",
                )
            )
            db.add(
                CostEntry(
                    id="cost-del-1", task_id="task-del-cost", workflow_id="wf-del-cost",
                    source="pi", cost_usd=0.05,
                )
            )

        resp = client.delete("/api/autopilot/features/feat-cost")
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            assert db.query(Feature).filter_by(id="feat-cost").first() is None
            assert db.query(CostEntry).filter_by(id="cost-del-1").first() is None

    def test_deletes_feature_with_phases_and_phase_executions(self, project_client):
        """Phase rows (and their PhaseExecution children) were never
        cleaned up, and the PhaseExecution cleanup that did exist filtered
        on the unused workflow_execution_id column instead of joining
        through Phase.workflow_id -- both left phases.workflow_id (and
        phase_executions.phase_id) FK rows behind, so DELETE FROM workflows
        failed with an IntegrityError. tasks.phase_id also FKs to phases.id,
        so Task (via its own phase_id, not just workflow_id) must be
        deleted before Phase or DELETE FROM phases fails the same way."""
        client, dirs = project_client
        from src.core.database import Feature, Phase, PhaseExecution, Task, Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-del-phase", name="t", phases_folder_path="/tmp",
                    status="active", definition_id="autopilot",
                )
            )
            db.add(
                Feature(
                    id="feat-phase", design_id="does-not-matter", feature_key="p",
                    name="P", scope="s", status="active", workflow_id="wf-del-phase",
                )
            )
            db.add(
                Phase(
                    id="phase-del-1", workflow_id="wf-del-phase", order=1,
                    name="p1", description="d", done_definitions=[],
                )
            )
            db.add(
                PhaseExecution(
                    id="phase-exec-del-1", phase_id="phase-del-1", status="completed",
                )
            )
            db.add(
                Task(
                    id="task-del-phase", workflow_id="wf-del-phase", phase_id="phase-del-1",
                    raw_description="r", done_definition="d", status="done",
                )
            )

        resp = client.delete("/api/autopilot/features/feat-phase")
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            assert db.query(Feature).filter_by(id="feat-phase").first() is None
            assert db.query(Workflow).filter_by(id="wf-del-phase").first() is None
            assert db.query(Phase).filter_by(id="phase-del-1").first() is None
            assert db.query(PhaseExecution).filter_by(id="phase-exec-del-1").first() is None
            assert db.query(Task).filter_by(id="task-del-phase").first() is None

    def test_terminates_assigned_agent_before_deleting(self, project_client, monkeypatch):
        client, dirs = project_client
        from src.core.database import Agent, Feature, Task, Workflow, get_db

        with get_db() as db:
            db.add(
                Workflow(
                    id="wf-del-2", name="t", phases_folder_path="/tmp",
                    status="active", definition_id="autopilot",
                )
            )
            db.add(
                Feature(
                    id="feat-3", design_id="does-not-matter", feature_key="z",
                    name="Z", scope="s", status="active", workflow_id="wf-del-2",
                )
            )
            db.add(
                Agent(id="agent-del-1", system_prompt="p", status="working", cli_type="claude")
            )
            db.add(
                Task(
                    id="task-del-2", workflow_id="wf-del-2", phase_id="phase-1",
                    raw_description="r", done_definition="d",
                    status="in_progress", assigned_agent_id="agent-del-1",
                )
            )

        mock_state = Mock()
        mock_state.agent_manager.terminate_agent = AsyncMock()
        monkeypatch.setattr(
            "src.core.app_context.get_app_state", lambda: mock_state
        )

        resp = client.delete("/api/autopilot/features/feat-3")
        assert resp.status_code == 200
        mock_state.agent_manager.terminate_agent.assert_awaited_once_with("agent-del-1")

        with get_db() as db:
            assert db.query(Feature).filter_by(id="feat-3").first() is None

    def test_missing_feature_returns_404(self, project_client):
        client, dirs = project_client
        resp = client.delete("/api/autopilot/features/does-not-exist")
        assert resp.status_code == 404


# ── stop_pipeline ─────────────────────────────────────────────────


@pytest.fixture
def stop_pipeline_client(tmp_path, monkeypatch):
    """Real DB with one is_active AutopilotProject, wired to a fake
    AutopilotService so stop_pipeline never needs a real running pipeline."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)

    from src.core.database import AutopilotProject, DatabaseManager

    db_manager = DatabaseManager(db_path)
    db_manager.create_tables()

    with db_manager.session_scope() as session:
        session.add(
            AutopilotProject(id="proj-stop", name="proj-stop", base_dir=str(tmp_path), is_active=True)
        )

    fake_service = Mock()
    fake_service.stop = AsyncMock(return_value={"stopped": True})
    monkeypatch.setattr(
        "src.autopilot.service.get_autopilot_service", lambda project_id: fake_service
    )

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    from src.mcp.autopilot import _shared as api_mod

    api_mod._cache.clear()
    yield client
    api_mod._cache.clear()


class TestStopPipelineDeactivatesProject:
    """Regression: stop_pipeline's is_active-clearing step referenced
    AutopilotProject without importing it in this function's scope --
    a bare NameError on every call, caught and swallowed by the broad
    except Exception below it. Since get_db()'s context manager rolls
    back on any exception raised inside the `with` block, this didn't
    just skip clearing is_active -- it rolled back pause_project_workflows'
    changes too, silently, on every single stop_pipeline call."""

    def test_deactivates_the_stopped_project(self, stop_pipeline_client):
        from src.core.database import AutopilotProject, get_db

        resp = stop_pipeline_client.post(
            "/api/autopilot/stop", params={"project_id": "proj-stop"}
        )
        assert resp.status_code == 200, resp.text

        with get_db() as db:
            proj = db.query(AutopilotProject).filter_by(id="proj-stop").first()
            assert proj.is_active is False


# ── Router aggregation guard ────────────────────────────────────────────


class TestRouterAggregation:
    # Every route the pre-split autopilot_api module exposed
    # (backend_module_decomposition.md §6). Pinned as a set rather than a
    # count: a bare count assertion goes red the first time a route is
    # legitimately added (it did — three multi-project activation routes
    # took it from 63 to 66), and a permanently-red guardrail stops
    # guarding anything. A subset check keeps catching the failure mode
    # this actually exists for -- include_router() wiring silently
    # dropping a route -- while staying green as the surface grows.
    PRE_SPLIT_ROUTES = frozenset({
        ("POST", "/api/autopilot/cleanup-branches"),
        ("POST", "/api/autopilot/cost-entries"),
        ("POST", "/api/autopilot/designs/add"),
        ("GET", "/api/autopilot/designs/{design_id}/costs"),
        ("GET", "/api/autopilot/feature-records/{feature_id}/docs"),
        ("GET", "/api/autopilot/feature-records/{feature_id}/docs/{doc_name}"),
        ("GET", "/api/autopilot/feature-records/{feature_id}/report"),
        ("GET", "/api/autopilot/features"),
        ("DELETE", "/api/autopilot/features/{feature_id}"),
        ("GET", "/api/autopilot/features/{feature_id}"),
        ("GET", "/api/autopilot/features/{feature_id}/costs"),
        ("GET", "/api/autopilot/features/{feature_id}/docs/{doc_name}"),
        ("GET", "/api/autopilot/features/{feature_id}/download"),
        ("GET", "/api/autopilot/features/{feature_id}/logs"),
        ("GET", "/api/autopilot/features/{feature_id}/logs/{log_name}"),
        ("POST", "/api/autopilot/features/{feature_id}/pause"),
        ("GET", "/api/autopilot/features/{feature_id}/report"),
        ("POST", "/api/autopilot/features/{feature_id}/resume"),
        ("POST", "/api/autopilot/features/{feature_id}/review"),
        ("GET", "/api/autopilot/health"),
        ("GET", "/api/autopilot/input"),
        ("POST", "/api/autopilot/input"),
        ("DELETE", "/api/autopilot/input/{request_id}"),
        ("GET", "/api/autopilot/logs"),
        ("GET", "/api/autopilot/messages"),
        ("POST", "/api/autopilot/messages/archive"),
        ("GET", "/api/autopilot/messages/archived"),
        ("POST", "/api/autopilot/messages/cleanup-archives"),
        ("POST", "/api/autopilot/messages/unarchive"),
        ("POST", "/api/autopilot/messages/unarchive-all"),
        ("GET", "/api/autopilot/projects"),
        ("POST", "/api/autopilot/projects"),
        ("DELETE", "/api/autopilot/projects/{project_id}"),
        ("GET", "/api/autopilot/projects/{project_id}"),
        ("PUT", "/api/autopilot/projects/{project_id}"),
        ("GET", "/api/autopilot/projects/{project_id}/browse"),
        ("GET", "/api/autopilot/projects/{project_id}/browse/content"),
        ("GET", "/api/autopilot/projects/{project_id}/costs"),
        ("GET", "/api/autopilot/projects/{project_id}/designs"),
        ("POST", "/api/autopilot/projects/{project_id}/designs"),
        ("POST", "/api/autopilot/projects/{project_id}/designs/reload"),
        ("PUT", "/api/autopilot/projects/{project_id}/designs/reorder"),
        ("DELETE", "/api/autopilot/projects/{project_id}/designs/{filename}"),
        ("GET", "/api/autopilot/projects/{project_id}/designs/{filename}/content"),
        ("GET", "/api/autopilot/projects/{project_id}/designs/{filename}/status"),
        ("PATCH", "/api/autopilot/projects/{project_id}/review-mode"),
        ("POST", "/api/autopilot/projects/{project_id}/sync"),
        ("GET", "/api/autopilot/queue"),
        ("POST", "/api/autopilot/queue"),
        ("POST", "/api/autopilot/queue/reorder"),
        ("POST", "/api/autopilot/queue/repair"),
        ("GET", "/api/autopilot/queue/repair/{repair_id}"),
        ("POST", "/api/autopilot/queue/requeue"),
        ("POST", "/api/autopilot/queue/rerun"),
        ("DELETE", "/api/autopilot/queue/{filename}"),
        ("GET", "/api/autopilot/queue/{filename}/content"),
        ("POST", "/api/autopilot/start"),
        ("GET", "/api/autopilot/status"),
        ("POST", "/api/autopilot/stop"),
        ("GET", "/api/autopilot/tasks/{task_id}/costs"),
        ("GET", "/api/autopilot/workflows/{workflow_id}/costs"),
        ("GET", "/api/autopilot/workflows/{workflow_id}/decomposition_review"),
        ("GET", "/api/autopilot/workflows/{workflow_id}/feature_report"),
    })

    def test_no_pre_split_route_was_dropped(self):
        from src.mcp.autopilot import router

        def _flatten(routes):
            # FastAPI >= 0.137 wraps include_router() in lazy _IncludedRouter
            # objects; expand them to the concrete leaf routes
            out = []
            for r in routes:
                if hasattr(r, "effective_candidates"):
                    out.extend(_flatten(r.effective_candidates()))
                else:
                    out.append(r)
            return out

        flat = _flatten(router.routes)
        current = {
            (method, r.path)
            for r in flat
            for method in (getattr(r, "methods", None) or set())
        }
        missing = self.PRE_SPLIT_ROUTES - current
        assert not missing, f"routes lost since the split: {sorted(missing)}"

        paths = {r.path for r in flat}
        for critical in (
            "/api/autopilot/status",
            "/api/autopilot/health",
            "/api/autopilot/queue",
            "/api/autopilot/projects",
            "/api/autopilot/features",
            "/api/autopilot/messages",
            "/api/autopilot/input",
        ):
            assert critical in paths, f"missing critical route {critical}"
