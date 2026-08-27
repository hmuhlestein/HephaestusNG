"""A bugfix design skips Phase 0 (Feature Architect decomposition)
entirely, per docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md's plan overridden to
drop decomposition for bugfix designs -- a bug report is already a
single, atomic fix; there's nothing to decompose it into, and running
the Feature Architect agent anyway would just add the exact
decomposition-overhead cost this workflow-type split was meant to
remove.

run_bugfix_single_feature (src/autopilot/orchestrator/pipeline.py)
constructs the same (features_json, designs_folder) result run_phase0
returns, reusing its downstream machinery (_create_feature_records,
which denormalizes workflow_type='bugfix' onto the Feature row) without
ever launching the feature_architect workflow.
"""

import re
import uuid
from unittest.mock import MagicMock, patch

import pytest

from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager, Feature


@pytest.fixture
def db_manager(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def bugfix_design(db_manager, tmp_path):
    """Seed a minimal AutopilotProject + AutopilotDesign row marked
    workflow_type='bugfix', return its id."""
    session = db_manager.get_session()
    project_id = f"proj-{uuid.uuid4().hex[:8]}"
    design_id = f"des-{uuid.uuid4().hex[:8]}"
    session.add(AutopilotProject(id=project_id, name="p", base_dir=str(tmp_path)))
    session.add(
        AutopilotDesign(
            id=design_id,
            project_id=project_id,
            filename="bug.md",
            name="Login crashes on empty password",
            status="pending",
            workflow_type="bugfix",
        )
    )
    session.commit()
    session.close()
    return design_id


def _make_design_entry(design_id, tmp_path, content="# Bug: login crashes\n\nSteps to reproduce..."):
    from src.autopilot.orchestrator.state import DesignEntry

    design_path = tmp_path / "bug.md"
    design_path.write_text(content)
    return DesignEntry(
        path=design_path,
        name="Login crashes on empty password",
        content_hash="abc123",
        db_id=design_id,
    )


class TestRunBugfixSingleFeature:
    def test_creates_one_feature_without_running_phase0_agent(
        self, db_manager, bugfix_design, tmp_path
    ):
        from src.autopilot.orchestrator import run_bugfix_single_feature

        design_entry = _make_design_entry(bugfix_design, tmp_path)

        with patch("src.autopilot.orchestrator.pipeline.run_single_workflow") as mock_run:
            features_json, designs_folder = run_bugfix_single_feature(
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        mock_run.assert_not_called()
        assert features_json is not None
        assert len(features_json["features"]) == 1
        assert designs_folder is not None

        session = db_manager.get_session()
        features = session.query(Feature).filter_by(design_id=bugfix_design).all()
        session.close()
        assert len(features) == 1
        assert features[0].workflow_type == "bugfix"

    def test_scope_is_the_bug_report_content_verbatim(
        self, db_manager, bugfix_design, tmp_path
    ):
        """No decomposition means no separate scope-writing step -- the
        bug report itself IS the scope, passed through unchanged."""
        from src.autopilot.orchestrator import run_bugfix_single_feature

        design_entry = _make_design_entry(
            bugfix_design, tmp_path, content="# Bug: crashes on empty password\n\nSpecific repro steps."
        )

        features_json, designs_folder = run_bugfix_single_feature(
            design_entry=design_entry, project_path=tmp_path, logger=MagicMock()
        )

        assert "crashes on empty password" in features_json["features"][0]["scope"]
        scope_path = designs_folder / "features" / features_json["features"][0]["id"] / "scope.md"
        assert scope_path.exists()
        assert "Specific repro steps" in scope_path.read_text()

    def test_feature_key_strips_unsafe_characters(self, db_manager, bugfix_design, tmp_path):
        """A user-typed bug title can contain punctuation that isn't safe
        as a directory name component on every filesystem."""
        from src.autopilot.orchestrator import run_bugfix_single_feature

        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=bugfix_design).first()
        d.name = "Bug: can't submit form!"
        session.commit()
        session.close()

        design_entry = _make_design_entry(bugfix_design, tmp_path)
        design_entry.name = "Bug: can't submit form!"

        features_json, designs_folder = run_bugfix_single_feature(
            design_entry=design_entry, project_path=tmp_path, logger=MagicMock()
        )

        feature_key = features_json["features"][0]["id"]
        assert re.match(r"^[a-z0-9\-_]+$", feature_key)
        assert (designs_folder / "features" / feature_key / "scope.md").exists()

    def test_designs_folder_persisted_onto_design_row(
        self, db_manager, bugfix_design, tmp_path
    ):
        """feature_record_routes.py's content/status/delete endpoints locate
        a feature's scope.md via AutopilotDesign.designs_folder -- without
        persisting it here (run_phase0's own equivalent call does), those
        endpoints can never find a bugfix feature's scope.md."""
        from src.autopilot.orchestrator import run_bugfix_single_feature

        design_entry = _make_design_entry(bugfix_design, tmp_path)

        _, designs_folder = run_bugfix_single_feature(
            design_entry=design_entry, project_path=tmp_path, logger=MagicMock()
        )

        session = db_manager.get_session()
        d = session.query(AutopilotDesign).filter_by(id=bugfix_design).first()
        session.close()
        assert d.designs_folder == str(designs_folder)

    def test_idempotent_when_feature_already_exists(
        self, db_manager, bugfix_design, tmp_path
    ):
        """A restart re-entering here must not create a second Feature row
        for the same design -- matches run_phase0's own Tier 1 guard."""
        from src.autopilot.orchestrator import run_bugfix_single_feature

        session = db_manager.get_session()
        session.add(
            Feature(
                id=f"feat-{uuid.uuid4().hex[:8]}",
                design_id=bugfix_design,
                feature_key="login-crashes",
                name="Login crashes",
                scope="existing scope",
                status="pending",
                workflow_type="bugfix",
            )
        )
        session.commit()
        session.close()

        design_entry = _make_design_entry(bugfix_design, tmp_path)

        features_json, designs_folder = run_bugfix_single_feature(
            design_entry=design_entry, project_path=tmp_path, logger=MagicMock()
        )

        assert features_json["features"][0]["id"] == "login-crashes"
        session = db_manager.get_session()
        features = session.query(Feature).filter_by(design_id=bugfix_design).all()
        d = session.query(AutopilotDesign).filter_by(id=bugfix_design).first()
        session.close()
        assert len(features) == 1
        assert d.designs_folder == str(designs_folder)


class TestRunSingleDesignRoutesByWorkflowType:
    def test_bugfix_design_calls_bypass_not_phase0(self, db_manager, bugfix_design, tmp_path):
        from src.autopilot.orchestrator.pipeline import run_single_design

        design_entry = _make_design_entry(bugfix_design, tmp_path)

        with patch("src.autopilot.orchestrator.pipeline.run_bugfix_single_feature") as mock_bypass, \
             patch("src.autopilot.orchestrator.pipeline.run_phase0") as mock_phase0, \
             patch("src.autopilot.orchestrator.pipeline._relink_features_to_workflows"), \
             patch("src.autopilot.orchestrator.pipeline.run_feature_pipelines", return_value=[]), \
             patch("src.autopilot.orchestrator.pipeline.run_design_aggregate", return_value=(MagicMock(), MagicMock())):
            mock_bypass.return_value = ({"design_name": "x", "features": []}, tmp_path)

            run_single_design(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        mock_bypass.assert_called_once()
        mock_phase0.assert_not_called()

    def test_feature_design_still_calls_phase0(self, db_manager, tmp_path):
        from src.autopilot.orchestrator.pipeline import run_single_design

        session = db_manager.get_session()
        project_id = f"proj-{uuid.uuid4().hex[:8]}"
        design_id = f"des-{uuid.uuid4().hex[:8]}"
        session.add(AutopilotProject(id=project_id, name="p", base_dir=str(tmp_path)))
        session.add(
            AutopilotDesign(
                id=design_id, project_id=project_id, filename="d.md", name="D",
                status="pending", workflow_type="feature",
            )
        )
        session.commit()
        session.close()
        design_entry = _make_design_entry(design_id, tmp_path)

        with patch("src.autopilot.orchestrator.pipeline.run_bugfix_single_feature") as mock_bypass, \
             patch("src.autopilot.orchestrator.pipeline.run_phase0") as mock_phase0, \
             patch("src.autopilot.orchestrator.pipeline._relink_features_to_workflows"), \
             patch("src.autopilot.orchestrator.pipeline.run_feature_pipelines", return_value=[]), \
             patch("src.autopilot.orchestrator.pipeline.run_design_aggregate", return_value=(MagicMock(), MagicMock())):
            mock_phase0.return_value = ({"design_name": "x", "features": []}, tmp_path)

            run_single_design(
                sdk=MagicMock(),
                design_entry=design_entry,
                project_path=tmp_path,
                logger=MagicMock(),
            )

        mock_phase0.assert_called_once()
        mock_bypass.assert_not_called()
