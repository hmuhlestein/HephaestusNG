"""AutopilotProject.verify_tests_command -- a per-project override for the
QA gate's independent test verification (run_independent_test_verification
in src/autopilot/spec.py), which otherwise hardcodes a `python -m pytest`
invocation with no branch at all for a non-Python target project (external
evaluation §3.2: Go/TypeScript projects silently fall back to trusting the
agent's self-reported QA metrics).

Covers: the AutopilotProject.verify_tests_command column + migration, the
PATCH endpoint, the _project_verify_tests_command workflow->project lookup,
run_independent_test_verification's custom-command branch, and score_qa's
end-to-end wiring (a failing configured command must be a real QA failure,
not silently ignored).
"""

import uuid
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from src.autopilot.spec import (
    DEFAULT_SPEC,
    _project_verify_tests_command,
    build_phase_output,
    run_independent_test_verification,
    score_qa,
)
from src.core.database import AutopilotProject
from src.core.schema_migrations import migrate_verify_tests_command_column


class TestSchemaAndMigration:
    def test_verify_tests_command_defaults_none(self, db_manager):
        from src.core.database import get_db

        with get_db() as db:
            proj = AutopilotProject(id=f"proj-{uuid.uuid4().hex[:8]}", name="p", base_dir="/tmp")
            db.add(proj)
            db.commit()
            assert proj.verify_tests_command is None

    def test_migration_adds_column_to_existing_db(self, tmp_path):
        # Simulate a pre-existing DB by creating the table without the new
        # column, then running the migration against it.
        db_path = tmp_path / "legacy.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE autopilot_projects (id VARCHAR PRIMARY KEY, name VARCHAR, base_dir VARCHAR)"))
            conn.execute(text("INSERT INTO autopilot_projects (id, name, base_dir) VALUES ('p1', 'P', '/tmp')"))
            conn.commit()

        migrate_verify_tests_command_column(engine)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT verify_tests_command FROM autopilot_projects")).fetchall()
        assert rows == [(None,)]

    def test_migration_is_idempotent(self, tmp_path):
        db_path = tmp_path / "legacy2.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE autopilot_projects (id VARCHAR PRIMARY KEY)"))
            conn.commit()

        migrate_verify_tests_command_column(engine)
        migrate_verify_tests_command_column(engine)  # must not raise


class TestPatchEndpoint:
    @pytest.fixture
    def db_session(self):
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool

        from src.core.database import Base

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine)()
        yield session
        session.close()

    def _cm(self, session):
        cm = MagicMock()
        cm.__enter__ = Mock(return_value=session)
        cm.__exit__ = Mock(return_value=False)
        return cm

    async def test_set_verify_tests_command_persists_value(self, db_session):
        from src.mcp.autopilot.feature_review_routes import (
            VerifyTestsCommandUpdate,
            set_verify_tests_command,
        )

        proj = AutopilotProject(id=f"proj-{uuid.uuid4().hex[:8]}", name="p", base_dir="/tmp")
        db_session.add(proj)
        db_session.commit()

        with patch("src.core.database.get_db", return_value=self._cm(db_session)), \
             patch("src.mcp.autopilot.feature_review_routes._invalidate"):
            result = await set_verify_tests_command(
                proj.id, VerifyTestsCommandUpdate(verify_tests_command="go test ./...")
            )

        assert result == {"verify_tests_command": "go test ./..."}
        db_session.refresh(proj)
        assert proj.verify_tests_command == "go test ./..."

    async def test_blank_command_is_stored_as_none(self, db_session):
        """Clearing the override (empty string / whitespace-only) must
        store None, not an empty string that would compare truthy and
        (incorrectly) be treated as configured."""
        from src.mcp.autopilot.feature_review_routes import (
            VerifyTestsCommandUpdate,
            set_verify_tests_command,
        )

        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:8]}", name="p", base_dir="/tmp",
            verify_tests_command="npm test",
        )
        db_session.add(proj)
        db_session.commit()

        with patch("src.core.database.get_db", return_value=self._cm(db_session)), \
             patch("src.mcp.autopilot.feature_review_routes._invalidate"):
            result = await set_verify_tests_command(
                proj.id, VerifyTestsCommandUpdate(verify_tests_command="   ")
            )

        assert result == {"verify_tests_command": None}
        db_session.refresh(proj)
        assert proj.verify_tests_command is None

    async def test_unknown_project_404s(self, db_session):
        from src.mcp.autopilot.feature_review_routes import (
            VerifyTestsCommandUpdate,
            set_verify_tests_command,
        )

        with patch("src.core.database.get_db", return_value=self._cm(db_session)):
            with pytest.raises(HTTPException) as exc_info:
                await set_verify_tests_command(
                    "does-not-exist", VerifyTestsCommandUpdate(verify_tests_command="npm test")
                )
        assert exc_info.value.status_code == 404

    async def test_setting_one_project_does_not_affect_another(self, db_session):
        from src.mcp.autopilot.feature_review_routes import (
            VerifyTestsCommandUpdate,
            set_verify_tests_command,
        )

        proj_a = AutopilotProject(id=f"proj-{uuid.uuid4().hex[:8]}", name="A", base_dir="/tmp/a")
        proj_b = AutopilotProject(id=f"proj-{uuid.uuid4().hex[:8]}", name="B", base_dir="/tmp/b")
        db_session.add_all([proj_a, proj_b])
        db_session.commit()

        with patch("src.core.database.get_db", return_value=self._cm(db_session)), \
             patch("src.mcp.autopilot.feature_review_routes._invalidate"):
            await set_verify_tests_command(
                proj_a.id, VerifyTestsCommandUpdate(verify_tests_command="npm test")
            )

        db_session.refresh(proj_a)
        db_session.refresh(proj_b)
        assert proj_a.verify_tests_command == "npm test"
        assert proj_b.verify_tests_command is None


