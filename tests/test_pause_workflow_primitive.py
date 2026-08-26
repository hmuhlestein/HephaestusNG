"""Characterization tests for Phase 2 §4.8's pause_workflow/resume_workflow
primitive (src/autopilot/orchestrator/engine_client.py).

Covers the primitive itself, plus each of the four historical pause-write
bug commits named in docs/AUTOPILOT_REFACTOR_PLAN.md §4.8 (9aa2a19,
ce0c4a7, bacaf6b, 22178b1) and the related auto-resume guard fix (a333616)
-- asserting the migrated call sites still leave Workflow.status/paused_by/
paused_at and Feature.status mutually consistent.
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


def _make_workflow(
    db, wf_id, status="active", paused_by=None, paused_at=None, status_reason=None,
    paused_retry_count=0,
):
    from src.core.database import Workflow

    with db.session_scope() as session:
        session.add(
            Workflow(
                id=wf_id,
                name="test",
                phases_folder_path="/tmp",
                status=status,
                paused_by=paused_by,
                paused_at=paused_at,
                status_reason=status_reason,
                paused_retry_count=paused_retry_count,
            )
        )


def _make_feature(db, feature_id, workflow_id, status="active"):
    from src.core.database import Feature

    with db.session_scope() as session:
        session.add(
            Feature(
                id=feature_id,
                design_id="des-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Build the test feature",
                workflow_id=workflow_id,
                status=status,
            )
        )


class TestPauseWorkflowPrimitive:
    def test_sets_all_three_fields_together(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import pause_workflow
        from src.core.database import Workflow

        _make_workflow(orch_db_env, "wf-1")

        assert pause_workflow("wf-1", reason="user") is True

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "user"
            assert wf.paused_at is not None

    def test_cascades_to_feature_by_default(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import pause_workflow
        from src.core.database import Feature

        _make_workflow(orch_db_env, "wf-1")
        _make_feature(orch_db_env, "feat-1", "wf-1")

        pause_workflow("wf-1", reason="user")

        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feat-1").first()
            assert feat.status == "paused"

    # "validated" is deliberately absent: FeatureStatus.VALIDATED exists in
    # Python but the features table's CHECK constraint rejects it, so no
    # feature row can ever hold it (see database.py's Feature constraint).
    @pytest.mark.parametrize("terminal", ["completed", "failed", "skipped"])
    def test_cascade_never_pauses_a_terminal_feature(self, orch_db_env, terminal):
        """A terminal feature must survive a workflow pause/resume cycle.

        derive_feature_status returns early on PAUSED -- it is the one
        status it never re-derives -- so a wrongly-cascaded pause is
        never repaired, and resume's mirror cascade sends it to "active",
        silently turning finished work back into live-looking work.
        """
        from src.autopilot.orchestrator.engine_client import pause_workflow, resume_workflow
        from src.core.database import Feature

        _make_workflow(orch_db_env, "wf-1")
        _make_feature(orch_db_env, "feat-done", "wf-1", status=terminal)
        _make_feature(orch_db_env, "feat-live", "wf-1", status="active")

        pause_workflow("wf-1", reason="user")

        with orch_db_env.session_scope() as session:
            assert session.query(Feature).filter_by(id="feat-done").first().status == terminal
            assert session.query(Feature).filter_by(id="feat-live").first().status == "paused"

        resume_workflow("wf-1", force=True)

        with orch_db_env.session_scope() as session:
            assert session.query(Feature).filter_by(id="feat-done").first().status == terminal
            assert session.query(Feature).filter_by(id="feat-live").first().status == "active"

    def test_cascade_can_be_disabled(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import pause_workflow
        from src.core.database import Feature

        _make_workflow(orch_db_env, "wf-1")
        _make_feature(orch_db_env, "feat-1", "wf-1")

        pause_workflow("wf-1", reason="user", cascade_to_feature=False)

        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feat-1").first()
            assert feat.status == "active"

    def test_status_reason_set_when_provided(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import pause_workflow
        from src.core.database import Workflow

        _make_workflow(orch_db_env, "wf-1")

        pause_workflow("wf-1", reason="budget", status_reason="Budget limit reached")

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status_reason == "Budget limit reached"

    def test_returns_false_for_missing_workflow(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import pause_workflow

        assert pause_workflow("no-such-wf", reason="user") is False


class TestResumeWorkflowPrimitive:
    def test_clears_all_pause_fields_together(self, orch_db_env):
        from datetime import datetime

        from src.autopilot.orchestrator.engine_client import resume_workflow
        from src.core.database import Workflow

        _make_workflow(
            orch_db_env, "wf-1", status="paused", paused_by="system",
            paused_at=datetime.utcnow(), status_reason="something broke",
        )

        assert resume_workflow("wf-1") is True

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            assert wf.paused_by is None
            assert wf.paused_at is None
            assert wf.status_reason is None

    def test_system_pause_resumes_without_force(self, orch_db_env):
        """a333616: paused_by="system" is a heuristic give-up, not operator
        intent, and must remain eligible for auto-resume without force."""
        from src.autopilot.orchestrator.engine_client import resume_workflow

        _make_workflow(orch_db_env, "wf-1", status="paused", paused_by="system")

        assert resume_workflow("wf-1", force=False) is True

    @pytest.mark.parametrize("reason", ["user", "budget", "review", "system-exhausted"])
    def test_deliberate_pause_requires_force(self, orch_db_env, reason):
        """a333616's own fix: only "system" is let through un-forced --
        every deliberate/permanent pause reason must be a no-op without
        force=True, or a self-heal sweep could silently revert a pause the
        user, budget policy, or review gate put in place on purpose."""
        from src.autopilot.orchestrator.engine_client import resume_workflow
        from src.core.database import Workflow

        _make_workflow(orch_db_env, "wf-1", status="paused", paused_by=reason)

        assert resume_workflow("wf-1", force=False) is False

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == reason

    def test_force_overrides_narrowing(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import resume_workflow

        _make_workflow(orch_db_env, "wf-1", status="paused", paused_by="user")

        assert resume_workflow("wf-1", force=True) is True

    def test_noop_when_not_paused(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import resume_workflow

        _make_workflow(orch_db_env, "wf-1", status="active")

        assert resume_workflow("wf-1", force=True) is False

    def test_cascades_paused_feature_back_to_active(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import resume_workflow
        from src.core.database import Feature

        _make_workflow(orch_db_env, "wf-1", status="paused", paused_by="system")
        _make_feature(orch_db_env, "feat-1", "wf-1", status="paused")

        resume_workflow("wf-1")

        with orch_db_env.session_scope() as session:
            feat = session.query(Feature).filter_by(id="feat-1").first()
            assert feat.status == "active"

    def test_resets_paused_retry_count(self, orch_db_env):
        """§4.8 gap: a stale paused_retry_count carried across a resolved
        pause episode let the very next "system" pause immediately trip
        _retry_exhausted_paused_workflows's max-cycles cap
        (phase_transitions.py:456), with zero real retries this time
        around. A successful resume must zero the counter."""
        from src.autopilot.orchestrator.engine_client import resume_workflow
        from src.core.database import Workflow

        _make_workflow(
            orch_db_env, "wf-1", status="paused", paused_by="system",
            paused_retry_count=2,
        )

        assert resume_workflow("wf-1") is True

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.paused_retry_count == 0

    def test_noop_resume_leaves_paused_retry_count_untouched(self, orch_db_env):
        """A resume that's rejected by the paused_by narrowing (no force)
        must not reset the counter either -- the pause episode isn't
        actually over."""
        from src.autopilot.orchestrator.engine_client import resume_workflow
        from src.core.database import Workflow

        _make_workflow(
            orch_db_env, "wf-1", status="paused", paused_by="user",
            paused_retry_count=2,
        )

        assert resume_workflow("wf-1", force=False) is False

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.paused_retry_count == 2


class TestHistoricalPauseSiteConsistency:
    """One characterization test per historical bug commit named in
    §4.8, run against the current (migrated) code -- each must leave
    Workflow.status/paused_by/paused_at and, where applicable,
    Feature.status mutually consistent."""

    def test_pause_workflow_direct_sets_full_triad(self, orch_db_env):
        """9aa2a19: pause_workflow_direct used to set status without
        paused_by/paused_at."""
        from src.autopilot.orchestrator.engine_client import pause_workflow_direct
        from src.core.database import Workflow

        _make_workflow(orch_db_env, "wf-1")

        assert pause_workflow_direct("wf-1") is True

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            # Defaults to "system", not "user" -- every caller (duplicate-
            # workflow guard, workflow cleanup, project-orchestrator-stop
            # cascade) is an automated cleanup path, not an operator
            # clicking pause. "user" would make resume_workflow require
            # force=True and exclude this workflow from
            # _try_auto_resume_paused_workflow's cooldown-based retry
            # forever.
            assert wf.paused_by == "system"
            assert wf.paused_at is not None

    def test_pause_workflow_direct_accepts_an_explicit_reason(self, orch_db_env):
        from src.autopilot.orchestrator.engine_client import pause_workflow_direct
        from src.core.database import Workflow

        _make_workflow(orch_db_env, "wf-2")

        assert pause_workflow_direct("wf-2", reason="user") is True

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-2").first()
            assert wf.paused_by == "user"

    def test_pause_feature_for_review_skips_approved_feature(self, orch_db_env):
        """ce0c4a7: an already-approved feature must not get re-paused for
        review on the next cycle."""
        from unittest.mock import Mock

        from src.autopilot.orchestrator import _pause_feature_for_review
        from src.core.database import Feature, Workflow

        _make_workflow(orch_db_env, "wf-1")
        with orch_db_env.session_scope() as session:
            session.add(
                Feature(
                    id="feat-1", design_id="des-1", feature_key="k", name="n",
                    scope="s", workflow_id="wf-1", status="active",
                    review_status="approved",
                )
            )

        _pause_feature_for_review("feat-1", Mock())

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            assert wf.paused_by is None

    def test_pause_feature_for_review_sets_full_triad_and_cascades(self, orch_db_env):
        """ce0c4a7's own site, migrated onto pause_workflow at this
        handoff: paused_at was never set here before this item -- confirm
        the migration closed that gap too, not just the approved-check."""
        from unittest.mock import Mock

        from src.autopilot.orchestrator import _pause_feature_for_review
        from src.core.database import Feature, Workflow

        _make_workflow(orch_db_env, "wf-1")
        _make_feature(orch_db_env, "feat-1", "wf-1")

        _pause_feature_for_review("feat-1", Mock())

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            feat = session.query(Feature).filter_by(id="feat-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "review"
            assert wf.paused_at is not None
            assert feat.status == "paused"

    def test_corrective_task_skips_deliberately_paused_workflow(self, orch_db_env):
        """bacaf6b: _create_corrective_task must not reactivate a
        deliberately-paused workflow (nor, worse, spawn a live agent
        against it)."""
        from unittest.mock import Mock, patch

        from src.autopilot.orchestrator.phase_transitions import _create_corrective_task
        from src.core.database import Phase, PhaseExecution, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="paused", paused_by="user",
                )
            )
            session.add(
                Phase(
                    id="phase-1", workflow_id="wf-1", order=1,
                    name="dev", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-1", phase_id="phase-1",
                    workflow_execution_id="wf-1", status="completed",
                )
            )

        with patch(
            "src.autopilot.orchestrator.phase_transitions.create_agent_for_task_direct"
        ) as mock_create_agent:
            result = _create_corrective_task("wf-1", "phase-1", "dev", "feedback", Mock())

        assert result is None
        mock_create_agent.assert_not_called()

    def test_resume_stuck_workflow_tasks_skips_paused_workflow(self, orch_db_env):
        """bacaf6b's sibling guard on _resume_stuck_workflow_tasks."""
        from unittest.mock import Mock

        from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
        from src.core.database import Workflow

        _make_workflow(orch_db_env, "wf-1", status="paused", paused_by="user")

        restarted = _resume_stuck_workflow_tasks("wf-1", Mock())

        assert restarted == 0
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"

    def test_pause_project_workflows_cascades_feature_status(self, orch_db_env):
        """22178b1: pause_project_workflows never synced Feature.status,
        so paused features kept showing "Active" in the UI."""
        from src.autopilot.orchestrator.engine_client import pause_project_workflows
        from src.core.database import AutopilotProject, Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="active", project_id="proj-1",
                    definition_id="autopilot",
                )
            )
            session.add(
                Feature(
                    id="feat-1", design_id="des-1", feature_key="k", name="n",
                    scope="s", workflow_id="wf-1", status="active",
                )
            )

        with orch_db_env.session_scope() as session:
            pause_project_workflows(
                session, "proj-1", paused_by="user", definition_ids=("autopilot",),
            )

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            feat = session.query(Feature).filter_by(id="feat-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "user"
            assert wf.paused_at is not None
            assert feat.status == "paused"

    def test_pause_project_workflows_stamps_user_terminated_reason(self, orch_db_env):
        """Without this, a task reset here reads as if its agent just
        silently vanished -- if the workflow is never resumed to redispatch
        it, health_audit's stuck-detector eventually mislabels it "no agent
        activity for >30 minutes", which reads like the agent hung when it
        was actually killed by this exact pause seconds after starting."""
        from src.autopilot.orchestrator.engine_client import pause_project_workflows
        from src.core.database import Agent, AutopilotProject, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="active", project_id="proj-1",
                    definition_id="autopilot",
                )
            )
            session.add(
                Agent(
                    id="agent-1", system_prompt="p", status="working",
                    cli_type="claude", current_task_id="task-1",
                )
            )
            session.add(
                Task(
                    id="task-1", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="in_progress",
                    assigned_agent_id="agent-1",
                )
            )

        with orch_db_env.session_scope() as session:
            pause_project_workflows(
                session, "proj-1", paused_by="user", definition_ids=("autopilot",),
            )

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "pending"
            assert task.assigned_agent_id is None
            assert task.failure_reason == "User terminated: workflow was paused"

    def test_pause_project_workflows_collects_queued_task_ids(self, orch_db_env):
        """"queued" tasks were previously left untouched entirely -- still
        eligible for claim_next_queued_task to dispatch even after their
        workflow was just paused. Not reset here (see this function's
        docstring for why -- the caller must apply
        QueueService.reset_queued_task_to_pending after committing), but
        their IDs must be returned so a caller that DOES have a safe commit
        boundary (stop_pipeline) can act on them."""
        from src.autopilot.orchestrator.engine_client import pause_project_workflows
        from src.core.database import AutopilotProject, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="active", project_id="proj-1",
                    definition_id="autopilot",
                )
            )
            session.add(
                Task(
                    id="task-queued", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="queued",
                )
            )
            session.add(
                Task(
                    id="task-pending", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="pending",
                )
            )

        with orch_db_env.session_scope() as session:
            paused_count, queued_task_ids = pause_project_workflows(
                session, "proj-1", paused_by="user", definition_ids=("autopilot",),
            )

        assert paused_count == 1
        assert queued_task_ids == ["task-queued"]

        with orch_db_env.session_scope() as session:
            # Untouched by this function itself -- still "queued", per the
            # docstring's note that the caller must reset it separately.
            task = session.query(Task).filter_by(id="task-queued").first()
            assert task.status == "queued"

    @pytest.mark.parametrize("task_status", ["assigned", "under_review", "needs_work"])
    def test_pause_resets_and_labels_tasks_beyond_in_progress(self, orch_db_env, task_status):
        """agents_to_terminate above kills any live (working/starting/idle)
        agent regardless of its task's own status -- a
        bump_task_priority_endpoint dispatch commits status="assigned"
        directly (not "in_progress"), and a task kept alive for validation
        sits "under_review"/"needs_work" with a still-live agent. Missing
        any of these here would leave that task pointing at a corpse agent,
        uncaught until _clean_stale_assigned_tasks's unrelated, generic-
        reason sweep eventually notices."""
        from src.autopilot.orchestrator.engine_client import pause_project_workflows
        from src.core.database import Agent, AutopilotProject, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="active", project_id="proj-1",
                    definition_id="autopilot",
                )
            )
            session.add(
                Agent(
                    id="agent-1", system_prompt="p", status="working",
                    cli_type="claude", current_task_id="task-1",
                )
            )
            session.add(
                Task(
                    id="task-1", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status=task_status,
                    assigned_agent_id="agent-1",
                )
            )

        with orch_db_env.session_scope() as session:
            pause_project_workflows(
                session, "proj-1", paused_by="user", definition_ids=("autopilot",),
            )

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "pending", (
                f"task left {task_status!r} with a live agent that was just "
                "terminated -- uncaught by this reset, only findable later "
                "via a different sweep's generic reason"
            )
            assert task.assigned_agent_id is None
            assert task.failure_reason == "User terminated: workflow was paused"

    @pytest.mark.parametrize("paused_by", ["budget", "system"])
    def test_non_user_pause_does_not_claim_user_terminated(self, orch_db_env, paused_by):
        """A budget/system pause has its own accurate story
        (wf.status_reason) -- mislabeling either as "User terminated" would
        be wrong, not just imprecise."""
        from src.autopilot.orchestrator.engine_client import pause_project_workflows
        from src.core.database import Agent, AutopilotProject, Task, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp"))
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="active", project_id="proj-1",
                    definition_id="autopilot",
                )
            )
            session.add(
                Agent(
                    id="agent-1", system_prompt="p", status="working",
                    cli_type="claude", current_task_id="task-1",
                )
            )
            session.add(
                Task(
                    id="task-1", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="in_progress",
                    assigned_agent_id="agent-1",
                )
            )

        with orch_db_env.session_scope() as session:
            pause_project_workflows(
                session, "proj-1", paused_by=paused_by, definition_ids=("autopilot",),
            )

        with orch_db_env.session_scope() as session:
            task = session.query(Task).filter_by(id="task-1").first()
            assert task.status == "pending"
            assert task.failure_reason != "User terminated: workflow was paused"

    def test_auto_resume_respects_deliberate_pause_guard(self, orch_db_env):
        """a333616: paused_by is not None used to be treated as "leave
        alone" for every pause reason, including "system" -- making the
        guard's whole body dead code, since every real pause site sets a
        non-None value. "system" must resume when a done task appears;
        "user" must not."""
        from unittest.mock import Mock

        from src.autopilot.orchestrator.phase_transitions import _try_auto_resume_paused_workflow
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        def _seed(wf_id, paused_by):
            with orch_db_env.session_scope() as session:
                session.add(
                    Workflow(
                        id=wf_id, name="t", phases_folder_path="/tmp",
                        status="paused", paused_by=paused_by,
                    )
                )
                session.add(
                    Phase(
                        id=f"phase-{wf_id}", workflow_id=wf_id, order=1,
                        name="dev", description="d", done_definitions=["x"],
                    )
                )
                session.add(
                    PhaseExecution(
                        id=f"exec-{wf_id}", phase_id=f"phase-{wf_id}",
                        workflow_execution_id=wf_id, status="in_progress",
                    )
                )
                session.add(
                    Task(
                        id=f"task-{wf_id}", workflow_id=wf_id, phase_id=f"phase-{wf_id}",
                        raw_description="r", done_definition="d", status="done",
                    )
                )

        _seed("wf-system", "system")
        _seed("wf-user", "user")

        with orch_db_env.session_scope() as session:
            wf_system = session.query(Workflow).filter_by(id="wf-system").first()
            _try_auto_resume_paused_workflow(session, "wf-system", wf_system, Mock())
            wf_user = session.query(Workflow).filter_by(id="wf-user").first()
            _try_auto_resume_paused_workflow(session, "wf-user", wf_user, Mock())

        with orch_db_env.session_scope() as session:
            wf_system = session.query(Workflow).filter_by(id="wf-system").first()
            wf_user = session.query(Workflow).filter_by(id="wf-user").first()
            assert wf_system.status == "active"
            assert wf_system.paused_by is None
            assert wf_system.paused_at is None
            assert wf_user.status == "paused"
            assert wf_user.paused_by == "user"

    def test_project_reactivation_resumes_user_paused_workflow_fully(self, orch_db_env, tmp_path):
        """_get_or_create_project_id (src/autopilot/orchestrator/state.py)
        used to clear status/paused_by via a bulk .update() that left
        paused_at stale, and never touched Feature.status -- found during
        this handoff's own re-audit of the freshness check's write-site
        list, not one of the four named commits. Migrated to loop over
        resume_workflow per workflow; this is that migration's own
        characterization test, since none existed for the successful
        (not-blocked-by-cap) resume path before this item."""
        from src.autopilot.orchestrator.state import _get_or_create_project_id
        from src.core.database import AutopilotProject, Feature, Workflow

        project_dir = tmp_path / "project-reactivate"
        project_dir.mkdir()

        with orch_db_env.session_scope() as session:
            session.add(
                AutopilotProject(
                    id="proj-1", name="project-reactivate",
                    base_dir=str(project_dir.resolve()), is_active=False,
                )
            )
            session.add(
                Workflow(
                    id="wf-1", project_id="proj-1", definition_id="autopilot",
                    name="t", phases_folder_path="/tmp",
                    status="paused", paused_by="user",
                )
            )
            session.add(
                Feature(
                    id="feat-1", design_id="des-1", feature_key="k", name="n",
                    scope="s", workflow_id="wf-1", status="paused",
                )
            )

        _get_or_create_project_id(str(project_dir))

        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            feat = session.query(Feature).filter_by(id="feat-1").first()
            assert wf.status == "active"
            assert wf.paused_by is None
            assert wf.paused_at is None
            assert feat.status == "active"

    @pytest.mark.asyncio
    async def test_review_feature_approve_clears_full_triad(self, orch_db_env):
        """review_feature's approve branch (feature_review_routes.py) -- found
        during this handoff's own re-audit, not one of the four named
        commits: cleared status/paused_by but left paused_at stale, same
        bug class as the other resume-side gaps."""
        from src.core.database import Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="paused", paused_by="review",
                )
            )
            session.add(
                Feature(
                    id="feat-1", design_id="des-1", feature_key="k", name="n",
                    scope="s", workflow_id="wf-1", status="paused",
                )
            )

        from src.mcp.autopilot.feature_review_routes import FeatureReviewRequest, review_feature

        result = await review_feature("feat-1", FeatureReviewRequest(action="approve"))

        assert result["success"] is True
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            feat = session.query(Feature).filter_by(id="feat-1").first()
            assert wf.paused_by is None
            assert wf.paused_at is None
            assert feat.status in ("active", "completed")

    @pytest.mark.asyncio
    async def test_phase0_review_approve_clears_full_triad(self, orch_db_env):
        """_review_phase0_decomposition's approve branch (feature_review_routes.py)
        -- same gap as review_feature's, found in the same re-audit."""
        from unittest.mock import patch

        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    status="paused", paused_by="review",
                )
            )

        from src.mcp.autopilot.feature_review_routes import (
            FeatureReviewRequest,
            _review_phase0_decomposition,
        )

        with patch("src.autopilot.orchestrator.pipeline.finalize_phase0_workflow"):
            result = await _review_phase0_decomposition(
                "wf-1", FeatureReviewRequest(action="approve")
            )

        assert result["success"] is True
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "active"
            assert wf.paused_by is None
            assert wf.paused_at is None


