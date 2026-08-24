"""Tests for centralized status derivation functions.

Tests for src/core/status_derivation.py (H-3 fix).
"""


import pytest

from src.core.database import (
    AutopilotDesign,
    DatabaseManager,
    Feature,
    Task,
    Workflow,
)
from src.core.status_derivation import (
    derive_design_status,
    derive_feature_status,
    derive_workflow_status,
)


@pytest.fixture
def db_manager(tmp_path):
    """Create a test database manager."""
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _create_design(session, design_id="design-1", status="active"):
    """Helper to create an AutopilotDesign for tests."""
    from src.core.database import AutopilotProject

    # Create project first (required for design)
    project = AutopilotProject(
        id="project-1",
        name="Test Project",
        base_dir="/tmp/test-project",
        is_active=True,
    )
    session.add(project)

    design = AutopilotDesign(
        id=design_id,
        project_id="project-1",
        name="Test Design",
        filename="test.md",
        status=status,
    )
    session.add(design)
    return design


class TestDeriveFeatureStatus:
    """Tests for derive_feature_status function."""

    def test_returns_paused_when_feature_paused(self, db_manager):
        """Should return 'paused' when feature is explicitly paused."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="paused",
            )
            session.add(feature)

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "paused"

    def test_does_not_stay_paused_once_its_workflow_resumed(self, db_manager):
        """A workflow can resume through paths that never call
        resume_feature (the self-heal auto-resume sweep, a direct DB/admin
        resume) -- Feature.status must not be trusted forever once that
        happens, or the feature reports "paused" indefinitely while its
        workflow is genuinely back to dispatching real tasks. Confirmed
        live: feature feat-e1d649cf (WorktreeManager Parameterization)
        showed "paused" in the UI while its development-phase task was
        pending/in-flight, because its workflow had resumed without going
        through resume_feature."""
        with db_manager.session_scope() as session:
            _create_design(session)
            # Workflow is active again, not paused -- e.g. resumed by the
            # self-heal sweep or an admin action, bypassing resume_feature.
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="paused",  # stale -- resume_feature never ran
            )
            session.add(feature)

            # Real, in-flight work under the resumed workflow.
            session.add(Task(
                id="task-1", workflow_id="wf-1", raw_description="Task 1",
                done_definition="Done", status="done",
            ))
            session.add(Task(
                id="task-2", workflow_id="wf-1", raw_description="Task 2",
                done_definition="Done", status="pending",
            ))

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "active"

    def test_returns_completed_when_all_tasks_done(self, db_manager):
        """Should return 'completed' when all tasks are done."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)

            for i in range(3):
                task = Task(
                    id=f"task-{i}",
                    workflow_id="wf-1",
                    raw_description=f"Task {i}",
                    done_definition="Done",
                    status="done",
                )
                session.add(task)

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "completed"

    def test_returns_completed_despite_a_skipped_phase(self, db_manager):
        """A conditionally-skipped phase (e.g. architectural_review/
        adversarial_review/security_review) must count as done, the same
        way derive_workflow_status already treats "skipped" as terminal
        (its own PhaseExecution.status.notin_(["completed", "skipped"])
        check). Excluding "skipped" here disagreed with that and flapped a
        feature back to "active" on every self-heal poll right after
        review_feature's approve handler had just set it "completed" --
        observed live, the feature never settled on Done."""
        from src.core.database import Phase, PhaseExecution

        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="completed", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)

            phase_a = Phase(id="phase-a", workflow_id="wf-1", name="development", order=1, description="d", done_definitions=["x"])
            phase_b = Phase(id="phase-b", workflow_id="wf-1", name="security_review", order=2, description="d", done_definitions=["x"])
            session.add(phase_a)
            session.add(phase_b)
            session.add(PhaseExecution(id="pe-a", phase_id="phase-a", workflow_execution_id="wf-1", status="completed"))
            session.add(PhaseExecution(id="pe-b", phase_id="phase-b", workflow_execution_id="wf-1", status="skipped"))

            task = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task 1",
                done_definition="Done",
                status="done",
            )
            session.add(task)

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "completed"

    def test_returns_active_when_tasks_in_progress(self, db_manager):
        """Should return 'active' when some tasks are in progress."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)

            # Mix of done and in_progress tasks
            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task 1",
                done_definition="Done",
                status="done",
            )
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="Task 2",
                done_definition="Done",
                status="in_progress",
            )
            session.add(task1)
            session.add(task2)

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "active"

    def test_returns_failed_when_all_tasks_failed(self, db_manager):
        """Should return 'failed' when all tasks are failed."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)

            for i in range(2):
                task = Task(
                    id=f"task-{i}",
                    workflow_id="wf-1",
                    raw_description=f"Task {i}",
                    done_definition="Done",
                    status="failed",
                )
                session.add(task)

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "failed"

    def test_excludes_diagnostic_tasks(self, db_manager):
        """Should exclude DIAGNOSTIC: prefixed tasks from status derivation."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature)

            # Regular done task
            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Regular task",
                done_definition="Done",
                status="done",
            )
            session.add(task1)

            # Diagnostic task (should be excluded)
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="DIAGNOSTIC: Health check",
                done_definition="Done",
                status="in_progress",  # Would make feature "active" if included
            )
            session.add(task2)

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        # Should be completed because diagnostic task is excluded
        assert result == "completed"

    def test_self_heals_stale_status(self, db_manager):
        """Should update feature status when it disagrees with derived status."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",  # Stale - should be completed
            )
            session.add(feature)

            task = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task",
                done_definition="Done",
                status="done",
            )
            session.add(task)

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1", write_back=True)

        assert result == "completed"

        # Verify status was self-healed
        with db_manager.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feat-1").first()
            assert feature.status == "completed"

    def test_pending_feature_with_no_workflow_ignores_unrelated_null_workflow_tasks(
        self, db_manager
    ):
        """Regression: Task.workflow_id == feature.workflow_id becomes
        Task.workflow_id IS NULL when a feature hasn't had its own
        workflow launched yet (workflow_id is None) -- that matches every
        OTHER task in the system with a null workflow_id, not "no tasks
        for this feature". Observed live: leftover SDK/API test tasks
        created without a workflow_id, all status="failed", made every
        not-yet-started feature derive (and self-heal write back) status
        "failed" before it had ever actually run -- a freshly decomposed
        design's features all showed "failed" immediately, with no
        workflow, no error, no started_at."""
        with db_manager.session_scope() as session:
            _create_design(session)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id=None,
                status="pending",
            )
            session.add(feature)

            # Unrelated stray task with no workflow_id -- must not be
            # attributed to this feature.
            session.add(
                Task(
                    id="stray-task",
                    workflow_id=None,
                    raw_description="Test task without a workflow",
                    done_definition="Done",
                    status="failed",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1", write_back=True)

        assert result == "pending"
        with db_manager.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feat-1").first()
            assert feature.status == "pending"

    def test_completed_workflow_overrides_an_old_superseded_failed_task(
        self, db_manager
    ):
        """Regression, observed live: a phase can genuinely fail on an
        early attempt and succeed on a later retry within that same
        phase, leaving an old, superseded "failed" Task row behind forever
        -- real history, not evidence of unfinished work. Every branch
        before this fix treated a mix of "done" and "failed" statuses as
        proof the feature still had work to do, self-healing it back to
        "active" on every single poll even though the workflow itself had
        long since reached "completed" (all 12 phases done, merged to
        main). The workflow's own status is the authoritative "did the
        whole pipeline actually finish" signal and must win."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(
                id="wf-1", name="Test", status="completed", phases_folder_path="/tmp/phases"
            )
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="test-feature",
                name="Test Feature",
                scope="Test scope",
                workflow_id="wf-1",
                status="active",  # stale -- should self-heal to completed
            )
            session.add(feature)

            # 39 real "done" tasks plus one old, superseded "failed" one
            # from an early attempt at a phase that later succeeded.
            for i in range(3):
                session.add(
                    Task(
                        id=f"task-done-{i}",
                        workflow_id="wf-1",
                        raw_description=f"Task {i}",
                        done_definition="Done",
                        status="done",
                    )
                )
            session.add(
                Task(
                    id="task-old-failed",
                    workflow_id="wf-1",
                    raw_description="Early development attempt",
                    done_definition="Done",
                    status="failed",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1", write_back=True)

        assert result == "completed"
        with db_manager.session_scope() as session:
            feature = session.query(Feature).filter_by(id="feat-1").first()
            assert feature.status == "completed"

    def test_completed_despite_a_duplicated_task(self, db_manager):
        """Regression, observed live: a debris task resolved as
        "duplicated" (superseded by a sibling task that did the real
        work) permanently broke the `task_statuses == {DONE}` exact-set
        check -- DUPLICATED was never referenced anywhere in this
        function despite being a real terminal status, so a feature with
        every real task done stayed stuck at "active" forever the moment
        any task carried that status."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            session.add(
                Feature(
                    id="feat-1", design_id="design-1", feature_key="test-feature",
                    name="Test Feature", scope="Test scope", workflow_id="wf-1",
                    status="active",
                )
            )
            session.add(
                Task(
                    id="task-real", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="done",
                )
            )
            session.add(
                Task(
                    id="task-debris", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="duplicated",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_feature_status(session, "feat-1")
        assert result == "completed"


class TestDeriveWorkflowStatus:
    """Tests for derive_workflow_status function."""

    def test_returns_paused_when_workflow_paused(self, db_manager):
        """Should return 'paused' when workflow is explicitly paused."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="paused", phases_folder_path="/tmp/phases")
            session.add(wf)

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "paused"

    def test_returns_completed_when_all_tasks_done(self, db_manager):
        """Should return 'completed' when all tasks are done."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            for i in range(3):
                task = Task(
                    id=f"task-{i}",
                    workflow_id="wf-1",
                    raw_description=f"Task {i}",
                    done_definition="Done",
                    status="done",
                )
                session.add(task)

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "completed"

    def test_returns_active_when_tasks_mixed(self, db_manager):
        """Should return 'active' when tasks have mixed statuses."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task 1",
                done_definition="Done",
                status="done",
            )
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="Task 2",
                done_definition="Done",
                status="pending",
            )
            session.add(task1)
            session.add(task2)

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "active"

    def test_stays_active_when_a_later_phase_has_no_task_yet(self, db_manager):
        """Regression, observed live: task_statuses == {"done"} only looks
        at tasks that EXIST -- a phase that hasn't been dispatched yet has
        ZERO tasks, invisible to that check entirely. A workflow whose only
        task (for product_validation) is "done" while doc_review,
        forensics_analysis, git_expert, and deploy are all still
        "pending" with no task ever created for them must NOT derive
        "completed" -- that's a workflow that hasn't reached the phase
        that actually merges to main, not a finished one."""
        from src.core.database import Phase, PhaseExecution

        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            session.add(
                Phase(
                    id="phase-pv", workflow_id="wf-1", order=9,
                    name="product_validation", description="d", done_definitions=["x"],
                )
            )
            session.add(
                Phase(
                    id="phase-doc", workflow_id="wf-1", order=10,
                    name="doc_review", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-doc", phase_id="phase-doc",
                    workflow_execution_id="wf-1", status="pending",
                )
            )
            session.add(
                Task(
                    id="task-pv", workflow_id="wf-1", phase_id="phase-pv",
                    raw_description="product_validation", done_definition="Done",
                    status="done",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "active"

    def test_completes_when_every_phase_execution_is_done(self, db_manager):
        """Sanity check the fix isn't overbroad: a genuinely finished
        workflow (every Phase's own PhaseExecution reached completed) with
        all tasks done must still derive "completed"."""
        from src.core.database import Phase, PhaseExecution

        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            session.add(
                Phase(
                    id="phase-deploy", workflow_id="wf-1", order=13,
                    name="deploy", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-deploy", phase_id="phase-deploy",
                    workflow_execution_id="wf-1", status="completed",
                )
            )
            session.add(
                Task(
                    id="task-deploy", workflow_id="wf-1", phase_id="phase-deploy",
                    raw_description="deploy", done_definition="Done",
                    status="done",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "completed"

    def test_respects_deliberate_failed_status_with_an_incomplete_phase(self, db_manager):
        """Regression, observed live: _trigger_arbitration marks a workflow
        "failed" (with status_reason) once a phase has exhausted its
        arbitration-attempts cap -- a deliberate, workflow-level terminal
        decision with NO corresponding task-level trace (it never creates a
        new task). Every EXISTING task can still legitimately be "done", so
        without this fix task_statuses == {DONE} derived "active" and
        silently resurrected the workflow within ~1-2 seconds, every single
        self-heal cycle, forever -- re-triggering the same doomed
        evaluation and climbing total_gotos into the hundreds with no way
        to actually progress, since the arbitration cap only blocks a NEW
        arbitration task, not another trip through this exact loop."""
        from src.core.database import Phase, PhaseExecution

        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(
                id="wf-1", name="Test", status="failed",
                status_reason="scope_review: arbitrated 3 times without converging",
                phases_folder_path="/tmp/phases",
            )
            session.add(wf)
            # scope_review stuck "in_progress" (arbitration never resolved
            # it) -- an incomplete phase, same as the live incident.
            session.add(Phase(
                id="phase-scope", workflow_id="wf-1", order=2,
                name="scope_review", description="d", done_definitions=["x"],
            ))
            session.add(PhaseExecution(
                id="exec-scope", phase_id="phase-scope",
                workflow_execution_id="wf-1", status="in_progress",
            ))
            # Every task that actually exists is "done" -- no task-level
            # signal that anything is wrong.
            session.add(Task(
                id="task-scope", workflow_id="wf-1", phase_id="phase-scope",
                raw_description="scope review", done_definition="Done",
                status="done",
            ))

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "failed"

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed", "must not have been written back to active"

    def test_respects_deliberate_failed_status_even_when_every_phase_is_complete(
        self, db_manager
    ):
        """Regression, observed live: the FAILED guard above only fired
        inside the incomplete_phase branch -- unreachable whenever every
        phase happens to already look complete/skipped, exactly the shape
        of an abandoned review-pause (the last real task, e.g.
        feature_review, completed normally before the workflow was later
        marked "failed" for an unrelated reason with no new task ever
        created). Without this, the phase-completeness branch above
        force-writes "completed" over a deliberate "failed" -- observed
        live via the periodic design-status poll's write_back=True call,
        which flipped a review-gate workflow from "failed" back to
        "completed" every ~10s, resurrecting it behind the user's back and
        making the Rerun/Recover button (which only matches status in
        {active, paused, failed}) silently no-op."""
        from src.core.database import Phase, PhaseExecution

        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(
                id="wf-1", name="Test", status="failed",
                status_reason="Abandoned: no agent/task activity for 6 consecutive resume attempts",
                paused_by="review",
                phases_folder_path="/tmp/phases",
            )
            session.add(wf)
            session.add(Phase(
                id="phase-review", workflow_id="wf-1", order=1,
                name="feature_review", description="d", done_definitions=["x"],
            ))
            session.add(PhaseExecution(
                id="exec-review", phase_id="phase-review",
                workflow_execution_id="wf-1", status="completed",
            ))
            session.add(Task(
                id="task-review", workflow_id="wf-1", phase_id="phase-review",
                raw_description="feature review", done_definition="Done",
                status="done",
            ))

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "failed"

        with db_manager.session_scope() as session:
            wf = session.query(Workflow).filter_by(id="wf-1").first()
            assert wf.status == "failed", "must not have been written back to completed"

    def test_completes_despite_an_old_superseded_failed_task(self, db_manager):
        """Regression, observed live: a long goto/retry history leaves old
        "failed" Task rows behind as real history even after a later retry
        of the same phase succeeded (nothing ever deletes them). Every
        Phase's own PhaseExecution having reached completed/skipped is the
        authoritative "did the whole pipeline finish" signal -- mirrors
        derive_feature_status's identical, already-proven protection for
        this exact class of artifact. Without it, a single harmless
        leftover failed task anywhere in history permanently blocks a
        genuinely finished workflow from ever deriving "completed" again."""
        from src.core.database import Phase, PhaseExecution

        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            session.add(
                Phase(
                    id="phase-deploy", workflow_id="wf-1", order=13,
                    name="deploy", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-deploy", phase_id="phase-deploy",
                    workflow_execution_id="wf-1", status="completed",
                )
            )
            # An early attempt at this phase failed; a later retry (below)
            # succeeded. The failed row is real history, never deleted.
            session.add(
                Task(
                    id="task-deploy-attempt-1", workflow_id="wf-1", phase_id="phase-deploy",
                    raw_description="deploy", done_definition="Done",
                    status="failed",
                )
            )
            session.add(
                Task(
                    id="task-deploy-attempt-2", workflow_id="wf-1", phase_id="phase-deploy",
                    raw_description="deploy", done_definition="Done",
                    status="done",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "completed"

    def test_completed_despite_a_duplicated_task(self, db_manager):
        """Same regression as TestDeriveFeatureStatus's version, one level
        up: a task resolved as "duplicated" must not break the
        `task_statuses == {DONE}` check here either."""
        with db_manager.session_scope() as session:
            _create_design(session)
            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)
            session.add(
                Task(
                    id="task-real", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="done",
                )
            )
            session.add(
                Task(
                    id="task-debris", workflow_id="wf-1", raw_description="r",
                    done_definition="d", status="duplicated",
                )
            )

        with db_manager.session_scope() as session:
            result = derive_workflow_status(session, "wf-1")
        assert result == "completed"


class TestDeriveDesignStatus:
    """Tests for derive_design_status function."""

    def test_returns_pending_when_no_features(self, db_manager):
        """Should return design's DB status when no features exist."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1", status="pending")

        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "pending"

    def test_returns_completed_when_all_features_completed(self, db_manager):
        """Should return 'completed' when all features are completed."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1")

            wf = Workflow(id="wf-1", name="Test", status="completed", phases_folder_path="/tmp/phases")
            session.add(wf)

            feature = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="feature-1",
                name="Feature 1",
                scope="Scope 1",
                workflow_id="wf-1",
                status="completed",
            )
            session.add(feature)

            # Add done task so derive_feature_status returns completed
            task = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task",
                done_definition="Done",
                status="done",
            )
            session.add(task)

        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "completed"

    def test_returns_active_when_any_feature_active(self, db_manager):
        """Should return 'active' when any feature is active."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1")

            wf = Workflow(id="wf-1", name="Test", status="active", phases_folder_path="/tmp/phases")
            session.add(wf)

            # Completed feature
            feature1 = Feature(
                id="feat-1",
                design_id="design-1",
                feature_key="feature-1",
                name="Feature 1",
                scope="Scope 1",
                workflow_id="wf-1",
                status="completed",
            )
            session.add(feature1)

            # Active feature
            feature2 = Feature(
                id="feat-2",
                design_id="design-1",
                feature_key="feature-2",
                name="Feature 2",
                scope="Scope 2",
                workflow_id="wf-1",
                status="active",
            )
            session.add(feature2)

            # Tasks for completed feature
            task1 = Task(
                id="task-1",
                workflow_id="wf-1",
                raw_description="Task 1",
                done_definition="Done",
                status="done",
            )
            session.add(task1)

            # Tasks for active feature
            task2 = Task(
                id="task-2",
                workflow_id="wf-1",
                raw_description="Task 2",
                done_definition="Done",
                status="in_progress",
            )
            session.add(task2)

        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "active"

    def test_orphaned_failed_workflow_does_not_block_completion(self, db_manager):
        """Regression: a failed workflow linked to the design (design_id
        set) but to no feature at all -- e.g. a failed Feature Architect
        retry attempt superseded by a later successful one -- used to keep
        has_failed_wf True forever, even once every real feature was
        genuinely completed. That kept the design stuck showing "active"
        in the UI indefinitely, since nothing but pick_next_design's own
        active-designs loop (a different, much less frequently invoked
        code path) ever clears the orphan's design_id.

        A second, skipped feature is required to actually reach the
        has_failed_wf-dependent branch: the plain
        `feature_statuses == {COMPLETED}` check earlier in the if/elif
        chain fires unconditionally when the only feature is "completed",
        before has_failed_wf is ever consulted."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1")

            wf_done = Workflow(id="wf-done", name="Test", status="completed", phases_folder_path="/tmp")
            session.add(wf_done)
            wf_orphaned = Workflow(
                id="wf-orphaned", name="Feature Architect", status="failed",
                phases_folder_path="/tmp", design_id="design-1",
            )
            session.add(wf_orphaned)

            feature = Feature(
                id="feat-1", design_id="design-1", feature_key="feature-1",
                name="Feature 1", scope="Scope 1", workflow_id="wf-done",
                status="completed",
            )
            session.add(feature)
            skipped = Feature(
                id="feat-2", design_id="design-1", feature_key="feature-2",
                name="Feature 2", scope="Scope 2", status="skipped",
            )
            session.add(skipped)

            task = Task(
                id="task-1", workflow_id="wf-done", raw_description="Task",
                done_definition="Done", status="done",
            )
            session.add(task)

        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "completed"

    def test_failed_workflow_linked_to_a_feature_keeps_active(self, db_manager):
        """Companion to the orphan regression above: the scoping fix must
        not swallow a GENUINE failure just because it excludes true
        orphans. A feature with no tasks yet for its linked workflow
        returns its raw persisted status untouched (derive_feature_status's
        "no tasks yet" branch) -- so a feature marked "completed" whose
        linked workflow subsequently failed (e.g. a lightweight feature
        with no per-task tracking) must still keep the design "active" for
        retry, not roll up to "completed" just because every non-skipped
        Feature.status happens to already read "completed". A second,
        skipped feature is required to reach this branch at all: the plain
        `feature_statuses == {COMPLETED}` check earlier in the if/elif
        chain fires unconditionally otherwise, before has_failed_wf is
        ever consulted -- has_failed_wf only matters when non_skipped_
        statuses differs from feature_statuses (i.e. a skipped feature is
        also present)."""
        with db_manager.session_scope() as session:
            _create_design(session, "design-1")

            wf_failed = Workflow(
                id="wf-failed", name="Test", status="failed", phases_folder_path="/tmp",
                design_id="design-1",
            )
            session.add(wf_failed)

            # No Task rows for wf-failed -- derive_feature_status returns
            # feature.status as-is instead of deriving from tasks.
            feature = Feature(
                id="feat-1", design_id="design-1", feature_key="feature-1",
                name="Feature 1", scope="Scope 1", workflow_id="wf-failed",
                status="completed",
            )
            session.add(feature)
            skipped = Feature(
                id="feat-2", design_id="design-1", feature_key="feature-2",
                name="Feature 2", scope="Scope 2", status="skipped",
            )
            session.add(skipped)

        with db_manager.session_scope() as session:
            result = derive_design_status(session, "design-1")
        assert result == "active"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