class TestProjectVerifyTestsCommandLookup:
    """_project_verify_tests_command: workflow_id -> AutopilotProject.verify_tests_command."""

    def test_no_workflow_id_returns_none(self):
        assert _project_verify_tests_command(None) is None

    def test_unknown_workflow_id_returns_none(self, db_manager):
        assert _project_verify_tests_command("wf-does-not-exist") is None

    def test_workflow_with_no_project_returns_none(self, db_manager):
        from src.core.database import Workflow, get_db

        with get_db() as db:
            db.add(Workflow(id="wf-vtc-noproj", name="t", phases_folder_path="/tmp", status="active"))
            db.commit()

        assert _project_verify_tests_command("wf-vtc-noproj") is None

    def test_project_without_override_returns_none(self, db_manager):
        from src.core.database import Workflow, get_db

        with get_db() as db:
            db.add(AutopilotProject(id="proj-vtc-1", name="p", base_dir="/tmp/vtc-1"))
            db.add(Workflow(
                id="wf-vtc-1", name="t", phases_folder_path="/tmp",
                status="active", project_id="proj-vtc-1",
            ))
            db.commit()

        assert _project_verify_tests_command("wf-vtc-1") is None

    def test_project_with_override_returns_it(self, db_manager):
        from src.core.database import Workflow, get_db

        with get_db() as db:
            db.add(AutopilotProject(
                id="proj-vtc-2", name="p", base_dir="/tmp/vtc-2",
                verify_tests_command="go test ./...",
            ))
            db.add(Workflow(
                id="wf-vtc-2", name="t", phases_folder_path="/tmp",
                status="active", project_id="proj-vtc-2",
            ))
            db.commit()

        assert _project_verify_tests_command("wf-vtc-2") == "go test ./..."