class TestPauseReasonValidation:
    """paused_by is consumed by exact-literal comparisons, never by a
    catch-all. resume_workflow narrows on "system"; _wait_for_phase0_review_
    clearance polls for "review"; the budget sweep filters on "budget". An
    unrecognised reason therefore does not fail anywhere -- it silently makes
    every one of those guards miss, and the workflow stays paused with no
    path that can resume it. Validating at the single write site is what
    makes that unrepresentable."""

    @pytest.mark.parametrize(
        "reason", ["user", "budget", "review", "system", "system-exhausted"]
    )
    def test_every_documented_reason_is_accepted(self, orch_db_env, reason):
        from src.autopilot.orchestrator.engine_client import pause_workflow
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id=f"wf-{reason}", name="w", phases_folder_path="/tmp",
                    status="active",
                )
            )

        assert pause_workflow(f"wf-{reason}", reason=reason) is True
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id=f"wf-{reason}").first()
            assert wf.paused_by == reason

    @pytest.mark.parametrize("reason", ["users", "USER", "", "manual", "paused"])
    def test_an_unrecognised_reason_raises_instead_of_writing_it(
        self, orch_db_env, reason
    ):
        from src.autopilot.orchestrator.engine_client import pause_workflow
        from src.core.database import Workflow

        with orch_db_env.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-bad", name="w", phases_folder_path="/tmp", status="active"
                )
            )

        with pytest.raises(ValueError, match="unknown reason"):
            pause_workflow("wf-bad", reason=reason)

        # And it must not have half-applied the pause on the way out.
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-bad").first()
            assert wf.status == "active"
            assert wf.paused_by is None


