"""Regression (external evaluation finding, §4): check_setup_macos.py's
pre-flight dependency check only tested whether fastapi/qdrant_client/
sqlalchemy already import -- which only ever reports whether an install
already succeeded, not whether one would. A fresh clone with a genuinely
broken pyproject.toml (two mutually unsatisfiable version constraints was
the actual live bug -- see the pyproject.toml dependency-conflict fix
elsewhere in this project's history) showed three clean "not installed
yet" red Xs with no way to tell the installer was about to fail outright.

check_dependency_resolution() runs the real `uv pip install -e .
--dry-run` resolution (the same command scripts/install.sh's actual
install step uses) instead.
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from check_setup_macos import SetupChecker  # noqa: E402


class TestCheckDependencyResolution:
    def test_reports_false_when_uv_is_not_installed(self):
        checker = SetupChecker()
        with patch("shutil.which", return_value=None):
            result = checker.check_dependency_resolution()
        assert result is False
        assert checker.results["dependencies"]["dependency resolution (uv --dry-run)"] is False

    def test_reports_true_on_a_clean_resolution(self):
        checker = SetupChecker()
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ) as mock_run,
        ):
            result = checker.check_dependency_resolution()
        assert result is True
        assert checker.results["dependencies"]["dependency resolution (uv --dry-run)"] is True
        assert mock_run.call_args.args[0][:5] == ["uv", "pip", "install", "-e", "."]
        assert "--dry-run" in mock_run.call_args.args[0]

    def test_reports_false_and_surfaces_the_conflict_on_a_real_failure(self, capsys):
        checker = SetupChecker()
        conflict_message = (
            "  × No solution found when resolving dependencies:\n"
            "  ╰─▶ Because openai==1.0.0 and langchain-openai>=1.1.14 depend on\n"
            "      incompatible openai versions, we can conclude that your\n"
            "      requirements are unsatisfiable."
        )
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch(
                "subprocess.run",
                return_value=MagicMock(returncode=1, stdout="", stderr=conflict_message),
            ),
        ):
            result = checker.check_dependency_resolution()
        assert result is False
        assert checker.results["dependencies"]["dependency resolution (uv --dry-run)"] is False
        printed = capsys.readouterr().out
        assert "unsatisfiable" in printed

    def test_timeout_is_treated_as_failure_not_a_crash(self):
        checker = SetupChecker()
        with (
            patch("shutil.which", return_value="/usr/local/bin/uv"),
            patch(
                "subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd="uv", timeout=60),
            ),
        ):
            result = checker.check_dependency_resolution()
        assert result is False

    def test_uv_is_checked_as_a_prerequisite_command(self):
        """The resolution check depends on uv being present -- it must
        also show up in the ordinary CLI-tools prerequisite list, not
        just silently fail later with no indication why."""
        checker = SetupChecker()
        checker.check_command("uv")
        assert "uv" in checker.results["cli_tools"]
