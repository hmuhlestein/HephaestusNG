"""Tests for the heph CLI.

Tests the CLI commands using mocked HTTP responses.
Does NOT require a running backend.
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.cli.main import build_parser, main
from src.cli.utils import (
    PID_DIR,
    api_delete,
    api_get,
    api_post,
    check_backend,
    is_process_running,
    read_pid,
    remove_pid,
    require_backend,
    save_pid,
    status_icon,
    table,
    time_ago,
    truncate,
)

# ─── Fixtures ───────────────────────────────────────────────────────


@pytest.fixture
def args():
    """Create a mock args namespace."""
    ns = build_parser().parse_args(["status"])
    ns.api_base = "http://127.0.0.1:9999"
    ns.json = False
    return ns


@pytest.fixture
def args_json():
    ns = build_parser().parse_args(["status"])
    ns.api_base = "http://127.0.0.1:9999"
    ns.json = True
    return ns


@pytest.fixture
def mock_health():
    """Mock a healthy backend."""
    with patch("src.cli.utils.httpx") as mock:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "healthy", "qdrant": True}
        resp.text = '{"status": "healthy"}'
        mock.get.return_value = resp
        mock.post.return_value = resp
        mock.ConnectError = Exception
        yield mock


# ─── Parser Tests ───────────────────────────────────────────────────


class TestParser:
    def test_parser_creates(self):
        parser = build_parser()
        assert parser is not None

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0

    def test_no_command_shows_banner(self, capsys):
        result = main([])
        assert result == 0
        captured = capsys.readouterr().out
        # Banner or help text should appear
        assert "heph" in captured.lower() or "Hephaestus" in captured

    def test_unknown_command_errors(self):
        parser = build_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["nonexistent"])
        assert exc.value.code == 2

    def test_status_command_parses(self):
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_json_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--json", "status"])
        assert args.json is True

    def test_host_port_flags(self):
        parser = build_parser()
        args = parser.parse_args(["--host", "10.0.0.1", "--port", "9000", "status"])
        assert args.host == "10.0.0.1"
        assert args.port == 9000

    def test_all_commands_registered(self):
        parser = build_parser()
        commands = [
            "status",
            "start",
            "stop",
            "restart",
            "init",
            "workflow",
            "agent",
            "task",
            "autopilot",
            "memory",
            "exec",
            "config",
        ]
        for cmd in commands:
            args = parser.parse_args([cmd])
            assert args.command == cmd


# ─── Utility Tests ──────────────────────────────────────────────────


class TestTruncate:
    def test_short_string(self):
        assert truncate("hello", 10) == "hello"

    def test_exact_length(self):
        assert truncate("hello", 5) == "hello"

    def test_long_string(self):
        result = truncate("hello world", 5)
        assert result == "hello..."
        assert len(result) == 8

    def test_empty_string(self):
        assert truncate("", 10) == ""

    def test_none_string(self):
        assert truncate(None, 10) == ""


class TestStatusIcon:
    def test_done(self):
        assert status_icon("done") == "OK"

    def test_completed(self):
        assert status_icon("completed") == "OK"

    def test_failed(self):
        assert status_icon("failed") == "FAIL"

    def test_working(self):
        assert status_icon("working") == "..."

    def test_unknown(self):
        assert status_icon("unknown") == "?"

    def test_none(self):
        assert status_icon(None) == "?"


class TestTimeAgo:
    def test_none_returns_never(self):
        assert time_ago(None) == "never"

    def test_empty_returns_never(self):
        assert time_ago("") == "never"

    def test_recent_seconds(self):
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).isoformat()
        result = time_ago(ts)
        assert "s ago" in result or "0s ago" in result


class TestTable:
    def test_empty_rows(self, capsys):
        table(["A", "B"], [])
        out = capsys.readouterr().out
        assert "(none)" in out

    def test_with_rows(self, capsys):
        table(["Name", "Value"], [["foo", "bar"], ["baz", "qux"]])
        out = capsys.readouterr().out
        assert "foo" in out
        assert "bar" in out
        assert "Name" in out


# ─── PID Management Tests ──────────────────────────────────────────


class TestPidManagement:
    def setup_method(self):
        # Clean up any existing test PIDs
        remove_pid("test_service")

    def teardown_method(self):
        remove_pid("test_service")

    def test_save_and_read_pid(self):
        save_pid("test_service", 12345)
        assert read_pid("test_service") == 12345

    def test_read_nonexistent_pid(self):
        assert read_pid("nonexistent_service") is None

    def test_remove_pid(self):
        save_pid("test_service", 12345)
        remove_pid("test_service")
        assert read_pid("test_service") is None

    def test_save_creates_directory(self):
        save_pid("test_service", 999)
        assert PID_DIR.exists()

    def test_read_corrupt_pid(self):
        PID_DIR.mkdir(parents=True, exist_ok=True)
        (PID_DIR / "corrupt.pid").write_text("not_a_number")
        assert read_pid("corrupt") is None
        (PID_DIR / "corrupt.pid").unlink()


class TestIsProcessRunning:
    def test_current_process_is_running(self):
        assert is_process_running(os.getpid()) is True

    def test_nonexistent_pid(self):
        assert is_process_running(999999999) is False


# ─── API Helper Tests ───────────────────────────────────────────────


class TestApiHelpers:
    def test_api_get_connection_refused(self, args):
        result = api_get(args, "/health")
        assert result is None

    def test_api_post_connection_refused(self, args):
        result = api_post(args, "/test", {"key": "value"})
        assert result is None

    def test_api_delete_connection_refused(self, args):
        result = api_delete(args, "/test")
        assert result is None

    def test_api_get_success(self, args, mock_health):
        result = api_get(args, "/health")
        assert result is not None
        assert result["status"] == "healthy"

    def test_api_get_error_response(self, args):
        with patch("src.cli.utils.httpx") as mock:
            resp = MagicMock()
            resp.status_code = 500
            resp.text = "Internal Server Error"
            mock.get.return_value = resp
            mock.ConnectError = Exception
            result = api_get(args, "/health")
            assert result["error"] == 500

    def test_check_backend_healthy(self, args, mock_health):
        assert check_backend(args) is True

    def test_check_backend_unreachable(self, args):
        assert check_backend(args) is False

    def test_check_backend_unhealthy(self, args):
        with patch("src.cli.utils.httpx") as mock:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"status": "unhealthy"}
            mock.get.return_value = resp
            mock.ConnectError = Exception
            assert check_backend(args) is False

    def test_require_backend_fails_when_down(self, args, capsys):
        result = require_backend(args)
        assert result is False
        out = capsys.readouterr()
        assert "not running" in out.err

    def test_require_backend_json_error(self, args_json, capsys):
        result = require_backend(args_json)
        assert result is False
        out = capsys.readouterr()
        data = json.loads(out.out)
        assert data["error"] == "Backend not running"


# ─── Status Command Tests ───────────────────────────────────────────


class TestStatusCommand:
    def test_status_when_backend_down(self, args, capsys):
        from src.cli.commands.status import run

        result = run(args)
        assert result == 1
        out = capsys.readouterr().out
        assert "unreachable" in out.lower()

    def test_status_json_when_backend_down(self, args_json, capsys):
        from src.cli.commands.status import run

        result = run(args_json)
        assert result == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["backend"] == "unreachable"

    def test_status_with_mock_backend(self, args, capsys):
        from src.cli.commands.status import run

        def mock_api_get(a, endpoint, timeout=5):
            responses = {
                "/health": {"status": "healthy"},
                "/api/agents": [{"agent_id": "a1", "status": "working"}],
                "/api/tasks?status=pending": [],
                "/api/tasks?status=in_progress": [],
                "/api/tasks?status=done": [],
                "/api/tasks?status=failed": [],
                "/api/workflow-definitions": [],
                "/api/workflow-executions": [],
                "/api/queue_status": {"status": "empty"},
            }
            return responses.get(endpoint, {})

        with patch("src.cli.commands.status.api_get", side_effect=mock_api_get):
            result = run(args)
        assert result == 0
        out = capsys.readouterr().out
        assert "OK" in out


# ─── Workflow Command Tests ─────────────────────────────────────────


class TestWorkflowCommand:
    def test_list_definitions_when_backend_down(self, args, capsys):
        from src.cli.commands.workflow import list_definitions

        result = list_definitions(args)
        assert result == 1

    def test_list_executions_when_backend_down(self, args, capsys):
        from src.cli.commands.workflow import list_executions

        args.status = None
        result = list_executions(args)
        assert result == 1

    def test_list_definitions_with_data(self, args, capsys):
        from src.cli.commands.workflow import list_definitions

        mock_data = [
            {"id": "wf1", "name": "Test WF", "description": "A test"},
        ]
        with (
            patch("src.cli.commands.workflow.api_get", return_value=mock_data),
            patch("src.cli.commands.workflow.require_backend", return_value=True),
        ):
            result = list_definitions(args)
        assert result == 0
        out = capsys.readouterr().out
        assert "wf1" in out

    def test_launch_when_backend_down(self, args, capsys):
        from src.cli.commands.workflow import launch

        args.definition_id = "test"
        args.description = "test desc"
        args.path = None
        result = launch(args)
        assert result == 1

    def test_stop_single_workflow_success(self, args, capsys):
        """Reports what /api/workflow-executions/{id}/stop actually did.

        That endpoint pauses the workflow (resetting in-flight tasks to
        pending for a later resume) and returns status/agents_terminated
        with no "message" key. Printing a bare "Workflow stopped" here --
        the old fallback -- would tell the operator a reversible pause was
        a terminal stop.
        """
        from src.cli.commands.workflow import stop_workflow

        args.all = False
        args.workflow_id = "wf-1"

        def mock_api_get(a, endpoint, **kw):
            if endpoint == "/api/agents":
                return {"agents": []}
            return {}

        with (
            patch("src.cli.commands.workflow.api_get", side_effect=mock_api_get),
            patch(
                "src.cli.commands.workflow.api_post",
                return_value={
                    "status": "paused",
                    "workflow_id": "wf-1",
                    "agents_terminated": 2,
                },
            ),
            patch("src.cli.commands.workflow.require_backend", return_value=True),
        ):
            result = stop_workflow(args)

        assert result == 0
        out = capsys.readouterr().out
        assert "paused" in out
        assert "2 agent(s) terminated" in out

    def test_stop_single_workflow_reports_already_stopped_message(self, args, capsys):
        """The endpoint's early return for an already-terminal workflow does
        carry a message; it must win over the derived description."""
        from src.cli.commands.workflow import stop_workflow

        args.all = False
        args.workflow_id = "wf-1"

        def mock_api_get(a, endpoint, **kw):
            if endpoint == "/api/agents":
                return {"agents": []}
            return {}

        with (
            patch("src.cli.commands.workflow.api_get", side_effect=mock_api_get),
            patch(
                "src.cli.commands.workflow.api_post",
                return_value={"status": "completed", "message": "Already stopped"},
            ),
            patch("src.cli.commands.workflow.require_backend", return_value=True),
        ):
            result = stop_workflow(args)

        assert result == 0
        assert "Already stopped" in capsys.readouterr().out

    def test_stop_single_workflow_prints_warning_on_agent_termination_failure(
        self, args, capsys
    ):
        """Regression (SOLID review Theme D, 2026-08-20): a failed
        terminate_agent call for this workflow's agents used to be
        swallowed silently (except Exception: pass, no output) -- the
        operator got no signal an agent might still be running and
        writing to the shared worktree even though the command reported
        success. Must now print a warning, matching the sibling --all
        path's existing per-agent status output."""
        from src.cli.commands.workflow import stop_workflow

        args.all = False
        args.workflow_id = "wf-1"

        def mock_api_get(a, endpoint, **kw):
            if endpoint == "/api/agents":
                return {
                    "agents": [
                        {
                            "id": "agent-1",
                            "status": "working",
                            "workflow": {"id": "wf-1"},
                        }
                    ]
                }
            return {}

        def mock_api_post(a, endpoint, *rest, **kw):
            if endpoint == "/api/terminate_agent":
                raise ConnectionError("simulated connection failure")
            return {"message": "Workflow stopped"}

        with (
            patch("src.cli.commands.workflow.api_get", side_effect=mock_api_get),
            patch("src.cli.commands.workflow.api_post", side_effect=mock_api_post),
            patch("src.cli.commands.workflow.require_backend", return_value=True),
        ):
            result = stop_workflow(args)

        out = capsys.readouterr().out
        assert "agent-1" in out
        assert "could not terminate" in out.lower()
        # The workflow stop itself still proceeds despite the agent-
        # termination warning -- matches this function's pre-existing
        # behavior of not letting agent-cleanup issues block the stop.
        assert result == 0
        assert "Workflow stopped" in out


