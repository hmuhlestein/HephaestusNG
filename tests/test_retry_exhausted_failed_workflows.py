"""A workflow failed by a retry-capped phase must recover automatically.

_phase_case_steps.py's "all failed tasks past retry cap" branch sets BOTH
PhaseExecution.status="failed" and Workflow.status="failed". Nothing in the
background sweep recovered that state:

  - _retry_exhausted_paused_workflows selects status=="paused" only.
  - _recover_abandoned_workflows_with_completed_phase requires
    status_reason LIKE "Abandoned:%" AND an in_progress phase; the
    exhausted phase's execution is "failed", so neither matches.
  - Every _advance_phases dispatch case filters phase_statuses to exactly
    "pending"/"in_progress"/"completed", so a "failed" execution is
    invisible to all four.

The only way out was a human clicking Resume. These tests pin the automated
path, and equally importantly its bounds -- automation that retries forever
is the tight-loop problem the retry cap exists to prevent.
"""

from datetime import timedelta
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
    utc_now,
)

RETRY_CAP_REASON = "development: 2 task(s) exhausted the retry cap without producing a valid output"


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed(
    db,
    *,
    status_reason=RETRY_CAP_REASON,
    failed_at_offset_seconds=-3600,
    paused_retry_count=0,
    with_feature=True,
    execution_status="failed",
):
    """A workflow in the exact state the retry-cap branch leaves behind."""
    with db.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
        session.add(AutopilotDesign(
            id="des-1", project_id="proj-1", filename="d.md", name="d", status="active",
        ))
        session.add(Workflow(
            id="wf-1", name="autopilot", phases_folder_path="/tmp",
            definition_id="autopilot", status="failed", status_reason=status_reason,
            paused_retry_count=paused_retry_count, project_id="proj-1", design_id="des-1",
        ))
        if with_feature:
            session.add(Feature(
                id="feat-1", design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id="wf-1", status="failed",
            ))
        session.add(Phase(
            id="phase-dev", workflow_id="wf-1", name="development", order=5,
            description="d", done_definitions=["d"],
        ))
        session.add(PhaseExecution(
            id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
            status=execution_status,
            completed_at=utc_now() + timedelta(seconds=failed_at_offset_seconds),
        ))
        session.add(Task(
            id="task-dev", workflow_id="wf-1", phase_id="phase-dev",
            raw_description="r", done_definition="d", status="failed",
        ))


def _run(db_env):
    from src.autopilot.orchestrator.phase_transitions import _retry_exhausted_failed_workflows

    return _retry_exhausted_failed_workflows(MagicMock())


class TestAutomaticRecovery:
    def test_resets_the_failed_execution_and_resumes_the_workflow(self, db_env):
        _seed(db_env)

        assert _run(db_env) == 1

        with db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            assert wf.status_reason is None
            # The bound is tracked, so repeated rescues can't run forever.
            assert wf.paused_retry_count == 1
            exec_row = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            assert exec_row.status == "pending"
            feature = session.query(Feature).filter_by(id="feat-1").first()
            assert feature.status == "active"

    def test_leaves_the_workflow_alone_while_still_cooling_down(self, db_env):
        """The cooldown is anchored on when the phase actually failed --
        there is no paused_at on a failed workflow to use instead."""
        _seed(db_env, failed_at_offset_seconds=-5)

        assert _run(db_env) == 0

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "failed"
            assert session.query(PhaseExecution).filter_by(id="exec-dev").first().status == "failed"

    def test_gives_up_permanently_once_the_cycle_cap_is_hit(self, db_env):
        from src.autopilot.orchestrator import _get_paused_workflow_max_retry_cycles

        _seed(db_env, paused_retry_count=_get_paused_workflow_max_retry_cycles())

        _run(db_env)

        with db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed", "must stay failed -- a human has to look at it"
            assert "manual resume required" in wf.status_reason
            assert session.query(PhaseExecution).filter_by(id="exec-dev").first().status == "failed"

    def test_the_permanent_give_up_is_not_re_announced_every_tick(self, db_env):
        from src.autopilot.orchestrator import _get_paused_workflow_max_retry_cycles

        _seed(db_env, paused_retry_count=_get_paused_workflow_max_retry_cycles())

        _run(db_env)
        with db_env.session_scope() as session:
            reason_after_first = session.query(Workflow).filter_by(id="wf-1").first().status_reason

        assert _run(db_env) == 0, "already-given-up workflows must stop being counted"
        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status_reason == reason_after_first


