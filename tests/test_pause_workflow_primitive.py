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
            assert wf.paused_by == "user"
            assert wf.paused_at is not None

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
        """review_feature's approve branch (feature_routes.py) -- found
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

        from src.mcp.autopilot.feature_routes import FeatureReviewRequest, review_feature

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
        """_review_phase0_decomposition's approve branch (feature_routes.py)
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

        from src.mcp.autopilot.feature_routes import (
            FeatureReviewRequest,
            _review_phase0_decomposition,
        )

        with patch("src.autopilot.orchestrator.finalize_phase0_workflow"):
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

        from src.mcp.autopilot import feature_routes
        spawn_mock = AsyncMock()
        monkeypatch_target = feature_routes
        import unittest.mock
        with unittest.mock.patch.object(monkeypatch_target, "_spawn_agent_for_task", spawn_mock):
            from src.mcp.autopilot.feature_routes import FeatureReviewRequest, review_feature
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

        from src.mcp.autopilot import feature_routes
        spawn_mock = AsyncMock()
        monkeypatch.setattr(feature_routes, "_spawn_agent_for_task", spawn_mock)

        from src.mcp.autopilot.feature_routes import FeatureReviewRequest, review_feature
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
