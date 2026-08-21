"""Tests for Phase 0's idempotency mechanism (_get_phase0_completion and
run_phase0's three-tier skip/resume/run check).

See docs/LOOP_ENGINEERING_REVIEW.md for the "Phase 0 bolt-on" finding this
addresses: run_phase0 previously decided whether to skip re-running purely
by querying Feature DB rows, instead of the same PhaseExecution-status
idempotency concept PhaseManager.mark_phase_complete provides every other
phase for free.
"""

import json
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.autopilot.orchestrator.state import FeatureRunStatus
from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    Feature,
    Phase,
    PhaseExecution,
    Workflow,
)


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    """_get_phase0_completion / run_phase0 open sessions via the module-level
    get_db(), which reads HEPHAESTUS_TEST_DB — point it at this fixture's DB."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def design(db_manager, tmp_path):
    """Seed a minimal AutopilotProject + AutopilotDesign row, return its id."""
    session = db_manager.get_session()
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    design_id = f"des-{uuid.uuid4().hex[:8]}"
    session.add(
        AutopilotProject(id=project_id, name="p", base_dir=str(tmp_path))
    )
    session.add(
        AutopilotDesign(
            id=design_id,
            project_id=project_id,
            filename="design.md",
            name="Test Design",
            status="pending",
        )
    )
    session.commit()
    session.close()
    return design_id


def _seed_phase0_workflow(db_manager, design_id, phase_execution_status):
    """Create a Workflow + Phase + PhaseExecution for Phase 0, link it to the
    design via phase0_workflow_id, return the workflow_id."""
    session = db_manager.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    phase_id = f"phase-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(
            id=workflow_id,
            name="Phase 0",
            phases_folder_path="/tmp",
            status="completed" if phase_execution_status == "completed" else "active",
            definition_id="feature_architect",
            design_id=design_id,
        )
    )
    session.add(
        Phase(
            id=phase_id,
            workflow_id=workflow_id,
            order=1,
            name="Feature Architect",
            description="d",
            done_definitions=["done"],
        )
    )
    execution_status = (
        "in_progress" if phase_execution_status == "active" else phase_execution_status
    )
    session.add(
        PhaseExecution(
            id=f"exec-{uuid.uuid4().hex[:8]}",
            phase_id=phase_id,
            status=execution_status,
        )
    )
    design = session.query(AutopilotDesign).filter_by(id=design_id).first()
    design.phase0_workflow_id = workflow_id
    session.commit()
    session.close()
    return workflow_id


class TestGetPhase0Completion:
    def test_returns_none_when_no_workflow_id_set(self, db_manager, design):
        from src.autopilot.orchestrator.queue import _get_phase0_completion

        assert _get_phase0_completion(design) is None

    def test_returns_none_when_workflow_not_completed(self, db_manager, design):
        from src.autopilot.orchestrator.queue import _get_phase0_completion

        _seed_phase0_workflow(db_manager, design, phase_execution_status="active")

        assert _get_phase0_completion(design) is None

    def test_returns_none_when_designs_folder_not_set(self, db_manager, design):
        """designs_folder must also be set — that's what the resume path reads
        features.json back from."""
        from src.autopilot.orchestrator.queue import _get_phase0_completion

        _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        # designs_folder deliberately left unset on the design row

        assert _get_phase0_completion(design) is None

    def test_returns_completion_data_when_completed(self, db_manager, design):
        from src.autopilot.orchestrator.queue import _get_phase0_completion

        workflow_id = _seed_phase0_workflow(
            db_manager, design, phase_execution_status="completed"
        )
        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = "/tmp/some-designs-folder"
        session.commit()
        session.close()

        result = _get_phase0_completion(design)

        assert result is not None
        assert result["workflow_id"] == workflow_id
        assert result["designs_folder"] == "/tmp/some-designs-folder"

    def test_returns_none_for_missing_design(self, db_manager):
        from src.autopilot.orchestrator.queue import _get_phase0_completion

        assert _get_phase0_completion("nonexistent-design-id") is None

    def test_returns_none_for_none_design_id(self, db_manager):
        from src.autopilot.orchestrator.queue import _get_phase0_completion

        assert _get_phase0_completion(None) is None

    def test_returns_none_when_workflow_force_completed_but_last_phase_didnt_run(
        self, db_manager, design
    ):
        """A generic teardown path (WorkflowTerminationHandler, the admin
        POST /api/workflow-executions/{id}/complete endpoint) can set
        Workflow.status="completed" without feature_review ever having run.
        Trusting Workflow.status alone would wrongly treat Phase 0 as fully
        reviewed; the last-phase PhaseExecution check must catch this."""
        from src.autopilot.orchestrator.queue import _get_phase0_completion

        session = db_manager.get_session()
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
        phase_id = f"phase-{uuid.uuid4().hex[:8]}"
        session.add(
            Workflow(
                id=workflow_id,
                name="Phase 0",
                phases_folder_path="/tmp",
                status="completed",  # forced complete by a generic teardown path
                definition_id="feature_architect",
                design_id=design,
            )
        )
        session.add(
            Phase(
                id=phase_id,
                workflow_id=workflow_id,
                order=1,
                name="Feature Architect",
                description="d",
                done_definitions=["done"],
            )
        )
        session.add(
            PhaseExecution(
                id=f"exec-{uuid.uuid4().hex[:8]}",
                phase_id=phase_id,
                status="in_progress",  # feature_review never ran or completed
            )
        )
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.phase0_workflow_id = workflow_id
        d.designs_folder = "/tmp/some-designs-folder"
        session.commit()
        session.close()

        assert _get_phase0_completion(design) is None


class TestRunPhase0Tiers:
    def _make_design_entry(self, design_id, tmp_path):
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design")
        return DesignEntry(
            path=design_path,
            name="Test Design",
            content_hash="abc123",
            db_id=design_id,
        )

    def test_tier1_skips_when_feature_rows_exist(self, db_manager, design, tmp_path):
        """Existing behavior, must stay unchanged: Feature rows present ->
        never call run_single_workflow."""
        from src.autopilot.orchestrator import run_phase0

        session = db_manager.get_session()
        session.add(
            Feature(
                id=f"feat-{uuid.uuid4().hex[:8]}",
                design_id=design,
                feature_key="auth",
                name="Auth",
                scope="s",
                status="pending",
            )
        )
        session.commit()
        session.close()

        design_entry = self._make_design_entry(design, tmp_path)

        with patch("src.autopilot.orchestrator.pipeline.run_single_workflow") as mock_run:
            features_json, designs_folder = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        mock_run.assert_not_called()
        assert features_json is not None
        assert features_json["features"][0]["id"] == "auth"

    def test_tier2_resumes_without_rerunning_agent(self, db_manager, design, tmp_path):
        """Workflow already completed, no Feature rows yet (simulates a crash
        between workflow completion and _create_feature_records) -> resume
        from the persisted features.json instead of calling run_single_workflow."""
        from src.autopilot.orchestrator import run_phase0

        _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")

        designs_folder = tmp_path / "designs" / "prior_run"
        designs_folder.mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "s",
                    "files": ["src/auth.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        (designs_folder / "features.json").write_text(json.dumps(features_json_content))

        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = str(designs_folder)
        session.commit()
        session.close()

        design_entry = self._make_design_entry(design, tmp_path)

        with patch("src.autopilot.orchestrator.pipeline.run_single_workflow") as mock_run:
            features_json, returned_folder = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        mock_run.assert_not_called()
        assert features_json["features"][0]["id"] == "auth"
        assert str(returned_folder) == str(designs_folder)

        # Feature record should now have been created from the resumed data
        session = db_manager.get_session()
        features = session.query(Feature).filter_by(design_id=design).all()
        assert len(features) == 1
        assert features[0].feature_key == "auth"
        session.close()

    def test_tier2_falls_through_to_full_rerun_when_features_json_missing(
        self, db_manager, design, tmp_path
    ):
        """Workflow marked completed but no features.json on disk anywhere ->
        must NOT crash, falls through to a full re-run instead."""
        from src.autopilot.orchestrator import run_phase0

        _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")

        # designs_folder points somewhere real but features.json is missing
        designs_folder = tmp_path / "designs" / "prior_run_incomplete"
        designs_folder.mkdir(parents=True)
        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = str(designs_folder)
        session.commit()
        session.close()

        design_entry = self._make_design_entry(design, tmp_path)

        with patch("src.autopilot.orchestrator.pipeline.run_single_workflow") as mock_run, \
             patch("src.autopilot.orchestrator.pipeline._create_integration_worktree") as mock_wt:
            mock_wt.return_value = None  # short-circuit before launching a real workflow
            run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        # Falls through past tier 2 into the normal tier-3 path, which then
        # tries (and in this test, fails) to create a worktree -- the key
        # assertion is that it did NOT resume via run_single_workflow being
        # skipped a second time / crash on the missing file.
        mock_run.assert_not_called()
        mock_wt.assert_called_once()

    def test_tier3_sets_phase0_workflow_id_on_full_run(
        self, db_manager, design, tmp_path
    ):
        """Neither tier 1 nor tier 2 applies -> full run; on success,
        phase0_workflow_id must get set on the AutopilotDesign row so a
        future re-entrant call can use tier 2."""
        from src.autopilot.orchestrator import run_phase0

        design_entry = self._make_design_entry(design, tmp_path)
        worktree = tmp_path / "worktree"
        (worktree / ".hephaestus" / "features").mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "s",
                    "files": ["src/auth.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        (worktree / ".hephaestus" / "features.json").write_text(
            json.dumps(features_json_content)
        )

        # Fake the Workflow row that a real run_single_workflow would create
        real_workflow_id = f"wf-{uuid.uuid4().hex[:8]}"

        def fake_run_single_workflow(*args, **kwargs):
            session = db_manager.get_session()
            session.add(
                Workflow(
                    id=real_workflow_id,
                    name="Phase 0",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_architect",
                    design_id=design,
                )
            )
            session.commit()
            session.close()
            # run_single_workflow now persists phase0_workflow_id itself,
            # immediately after launch (not after this mock's caller sees
            # "completed") -- replicate that here since this fake replaces
            # the whole function.
            session = db_manager.get_session()
            d = session.query(AutopilotDesign).filter_by(id=design).first()
            d.phase0_workflow_id = real_workflow_id
            session.commit()
            session.close()
            return FeatureRunStatus.COMPLETED

        with patch(
            "src.autopilot.orchestrator.pipeline._create_integration_worktree",
            return_value=worktree,
        ), patch(
            "src.autopilot.orchestrator.pipeline.run_single_workflow",
            side_effect=fake_run_single_workflow,
        ), patch(
            "src.autopilot.orchestrator.worktree_integration._cleanup_worktree"
        ):
            features_json, designs_folder = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        assert features_json["features"][0]["id"] == "auth"

        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        assert d.phase0_workflow_id == real_workflow_id
        wf = session.query(Workflow).filter_by(id=real_workflow_id).first()
        assert wf.workflow_type == "design"

    def test_feature_review_report_copied_to_designs_folder(
        self, db_manager, design, tmp_path
    ):
        """Regression: .hephaestus/ is git-excluded and gets deleted
        entirely by _cleanup_worktree once Phase 0's workflow finishes --
        unlike features.json/scope.md, feature_review's report/result had
        no equivalent copy-to-designs_folder step, so a clean review pass
        (no goto ever fired to embed the report text in a corrective task)
        left zero audit trail of what the reviewer actually checked."""
        from src.autopilot.orchestrator import run_phase0

        design_entry = self._make_design_entry(design, tmp_path)
        worktree = tmp_path / "worktree"
        (worktree / ".hephaestus" / "features").mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "s",
                    "files": ["src/auth.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        (worktree / ".hephaestus" / "features.json").write_text(
            json.dumps(features_json_content)
        )
        (worktree / ".hephaestus" / "feature_review").mkdir(parents=True)
        (worktree / ".hephaestus" / "feature_review" / "feature_review.md").write_text(
            "---\ntype: feature_review_result\nblocker_count: 0\nfix_count: 0\ndefer_count: 0\n---\n\n"
            "# Feature Review Report\n\nClean pass."
        )

        def fake_run_single_workflow(*args, **kwargs):
            session = db_manager.get_session()
            session.add(
                Workflow(
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    name="Phase 0",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_architect",
                    design_id=design,
                )
            )
            session.commit()
            session.close()
            return FeatureRunStatus.COMPLETED

        with patch(
            "src.autopilot.orchestrator.pipeline._create_integration_worktree",
            return_value=worktree,
        ), patch(
            "src.autopilot.orchestrator.pipeline.run_single_workflow",
            side_effect=fake_run_single_workflow,
        ), patch(
            "src.autopilot.orchestrator.worktree_integration._cleanup_worktree"
        ):
            features_json, designs_folder = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        assert (designs_folder / "feature_review.md").read_text() == (
            "---\ntype: feature_review_result\nblocker_count: 0\nfix_count: 0\ndefer_count: 0\n---\n\n"
            "# Feature Review Report\n\nClean pass."
        )

    def test_feature_report_synopsis_copied_to_designs_folder(
        self, db_manager, design, tmp_path
    ):
        """feature_review's HTML decomposition synopsis needs the same
        durability copy as feature_review.md -- otherwise it's gone the
        moment _cleanup_worktree removes the (git-excluded) worktree."""
        from src.autopilot.orchestrator import run_phase0

        design_entry = self._make_design_entry(design, tmp_path)
        worktree = tmp_path / "worktree"
        (worktree / ".hephaestus" / "features").mkdir(parents=True)
        (worktree / ".hephaestus" / "features.json").write_text(
            json.dumps(
                {
                    "design_name": "Test Design",
                    "features": [
                        {
                            "id": "auth",
                            "name": "Auth",
                            "scope": "s",
                            "files": ["src/auth.py"],
                            "depends_on": [],
                            "execution": "parallel",
                        }
                    ],
                }
            )
        )
        (worktree / ".hephaestus" / "feature_review").mkdir(parents=True)
        (worktree / ".hephaestus" / "feature_review" / "feature_report.html").write_text(
            "<html><title>Test Project: Feature Decomposition</title></html>"
        )

        def fake_run_single_workflow(*args, **kwargs):
            session = db_manager.get_session()
            session.add(
                Workflow(
                    id=f"wf-{uuid.uuid4().hex[:8]}",
                    name="Phase 0",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_architect",
                    design_id=design,
                )
            )
            session.commit()
            session.close()
            return FeatureRunStatus.COMPLETED

        with patch(
            "src.autopilot.orchestrator.pipeline._create_integration_worktree",
            return_value=worktree,
        ), patch(
            "src.autopilot.orchestrator.pipeline.run_single_workflow",
            side_effect=fake_run_single_workflow,
        ), patch(
            "src.autopilot.orchestrator.worktree_integration._cleanup_worktree"
        ):
            _, designs_folder = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        assert (designs_folder / "feature_report.html").read_text() == (
            "<html><title>Test Project: Feature Decomposition</title></html>"
        )


class TestRunPhase0ReviewMode:
    """Phase 0's own review-mode gate: pause after the decomposition is
    reviewed (feature_review passes), before any Feature rows -- and
    therefore any per-feature pipeline -- get created from it."""

    def _make_design_entry(self, design_id, tmp_path):
        from src.autopilot.orchestrator.state import DesignEntry

        design_path = tmp_path / "design.md"
        design_path.write_text("# Design")
        return DesignEntry(
            path=design_path,
            name="Test Design",
            content_hash="abc123",
            db_id=design_id,
        )

    def _enable_review_mode(self, db_manager, design) -> str:
        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        proj = session.query(AutopilotProject).filter_by(id=d.project_id).first()
        proj.review_mode = True
        project_id = proj.id
        session.commit()
        session.close()
        return project_id

    def _fake_run_single_workflow(self, db_manager, design, workflow_id):
        def _fake(*args, **kwargs):
            session = db_manager.get_session()
            session.add(
                Workflow(
                    id=workflow_id,
                    name="Phase 0",
                    phases_folder_path="/tmp",
                    status="completed",
                    definition_id="feature_architect",
                    design_id=design,
                )
            )
            session.commit()
            session.close()
            return FeatureRunStatus.COMPLETED

        return _fake

    def _worktree_with_features_json(self, tmp_path):
        worktree = tmp_path / "worktree"
        (worktree / ".hephaestus" / "features").mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "s",
                    "files": ["src/auth.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        (worktree / ".hephaestus" / "features.json").write_text(
            json.dumps(features_json_content)
        )
        return worktree

    def test_pauses_and_creates_features_only_after_clearance(
        self, db_manager, design, tmp_path
    ):
        from src.autopilot.orchestrator import run_phase0

        project_id = self._enable_review_mode(db_manager, design)
        design_entry = self._make_design_entry(design, tmp_path)
        worktree = self._worktree_with_features_json(tmp_path)
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"

        with patch(
            "src.autopilot.orchestrator.pipeline._create_integration_worktree",
            return_value=worktree,
        ), patch(
            "src.autopilot.orchestrator.pipeline.run_single_workflow",
            side_effect=self._fake_run_single_workflow(db_manager, design, workflow_id),
        ), patch(
            "src.autopilot.orchestrator.worktree_integration._cleanup_worktree"
        ), patch(
            "src.autopilot.orchestrator._wait_for_phase0_review_clearance",
            return_value=True,
        ) as mock_wait:
            features_json, _ = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
                project_id=project_id,
            )

        mock_wait.assert_called_once()
        assert mock_wait.call_args[0][0] == workflow_id
        assert features_json["features"][0]["id"] == "auth"

        session = db_manager.get_session()
        # Paused, then cleared -- must end up "completed", not stuck on
        # whatever the generic resume/recover action would have left it as
        # ("active"), or _get_phase0_completion's Tier-2 recovery check
        # never recognizes this workflow as done again.
        wf = session.query(Workflow).filter_by(id=workflow_id).first()
        assert wf.status == "completed"
        features = session.query(Feature).filter_by(design_id=design).all()
        assert len(features) == 1
        session.close()

    def test_skips_pause_when_review_mode_disabled(self, db_manager, design, tmp_path):
        """Default behavior, must stay unchanged: no review_mode -> no
        pause, no wait, features created immediately."""
        from src.autopilot.orchestrator import run_phase0

        design_entry = self._make_design_entry(design, tmp_path)
        worktree = self._worktree_with_features_json(tmp_path)
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"

        with patch(
            "src.autopilot.orchestrator.pipeline._create_integration_worktree",
            return_value=worktree,
        ), patch(
            "src.autopilot.orchestrator.pipeline.run_single_workflow",
            side_effect=self._fake_run_single_workflow(db_manager, design, workflow_id),
        ), patch(
            "src.autopilot.orchestrator.worktree_integration._cleanup_worktree"
        ), patch(
            "src.autopilot.orchestrator._wait_for_phase0_review_clearance"
        ) as mock_wait:
            features_json, _ = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        mock_wait.assert_not_called()
        assert features_json["features"][0]["id"] == "auth"
        session = db_manager.get_session()
        assert session.query(Feature).filter_by(design_id=design).count() == 1
        session.close()

    def test_stop_signal_during_wait_creates_no_features(
        self, db_manager, design, tmp_path
    ):
        """_wait_for_phase0_review_clearance returning False means the
        stop signal fired (or the workflow vanished) -- must not create
        Feature rows from a decomposition nobody approved."""
        from src.autopilot.orchestrator import run_phase0

        project_id = self._enable_review_mode(db_manager, design)
        design_entry = self._make_design_entry(design, tmp_path)
        worktree = self._worktree_with_features_json(tmp_path)
        workflow_id = f"wf-{uuid.uuid4().hex[:8]}"

        with patch(
            "src.autopilot.orchestrator.pipeline._create_integration_worktree",
            return_value=worktree,
        ), patch(
            "src.autopilot.orchestrator.pipeline.run_single_workflow",
            side_effect=self._fake_run_single_workflow(db_manager, design, workflow_id),
        ), patch(
            "src.autopilot.orchestrator.worktree_integration._cleanup_worktree"
        ), patch(
            "src.autopilot.orchestrator._wait_for_phase0_review_clearance",
            return_value=False,
        ):
            result = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
                project_id=project_id,
            )

        assert result == (None, None)
        session = db_manager.get_session()
        assert session.query(Feature).filter_by(design_id=design).count() == 0
        session.close()

    def test_restart_during_pause_reenters_wait_without_rerunning_agent(
        self, db_manager, design, tmp_path
    ):
        """Regression: a backend restart while Phase 0 sat paused for
        review used to re-enter run_phase0 with no Feature rows yet and a
        Workflow.status of "paused" (not "completed") -- Tier 2's
        wf.status == "completed" check failed, falling through to a full,
        wasteful re-decomposition of already-finished, already-reviewed
        work. Must re-enter the wait instead."""
        from src.autopilot.orchestrator import run_phase0

        self._enable_review_mode(db_manager, design)
        design_entry = self._make_design_entry(design, tmp_path)

        # _get_phase0_completion (Tier 2) needs a genuinely completed
        # Phase+PhaseExecution underneath the Workflow row too, not just
        # the Workflow itself -- use the same helper the Tier-2-specific
        # tests above use, then flip the resulting row to "paused for
        # review" to simulate a restart mid-wait.
        workflow_id = _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        designs_folder = tmp_path / "designs" / "prior_run"
        designs_folder.mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "s",
                    "files": ["src/auth.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        (designs_folder / "features.json").write_text(json.dumps(features_json_content))

        session = db_manager.get_session()
        wf = session.query(Workflow).filter_by(id=workflow_id).first()
        wf.status = "paused"
        wf.paused_by = "review"
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = str(designs_folder)
        session.commit()
        session.close()

        with patch(
            "src.autopilot.orchestrator.pipeline.run_single_workflow"
        ) as mock_run, patch(
            "src.autopilot.orchestrator._wait_for_phase0_review_clearance",
            return_value=True,
        ) as mock_wait:
            features_json, returned_folder = run_phase0(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        mock_run.assert_not_called()
        mock_wait.assert_called_once()
        assert mock_wait.call_args[0][0] == workflow_id
        assert features_json["features"][0]["id"] == "auth"
        assert str(returned_folder) == str(designs_folder)

        session = db_manager.get_session()
        wf = session.query(Workflow).filter_by(id=workflow_id).first()
        assert wf.status == "completed"
        assert session.query(Feature).filter_by(design_id=design).count() == 1
        session.close()


class TestFinalizePhase0Workflow:
    """finalize_phase0_workflow: the generic, independent path to recover a
    Phase 0 workflow that reached "completed" without run_phase0's own
    synchronous tail ever observing it (e.g. a backend restart mid-wait) --
    root cause of the FRONTEND_DESIGN.md incident, where a genuinely
    completed Phase 0 workflow was left with zero Feature rows and no
    recovery short of "Rerun"."""

    def test_idempotent_when_features_already_exist(self, db_manager, design, tmp_path):
        from src.autopilot.orchestrator import finalize_phase0_workflow

        workflow_id = _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        session = db_manager.get_session()
        session.add(
            Feature(
                id=f"feat-{uuid.uuid4().hex[:8]}",
                design_id=design,
                feature_key="auth",
                name="Auth",
                scope="s",
                status="pending",
            )
        )
        session.commit()
        session.close()

        result = finalize_phase0_workflow(workflow_id, logger=MagicMock())

        assert result is True

    def test_returns_false_when_no_designs_folder(self, db_manager, design, tmp_path):
        from src.autopilot.orchestrator import finalize_phase0_workflow

        workflow_id = _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        # designs_folder deliberately left unset

        result = finalize_phase0_workflow(workflow_id, logger=MagicMock())

        assert result is False
        session = db_manager.get_session()
        assert session.query(Feature).filter_by(design_id=design).count() == 0
        session.close()

    def test_recovers_features_json_from_designs_folder_when_worktree_gone(
        self, db_manager, design, tmp_path
    ):
        """The worktree is long cleaned up by the time an out-of-band
        completion is noticed -- features.json must still be recoverable
        from the permanent designs_folder alone."""
        from src.autopilot.orchestrator import finalize_phase0_workflow

        workflow_id = _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        designs_folder = tmp_path / "designs" / "prior_run"
        designs_folder.mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "s",
                    "files": ["src/auth.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }
        (designs_folder / "features.json").write_text(json.dumps(features_json_content))

        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = str(designs_folder)
        session.commit()
        session.close()

        result = finalize_phase0_workflow(workflow_id, logger=MagicMock())

        assert result is True
        session = db_manager.get_session()
        features = session.query(Feature).filter_by(design_id=design).all()
        assert len(features) == 1
        assert features[0].feature_key == "auth"
        session.close()

    def test_returns_false_when_features_json_unrecoverable(self, db_manager, design, tmp_path):
        """No features.json in the worktree or designs_folder -- e.g.
        FRONTEND_DESIGN.md's actual state -- must not crash, just report
        failure so the caller (or a human) knows Rerun is needed."""
        from src.autopilot.orchestrator import finalize_phase0_workflow

        workflow_id = _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        designs_folder = tmp_path / "designs" / "prior_run"
        designs_folder.mkdir(parents=True)
        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = str(designs_folder)
        session.commit()
        session.close()

        result = finalize_phase0_workflow(workflow_id, logger=MagicMock())

        assert result is False
        session = db_manager.get_session()
        assert session.query(Feature).filter_by(design_id=design).count() == 0
        session.close()

    def test_review_mode_pauses_and_returns_false_without_creating_features(
        self, db_manager, design, tmp_path
    ):
        """Unlike run_phase0's own blocking wait, this is called from
        inline phase-advancement code -- it must pause and return
        immediately (non-blocking), not hang waiting for a human."""
        from src.autopilot.orchestrator import finalize_phase0_workflow

        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        proj = session.query(AutopilotProject).filter_by(id=d.project_id).first()
        proj.review_mode = True
        project_id = proj.id
        session.commit()
        session.close()

        workflow_id = _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        designs_folder = tmp_path / "designs" / "prior_run"
        designs_folder.mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {"id": "auth", "name": "Auth", "scope": "s", "files": [], "depends_on": [], "execution": "parallel"}
            ],
        }
        (designs_folder / "features.json").write_text(json.dumps(features_json_content))
        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = str(designs_folder)
        session.commit()
        session.close()

        result = finalize_phase0_workflow(workflow_id, logger=MagicMock(), project_id=project_id)

        assert result is False
        session = db_manager.get_session()
        wf = session.query(Workflow).filter_by(id=workflow_id).first()
        assert wf.status == "paused"
        assert wf.paused_by == "review"
        assert session.query(Feature).filter_by(design_id=design).count() == 0
        session.close()

    def test_skip_review_gate_creates_features_despite_review_mode(
        self, db_manager, design, tmp_path
    ):
        """The review-approve endpoint calls back in with
        skip_review_gate=True after a human has already cleared the
        pause -- must finish the job rather than re-pausing."""
        from src.autopilot.orchestrator import finalize_phase0_workflow

        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        proj = session.query(AutopilotProject).filter_by(id=d.project_id).first()
        proj.review_mode = True
        project_id = proj.id
        session.commit()
        session.close()

        workflow_id = _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        designs_folder = tmp_path / "designs" / "prior_run"
        designs_folder.mkdir(parents=True)
        features_json_content = {
            "design_name": "Test Design",
            "features": [
                {"id": "auth", "name": "Auth", "scope": "s", "files": [], "depends_on": [], "execution": "parallel"}
            ],
        }
        (designs_folder / "features.json").write_text(json.dumps(features_json_content))
        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=design).first()
        d.designs_folder = str(designs_folder)
        session.commit()
        session.close()

        result = finalize_phase0_workflow(
            workflow_id, logger=MagicMock(), project_id=project_id, skip_review_gate=True
        )

        assert result is True
        session = db_manager.get_session()
        assert session.query(Feature).filter_by(design_id=design).count() == 1
        session.close()
