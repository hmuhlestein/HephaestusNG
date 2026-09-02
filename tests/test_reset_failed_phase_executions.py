"""Regression: un-failing a workflow left its own phase's PhaseExecution
row stuck "failed" forever, permanently blocking that workflow (and its
feature) from ever deriving "completed" again.

A phase's PhaseExecution is marked "failed" only when its retry cap is
exhausted (_phase_case_steps.py), which fails the WORKFLOW at the same
time. Both places that un-fail a workflow -- resume_feature
(feature_routes.py) and _resume_stuck_workflow_tasks (phase_transitions.py)
-- reset the workflow's and its tasks' status back to active/pending, but
neither touched this row. Nothing in the sweep's phase-dispatch cases
(_case_start_first_phase/_case_in_progress_no_tasks/_case_in_progress_
complete/_case_completed_with_successor) even looks at a "failed"-status
phase; each filters phase_statuses to exactly "pending"/"in_progress"/
"completed". The phase can still be driven directly through the goto/retry
machinery (which never checks PhaseExecution.status) and keep completing
successfully for the rest of the pipeline -- but the row itself never
becomes "completed", so derive_workflow_status/derive_feature_status's
phase-completeness check (every PhaseExecution must be "completed" or
"skipped") can never derive "completed" again.

Observed live: workflow 72ed4df8's development phase failed its retry cap
early on; resuming un-failed the workflow, and development went on to run
and complete several more times over the following day, all the way
through deploy -- while its own PhaseExecution sat "failed" with the
original attempt's timestamps the entire time, permanently blocking the
feature from ever showing "completed" despite the pipeline genuinely
finishing.
"""

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def orch_db_env(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_failed_workflow(db, workflow_id="wf-1", feature_id="feat-1", project_id="proj-1"):
    from src.core.database import AutopilotProject, Feature, Phase, PhaseExecution, Task, Workflow

    with db.session_scope() as session:
        session.add(AutopilotProject(id=project_id, name="p", base_dir="/tmp"))
        session.add(
            Workflow(
                id=workflow_id,
                name="t",
                phases_folder_path="/tmp",
                status="failed",
                status_reason="development: 2 task(s) exhausted the retry cap",
                project_id=project_id,
            )
        )
        session.add(
            Feature(
                id=feature_id, design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id=workflow_id, status="failed",
            )
        )
        session.add(
            Phase(
                id="phase-dev", workflow_id=workflow_id, name="development",
                order=5, description="d", done_definitions=["d"],
            )
        )
        session.add(
            PhaseExecution(
                id="exec-dev", phase_id="phase-dev", workflow_execution_id=workflow_id,
                status="failed", started_at=None, completed_at=None,
            )
        )
        session.add(
            Task(
                id="task-dev-failed", workflow_id=workflow_id, phase_id="phase-dev",
                raw_description="r", done_definition="d", status="failed",
                failure_reason="retry cap exhausted",
            )
        )


class TestResetFailedPhaseExecutionsPrimitive:
    def test_resets_a_failed_execution_and_clears_its_timestamps(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import reset_failed_phase_executions
        from src.core.database import PhaseExecution
        from src.core.database import utc_now

        _seed_failed_workflow(orch_db_env)
        with orch_db_env.session_scope() as session:
            exec_row = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            exec_row.started_at = utc_now()
            exec_row.completed_at = utc_now()
            exec_row.task_creation_claimed_at = utc_now()

        n = reset_failed_phase_executions("wf-1")
        assert n == 1

        with orch_db_env.session_scope() as session:
            exec_row = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            assert exec_row.status == "pending"
            assert exec_row.started_at is None
            assert exec_row.completed_at is None
            assert exec_row.task_creation_claimed_at is None

    def test_does_not_touch_a_completed_or_pending_execution(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import reset_failed_phase_executions
        from src.core.database import Phase, PhaseExecution

        _seed_failed_workflow(orch_db_env)
        with orch_db_env.session_scope() as session:
            session.add(Phase(
                id="phase-done", workflow_id="wf-1", name="scope_review",
                order=2, description="d", done_definitions=["d"],
            ))
            session.add(PhaseExecution(
                id="exec-done", phase_id="phase-done", workflow_execution_id="wf-1",
                status="completed",
            ))

        reset_failed_phase_executions("wf-1")

        with orch_db_env.session_scope() as session:
            done = session.query(PhaseExecution).filter_by(id="exec-done").first()
            assert done.status == "completed"

    def test_is_a_noop_when_nothing_is_failed(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import reset_failed_phase_executions
        from src.core.database import AutopilotProject, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
            session.add(Workflow(id="wf-1", name="t", phases_folder_path="/tmp", status="active"))

        assert reset_failed_phase_executions("wf-1") == 0


class TestUnfailPathsResetTheStalePhaseExecution:
    """Both real un-fail call sites, exercised end to end."""

    @pytest.mark.asyncio
    async def test_resume_feature_resets_the_failed_phase_execution(
        self, orch_db_env, monkeypatch
    ):
        _seed_failed_workflow(orch_db_env)

        from src.mcp.autopilot import feature_routes

        monkeypatch.setattr(feature_routes, "_spawn_agent_for_task", AsyncMock())

        from src.mcp.autopilot.feature_routes import resume_feature

        result = await resume_feature("feat-1")
        assert result["success"] is True

        from src.core.database import PhaseExecution, Workflow

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            exec_row = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            assert exec_row.status == "pending"

    def test_resume_stuck_workflow_tasks_resets_the_failed_phase_execution(self, orch_db_env):
        from unittest.mock import Mock

        _seed_failed_workflow(orch_db_env)

        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks

        _resume_stuck_workflow_tasks("wf-1", Mock())

        from src.core.database import PhaseExecution, Workflow

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            exec_row = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            assert exec_row.status == "pending"