# ─── Agent Command Tests ────────────────────────────────────────────


class TestAgentCommand:
    def test_list_agents_when_backend_down(self, args, capsys):
        from src.cli.commands.agent import list_agents

        args.status = None
        result = list_agents(args)
        assert result == 1

    def test_terminate_when_backend_down(self, args, capsys):
        from src.cli.commands.agent import terminate

        args.agent_id = "test_agent"
        result = terminate(args)
        assert result == 1


# ─── Task Command Tests ─────────────────────────────────────────────


class TestTaskCommand:
    def test_list_tasks_when_backend_down(self, args, capsys):
        from src.cli.commands.task import list_tasks

        args.status = None
        args.limit = 20
        result = list_tasks(args)
        assert result == 1

    def test_create_task_when_backend_down(self, args, capsys):
        from src.cli.commands.task import create_task

        args.description = "test task"
        args.priority = "medium"
        args.phase = None
        result = create_task(args)
        assert result == 1


# ─── Autopilot Command Tests ───────────────────────────────────────


class TestAutopilotCommand:
    def test_show_queue_nonexistent_dir(self, args, capsys):
        from src.cli.commands.autopilot import show_queue

        args.project_path = "/tmp/nonexistent_project_xyz"
        result = show_queue(args)
        assert result == 0
        out = capsys.readouterr().out
        assert "not found" in out.lower() or "empty" in out.lower()

    def test_show_queue_empty(self, args, capsys):
        from src.cli.commands.autopilot import show_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / ".hephaestus" / "specs"
            queue_dir.mkdir(parents=True)
            args.project_path = tmpdir
            result = show_queue(args)
        assert result == 0
        out = capsys.readouterr().out
        assert "empty" in out.lower()

    def test_show_queue_with_files(self, args, capsys):
        from src.cli.commands.autopilot import show_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            queue_dir = Path(tmpdir) / ".hephaestus" / "specs"
            queue_dir.mkdir(parents=True)
            (queue_dir / "design1.md").write_text("# Design 1")
            (queue_dir / "design2.md").write_text("# Design 2")
            args.project_path = tmpdir
            result = show_queue(args)
        assert result == 0
        out = capsys.readouterr().out
        assert "design1" in out
        assert "design2" in out

    def test_add_to_queue(self, args, capsys):
        """Regression: this test used to call the real, running backend
        (add_to_queue does a genuine requests.post to 127.0.0.1:8300 with
        no mock) -- every run silently created a real AutopilotProject
        pointed at this test's throwaway tmp directory against whatever
        live dev database happened to be running. Observed live: dozens of
        "tmpXXXXXXXX" projects accumulated over days, several ending up
        simultaneously is_active=True and hijacking the phase-advancement
        sweep's project scoping away from the real project. Mock the HTTP
        call -- this test verifies add_to_queue's own request/response
        handling, not the live server."""
        from src.cli.commands.autopilot import add_to_queue

        with tempfile.TemporaryDirectory() as tmpdir:
            # Create source file
            source = Path(tmpdir) / "source.md"
            source.write_text("# My Design")
            args.file = str(source)
            args.project_path = tmpdir

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "id": "des-test1234",
                "name": "source.md",
                "status": "pending",
            }
            with patch("requests.post", return_value=mock_response) as mock_post:
                result = add_to_queue(args)

        mock_post.assert_called_once()
        assert result == 0
        out = capsys.readouterr().out
        assert "Added" in out

    def test_add_nonexistent_file(self, args, capsys):
        from src.cli.commands.autopilot import add_to_queue

        args.file = "/tmp/nonexistent_xyz.md"
        args.project_path = "/tmp/test"
        result = add_to_queue(args)
        assert result == 1

    def test_pipeline_status_when_not_running(self, args, capsys):
        """An unreachable backend is a failed command: exit 1, like every
        other error path in pipeline_status (non-200, unresolvable
        project). The mock matters as much as the assertion -- without it
        this test made a real request to args.api_base and passed or
        failed on whether anything happened to be listening there.
        """
        from unittest.mock import patch

        import requests as _requests

        from src.cli.commands.autopilot import pipeline_status

        with patch(
            "requests.get", side_effect=_requests.exceptions.ConnectionError("refused")
        ):
            result = pipeline_status(args)

        assert result == 1
        assert "Backend not running" in capsys.readouterr().out


