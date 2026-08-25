"""Regression coverage for Phase 3 Tier 2 item 13
(docs/AUTOPILOT_REFACTOR_PLAN.md): blocking subprocess.run calls in async
route handlers that weren't offloaded via run_in_executor, so they blocked
the whole FastAPI process's event loop -- every other request being served
-- for as long as the subprocess took to respond.

Covers the three sites found by this item's fresh audit (item 10's
get_commit_diff_endpoint fix has its own coverage in
tests/test_mcp_server_tickets.py::TestGetCommitDiffTimeouts, which also
now exercises the offloaded path end-to-end):
- control_routes.get_system_health -> run_health_audit
- design_file_routes.remove_project_design's per-agent tmux kill-session
- feature_review_routes.review_feature's `gh pr merge`
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.database import Agent, DatabaseManager, Feature, Task, Workflow


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


@pytest.mark.asyncio
async def test_get_system_health_offloads_run_health_audit():
    from src.mcp.autopilot.control_routes import get_system_health

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value={"findings": []})

    with patch("asyncio.get_event_loop", return_value=fake_loop):
        result = await get_system_health()

    assert result == {"findings": []}
    fake_loop.run_in_executor.assert_called_once()
    executor_arg, fn_arg = fake_loop.run_in_executor.call_args.args
    assert executor_arg is None
    assert fn_arg.__name__ == "run_health_audit"


def test_run_health_audit_checks_unmerged_branches_for_every_active_project(
    db, tmp_path, monkeypatch
):
    """Regression: run_health_audit's unmerged-branches check used
    .filter_by(is_active=True).first() -- under the documented
    concurrent-active-projects model (max_concurrent_projects), more than
    one project can be active at once, and .first() silently skipped
    every active project except whichever one the query happened to
    return."""
    from src.core.database import AutopilotProject
    from src.mcp.autopilot.control_routes import run_health_audit

    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(tmp_path / "test.db"))

    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()

    session = db.get_session()
    session.add(AutopilotProject(
        id="proj-a", name="proj-a", base_dir=str(project_a),
        is_active=True, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))
    session.add(AutopilotProject(
        id="proj-b", name="proj-b", base_dir=str(project_b),
        is_active=True, created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    ))
    session.commit()
    session.close()

    def fake_run(cmd, **kwargs):
        result = MagicMock()
        if cmd[:2] == ["git", "branch"]:
            cwd = kwargs.get("cwd")
            if cwd == str(project_a):
                result.returncode = 0
                result.stdout = "agent-a1\n"
            elif cwd == str(project_b):
                result.returncode = 0
                result.stdout = "agent-b1\nagent-b2\n"
            else:
                result.returncode = 1
                result.stdout = ""
        else:
            result.returncode = 1
            result.stdout = ""
        return result

    with patch("subprocess.run", side_effect=fake_run):
        result = run_health_audit(db_manager=db)

    unmerged = [f for f in result["findings"] if f["type"] == "unmerged_branches"]
    assert {f["project_path"] for f in unmerged} == {str(project_a), str(project_b)}


@pytest.mark.asyncio
async def test_remove_project_design_offloads_tmux_kill(db, tmp_path):
    from src.core.database import AutopilotDesign, AutopilotProject
    from src.mcp.autopilot import design_file_routes

    session = db.get_session()
    session.add(
        AutopilotProject(
            id="proj-1",
            name="proj-1",
            base_dir=str(tmp_path),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    session.add(
        AutopilotDesign(
            id="des-1",
            project_id="proj-1",
            filename="des.md",
            name="des",
        )
    )
    session.add(
        Workflow(
            id="wf-1",
            name="wf-1",
            phases_folder_path="/tmp",
            status="failed",
            created_at=datetime.utcnow(),
            design_id="des-1",
            # No working_directory -- keeps the worktree-cleanup branch
            # (unrelated to this item) a no-op.
        )
    )
    session.add(
        Task(
            id="task-1",
            raw_description="do it",
            done_definition="done",
            status="in_progress",
            workflow_id="wf-1",
            assigned_agent_id="agent-1",
        )
    )
    session.add(
        Agent(
            id="agent-1",
            system_prompt="p",
            status="working",
            cli_type="test",
            current_task_id="task-1",
            tmux_session_name="agent-tmux-1",
        )
    )
    session.commit()
    session.close()

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(return_value=None)

    with (
        patch("asyncio.get_event_loop", return_value=fake_loop),
        patch("src.core.database.get_db", return_value=_SessionCtx(db)),
        patch(
            "src.autopilot.orchestrator.engine_client.terminate_agent",
            return_value=True,
        ),
        patch.object(design_file_routes, "_invalidate", return_value=None),
    ):
        result = await design_file_routes.remove_project_design("proj-1", "des.md", agent_id="ui-user")

    assert result == {"removed": "des.md"}
    fake_loop.run_in_executor.assert_called_once()
    executor_arg, fn_arg = fake_loop.run_in_executor.call_args.args
    assert executor_arg is None
    assert fn_arg.func.__name__ == "run"
    assert fn_arg.args[0] == ["tmux", "kill-session", "-t", "agent-tmux-1"]


class _SessionCtx:
    def __init__(self, db_manager):
        self._db_manager = db_manager

    def __enter__(self):
        return self._db_manager.get_session()

    def __exit__(self, *a):
        return False


@pytest.mark.asyncio
async def test_review_feature_offloads_gh_pr_merge(db):
    from src.mcp.autopilot import feature_review_routes

    session = db.get_session()
    session.add(
        Workflow(
            id="wf-1",
            name="wf-1",
            phases_folder_path="/tmp",
            status="active",
            created_at=datetime.utcnow(),
            paused_by="review",
            working_directory=None,
        )
    )
    session.add(
        Feature(
            id="feat-1",
            design_id="des-1",
            feature_key="feat-1",
            name="Feature 1",
            scope="Feature 1 scope",
            workflow_id="wf-1",
            status="active",
            pr_url="https://github.com/org/repo/pull/1",
        )
    )
    session.commit()
    session.close()

    fake_loop = MagicMock()
    fake_loop.run_in_executor = AsyncMock(
        return_value=MagicMock(returncode=0, stdout="", stderr="")
    )

    with (
        patch("asyncio.get_event_loop", return_value=fake_loop),
        patch("src.core.database.get_db", return_value=_SessionCtx(db)),
        patch(
            "src.autopilot.orchestrator.engine_client.resume_workflow",
            return_value=None,
        ),
        patch(
            "src.core.status_derivation.derive_workflow_status",
            return_value="active",
        ),
        patch.object(feature_review_routes, "_invalidate", return_value=None),
    ):
        from src.mcp.autopilot.feature_review_routes import FeatureReviewRequest

        await feature_review_routes.review_feature(
            "feat-1", FeatureReviewRequest(action="approve")
        )

    fake_loop.run_in_executor.assert_called_once()
    executor_arg, fn_arg = fake_loop.run_in_executor.call_args.args
    assert executor_arg is None
    assert fn_arg.func.__name__ == "run"
    assert fn_arg.args[0][:3] == ["gh", "pr", "merge"]
