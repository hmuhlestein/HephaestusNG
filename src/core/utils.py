"""Shared utility functions for the Hephaestus codebase.

FIX #9: Centralizes shared heuristics to prevent silent drift
between modules.
"""

import logging

logger = logging.getLogger(__name__)


def is_glm_model(model: str) -> bool:
    """Check if a model name refers to a GLM model.

    FIX #9: Shared by agents/manager.py and interfaces/cli_interface.py
    to prevent the GLM-detection heuristic from drifting out of sync.

    Args:
        model: Model name string (e.g., "GLM-4.6", "sonnet", "opus")

    Returns:
        True if the model is a GLM model, False otherwise.
    """
    return "GLM" in (model or "").upper()