class TestRunIndependentTestVerificationCustomCommand:
    """A non-Python project hands over its own real test/verify command via
    verify_tests_command, replacing the hardcoded pytest invocation."""

    def test_no_override_still_takes_the_pytest_branch(self, tmp_path, monkeypatch):
        """Python projects with no override must keep running pytest
        exactly as before -- still gated by _select_relevant_test_files,
        still invoking `python -m pytest`."""
        from src.autopilot import spec as spec_module

        monkeypatch.setattr(
            spec_module, "_select_relevant_test_files",
            lambda wd: ["tests/test_scoped.py"],
        )
        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            return MagicMock(returncode=0, stdout="1 passed in 0.01s", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        result = run_independent_test_verification(str(tmp_path), verify_tests_command=None)

        assert captured["args"][:3] == ["python", "-m", "pytest"]
        assert "tests/test_scoped.py" in captured["args"]
        assert result["passed"] == 1
        assert result["failed"] == 0
        assert result["source"] == "independent_verification"

    def test_configured_command_replaces_pytest_entirely(self, tmp_path, monkeypatch):
        """_select_relevant_test_files (a pytest/naming-convention
        heuristic) must not even be consulted when a command is
        configured -- the command owns its own scope."""
        from src.autopilot import spec as spec_module

        def fail_if_called(wd):
            raise AssertionError("_select_relevant_test_files should not run for a configured command")

        monkeypatch.setattr(spec_module, "_select_relevant_test_files", fail_if_called)

        captured = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["cwd"] = kwargs.get("cwd")
            captured["timeout"] = kwargs.get("timeout")
            return MagicMock(returncode=0, stdout="", stderr="")

        monkeypatch.setattr("subprocess.run", fake_run)

        result = run_independent_test_verification(
            str(tmp_path), timeout_seconds=42, verify_tests_command="npm test",
        )

        assert captured["args"] == ["npm", "test"]
        assert captured["cwd"] == str(tmp_path)
        assert captured["timeout"] == 42
        assert result == {
            "failed": 0,
            "passed": 1,
            "total": 1,
            "pass_rate": 100.0,
            "source": "independent_verification_custom_command",
            "exit_code": 0,
        }

    def test_nonzero_exit_is_a_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=1, stdout="", stderr=""),
        )

        result = run_independent_test_verification(
            str(tmp_path), verify_tests_command="go test ./...",
        )

        assert result["failed"] == 1
        assert result["passed"] == 0
        assert result["total"] == 1
        assert result["pass_rate"] == 0.0
        assert result["exit_code"] == 1

    def test_timeout_returns_none(self, tmp_path, monkeypatch):
        import subprocess

        def raise_timeout(*a, **k):
            raise subprocess.TimeoutExpired(cmd="go test ./...", timeout=5)

        monkeypatch.setattr("subprocess.run", raise_timeout)

        result = run_independent_test_verification(
            str(tmp_path), timeout_seconds=5, verify_tests_command="go test ./...",
        )

        assert result is None

    def test_executable_not_found_returns_none(self, tmp_path, monkeypatch):
        def raise_not_found(*a, **k):
            raise FileNotFoundError("no such file: boguscmd")

        monkeypatch.setattr("subprocess.run", raise_not_found)

        result = run_independent_test_verification(
            str(tmp_path), verify_tests_command="boguscmd test",
        )

        assert result is None

    def test_unparseable_command_returns_none(self, tmp_path):
        """An unterminated quote (shlex.split raises ValueError) must not
        crash the gate -- fall back to the agent report like any other
        unrunnable verification."""
        result = run_independent_test_verification(
            str(tmp_path), verify_tests_command='go test "unterminated',
        )
        assert result is None


class TestScoreQAWiring:
    """score_qa forwards verify_tests_command to
    run_independent_test_verification, and a failing configured command
    must be treated as a real QA failure -- not silently ignored."""

    def test_verify_tests_command_is_forwarded(self, tmp_path, monkeypatch):
        from src.autopilot import spec as spec_module

        received = {}

        def fake_verification(working_directory, timeout_seconds=300, verify_tests_command=None):
            received["verify_tests_command"] = verify_tests_command
            return None

        monkeypatch.setattr(spec_module, "run_independent_test_verification", fake_verification)

        result = {
            "failed_tests": 0, "passed_tests": 10, "total_tests": 10,
            "pass_rate": 100.0, "critical_issues": 0,
        }
        score_qa(
            result, DEFAULT_SPEC, working_directory=str(tmp_path),
            verify_tests_command="go test ./...",
        )

        assert received["verify_tests_command"] == "go test ./..."

    def test_no_verify_tests_command_forwards_none(self, tmp_path, monkeypatch):
        """Default (no config override) -- pytest-only behavior is
        unchanged: None reaches run_independent_test_verification exactly
        as before this feature existed."""
        from src.autopilot import spec as spec_module

        received = {}

        def fake_verification(working_directory, timeout_seconds=300, verify_tests_command=None):
            received["verify_tests_command"] = verify_tests_command
            return None

        monkeypatch.setattr(spec_module, "run_independent_test_verification", fake_verification)

        result = {
            "failed_tests": 0, "passed_tests": 10, "total_tests": 10,
            "pass_rate": 100.0, "critical_issues": 0,
        }
        score_qa(result, DEFAULT_SPEC, working_directory=str(tmp_path))

        assert received["verify_tests_command"] is None

    def test_failing_configured_command_overrides_an_optimistic_agent_report(
        self, tmp_path, monkeypatch
    ):
        """The core correctness requirement: if the agent's qa.md claims a
        clean pass but the project's real configured verify command fails,
        the gate must fail (band != pass), not silently trust the agent."""
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=1, stdout="", stderr=""),
        )

        result = {
            "failed_tests": 0, "passed_tests": 50, "total_tests": 50,
            "pass_rate": 100.0, "critical_issues": 0, "coverage_percent": 90,
        }
        score, meta = score_qa(
            result, DEFAULT_SPEC, working_directory=str(tmp_path),
            verify_tests_command="make verify",
        )

        assert meta["band"] == "development"
        assert meta["failed_tests"] >= 1
        assert any("failed_tests" in v for v in meta["violations"])
        assert meta["independent_verification"]["source"] == "independent_verification_custom_command"

    def test_passing_configured_command_agrees_with_agent_report(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=0, stdout="", stderr=""),
        )

        result = {
            "failed_tests": 0, "passed_tests": 50, "total_tests": 50,
            "pass_rate": 100.0, "critical_issues": 0, "coverage_percent": 90,
        }
        score, meta = score_qa(
            result, DEFAULT_SPEC, working_directory=str(tmp_path),
            verify_tests_command="make verify",
        )

        assert meta["band"] == "pass"
        assert meta["violations"] == []