class TestReviewFeatureReopensCompletedDevelopmentPhase:
    """Regression: review_feature's request_changes path, when creating a
    fresh corrective task from scratch (no restartable task existed),
    never reopened the development phase's own PhaseExecution to match --
    unlike _create_phase_task's own task-creation path, which always
    calls reopen_phase_execution. If development had already run to
    completion earlier in the workflow (the normal case -- review only
    happens after everything finished), its PhaseExecution reads
    "completed", and no dispatch/self-heal case recognizes a "completed"
    phase with a live pending task sitting in it: Case 2
    (_case_in_progress_complete) only ever looks at phases already
    "in_progress", and the two pending-phase self-heals
    (_release_pending_phases_with_done_tasks /
    _release_pending_phases_with_orphaned_task) only match "pending",
    never "completed". The new task sat invisible to every sweep tick.

    Confirmed live: task 146d191d (created by review_feature for "do
    another lint check") sat exactly this way after its first dispatch
    attempt's agent was killed by an unrelated bug (orphan_reaper, fixed
    in efaf430) and the task was reset back to "pending" -- its own
    phase still read "completed", so nothing ever picked it up again.
    """

    @pytest.mark.asyncio
    async def test_request_changes_reopens_a_completed_development_phase(
        self, orch_db_env,
    ):
        from src.core.database import AutopilotProject, Feature, Phase, PhaseExecution, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir="/tmp", review_mode=True))
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp",
                status="paused", paused_by="review", project_id="proj-1",
            ))
            session.add(Feature(
                id="feat-1", design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id="wf-1", status="paused",
            ))
            session.add(Phase(
                id="wf-1-dev", workflow_id="wf-1", name="development",
                order=5, description="d", done_definitions=["d"],
            ))
            session.add(PhaseExecution(
                id="exec-dev", phase_id="wf-1-dev", workflow_execution_id="wf-1",
                status="completed",
            ))

        from src.mcp.autopilot import feature_review_routes
        spawn_mock = AsyncMock()
        monkeypatch_target = feature_review_routes
        import unittest.mock
        with unittest.mock.patch.object(monkeypatch_target, "_spawn_agent_for_task", spawn_mock):
            from src.mcp.autopilot.feature_review_routes import FeatureReviewRequest, review_feature
            result = await review_feature(
                "feat-1", FeatureReviewRequest(action="request_changes", feedback="do another lint check"),
            )
        assert result["success"] is True

        with orch_db_env.session_scope() as session:
            execution = session.query(PhaseExecution).filter_by(phase_id="wf-1-dev").first()
            assert execution.status == "in_progress", (
                "the development phase must be reopened to match its "
                "freshly-created task, or nothing will ever pick that "
                "task up again"
            )


