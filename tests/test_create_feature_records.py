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

    def test_copies_parent_design_workflow_type_onto_feature_row(self, designs_folder, mock_logger):
        """Regression: _run_one_feature picks its workflow definition_id
        (autopilot vs bugfix) off Feature.workflow_type, so a decomposed
        feature must inherit its parent AutopilotDesign's workflow_type --
        not the column default -- or every bugfix-typed design would
        silently launch the full feature pipeline anyway."""
        from src.core.database import AutopilotDesign, Feature, get_db

        design_id = "des-bugfix123"
        with get_db() as db:
            db.add(
                AutopilotDesign(
                    id=design_id,
                    project_id="proj-1",
                    filename="fix_login.md",
                    name="Fix login crash",
                    workflow_type="bugfix",
                )
            )

        features_json = {
            "design_name": "Fix login crash",
            "features": [
                {"id": "login-fix", "name": "Login Fix", "scope": "Fix it", "files": [], "depends_on": [], "execution": "parallel"}
            ],
        }

        records = _create_feature_records(design_id, features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.workflow_type == "bugfix"

    def test_defaults_to_feature_when_parent_design_missing(self, designs_folder, mock_logger):
        """No AutopilotDesign row for design_id (e.g. tests above that pass
        a bare string) must fall back to "feature", not raise."""
        from src.core.database import Feature, get_db

        design_id = "des-does-not-exist"
        features_json = {
            "design_name": "Test Design",
            "features": [
                {"id": "auth", "name": "Auth", "scope": "s", "files": [], "depends_on": [], "execution": "parallel"}
            ],
        }

        records = _create_feature_records(design_id, features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.workflow_type == "feature"


class TestCreateFeatureRecordsRepoId:
    """Feature.repo_id set at creation time -- REQ-19's component."""

    def _seed_multi_repo_project(self, tmp_path, monkeypatch, design_id="des-multi"):
        from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager, ProjectRepo

        db_path = str(tmp_path / "repo_test.db")
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
        db = DatabaseManager(db_path)
        db.create_tables()

        backend = tmp_path / "backend"
        frontend = tmp_path / "frontend"
        backend.mkdir()
        frontend.mkdir()

        with db.session_scope() as session:
            session.add(AutopilotProject(id="proj-multi", name="p", base_dir=str(tmp_path)))
            session.add(ProjectRepo(id="repo-backend", project_id="proj-multi", label="backend", path=str(backend), is_primary=True))
            session.add(ProjectRepo(id="repo-frontend", project_id="proj-multi", label="frontend", path=str(frontend)))
            session.add(AutopilotDesign(id=design_id, project_id="proj-multi", filename="d.md", name="d"))
        return backend, frontend

    def test_explicit_repo_label_wins(self, designs_folder, mock_logger, tmp_path, monkeypatch):
        from src.core.database import Feature, get_db

        self._seed_multi_repo_project(tmp_path, monkeypatch)
        features_json = {
            "design_name": "d",
            "features": [
                {"id": "fe-feat", "name": "FE", "scope": "s", "files": [], "repo_label": "frontend", "depends_on": [], "execution": "parallel"}
            ],
        }

        records = _create_feature_records("des-multi", features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.repo_id == "repo-frontend"

    def test_inferred_from_files_majority(self, designs_folder, mock_logger, tmp_path, monkeypatch):
        from src.core.database import Feature, get_db

        backend, _ = self._seed_multi_repo_project(tmp_path, monkeypatch)
        features_json = {
            "design_name": "d",
            "features": [
                {
                    "id": "be-feat",
                    "name": "BE",
                    "scope": "s",
                    "files": [str(backend / "a.py"), str(backend / "b.py")],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }

        records = _create_feature_records("des-multi", features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.repo_id == "repo-backend"

    def test_inferred_from_relative_files_against_project_base_dir(self, designs_folder, mock_logger, tmp_path, monkeypatch):
        """The architect prompt's schema shows files like "src/auth/" --
        relative to the project root, not absolute. repo_id_for_path needs
        an absolute path to prefix-match a ProjectRepo.path, so relative
        entries must be resolved against the project's base_dir first (not
        left to resolve() against the process's own cwd, which would
        silently never match any repo)."""
        from src.core.database import Feature, get_db

        self._seed_multi_repo_project(tmp_path, monkeypatch)
        features_json = {
            "design_name": "d",
            "features": [
                {
                    "id": "be-feat-rel",
                    "name": "BE",
                    "scope": "s",
                    "files": ["backend/a.py", "backend/sub/b.py"],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }

        records = _create_feature_records("des-multi", features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.repo_id == "repo-backend"

    def test_no_majority_leaves_repo_id_none(self, designs_folder, mock_logger, tmp_path, monkeypatch):
        from src.core.database import Feature, get_db

        self._seed_multi_repo_project(tmp_path, monkeypatch)
        features_json = {
            "design_name": "d",
            "features": [
                {"id": "no-files-feat", "name": "N", "scope": "s", "files": [], "depends_on": [], "execution": "parallel"}
            ],
        }

        records = _create_feature_records("des-multi", features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.repo_id is None

    def test_files_spanning_two_repos_leaves_repo_id_none_and_warns(self, designs_folder, mock_logger, tmp_path, monkeypatch):
        """A feature whose files genuinely span two repos (the architect
        prompt's own forbidden case) must not silently majority-vote one
        repo and drop the other's files from scope -- leave repo_id
        unresolved and log loudly instead."""
        from src.core.database import Feature, get_db

        backend, frontend = self._seed_multi_repo_project(tmp_path, monkeypatch)
        features_json = {
            "design_name": "d",
            "features": [
                {
                    "id": "cross-repo-feat",
                    "name": "Cross",
                    "scope": "s",
                    "files": [str(backend / "api.py"), str(frontend / "api.py")],
                    "depends_on": [],
                    "execution": "parallel",
                }
            ],
        }

        records = _create_feature_records("des-multi", features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.repo_id is None

        assert any(
            "REPO-SCOPE" in str(call.args[0]) and "cross-repo-feat" in str(call.args[0])
            for call in mock_logger.warning.call_args_list
        )

    def test_single_repo_project_repo_id_always_none(self, designs_folder, mock_logger, tmp_path, monkeypatch):
        """Only one ProjectRepo row -- this whole component is a no-op,
        regression test asserts byte-identical Task/Feature creation flow
        to pre-migration."""
        from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager, Feature, ProjectRepo, get_db

        db_path = str(tmp_path / "single_repo.db")
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
        db = DatabaseManager(db_path)
        db.create_tables()
        with db.session_scope() as session:
            session.add(AutopilotProject(id="proj-single", name="p", base_dir=str(tmp_path)))
            session.add(ProjectRepo(id="repo-single", project_id="proj-single", label="primary", path=str(tmp_path), is_primary=True))
            session.add(AutopilotDesign(id="des-single", project_id="proj-single", filename="d.md", name="d"))

        features_json = {
            "design_name": "d",
            "features": [
                {"id": "f1", "name": "F1", "scope": "s", "files": [str(tmp_path / "a.py")], "depends_on": [], "execution": "parallel"}
            ],
        }

        records = _create_feature_records("des-single", features_json, designs_folder, mock_logger)

        with get_db() as db:
            feat = db.query(Feature).filter_by(id=records[0]["id"]).first()
            assert feat.repo_id is None
