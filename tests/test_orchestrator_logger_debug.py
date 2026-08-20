"""Regression: OrchestratorLogger.debug() must exist.

Three call sites already used logger.debug() on this class, which only ever
defined log/info/warning/error/event/save_state. Each raised AttributeError
instead of logging. Found by mypy immediately after the prose-comment fix in
c38f143 unblocked it -- the errors had been invisible for as long as mypy was
failing to parse src/autopilot/spec.py.

The consequential one is run_single_workflow's, which sits in the handler for
a failed pipeline_metrics.json patch. An AttributeError there escapes into the
enclosing `except Exception`, which logs "Failed to launch workflow" and
returns FAILED -- so a cosmetic metrics-file problem killed the whole workflow
launch and misreported the cause.
"""

from pathlib import Path

import pytest

from src.autopilot.orchestrator import OrchestratorLogger


@pytest.fixture
def orch_logger(tmp_path):
    return OrchestratorLogger(tmp_path / "logs")


def test_debug_exists_and_writes_at_debug_level(orch_logger):
    orch_logger.debug("resync skipped this tick")

    written = Path(orch_logger.log_file).read_text()
    assert "resync skipped this tick" in written
    assert "[DEBUG]" in written


def test_every_level_helper_the_orchestrator_calls_is_present():
    """Pins the full set rather than just debug: the gap existed because
    nothing asserted the logger satisfies the interface its callers assume."""
    for level in ("debug", "info", "warning", "error"):
        assert callable(getattr(OrchestratorLogger, level, None)), level


def test_a_failing_metrics_patch_does_not_report_a_failed_launch(orch_logger):
    """The exact shape of the bug: an exception handler that calls
    logger.debug must not itself raise, or the outer handler converts a
    recoverable problem into a workflow failure.
    """
    try:
        try:
            raise OSError("pipeline_metrics.json is unreadable")
        except Exception as inner:
            orch_logger.debug(f"Could not patch pipeline_metrics.json: {inner}")
    except Exception as escaped:  # pragma: no cover - fails the assert below
        pytest.fail(f"debug() raised out of the handler: {escaped!r}")

    assert "Could not patch pipeline_metrics.json" in Path(
        orch_logger.log_file
    ).read_text()
