"""Tests for _create_feature_records function."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.autopilot.orchestrator.features import _create_feature_records


@pytest.fixture(autouse=True)
def test_db(tmp_path, monkeypatch):
    """Set up test database for all tests."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    from src.core.database import DatabaseManager
    db = DatabaseManager(db_path)
    db.create_tables()
    yield db


@pytest.fixture
def mock_logger():
    """Create a mock logger."""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    return logger


@pytest.fixture
def designs_folder(tmp_path):
    """Create a temporary designs folder."""
    folder = tmp_path / "designs" / "test_design"
    folder.mkdir(parents=True)
    return folder


class TestCreateFeatureRecords:
    """Test cases for _create_feature_records."""

    def test_single_feature(self, designs_folder, mock_logger):
        """Test creating a single feature record."""
        design_id = "des-test123"
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Authentication",
                    "scope": "User authentication system",
                    "files": ["src/auth/"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }

        records = _create_feature_records(
            design_id, features_json, designs_folder, mock_logger
        )

        assert len(records) == 1
        assert records[0]["feature_key"] == "auth"
        assert records[0]["name"] == "Authentication"
        assert records[0]["scope"] == "User authentication system"
        assert records[0]["files"] == ["src/auth/"]
        assert records[0]["depends_on"] == []
        assert records[0]["execution"] == "parallel"
        assert "id" in records[0]
        assert records[0]["id"].startswith("feat-")

    def test_multiple_features(self, designs_folder, mock_logger):
        """Test creating multiple feature records."""
        design_id = "des-test123"
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Authentication",
                    "scope": "User authentication",
                    "files": ["src/auth/"],
                    "depends_on": [],
                    "execution": "parallel",
                },
                {
                    "id": "api",
                    "name": "API Gateway",
                    "scope": "API gateway",
                    "files": ["src/api/"],
                    "depends_on": ["auth"],
                    "execution": "parallel",
                },
            ],
        }

        records = _create_feature_records(
            design_id, features_json, designs_folder, mock_logger
        )

        assert len(records) == 2
        assert records[0]["feature_key"] == "auth"
        assert records[1]["feature_key"] == "api"
        assert records[1]["depends_on"] == ["auth"]

    def test_feature_record_path_created(self, designs_folder, mock_logger):
        """Test that feature record path is created."""
        design_id = "des-test123"
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "Scope",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }

        records = _create_feature_records(
            design_id, features_json, designs_folder, mock_logger
        )

        feature_record_path = Path(records[0]["feature_record_path"])
        assert feature_record_path.exists()
        assert feature_record_path.is_dir()

    def test_scope_doc_path_set(self, designs_folder, mock_logger):
        """Test that scope_doc_path is set correctly."""
        design_id = "des-test123"
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "auth",
                    "name": "Auth",
                    "scope": "Scope",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }

        # Create the scope.md file so scope_doc_path gets set
        scope_doc = designs_folder / "features" / "auth" / "scope.md"
        scope_doc.parent.mkdir(parents=True, exist_ok=True)
        scope_doc.write_text("# Auth Scope")

        records = _create_feature_records(
            design_id, features_json, designs_folder, mock_logger
        )

        assert records[0]["scope_doc_path"] is not None
        scope_doc_path = Path(records[0]["scope_doc_path"])
        assert scope_doc_path.name == "scope.md"
        assert "auth" in str(scope_doc_path)

    def test_empty_features(self, designs_folder, mock_logger):
        """Test with empty features list."""
        design_id = "des-test123"
        features_json = {
            "design_name": "Test Design",
            "features": [],
        }

        records = _create_feature_records(
            design_id, features_json, designs_folder, mock_logger
        )

        assert len(records) == 0


class TestCreateFeatureRecordsRepoResolution:
    """REQ-19: architecture.md's Flow 1 documents each feature's stated
    repo LABEL (features.json's new optional "repo" field) resolving to
    Feature.repo_id via the design's project's ProjectRepo rows -- this
    was the missing half of REQ-19: the prompt could instruct the
    architect perfectly and Feature.repo_id would still never get set,
    since nothing read the label back out of features.json."""

    def _seed_project_and_repos(self, db_manager, project_id="proj-1", design_id="des-1"):
        from src.core.database import AutopilotDesign, AutopilotProject, ProjectRepo

        with db_manager.session_scope() as session:
            session.add(AutopilotProject(id=project_id, name=project_id, base_dir="/tmp/proj-1"))
            session.add(
                ProjectRepo(id="repo-backend", project_id=project_id, label="backend", path="/tmp/proj-1/backend", is_primary=True)
            )
            session.add(
                ProjectRepo(id="repo-frontend", project_id=project_id, label="frontend", path="/tmp/proj-1/frontend", is_primary=False)
            )
            session.add(
                AutopilotDesign(
                    id=design_id, project_id=project_id,
                    filename="design.md", name="Test Design",
                )
            )

    def test_resolves_repo_label_to_repo_id(self, test_db, designs_folder, mock_logger):
        self._seed_project_and_repos(test_db)
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "api",
                    "name": "API",
                    "scope": "Backend API",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                    "repo": "backend",
                },
                {
                    "id": "ui",
                    "name": "UI",
                    "scope": "Frontend UI",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                    "repo": "frontend",
                },
            ],
        }

        records = _create_feature_records(
            "des-1", features_json, designs_folder, mock_logger
        )

        by_key = {r["feature_key"]: r for r in records}
        assert by_key["api"]["repo_id"] == "repo-backend"
        assert by_key["ui"]["repo_id"] == "repo-frontend"

    def test_unresolvable_label_leaves_repo_id_none_and_warns(self, test_db, designs_folder, mock_logger):
        self._seed_project_and_repos(test_db)
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "api",
                    "name": "API",
                    "scope": "Backend API",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                    "repo": "does-not-exist",
                },
            ],
        }

        records = _create_feature_records(
            "des-1", features_json, designs_folder, mock_logger
        )

        assert records[0]["repo_id"] is None
        mock_logger.warning.assert_called_once()

    def test_no_repo_field_leaves_repo_id_none(self, test_db, designs_folder, mock_logger):
        self._seed_project_and_repos(test_db)
        features_json = {
            "design_name": "Test Design",
            "features": [
                {
                    "id": "api",
                    "name": "API",
                    "scope": "Backend API",
                    "files": [],
                    "depends_on": [],
                    "execution": "parallel",
                },
            ],
        }

        records = _create_feature_records(
            "des-1", features_json, designs_folder, mock_logger
        )

        assert records[0]["repo_id"] is None
        mock_logger.warning.assert_not_called()
