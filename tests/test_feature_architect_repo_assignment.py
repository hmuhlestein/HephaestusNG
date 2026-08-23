"""Tests for C9: Feature Architect Repo Assignment."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    Base,
    Feature,
    ProjectRepo,
)
from src.autopilot.orchestrator.features import _validate_features_json


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


class TestValidateFeaturesJson:
    def test_repo_field_string_passes(self):
        """C9: repo field as string passes validation."""
        features = {
            "design_name": "test",
            "features": [
                {"id": "f1", "name": "F1", "scope": "s", "repo": "backend"},
            ],
        }
        _validate_features_json(features)  # should not raise

    def test_repo_field_non_string_raises(self):
        """C9: repo field as non-string raises ValueError."""
        features = {
            "design_name": "test",
            "features": [
                {"id": "f1", "name": "F1", "scope": "s", "repo": 123},
            ],
        }
        with pytest.raises(ValueError, match="repo must be a string"):
            _validate_features_json(features)

    def test_repo_field_absent_passes(self):
        """C9: no repo field passes (single-repo or architect omitted it)."""
        features = {
            "design_name": "test",
            "features": [
                {"id": "f1", "name": "F1", "scope": "s"},
            ],
        }
        _validate_features_json(features)  # should not raise


class TestCreateFeatureRecordsRepoResolution:
    def test_repo_label_resolves_to_repo_id(self, engine):
        """C9: features.json 'repo' label resolves to Feature.repo_id."""
        from unittest.mock import MagicMock
        from pathlib import Path
        from src.autopilot.orchestrator.features import _create_feature_records

        with Session(engine) as session:
            proj = AutopilotProject(id="p1", name="p", base_dir="/tmp")
            session.add(proj)
            session.flush()

            repo = ProjectRepo(
                id="repo-be", project_id="p1", label="backend",
                path="/code/backend", is_primary=True,
            )
            session.add(repo)
            session.flush()

            design = AutopilotDesign(
                id="des-1", project_id="p1", filename="d.md",
                name="d", ordinal=0, size_bytes=100,
            )
            session.add(design)
            session.commit()

        features_json = {
            "design_name": "test",
            "features": [
                {"id": "api", "name": "API", "scope": "s", "repo": "backend"},
            ],
        }

        logger = MagicMock()
        with Session(engine) as session:
            # Mock get_db to return our session
            from unittest.mock import patch
            with patch("src.autopilot.orchestrator.features.get_db") as mock_db:
                mock_db.return_value.__enter__ = MagicMock(return_value=session)
                mock_db.return_value.__exit__ = MagicMock(return_value=False)
                records = _create_feature_records(
                    "des-1", features_json, Path("/tmp/des"), logger
                )

        # Verify the Feature row has repo_id set
        with Session(engine) as session:
            feat = session.query(Feature).filter_by(feature_key="api").first()
            assert feat is not None
            assert feat.repo_id == "repo-be"

    def test_unknown_repo_label_warns_and_leaves_none(self, engine):
        """C9: unknown repo label logs warning, leaves repo_id=None."""
        from unittest.mock import MagicMock
        from pathlib import Path
        from src.autopilot.orchestrator.features import _create_feature_records

        with Session(engine) as session:
            proj = AutopilotProject(id="p1", name="p", base_dir="/tmp")
            session.add(proj)
            session.flush()

            repo = ProjectRepo(
                id="repo-be", project_id="p1", label="backend",
                path="/code/backend", is_primary=True,
            )
            session.add(repo)
            session.flush()

            design = AutopilotDesign(
                id="des-2", project_id="p1", filename="d.md",
                name="d", ordinal=0, size_bytes=100,
            )
            session.add(design)
            session.commit()

        features_json = {
            "design_name": "test",
            "features": [
                {"id": "api", "name": "API", "scope": "s", "repo": "nonexistent"},
            ],
        }

        logger = MagicMock()
        with Session(engine) as session:
            from unittest.mock import patch
            with patch("src.autopilot.orchestrator.features.get_db") as mock_db:
                mock_db.return_value.__enter__ = MagicMock(return_value=session)
                mock_db.return_value.__exit__ = MagicMock(return_value=False)
                records = _create_feature_records(
                    "des-2", features_json, Path("/tmp/des"), logger
                )

        logger.warning.assert_called()
        assert "unknown repo label" in str(logger.warning.call_args)

        with Session(engine) as session:
            feat = session.query(Feature).filter_by(feature_key="api").first()
            assert feat.repo_id is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
