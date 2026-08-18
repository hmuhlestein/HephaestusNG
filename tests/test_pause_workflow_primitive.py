"""Characterization tests for Phase 2 §4.8's pause_workflow/resume_workflow
primitive (src/autopilot/orchestrator/engine_client.py).

Covers the primitive itself, plus each of the four historical pause-write
bug commits named in docs/AUTOPILOT_REFACTOR_PLAN.md §4.8 (9aa2a19,
ce0c4a7, bacaf6b, 22178b1) and the related auto-resume guard fix (a333616)
-- asserting the migrated call sites still leave Workflow.status/paused_by/
paused_at and Feature.status mutually consistent.
"""

import pytest


@pytest.fixture
def orch_db_env(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_workflow(db, wf_id, status="active", paused_by=None, paused_at=None, status_reason=None):
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
