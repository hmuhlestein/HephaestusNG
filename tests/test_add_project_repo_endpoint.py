"""Tests for POST /projects/{project_id}/repos (add_project_repo) --
adversarial review WARNINGs: label content validation, and specific 409
errors that distinguish a path conflict from a label conflict instead of
mentioning both regardless of which one actually collided.
"""

import git
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import AutopilotProject, ProjectRepo


@pytest.fixture
def client():
    from src.mcp.autopilot import project_routes

    app = FastAPI()
    app.include_router(project_routes.router)
    return TestClient(app)


def _make_project_with_repo(db_manager, tmp_path, project_id="proj-a"):
    d = tmp_path / project_id
    d.mkdir()
    git.Repo.init(str(d))
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id=project_id, name=project_id, base_dir=str(d)))
        session.add(
            ProjectRepo(
                id=f"{project_id}-primary",
                project_id=project_id,
                label="main",
                path=str(d),
                is_primary=True,
            )
        )
    return str(d)


class TestAddProjectRepoLabelValidation:
    def test_rejects_empty_label(self, client, db_manager, tmp_path):
        _make_project_with_repo(db_manager, tmp_path, "proj-a")
        other = tmp_path / "other-repo"
        other.mkdir()
        git.Repo.init(str(other))

        resp = client.post(
            "/projects/proj-a/repos", json={"label": "", "path": str(other)}
        )

        assert resp.status_code == 400
        assert "label" in resp.json()["detail"].lower()

    def test_rejects_whitespace_only_label(self, client, db_manager, tmp_path):
        _make_project_with_repo(db_manager, tmp_path, "proj-a")
        other = tmp_path / "other-repo"
        other.mkdir()
        git.Repo.init(str(other))

        resp = client.post(
            "/projects/proj-a/repos", json={"label": "   ", "path": str(other)}
        )

        assert resp.status_code == 400
        assert "label" in resp.json()["detail"].lower()


class TestAddProjectRepo409Specificity:
    def test_duplicate_path_reports_path_not_label(self, client, db_manager, tmp_path):
        base_dir = _make_project_with_repo(db_manager, tmp_path, "proj-a")

        resp = client.post(
            "/projects/proj-a/repos",
            json={"label": "a-different-label", "path": base_dir},
        )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "path" in detail.lower()
        assert base_dir in detail
        assert "a-different-label" not in detail

    def test_duplicate_label_reports_label_not_path(self, client, db_manager, tmp_path):
        _make_project_with_repo(db_manager, tmp_path, "proj-a")
        other = tmp_path / "other-repo"
        other.mkdir()
        git.Repo.init(str(other))

        resp = client.post(
            "/projects/proj-a/repos", json={"label": "main", "path": str(other)}
        )

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "label" in detail.lower()
        assert "main" in detail
        assert str(other) not in detail
