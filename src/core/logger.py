"""Centralized logging utilities for Hephaestus components."""

import logging
from typing import Any


class SimpleLogger:
    """Minimal logger that wraps Python's logging module.
    
    Accepts string levels ("INFO", "ERROR", "WARN") like OrchestratorLogger.
    Use this when passing a logger to functions that expect the OrchestratorLogger interface.
    """
    
    def __init__(self, name: str = __name__):
        self._logger = logging.getLogger(name)

    def log(self, message: str, level: str = "INFO") -> None:
        level_map = {
            "DEBUG": 10, "INFO": 20, "WARN": 30,
            "WARNING": 30, "ERROR": 40,
        }
        lvl = level_map.get(str(level).upper(), 20) if isinstance(level, str) else level
        self._logger.log(lvl, message)

    def event(self, name: str, data: dict) -> None:
        pass

    def save_state(self, state: Any) -> None:
        pass
