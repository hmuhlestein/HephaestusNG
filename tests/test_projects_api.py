"""Tests for project_routes.py -- is_active cap enforcement.

Part of the multi-project concurrency fix: AutopilotProject.is_active is no
longer exclusive (clear-all-others-then-set-one) -- it's capped at
max_concurrent_projects, matching AutopilotServiceRegistry.can_start's own
cap-and-reject convention (src/autopilot/service.py) instead of silently
evicting whoever was active before.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import AutopilotProject


@pytest.fixture
def client(db_manager, monkeypatch, tmp_path):
    from src.mcp.autopilot import project_routes

    monkeypatch.setattr(
        project_routes, "_apply_active_project", lambda proj: None
    )

    app = FastAPI()
    app.include_router(project_routes.router)
    return TestClient(app)


def _make_project(db_manager, tmp_path, id_, is_active=False):
    d = tmp_path / id_
    d.mkdir()
    (d / ".git").mkdir()
    with db_manager.session_scope() as session:
        session.add(
            AutopilotProject(
                id=id_, name=id_, base_dir=str(d), is_active=is_active
            )
        )
    return str(d)


def _mock_config(max_concurrent=2):
    config = MagicMock()
    config.max_concurrent_projects = max_concurrent
    return config


class TestActivateCap:
    def test_rejects_activation_at_cap(self, client, db_manager, tmp_path):
        _make_project(db_manager, tmp_path, "proj-a", is_active=True)
        _make_project(db_manager, tmp_path, "proj-b", is_active=True)
        _make_project(db_manager, tmp_path, "proj-c", is_active=False)

        with patch(
            "src.core.simple_config.get_config", return_value=_mock_config(2)
        ):
            resp = client.post("/projects/proj-c/activate")

        assert resp.status_code == 409
        assert "Max concurrent projects (2) reached" in resp.json()["detail"]

    def test_allows_activation_under_cap(self, client, db_manager, tmp_path):
        _make_project(db_manager, tmp_path, "proj-a", is_active=True)
        _make_project(db_manager, tmp_path, "proj-b", is_active=False)

        with patch(
            "src.core.simple_config.get_config", return_value=_mock_config(2)
        ):
            resp = client.post("/projects/proj-b/activate")

        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

        # The previously-active project must be untouched -- no clear-all.
        with db_manager.session_scope() as session:
            proj_a = (
                session.query(AutopilotProject).filter_by(id="proj-a").first()
            )
            assert proj_a.is_active is True

    def test_reactivating_already_active_project_is_a_noop_not_a_409(
        self, client, db_manager, tmp_path
    ):
        """Mirrors AutopilotServiceRegistry.can_start's exemption: a
        project already occupying a slot doesn't count as a NEW one when
        re-activated."""
        _make_project(db_manager, tmp_path, "proj-a", is_active=True)
        _make_project(db_manager, tmp_path, "proj-b", is_active=True)

        with patch(
            "src.core.simple_config.get_config", return_value=_mock_config(2)
        ):
            resp = client.post("/projects/proj-a/activate")

        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    def test_deactivate_frees_a_slot(self, client, db_manager, tmp_path):
        _make_project(db_manager, tmp_path, "proj-a", is_active=True)
        _make_project(db_manager, tmp_path, "proj-b", is_active=True)
        _make_project(db_manager, tmp_path, "proj-c", is_active=False)

        with patch(
            "src.core.simple_config.get_config", return_value=_mock_config(2)
        ):
            deactivate_resp = client.post("/projects/proj-a/deactivate")
            assert deactivate_resp.status_code == 200
            assert deactivate_resp.json()["is_active"] is False

            resp = client.post("/projects/proj-c/activate")

        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    def test_activate_returns_404_for_unknown_project(self, client):
        resp = client.post("/projects/does-not-exist/activate")
        assert resp.status_code == 404

    def test_deactivate_returns_404_for_unknown_project(self, client):
        resp = client.post("/projects/does-not-exist/deactivate")
        assert resp.status_code == 404


class TestGetActiveProjects:
    """Regression: GET /active used to return Optional[ProjectItem] (a
    single project via .first()) -- with more than one project active at
    once, that silently hid every project but one."""

    def test_returns_every_active_project(self, client, db_manager, tmp_path):
        _make_project(db_manager, tmp_path, "proj-a", is_active=True)
        _make_project(db_manager, tmp_path, "proj-b", is_active=True)
        _make_project(db_manager, tmp_path, "proj-c", is_active=False)

        resp = client.get("/projects/active")

        assert resp.status_code == 200
        ids = {p["id"] for p in resp.json()}
        assert ids == {"proj-a", "proj-b"}

    def test_empty_list_when_none_active(self, client, db_manager, tmp_path):
        _make_project(db_manager, tmp_path, "proj-a", is_active=False)

        resp = client.get("/projects/active")

        assert resp.status_code == 200
        assert resp.json() == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
