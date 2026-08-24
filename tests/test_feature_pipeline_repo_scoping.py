"""Tests for C3: Per-Feature Worktree Path Resolution."""

from pathlib import Path
from unittest.mock import MagicMock, patch

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


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine) as s:
        yield s


def _seed_multi_repo_project(session):
    """Create a project with two repos and a design with two features."""
    project = AutopilotProject(id="proj-mr", name="multi-repo", base_dir="/workspace")
    session.add(project)
    session.flush()

    repo_main = ProjectRepo(
        id="repo-main",
        project_id="proj-mr",
        label="main",
        path="/workspace",
        is_primary=True,
    )
    repo_backend = ProjectRepo(
        id="repo-backend",
        project_id="proj-mr",
        label="backend",
        path="/code/backend",
        is_primary=False,
    )
    session.add_all([repo_main, repo_backend])
    session.flush()

    design = AutopilotDesign(
        id="des-1",
        project_id="proj-mr",
        filename="design.md",
        name="design",
        ordinal=0,
        size_bytes=100,
    )
    session.add(design)
    session.flush()

    feat_backend = Feature(
        id="feat-be",
        design_id="des-1",
        feature_key="backend-api",
        name="Backend API",
        scope="backend",
        repo_id="repo-backend",
    )
    feat_frontend = Feature(
        id="feat-fe",
        design_id="des-1",
        feature_key="frontend-ui",
        name="Frontend UI",
        scope="frontend",
        repo_id=None,
    )
    session.add_all([feat_backend, feat_frontend])
    session.flush()

    return project, design, repo_main, repo_backend


class TestResolveFeatureProjectPath:
    def test_feature_with_repo_id_resolves_to_that_repo(self, engine):
        """REQ-07/08: Feature with repo_id resolves to that repo's path."""
        from src.autopilot.orchestrator.pipeline import _resolve_feature_project_path

        with Session(engine) as session:
            _seed_multi_repo_project(session)
            session.commit()

        logger = MagicMock()
        with patch("src.core.database.get_db") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=Session(engine))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolve_feature_project_path(Path("/workspace"), "proj-mr", "des-1", "backend-api", logger)
        assert result == Path("/code/backend")

    def test_feature_with_none_repo_id_falls_back(self, engine):
        """REQ-06: Feature with repo_id=None falls back to design_project_path."""
        from src.autopilot.orchestrator.pipeline import _resolve_feature_project_path

        with Session(engine) as session:
            _seed_multi_repo_project(session)
            session.commit()

        logger = MagicMock()
        with patch("src.core.database.get_db") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=Session(engine))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolve_feature_project_path(Path("/workspace"), "proj-mr", "des-1", "frontend-ui", logger)
        assert result == Path("/workspace")

    def test_none_project_id_short_circuits(self, engine):
        """project_id=None returns design_project_path without DB query."""
        from src.autopilot.orchestrator.pipeline import _resolve_feature_project_path

        logger = MagicMock()
        result = _resolve_feature_project_path(Path("/workspace"), None, "des-1", "backend-api", logger)
        assert result == Path("/workspace")

    def test_none_design_id_short_circuits(self, engine):
        """design_id=None returns design_project_path without DB query."""
        from src.autopilot.orchestrator.pipeline import _resolve_feature_project_path

        logger = MagicMock()
        result = _resolve_feature_project_path(Path("/workspace"), "proj-mr", None, "backend-api", logger)
        assert result == Path("/workspace")

    def test_missing_feature_logs_warning_and_falls_back(self, engine):
        """Feature row not found → log warning, return design_project_path."""
        from src.autopilot.orchestrator.pipeline import _resolve_feature_project_path

        with Session(engine) as session:
            _seed_multi_repo_project(session)
            session.commit()

        logger = MagicMock()
        with patch("src.core.database.get_db") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=Session(engine))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            result = _resolve_feature_project_path(Path("/workspace"), "proj-mr", "des-1", "nonexistent", logger)
        assert result == Path("/workspace")
        logger.warning.assert_called()

    def test_two_features_different_repos_no_crosstalk(self, engine):
        """Two features bound to different repos each get their own path."""
        from src.autopilot.orchestrator.pipeline import _resolve_feature_project_path

        with Session(engine) as session:
            _seed_multi_repo_project(session)
            session.commit()

        logger = MagicMock()
        with patch("src.core.database.get_db") as mock_db:
            mock_db.return_value.__enter__ = MagicMock(return_value=Session(engine))
            mock_db.return_value.__exit__ = MagicMock(return_value=False)
            path_be = _resolve_feature_project_path(Path("/workspace"), "proj-mr", "des-1", "backend-api", logger)
            path_fe = _resolve_feature_project_path(Path("/workspace"), "proj-mr", "des-1", "frontend-ui", logger)
        assert path_be == Path("/code/backend")
        assert path_fe == Path("/workspace")  # primary repo fallback


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