class TestReviewAndResumeReuseOldPendingTasks:
    """Regression: review_feature's request_changes path and resume_feature
    both picked "restartable" candidates via Task.status.in_(["blocked",
    "failed", "assigned", "in_progress"]) -- missing "pending". An hours-
    old, never-dispatched pending task (no assigned_agent_id) is exactly
    as restartable as a failed one, but was invisible to this query.

    For review_feature specifically, that emptiness triggers the "no
    restartable tasks -> create a brand-new development task" branch,
    which creates a SECOND task for the same phase and reopens the
    phase's PhaseExecution to started_at="now" -- stranding the original
    pending task outside its own phase's cycle (every cycle-scoped query
    filters on Task.created_at >= cycle_start). The stranded task is then
    picked up by an unrelated staleness check and marked "Orphaned: never
    dispatched to an agent", even though nothing was ever wrong with it.

    Confirmed live: task 146d191d hit exactly this sequence through
    normal application code (no manual DB intervention) after an earlier
    cycle left it pending with no agent."""

    def _seed_review_mode_project_and_workflow(
        self, db, project_id="proj-1", workflow_id="wf-1", feature_id="feat-1",
    ):
        from src.core.database import AutopilotProject, Feature, Phase, Workflow

        with db.session_scope() as session:
            session.add(AutopilotProject(id=project_id, name="p", base_dir="/tmp", review_mode=True))
            session.add(Workflow(
                id=workflow_id, name="t", phases_folder_path="/tmp",
                status="paused", paused_by="review", project_id=project_id,
            ))
            session.add(Feature(
                id=feature_id, design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id=workflow_id, status="paused",
            ))
            session.add(Phase(
                id=f"{workflow_id}-dev", workflow_id=workflow_id, name="development",
                order=5, description="d", done_definitions=["d"],
            ))

    def _seed_old_pending_task(self, db, workflow_id="wf-1"):
        from src.core.database import Task

        with db.session_scope() as session:
            session.add(Task(
                id="task-old-pending", workflow_id=workflow_id, phase_id=f"{workflow_id}-dev",
                raw_description="r", done_definition="d", status="pending",
            ))

    @pytest.mark.asyncio
    async def test_request_changes_reuses_an_old_pending_task_instead_of_duplicating(
        self, orch_db_env, monkeypatch,
    ):
        self._seed_review_mode_project_and_workflow(orch_db_env)
        self._seed_old_pending_task(orch_db_env)

        from src.mcp.autopilot import feature_review_routes
        spawn_mock = AsyncMock()
        monkeypatch.setattr(feature_review_routes, "_spawn_agent_for_task", spawn_mock)

        from src.mcp.autopilot.feature_review_routes import FeatureReviewRequest, review_feature
        result = await review_feature(
            "feat-1", FeatureReviewRequest(action="request_changes", feedback="do another lint check"),
        )
        assert result["success"] is True

        from src.core.database import Task, TaskPromptOverride

        with orch_db_env.session_scope() as session:
            dev_tasks = session.query(Task).filter_by(phase_id="wf-1-dev").all()
            assert len(dev_tasks) == 1, "the old pending task must be reused, not duplicated"
            assert dev_tasks[0].id == "task-old-pending"

            override = session.query(TaskPromptOverride).filter_by(task_id="task-old-pending").first()
            assert override is not None
            assert "do another lint check" in override.user_prompt

        spawn_mock.assert_called_once_with("task-old-pending", "wf-1-dev")

    @pytest.mark.asyncio
    async def test_resume_feature_reuses_an_old_pending_task(self, orch_db_env, monkeypatch):
        self._seed_review_mode_project_and_workflow(orch_db_env)
        self._seed_old_pending_task(orch_db_env)

        from src.mcp.autopilot import feature_routes
        spawn_mock = AsyncMock()
        monkeypatch.setattr(feature_routes, "_spawn_agent_for_task", spawn_mock)

        from src.mcp.autopilot.feature_routes import resume_feature
        result = await resume_feature("feat-1")
        assert result["success"] is True

        spawn_mock.assert_called_once_with("task-old-pending", "wf-1-dev")

    def _seed_needs_work_task_with_terminated_agent(self, db, workflow_id="wf-1"):
        from src.core.database import Agent, Task

        with db.session_scope() as session:
            session.add(Agent(id="agent-dead", system_prompt="p", status="terminated", cli_type="pi"))
            session.add(Task(
                id="task-needs-work", workflow_id=workflow_id, phase_id=f"{workflow_id}-dev",
                raw_description="r", done_definition="d", status="needs_work",
                assigned_agent_id="agent-dead",
            ))

    @pytest.mark.asyncio
    async def test_resume_feature_restarts_a_needs_work_task_with_a_dead_agent(
        self, orch_db_env, monkeypatch,
    ):
        """needs_work is set when a validator rejects a task and sends
        feedback back to the same (still-running) agent -- assigned_
        agent_id still points at it. If that agent then dies before
        acting on the feedback, the task must still be restartable, not
        invisible to this same "restartable candidates" query."""
        self._seed_review_mode_project_and_workflow(orch_db_env)
        self._seed_needs_work_task_with_terminated_agent(orch_db_env)

        from src.mcp.autopilot import feature_routes
        spawn_mock = AsyncMock()
        monkeypatch.setattr(feature_routes, "_spawn_agent_for_task", spawn_mock)

        from src.mcp.autopilot.feature_routes import resume_feature
        result = await resume_feature("feat-1")
        assert result["success"] is True

        spawn_mock.assert_called_once_with("task-needs-work", "wf-1-dev")

    @pytest.mark.asyncio
    async def test_resume_feature_does_not_clear_a_review_pause(self, orch_db_env, monkeypatch):
        """Regression: resume_feature used to force-resume ANY paused
        workflow (force=True, matching every other pause reason), including
        one paused_by="review" -- clicking the generic Resume button (e.g.
        to recover this needs_work/dead-agent task) silently cleared the
        review gate itself, with no approve/request_changes decision ever
        recorded on the feature. The workflow then ran to completion again
        and derive_workflow_status's own completeness self-heal marked it
        "completed" directly, bypassing PhaseManager._complete_workflow's
        review-mode pause -- letting the design queue start the next
        feature with the human review never actually resolved. Confirmed
        live: feature feat-f47c93ba on workflow ca539a75.

        The task recovery itself must still work while paused for review --
        only the review_feature endpoint (approve/request_changes) may
        clear paused_by="review"."""
        self._seed_review_mode_project_and_workflow(orch_db_env)
        self._seed_needs_work_task_with_terminated_agent(orch_db_env)

        from src.mcp.autopilot import feature_routes
        spawn_mock = AsyncMock()
        monkeypatch.setattr(feature_routes, "_spawn_agent_for_task", spawn_mock)

        from src.mcp.autopilot.feature_routes import resume_feature
        result = await resume_feature("feat-1")
        assert result["success"] is True
        spawn_mock.assert_called_once_with("task-needs-work", "wf-1-dev")

        from src.core.database import Workflow
        with orch_db_env.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "paused"
            assert wf.paused_by == "review"


