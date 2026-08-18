"""Tests for src/cli/commands/autopilot.py -- multi-project concurrency's
CLI-facing pieces: `heph autopilot stop`/`status` no longer blindly
operate on "whatever's running" without a way to scope to one project.

Regression context: POST /api/autopilot/stop with no project_id tells the
backend to stop EVERY currently running project (there's no single global
service to fall back to now that projects run concurrently) -- the CLI's
`stop`/Ctrl+C-during-`start` paths called it with no project_id at all,
so stopping "your" pipeline could silently kill an unrelated project's.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.cli.commands import autopilot as autopilot_cli


def _args(**overrides):
    base = {"project_path": None, "json": False}
    base.update(overrides)
    return SimpleNamespace(**base)


PROJECTS = [
    {"id": "proj-a", "name": "proj-a", "base_dir": "/tmp/proj-a"},
    {"id": "proj-b", "name": "proj-b", "base_dir": "/tmp/proj-b"},
]


class TestResolveProjectIdByPath:
    def test_finds_matching_project(self):
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: PROJECTS)):
            assert autopilot_cli._resolve_project_id_by_path("/tmp/proj-b") == "proj-b"

    def test_returns_none_when_no_match(self):
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: PROJECTS)):
            assert autopilot_cli._resolve_project_id_by_path("/tmp/proj-c") is None

    def test_returns_none_when_backend_unreachable(self):
        with patch("requests.get", side_effect=Exception("connection refused")):
            assert autopilot_cli._resolve_project_id_by_path("/tmp/proj-a") is None


class TestStopPipeline:
    def test_no_project_path_stops_everything(self):
        """Default (no --project-path) preserves the documented /stop
        behavior of stopping every running project -- not a regression to
        fix, just must stay explicit rather than accidental."""
        mock_post = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"agents_terminated": 3}))
        with patch("requests.post", mock_post):
            rc = autopilot_cli.stop_pipeline(_args())

        assert rc == 0
        _, kwargs = mock_post.call_args
        assert "project_id" not in kwargs["params"]

    def test_project_path_scopes_stop_to_resolved_id(self):
        mock_post = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"agents_terminated": 1}))
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: PROJECTS)), \
             patch("requests.post", mock_post):
            rc = autopilot_cli.stop_pipeline(_args(project_path="/tmp/proj-a"))

        assert rc == 0
        _, kwargs = mock_post.call_args
        assert kwargs["params"]["project_id"] == "proj-a"

    def test_unresolvable_project_path_errors_without_calling_stop(self):
        mock_post = MagicMock()
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: PROJECTS)), \
             patch("requests.post", mock_post):
            rc = autopilot_cli.stop_pipeline(_args(project_path="/tmp/unknown"))

        assert rc == 1
        mock_post.assert_not_called()

    def test_uses_query_params_not_json_body(self):
        """/stop's clear_state/project_id are bare scalar FastAPI params,
        bound from the query string -- sending them as a JSON body (the
        pre-existing bug this call site had) is silently ignored server-side."""
        mock_post = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"agents_terminated": 0}))
        with patch("requests.post", mock_post):
            autopilot_cli.stop_pipeline(_args())

        _, kwargs = mock_post.call_args
        assert kwargs.get("json") is None
        assert "params" in kwargs


class TestPipelineStatus:
    def test_no_project_path_omits_project_id_param(self):
        mock_get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"running": False}))
        with patch("requests.get", mock_get):
            rc = autopilot_cli.pipeline_status(_args())

        assert rc == 0
        _, kwargs = mock_get.call_args
        assert kwargs["params"] == {}

    def test_project_path_scopes_status_to_resolved_id(self):
        def fake_get(url, **kwargs):
            if url.endswith("/api/autopilot/projects"):
                return MagicMock(status_code=200, json=lambda: PROJECTS)
            return MagicMock(status_code=200, json=lambda: {"running": True})

        with patch("requests.get", side_effect=fake_get):
            rc = autopilot_cli.pipeline_status(_args(project_path="/tmp/proj-a"))

        assert rc == 0


class TestPrintPipelineStatus:
    def test_shows_every_running_project(self, capsys):
        autopilot_cli._print_pipeline_status({
            "running": True,
            "running_projects": [
                {"id": "proj-a", "name": "proj-a", "base_dir": "/tmp/proj-a"},
                {"id": "proj-b", "name": "proj-b", "base_dir": "/tmp/proj-b"},
            ],
        })
        out = capsys.readouterr().out
        assert "proj-a" in out
        assert "proj-b" in out

    def test_no_running_projects_section_when_absent(self, capsys):
        autopilot_cli._print_pipeline_status({"running": False})
        out = capsys.readouterr().out
        assert "Running projects" not in out