# ─── Memory Command Tests ──────────────────────────────────────────


class TestMemoryCommand:
    def test_search_when_backend_down(self, args, capsys):
        from src.cli.commands.memory import search

        args.query = "test query"
        args.limit = 10
        args.memory_type = None
        result = search(args)
        assert result == 1

    def test_save_when_backend_down(self, args, capsys):
        from src.cli.commands.memory import save

        args.content = "test memory"
        args.memory_type = "discovery"
        args.tags = ["test"]
        result = save(args)
        assert result == 1


# ─── Exec Command Tests ────────────────────────────────────────────


class TestExecCommand:
    def test_ping_when_backend_down(self, args, capsys):
        from src.cli.commands.exec_cmd import ping

        result = ping(args)
        assert result == 1
        out = capsys.readouterr().out
        assert "Unreachable" in out

    def test_run_command_no_args(self, args, capsys):
        from src.cli.commands.exec_cmd import run_command

        args.command = []
        result = run_command(args)
        assert result == 1

    def test_raw_request_path_traversal_blocked(self, args, capsys):
        from src.cli.commands.exec_cmd import raw_request

        args.method = "GET"
        args.path = "../../etc/passwd"
        args.data = None
        result = raw_request(args)
        assert result == 1
        out = capsys.readouterr()
        assert "traversal" in out.err.lower()


