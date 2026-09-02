"""The abandoned-workflow recovery could not see a "failed" phase execution.

Third route into the same blind spot 6c13cd02 fixed for the retry-cap and
active-workflow cases: a "failed" PhaseExecution is invisible to every
_advance_phases dispatch case (all four filter phase_statuses to exactly
"pending"/"in_progress"/"completed"). _recover_abandoned_workflows_with_
completed_phase scoped itself to "in_progress" executions only, so a
workflow abandoned while one of its phases sat failed could never be
re-entered by anything at all.

Widening that scope is only safe with a superseded-work guard in BOTH
directions, which is the other half of this change -- see
test_leaves_an_orphaned_phase0_workflow_alone for the live case that
motivated it.
"""

from unittest.mock import MagicMock

import pytest

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Feature,
    Phase,
    PhaseExecution,
    Task,
    Workflow,
)

ABANDONED = "Abandoned: no agent/task activity for 10 consecutive scans -- likely lost mid-flight"


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed(
    db,
    tmp_path,
    *,
    execution_status="failed",
    definition_id="autopilot",
    with_feature=True,
    design_points_at_phase0=False,
    task_status="done",
):
    worktree = tmp_path / "wt"
    worktree.mkdir(exist_ok=True)
    with db.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        session.add(AutopilotDesign(
            id="des-1", project_id="proj-1", filename="d.md", name="d", status="active",
            phase0_workflow_id="wf-1" if design_points_at_phase0 else None,
        ))
        session.add(Workflow(
            id="wf-1", name="w", phases_folder_path="/tmp", definition_id=definition_id,
            status="failed", status_reason=ABANDONED, working_directory=str(worktree),
            design_id="des-1", project_id="proj-1",
        ))
        if with_feature:
            session.add(Feature(
                id="feat-1", design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id="wf-1", status="paused",
            ))
        session.add(Phase(
            id="phase-1", workflow_id="wf-1", name="development", order=5,
            description="d", done_definitions=["d"],
        ))
        session.add(PhaseExecution(
            id="exec-1", phase_id="phase-1", workflow_execution_id="wf-1",
            status=execution_status,
        ))
        session.add(Task(
            id="task-1", workflow_id="wf-1", phase_id="phase-1",
            raw_description="r", done_definition="d", status=task_status,
        ))
    return worktree


def _run():
    from src.autopilot.orchestrator.worktree_integration import (
        _recover_abandoned_workflows_with_completed_phase,
    )

    return _recover_abandoned_workflows_with_completed_phase(MagicMock())


class TestFailedExecutionIsNowRecoverable:
    def test_recovers_a_workflow_abandoned_with_a_failed_execution(self, db_env, tmp_path):
        _seed(db_env, tmp_path, execution_status="failed")

        assert _run() == 1

        with db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            assert wf.status_reason is None
            # Resuming alone is not enough -- the execution has to come back
            # into a status the dispatch cases can actually see.
            exec_row = session.query(PhaseExecution).filter_by(id="exec-1").first()
            assert exec_row.status == "pending"
            assert session.query(Feature).filter_by(id="feat-1").first().status == "active"

    def test_still_recovers_the_original_in_progress_case(self, db_env, tmp_path):
        """The pre-existing behaviour must be unchanged by the widening."""
        _seed(db_env, tmp_path, execution_status="in_progress")

        assert _run() == 1

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "active"

    def test_leaves_a_workflow_with_work_still_in_flight(self, db_env, tmp_path):
        """The unfinished gate applies identically to a failed execution."""
        _seed(db_env, tmp_path, execution_status="failed", task_status="in_progress")

        assert _run() == 0

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "failed"

    def test_leaves_a_workflow_with_nothing_evaluable(self, db_env, tmp_path):
        """No done task -- there is nothing for the next sweep to evaluate."""
        _seed(db_env, tmp_path, execution_status="failed", task_status="failed")

        assert _run() == 0

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "failed"


class TestSupersededWorkGuard:
    def test_leaves_an_orphaned_phase0_workflow_alone(self, db_env, tmp_path):
        """The live case that made the guard necessary: workflow 9bcdc55b, a
        feature_architect run with every task done, whose design had long
        since moved on to a later decomposition attempt. Resuming it is the
        "design started processing again by itself" bug."""
        _seed(
            db_env, tmp_path, execution_status="failed",
            definition_id="feature_architect", with_feature=False,
            design_points_at_phase0=False,
        )

        assert _run() == 0

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "failed"

    def test_recovers_a_phase0_workflow_its_design_still_points_at(self, db_env, tmp_path):
        _seed(
            db_env, tmp_path, execution_status="failed",
            definition_id="feature_architect", with_feature=False,
            design_points_at_phase0=True,
        )

        assert _run() == 1

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "active"

    def test_leaves_a_superseded_feature_workflow_alone(self, db_env, tmp_path):
        """Non-Phase0 half of the guard: no Feature points back at it."""
        _seed(db_env, tmp_path, execution_status="failed", with_feature=False)

        assert _run() == 0

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "failed"
