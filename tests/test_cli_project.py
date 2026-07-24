"""Tests for src/cli/commands/project.py -- multi-project concurrency's
CLI-facing pieces: activate's 409-cap handling, the new deactivate
command, and current showing every active project (not just one)."""

from types import SimpleNamespace
from unittest.mock import patch

from src.cli.commands import project as project_cli


def _args(**overrides):
    base = dict(api_base="http://127.0.0.1:8300", json=False, project="proj-a")
    base.update(overrides)
    return SimpleNamespace(**base)


PROJECTS = [
    {"id": "proj-a", "name": "proj-a", "base_dir": "/tmp/a"},
    {"id": "proj-b", "name": "proj-b", "base_dir": "/tmp/b"},
]


class TestActivateProject:
    def test_prints_friendly_error_on_cap_rejection(self, capsys):
        """Regression: activate_project only checked `result is None`
        (backend unreachable) -- a 409 cap-rejection returns
        {"error": 409, "detail": "..."}, not None, so it fell through to
        result['name'] and crashed with KeyError instead of printing the
        rejection message."""
        with patch.object(project_cli, "api_get", return_value=PROJECTS), patch.object(
            project_cli,
            "api_post",
            return_value={"error": 409, "detail": "Max concurrent projects (2) reached: a, b."},
        ):
            rc = project_cli.activate_project(_args(project="proj-a"))

        assert rc == 1
        out = capsys.readouterr().out
        assert "Max concurrent projects (2) reached" in out

    def test_prints_success_on_activation(self, capsys):
        with patch.object(project_cli, "api_get", return_value=PROJECTS), patch.object(
            project_cli,
            "api_post",
            return_value={"id": "proj-a", "name": "proj-a", "base_dir": "/tmp/a", "is_active": True},
        ):
            rc = project_cli.activate_project(_args(project="proj-a"))

        assert rc == 0
        assert "Activated: proj-a" in capsys.readouterr().out


class TestDeactivateProject:
    def test_deactivates_and_prints_confirmation(self, capsys):
        with patch.object(project_cli, "api_get", return_value=PROJECTS), patch.object(
            project_cli,
            "api_post",
            return_value={"id": "proj-a", "name": "proj-a", "base_dir": "/tmp/a", "is_active": False},
        ):
            rc = project_cli.deactivate_project(_args(project="proj-a"))

        assert rc == 0
        assert "Deactivated: proj-a" in capsys.readouterr().out

    def test_prints_friendly_error_on_backend_error(self, capsys):
        with patch.object(project_cli, "api_get", return_value=PROJECTS), patch.object(
            project_cli, "api_post", return_value={"error": 404, "detail": "Project not found"}
        ):
            rc = project_cli.deactivate_project(_args(project="proj-a"))

        assert rc == 1
        assert "Project not found" in capsys.readouterr().out


class TestCurrentProject:
    def test_shows_every_active_project_not_just_one(self, capsys):
        """Regression: GET /api/projects/active used to return a single
        project (Optional[ProjectItem] via .first()) -- with more than one
        project active at once, this silently hid every project but one."""
        with patch.object(
            project_cli,
            "api_get",
            return_value=[
                {"id": "proj-a", "name": "proj-a", "base_dir": "/tmp/a"},
                {"id": "proj-b", "name": "proj-b", "base_dir": "/tmp/b"},
            ],
        ):
            rc = project_cli.current_project(_args())

        assert rc == 0
        out = capsys.readouterr().out
        assert "proj-a" in out
        assert "proj-b" in out

    def test_no_active_projects(self, capsys):
        with patch.object(project_cli, "api_get", return_value=[]):
            rc = project_cli.current_project(_args())

        assert rc == 0
        assert "No active project" in capsys.readouterr().out
