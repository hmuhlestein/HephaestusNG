"""Shared logging configuration for Hephaestus.

Use this module to configure logging consistently across all entrypoints.
This prevents the duplicated FileHandler bug that was fixed in run_server.py
and run_monitor.py (see L-2 in ARCHITECTURE_REVIEW.md).

Usage:
    from src.core.logging_config import configure_logging
    
    configure_logging(level=logging.INFO)
"""

import logging
import sys
from typing import Optional


def configure_logging(
    level: int = logging.INFO,
    format_string: Optional[str] = None,
    log_file: Optional[str] = None,
) -> None:
    """Configure logging for Hephaestus processes.
    
    This is the SINGLE place to configure logging for all entrypoints.
    Do NOT add FileHandler in individual run_*.py files - the process
    stdout is already redirected to log files by start.py.
    
    Args:
        level: Logging level (default: INFO)
        format_string: Custom format string (optional)
        log_file: Optional log file path (for standalone use, not when launched by start.py)
    """
    if format_string is None:
        format_string = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    handlers = [logging.StreamHandler(sys.stdout)]
    
    # Only add FileHandler if explicitly requested (standalone mode)
    # When launched by start.py, stdout is already redirected to log files
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    
    logging.basicConfig(
        level=level,
        format=format_string,
        handlers=handlers,
        force=True,  # Override any existing configuration
    )
