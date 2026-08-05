"""Shared logging configuration for Hephaestus.

Use this module to configure logging consistently across all entrypoints.
This prevents the duplicated FileHandler bug that was fixed in run_server.py
and run_monitor.py (see L-2 in ARCHITECTURE_REVIEW.md).

Usage:
    from src.core.logging_config import configure_logging

    configure_logging(level=logging.INFO)
"""

import logging
import logging.handlers
import sys
from typing import Optional

# Retention for TimedRotatingFileHandler's daily rotation (see log_file
# below) -- backend.log/monitor.log/watchdog.log were previously raw
# subprocess stdout redirects with no rotation at all (start.py opened them
# once, in append mode, for the process's entire lifetime); observed live
# at 568MB (monitor.log) and 253MB (backend.log) after running unattended
# for about two months, on a disk that was down to 1.3GB free as a direct
# result.
LOG_ROTATE_BACKUP_COUNT = 7


def configure_logging(
    level: int = logging.INFO,
    format_string: Optional[str] = None,
    log_file: Optional[str] = None,
) -> None:
    """Configure logging for Hephaestus processes.

    This is the SINGLE place to configure logging for all entrypoints.
    Do NOT add FileHandler in individual run_*.py files - pass log_file
    here instead, so it goes through the same rotating handler as
    everything else.

    Args:
        level: Logging level (default: INFO)
        format_string: Custom format string (optional)
        log_file: Path to log to, via a daily-rotating handler (see
            LOG_ROTATE_BACKUP_COUNT) -- replaces the StreamHandler(stdout)
            entrypoints used to rely on start.py redirecting to a file
            for them (which could never rotate, since rotation has to
            happen from inside the writing process). When omitted
            (standalone/interactive use), logs to stdout instead, un-rotated.
    """
    from src.core.log_context import ContextFormatter, StructuredContextFilter

    if format_string is None:
        format_string = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

    context_filter = StructuredContextFilter()
    formatter = ContextFormatter(format_string)

    if log_file:
        handlers = [
            logging.handlers.TimedRotatingFileHandler(
                log_file,
                when="midnight",
                backupCount=LOG_ROTATE_BACKUP_COUNT,
                encoding="utf-8",
            )
        ]
    else:
        handlers = [logging.StreamHandler(sys.stdout)]

    for handler in handlers:
        handler.addFilter(context_filter)
        handler.setFormatter(formatter)

    logging.basicConfig(
        level=level,
        format=format_string,
        handlers=handlers,
        force=True,  # Override any existing configuration
    )

    if log_file:
        # Uncaught exceptions otherwise go straight to the real stderr
        # (Python's default excepthook, bypassing logging entirely) --
        # with stdout/stderr no longer redirected to this same file by
        # start.py (see its _start_backend/_start_monitor/_start_watchdog),
        # that would make a crash after this point completely silent.
        def _log_uncaught_exception(exc_type, exc_value, exc_tb):
            if issubclass(exc_type, KeyboardInterrupt):
                sys.__excepthook__(exc_type, exc_value, exc_tb)
                return
            logging.getLogger(__name__).critical(
                "Uncaught exception", exc_info=(exc_type, exc_value, exc_tb)
            )

        sys.excepthook = _log_uncaught_exception
