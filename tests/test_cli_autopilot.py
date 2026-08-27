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


# cli/main.py builds this as f"http://{args.host}:{args.port}" before
# dispatching, so every command function can rely on it being present.
API_BASE = "http://127.0.0.1:9999"


def _args(**overrides):
    base = {"project_path": None, "json": False, "api_base": API_BASE}
    base.update(overrides)
    return SimpleNamespace(**base)


PROJECTS = [
    {"id": "proj-a", "name": "proj-a", "base_dir": "/tmp/proj-a"},
    {"id": "proj-b", "name": "proj-b", "base_dir": "/tmp/proj-b"},
]


class TestResolveProjectIdByPath:
    def test_finds_matching_project(self):
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: PROJECTS)):
            assert autopilot_cli._resolve_project_id_by_path("/tmp/proj-b", API_BASE) == "proj-b"

    def test_returns_none_when_no_match(self):
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: PROJECTS)):
            assert autopilot_cli._resolve_project_id_by_path("/tmp/proj-c", API_BASE) is None

    def test_returns_none_when_backend_unreachable(self):
        with patch("requests.get", side_effect=Exception("connection refused")):
            assert autopilot_cli._resolve_project_id_by_path("/tmp/proj-a", API_BASE) is None


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


def _start_args(project_path, **overrides):
    base = {
        "project_path": str(project_path),
        "design_queue": None,
        "max_iterations": 3,
        "drop_db": False,
        "feature": None,
        "repo": None,
        "api_base": API_BASE,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _git_project(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


class TestStartPipelineSpeckitForwarding:
    """--feature/--repo forwarding (REQ-10/11/12/13) and 422 rendering."""

    def _running_status_stops_immediately(self):
        return MagicMock(status_code=200, json=lambda: {"running": False})

    def test_no_feature_omits_speckit_params(self, tmp_path):
        project = _git_project(tmp_path)
        mock_post = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"project": str(project)}))
        mock_get = MagicMock(return_value=self._running_status_stops_immediately())
        with patch("requests.post", mock_post), patch("requests.get", mock_get), patch("time.sleep"):
            rc = autopilot_cli.start_pipeline(_start_args(project))

        assert rc == 0
        _, kwargs = mock_post.call_args
        assert "feature" not in kwargs["params"]
        assert "repo" not in kwargs["params"]

    def test_feature_forwarded_without_repo(self, tmp_path):
        project = _git_project(tmp_path)
        mock_post = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"project": str(project)}))
        mock_get = MagicMock(return_value=self._running_status_stops_immediately())
        with patch("requests.post", mock_post), patch("requests.get", mock_get), patch("time.sleep"):
            autopilot_cli.start_pipeline(_start_args(project, feature="001-x"))

        _, kwargs = mock_post.call_args
        assert kwargs["params"]["feature"] == "001-x"
        assert "repo" not in kwargs["params"]

    def test_feature_and_repo_both_forwarded(self, tmp_path):
        project = _git_project(tmp_path)
        mock_post = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"project": str(project)}))
        mock_get = MagicMock(return_value=self._running_status_stops_immediately())
        with patch("requests.post", mock_post), patch("requests.get", mock_get), patch("time.sleep"):
            autopilot_cli.start_pipeline(_start_args(project, feature="001-x", repo="backend"))

        _, kwargs = mock_post.call_args
        assert kwargs["params"]["feature"] == "001-x"
        assert kwargs["params"]["repo"] == "backend"

    def test_ambiguous_selection_renders_candidates_and_returns_1(self, tmp_path, capsys):
        project = _git_project(tmp_path)
        body = {
            "code": "MULTIPLE_FEATURES",
            "message": "Multiple Spec Kit features found; pass --feature to select one",
            "candidates": ["001-x", "002-y"],
        }
        mock_post = MagicMock(return_value=MagicMock(status_code=422, json=lambda: {"detail": body}))
        with patch("requests.post", mock_post):
            rc = autopilot_cli.start_pipeline(_start_args(project))

        assert rc == 1
        out = capsys.readouterr().out
        assert "Multiple Spec Kit features found" in out
        assert "001-x" in out and "002-y" in out


class TestCheckSpeckitReadiness:
    def _check_args(self, project_path, **overrides):
        base = {"project_path": str(project_path), "feature": None, "repo": None, "api_base": API_BASE}
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_reports_missing_files_and_clarification_markers(self, tmp_path, capsys):
        data = {
            "multi_repo_scan": True,
            "features": [
                {
                    "number": "001",
                    "slug": "x",
                    "repo_label": None,
                    "missing_files": ["plan.md"],
                    "needs_clarification": ["which auth method?"],
                }
            ],
        }
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: data)):
            rc = autopilot_cli.check_speckit_readiness(self._check_args(tmp_path))

        assert rc == 0
        out = capsys.readouterr().out
        assert "001-x" in out
        assert "Missing: plan.md" in out
        assert "which auth method?" in out

    def test_ready_feature_reports_ready(self, tmp_path, capsys):
        data = {
            "multi_repo_scan": True,
            "features": [{"number": "001", "slug": "x", "repo_label": None, "missing_files": [], "needs_clarification": []}],
        }
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: data)):
            autopilot_cli.check_speckit_readiness(self._check_args(tmp_path))

        assert "Ready." in capsys.readouterr().out

    def test_no_features_found(self, tmp_path, capsys):
        data = {"multi_repo_scan": True, "features": []}
        with patch("requests.get", return_value=MagicMock(status_code=200, json=lambda: data)):
            rc = autopilot_cli.check_speckit_readiness(self._check_args(tmp_path))

        assert rc == 0
        assert "No Spec Kit features found." in capsys.readouterr().out

    def test_ambiguous_selection_renders_candidates_but_never_fails(self, tmp_path, capsys):
        """REQ-15: voluntary check must never fail the command, even on an
        ambiguous --feature match."""
        body = {"code": "AMBIGUOUS_REPO", "message": "ambiguous across repos", "candidates": ["001-x (backend)", "001-x (frontend)"]}
        with patch("requests.get", return_value=MagicMock(status_code=422, json=lambda: {"detail": body})):
            rc = autopilot_cli.check_speckit_readiness(self._check_args(tmp_path, feature="001"))

        assert rc == 0
        out = capsys.readouterr().out
        assert "ambiguous across repos" in out

    def test_backend_unreachable_never_fails(self, tmp_path, capsys):
        import requests

        with patch("requests.get", side_effect=requests.exceptions.ConnectionError()):
            rc = autopilot_cli.check_speckit_readiness(self._check_args(tmp_path))

        assert rc == 0
        assert "Backend not running" in capsys.readouterr().out
