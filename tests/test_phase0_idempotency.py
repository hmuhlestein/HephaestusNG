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
        from src.autopilot.orchestrator import _get_phase0_completion

        assert _get_phase0_completion(design) is None

    def test_returns_none_when_workflow_not_completed(self, db_manager, design):
        from src.autopilot.orchestrator import _get_phase0_completion

        _seed_phase0_workflow(db_manager, design, phase_execution_status="active")

        assert _get_phase0_completion(design) is None

    def test_returns_none_when_designs_folder_not_set(self, db_manager, design):
        """designs_folder must also be set — that's what the resume path reads
        features.json back from."""
        from src.autopilot.orchestrator import _get_phase0_completion

        _seed_phase0_workflow(db_manager, design, phase_execution_status="completed")
        # designs_folder deliberately left unset on the design row

        assert _get_phase0_completion(design) is None

    def test_returns_completion_data_when_completed(self, db_manager, design):
        from src.autopilot.orchestrator import _get_phase0_completion

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
        from src.autopilot.orchestrator import _get_phase0_completion

        assert _get_phase0_completion("nonexistent-design-id") is None

    def test_returns_none_for_none_design_id(self, db_manager):
        from src.autopilot.orchestrator import _get_phase0_completion

        assert _get_phase0_completion(None) is None

    def test_returns_none_when_workflow_force_completed_but_last_phase_didnt_run(
        self, db_manager, design
    ):
        """A generic teardown path (WorkflowTerminationHandler, the admin
        POST /api/workflow-executions/{id}/complete endpoint) can set
        Workflow.status="completed" without feature_review ever having run.
        Trusting Workflow.status alone would wrongly treat Phase 0 as fully
        reviewed; the last-phase PhaseExecution check must catch this."""
        from src.autopilot.orchestrator import _get_phase0_completion

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
        from src.autopilot.orchestrator import DesignEntry

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

        with patch("src.autopilot.orchestrator.run_single_workflow") as mock_run:
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

        with patch("src.autopilot.orchestrator.run_single_workflow") as mock_run:
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

        with patch("src.autopilot.orchestrator.run_single_workflow") as mock_run, \
             patch("src.autopilot.orchestrator._create_integration_worktree") as mock_wt:
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
            return "completed"

        with patch(
            "src.autopilot.orchestrator._create_integration_worktree",
            return_value=worktree,
        ), patch(
            "src.autopilot.orchestrator.run_single_workflow",
            side_effect=fake_run_single_workflow,
        ), patch(
            "src.autopilot.orchestrator._cleanup_worktree"
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
        session.close()