class TestBuildPhaseOutputEndToEnd:
    """The full wiring: build_phase_output resolves the project's
    verify_tests_command from the DB (via workflow_id) and threads it all
    the way down to the subprocess call that decides the gate's score."""

    def _okf(self, frontmatter_yaml: str) -> str:
        return f"---\n{frontmatter_yaml}\n---\n\n# Report\n"

    def test_configured_command_failure_fails_the_gate(self, tmp_path, db_manager, monkeypatch):
        from src.core.database import AutopilotProject, Workflow, get_db

        with get_db() as db:
            db.add(AutopilotProject(
                id="proj-bpo-vtc", name="p", base_dir=str(tmp_path),
                verify_tests_command="make verify",
            ))
            db.add(Workflow(
                id="wf-bpo-vtc", name="t", phases_folder_path=str(tmp_path),
                status="active", project_id="proj-bpo-vtc",
            ))
            db.commit()

        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        (docs / "qa.md").write_text(self._okf(
            "type: qa_validation_result\n"
            "failed_tests: 0\n"
            "passed_tests: 50\n"
            "pass_rate: 100.0\n"
            "critical_issues: 0\n"
            "coverage_percent: 90"
        ))

        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: MagicMock(returncode=1, stdout="", stderr=""),
        )

        result = build_phase_output("qa_validation", tmp_path, workflow_id="wf-bpo-vtc")

        assert result["score"] < 0.7
        assert result["spec_gate"]["independent_verification"]["source"] == "independent_verification_custom_command"

    def test_skip_independent_verification_never_touches_the_configured_command(
        self, tmp_path, db_manager, monkeypatch
    ):
        """skip_independent_verification=True (used by the fast synchronous
        pre-check paths) must not run any command at all, configured or
        not -- it exists specifically to avoid the slow subprocess call."""
        from src.core.database import AutopilotProject, Workflow, get_db

        with get_db() as db:
            db.add(AutopilotProject(
                id="proj-bpo-skip", name="p", base_dir=str(tmp_path),
                verify_tests_command="make verify",
            ))
            db.add(Workflow(
                id="wf-bpo-skip", name="t", phases_folder_path=str(tmp_path),
                status="active", project_id="proj-bpo-skip",
            ))
            db.commit()

        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        (docs / "qa.md").write_text(self._okf(
            "type: qa_validation_result\n"
            "failed_tests: 0\n"
            "passed_tests: 50\n"
            "pass_rate: 100.0\n"
            "critical_issues: 0\n"
            "coverage_percent: 90"
        ))

        def fail_if_called(*a, **k):
            raise AssertionError("no subprocess should run when independent verification is skipped")

        monkeypatch.setattr("subprocess.run", fail_if_called)

        result = build_phase_output(
            "qa_validation", tmp_path, workflow_id="wf-bpo-skip",
            skip_independent_verification=True,
        )

        assert result["score"] >= 0.7
