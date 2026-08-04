"""Tests for src/core/logging_config.py's configure_logging.

Regression coverage: backend.log/monitor.log/watchdog.log used to be raw
subprocess stdout redirects opened once by start.py in append mode for the
process's entire lifetime -- rotation has to happen from inside the
writing process, so they never rotated at all. Observed live at 568MB
(monitor.log) and 253MB (backend.log) after running unattended for about
two months, on a disk that was down to 1.3GB free as a direct result.
"""

import logging
import logging.handlers
import sys

import pytest

from src.core.logging_config import LOG_ROTATE_BACKUP_COUNT, configure_logging


@pytest.fixture(autouse=True)
def _restore_logging_state():
    """configure_logging(force=True) replaces the root logger's handlers
    and (when given a log_file) sys.excepthook -- both process-global
    state that must not leak between tests."""
    original_handlers = list(logging.root.handlers)
    original_level = logging.root.level
    original_excepthook = sys.excepthook
    yield
    for h in list(logging.root.handlers):
        logging.root.removeHandler(h)
        h.close()
    for h in original_handlers:
        logging.root.addHandler(h)
    logging.root.setLevel(original_level)
    sys.excepthook = original_excepthook


class TestConfigureLoggingRotation:
    def test_log_file_uses_timed_rotating_handler(self, tmp_path):
        log_file = tmp_path / "backend.log"

        configure_logging(log_file=str(log_file))

        assert len(logging.root.handlers) == 1
        handler = logging.root.handlers[0]
        assert isinstance(handler, logging.handlers.TimedRotatingFileHandler)
        assert handler.when == "MIDNIGHT"  # TimedRotatingFileHandler uppercases `when`
        assert handler.backupCount == LOG_ROTATE_BACKUP_COUNT

    def test_no_log_file_uses_stream_handler_not_file(self, tmp_path):
        """Standalone/interactive use (no log_file) must keep working
        exactly as before -- stdout, no file, no rotation."""
        configure_logging()

        assert len(logging.root.handlers) == 1
        assert isinstance(logging.root.handlers[0], logging.StreamHandler)
        assert not isinstance(
            logging.root.handlers[0], logging.handlers.TimedRotatingFileHandler
        )

    def test_log_file_actually_writes_and_rotation_is_daily(self, tmp_path):
        log_file = tmp_path / "backend.log"
        configure_logging(log_file=str(log_file))

        logging.getLogger("test").info("hello")

        assert log_file.exists()
        assert "hello" in log_file.read_text()
        handler = logging.root.handlers[0]
        assert handler.when == "MIDNIGHT"
        assert handler.interval == 60 * 60 * 24  # TimedRotatingFileHandler's own day-in-seconds for "midnight"


class TestConfigureLoggingUncaughtExceptions:
    """A raw subprocess stdout redirect used to catch an uncaught
    exception's traceback for free (Python's default excepthook writes to
    stderr, which start.py redirected into the same log file). With that
    redirect removed in favor of the rotating handler owning the file
    directly, configure_logging must install its own excepthook or a
    crash after startup becomes completely silent."""

    def test_installs_excepthook_when_log_file_given(self, tmp_path):
        configure_logging(log_file=str(tmp_path / "backend.log"))

        assert sys.excepthook is not sys.__excepthook__

    def test_does_not_install_excepthook_without_log_file(self, tmp_path):
        sys.excepthook = sys.__excepthook__
        configure_logging()

        assert sys.excepthook is sys.__excepthook__

    def test_uncaught_exception_is_logged_to_the_file(self, tmp_path):
        log_file = tmp_path / "backend.log"
        configure_logging(log_file=str(log_file))

        try:
            raise RuntimeError("boom")
        except RuntimeError:
            sys.excepthook(*sys.exc_info())

        content = log_file.read_text()
        assert "Uncaught exception" in content
        assert "RuntimeError: boom" in content

    def test_keyboard_interrupt_is_not_logged_as_an_error(self, tmp_path, capsys):
        """A user-initiated Ctrl+C is not a crash -- must still fall
        through to the default hook (clean exit), not get logged as an
        uncaught-exception error."""
        log_file = tmp_path / "backend.log"
        configure_logging(log_file=str(log_file))

        try:
            raise KeyboardInterrupt()
        except KeyboardInterrupt:
            sys.excepthook(*sys.exc_info())

        content = log_file.read_text() if log_file.exists() else ""
        assert "Uncaught exception" not in content