class TestScoping:
    def test_ignores_a_workflow_failed_for_an_unrelated_reason(self, db_env):
        """Notably the arbitration-deadlock terminal state ("human declined
        to continue"), which is a deliberate human decision and must never
        be auto-resumed."""
        _seed(db_env, status_reason="development: human declined to continue past the arbitration deadlock")

        assert _run(db_env) == 0

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "failed"

    def test_ignores_a_superseded_feature_workflow(self, db_env):
        """Same guard as the paused sibling: a per-feature workflow whose
        Feature no longer points back at it was replaced by a later attempt,
        and resuming it resurrects already-dead work."""
        _seed(db_env, with_feature=False)

        assert _run(db_env) == 0

        with db_env.session_scope() as session:
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "failed"

    def test_ignores_a_workflow_with_nothing_actually_failed_to_reset(self, db_env):
        """Something else already healed the execution -- not this
        function's case, and it must not bump the retry counter for it."""
        _seed(db_env, execution_status="completed")

        assert _run(db_env) == 0

        with db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.paused_retry_count == 0


class TestEndToEndSelfHealing:
    """The whole point: from the state the retry cap leaves behind, the
    pipeline must get itself moving again with no human involved."""

    def test_the_sweep_recovers_and_then_dispatches_the_phase(self, db_env, monkeypatch):
        from src.autopilot.orchestrator.phase_transitions import (
            _get_phase_statuses,
            _retry_exhausted_failed_workflows,
        )

        _seed(db_env)

        # Before: the phase is invisible to every dispatch case, which is
        # exactly why nothing could move it.
        with db_env.session_scope() as session:
            statuses = _get_phase_statuses(session, "wf-1")
            assert [p["status"] for p in statuses] == ["failed"]
            assert not [p for p in statuses if p["status"] in ("pending", "in_progress", "completed")]

        assert _retry_exhausted_failed_workflows(MagicMock()) == 1

        # After: it is back in the "pending" bucket _case_start_first_phase
        # dispatches from, and the workflow is active so the sweep's own
        # per-workflow loop will reach it.
        with db_env.session_scope() as session:
            statuses = _get_phase_statuses(session, "wf-1")
            assert [p["status"] for p in statuses] == ["pending"]
            assert session.query(Workflow).filter_by(id="wf-1").first().status == "active"

    def test_the_recovery_is_registered_in_the_background_sweep(self):
        """A recovery nothing calls is not automation. Pins the wiring."""
        import inspect

        from src.mcp.server.background_loops import _run_phase_advancement_sweep_once

        source = inspect.getsource(_run_phase_advancement_sweep_once)
        assert "_retry_exhausted_failed_workflows(sweep_logger)" in source


class TestActiveWorkflowWithAFailedExecution:
    """The state that actually required a manual repair in production:
    Workflow.status="active" with a phase execution still "failed".

    Every un-fail path resets the workflow and its tasks; historically none
    reset this row. The workflow then runs on -- its phases advancing
    through direct goto dispatch, which never consults
    PhaseExecution.status -- while the failed phase stays invisible to all
    four dispatch cases and derive_workflow_status can never see every
    execution as "completed"/"skipped". Observed live on workflow 72ed4df8
    for a full day.
    """

    def _seed_active_with_failed_execution(self, db):
        with db.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
            session.add(Workflow(
                id="wf-1", name="autopilot", phases_folder_path="/tmp",
                definition_id="autopilot", status="active", project_id="proj-1",
            ))
            session.add(Phase(
                id="phase-dev", workflow_id="wf-1", name="development", order=5,
                description="d", done_definitions=["d"],
            ))
            session.add(PhaseExecution(
                id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
                status="failed", completed_at=utc_now(),
            ))

    def test_advance_phases_heals_it_with_no_human_and_no_cooldown(self, db_env):
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        self._seed_active_with_failed_execution(db_env)

        _advance_phases("wf-1", MagicMock())

        with db_env.session_scope() as session:
            exec_row = session.query(PhaseExecution).filter_by(id="exec-dev").first()
            # Healed AND picked straight back up in the same pass: the reset
            # puts it in the "pending" bucket, and the dispatch cases further
            # down _advance_phases then start it. That whole chain is what
            # previously needed a human.
            assert exec_row.status != "failed", (
                "an active workflow's failed execution must be healed immediately -- "
                "it is invisible to every dispatch case until it is"
            )
            assert exec_row.status == "in_progress"

    def test_a_failed_workflows_execution_is_left_to_the_bounded_retry_policy(self, db_env):
        """Only ACTIVE workflows get the unconditional heal. A failed one is
        governed by _retry_exhausted_failed_workflows' cooldown and cycle
        cap, so this must not quietly bypass them."""
        from src.autopilot.orchestrator.phase_transitions import _advance_phases

        self._seed_active_with_failed_execution(db_env)
        with db_env.session_scope() as session:
            session.query(Workflow).filter_by(id="wf-1").first().status = "failed"

        _advance_phases("wf-1", MagicMock())

        with db_env.session_scope() as session:
            assert session.query(PhaseExecution).filter_by(id="exec-dev").first().status == "failed"