class TestReviewFeatureApproveLocalMergeFallback:
    """When git_expert couldn't create a PR (gh not installed/
    authenticated, no remote, etc -- its own instructions already say
    "or local merge if gh unavailable"), the reviewed work sits committed
    and pushed on the feature branch with nothing to merge it into main.
    review_feature's approve branch must fall back to a local merge
    instead of silently marking the workflow "completed" with real,
    approved work never landing on main.

    Uses real git repos (a git worktree of the project, matching
    production) rather than mocking GitPython -- the merge itself is the
    thing under test."""

    @pytest.fixture
    def git_project_with_feature_branch(self, tmp_path):
        import subprocess

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        def _git(*args, cwd):
            subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

        _git("init", "-b", "main", cwd=project_dir)
        _git("config", "user.email", "t@t.com", cwd=project_dir)
        _git("config", "user.name", "t", cwd=project_dir)
        (project_dir / "README.md").write_text("hello\n")
        _git("add", "-A", cwd=project_dir)
        _git("commit", "-m", "init", cwd=project_dir)

        worktree_dir = tmp_path / "worktree"
        _git("worktree", "add", "-b", "feature/test-branch", str(worktree_dir), cwd=project_dir)
        (worktree_dir / "new_file.txt").write_text("feature work\n")
        _git("add", "-A", cwd=worktree_dir)
        _git("commit", "-m", "feature work", cwd=worktree_dir)

        return project_dir, worktree_dir

    @pytest.mark.asyncio
    async def test_approve_merges_locally_when_no_pr_exists(
        self, orch_db_env, git_project_with_feature_branch,
    ):
        project_dir, worktree_dir = git_project_with_feature_branch
        from src.core.database import AutopilotProject, Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(project_dir)))
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp",
                status="paused", paused_by="review", project_id="proj-1",
                working_directory=str(worktree_dir),
            ))
            session.add(Feature(
                id="feat-1", design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id="wf-1", status="paused", pr_url=None,
            ))

        from src.mcp.autopilot.feature_review_routes import FeatureReviewRequest, review_feature
        result = await review_feature("feat-1", FeatureReviewRequest(action="approve"))
        assert result["success"] is True

        assert (project_dir / "new_file.txt").exists(), (
            "the feature branch's work must be merged into the project's "
            "main branch when no PR exists to merge instead"
        )

    @pytest.mark.asyncio
    async def test_local_merge_takes_the_shared_merge_lock(
        self, orch_db_env, git_project_with_feature_branch,
    ):
        """Regression: this fallback used to build a bare git.Repo directly
        and merge with no locking at all, unlike every other code path that
        merges into the same main_repo (WorktreeManager.merge_to_main,
        _merge_design_branch_into_main, cleanup_all_stale_branches) --
        all of which serialize via the same <repo>/.git/.hephaestus_merge_
        lock file. A review approval landing while one of those was
        mid-merge could race it. Pins that this path now takes that same
        lock (mirroring MergeLockManager.acquire/release being called)
        rather than asserting on lock-file bytes, which real concurrent
        git operations could also touch."""
        from src.core.worktree_merge_lock import MergeLockManager

        project_dir, worktree_dir = git_project_with_feature_branch
        from src.core.database import AutopilotProject, Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(project_dir)))
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp",
                status="paused", paused_by="review", project_id="proj-1",
                working_directory=str(worktree_dir),
            ))
            session.add(Feature(
                id="feat-1", design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id="wf-1", status="paused", pr_url=None,
            ))

        acquire_calls = []
        release_calls = []
        real_acquire = MergeLockManager.acquire
        real_release = MergeLockManager.release

        def _tracking_acquire(self, agent_id, timeout=300):
            acquire_calls.append(agent_id)
            return real_acquire(self, agent_id, timeout)

        def _tracking_release(self, lock_file, agent_id):
            release_calls.append(agent_id)
            return real_release(self, lock_file, agent_id)

        from unittest.mock import patch

        with (
            patch.object(MergeLockManager, "acquire", _tracking_acquire),
            patch.object(MergeLockManager, "release", _tracking_release),
        ):
            from src.mcp.autopilot.feature_review_routes import FeatureReviewRequest, review_feature
            result = await review_feature("feat-1", FeatureReviewRequest(action="approve"))

        assert result["success"] is True
        assert len(acquire_calls) == 1, f"expected exactly one merge-lock acquire, got {acquire_calls}"
        assert len(release_calls) == 1, f"expected exactly one merge-lock release, got {release_calls}"

    @pytest.mark.asyncio
    async def test_does_not_attempt_local_merge_when_a_pr_exists(
        self, orch_db_env, git_project_with_feature_branch, monkeypatch,
    ):
        """The gh pr merge path stays authoritative when a PR actually
        exists -- the local-merge fallback must not also run and double-
        merge."""
        project_dir, worktree_dir = git_project_with_feature_branch
        from src.core.database import AutopilotProject, Feature, Workflow

        with orch_db_env.session_scope() as session:
            session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(project_dir)))
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp",
                status="paused", paused_by="review", project_id="proj-1",
                working_directory=str(worktree_dir),
            ))
            session.add(Feature(
                id="feat-1", design_id="des-1", feature_key="k", name="n",
                scope="s", workflow_id="wf-1", status="paused",
                pr_url="https://github.com/org/repo/pull/1",
            ))

        from unittest.mock import MagicMock, patch

        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ) as mock_run:
            from src.mcp.autopilot.feature_review_routes import FeatureReviewRequest, review_feature
            result = await review_feature("feat-1", FeatureReviewRequest(action="approve"))
        assert result["success"] is True
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0][:3] == ["gh", "pr", "merge"]

        assert not (project_dir / "new_file.txt").exists(), (
            "no local merge should happen when a PR already exists to merge"
        )