# ─── Config Command Tests ──────────────────────────────────────────


class TestConfigCommand:
    def test_show_paths(self, args, capsys):
        from src.cli.commands.config import show_paths

        result = show_paths(args)
        assert result == 0
        out = capsys.readouterr().out
        assert "project_root" in out
        assert "config" in out
        assert "database" in out

    def test_show_config(self, args, capsys):
        from src.cli.commands.config import show

        result = show(args)
        # Config file exists in this project
        assert result == 0
        out = capsys.readouterr().out
        assert "llm" in out.lower() or "hephaestus" in out.lower()


# ─── Output Formatting Tests ────────────────────────────────────────


class TestOutput:
    def test_json_output(self, args_json, capsys):
        from src.cli.utils import output

        output(args_json, {"key": "value"})
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["key"] == "value"

    def test_human_output_with_formatter(self, args, capsys):
        from src.cli.utils import output

        output(args, {"key": "value"}, lambda d: print(f"Formatted: {d['key']}"))
        out = capsys.readouterr().out
        assert "Formatted: value" in out

    def test_human_output_without_formatter(self, args, capsys):
        from src.cli.utils import output

        output(args, {"key": "value"})
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["key"] == "value"


# ─── Integration: Full CLI Invocation ───────────────────────────────


class TestFullInvocation:
    def test_help(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_status_always_returns(self, capsys):
        """Status always returns 0 (backend up) or 1 (backend down)."""
        result = main(["status"])
        assert result in (0, 1)

    def test_json_status_always_returns(self, capsys):
        """JSON status always returns valid JSON."""
        result = main(["--json", "status"])
        assert result in (0, 1)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "backend" in data

    def test_exec_endpoints_always_returns(self, capsys):
        result = main(["exec", "endpoints"])
        assert result in (0, 1)

    def test_config_path(self, capsys):
        result = main(["config", "path"])
        assert result == 0
        out = capsys.readouterr().out
        assert "project_root" in out
