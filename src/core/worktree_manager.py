"""Backward compatibility shim — use branch_manager instead."""
from src.core.branch_manager import BranchManager as WorktreeManager  # noqa: F401
from src.core.branch_manager import BranchManager  # noqa: F401

__all__ = ["WorktreeManager", "BranchManager"]
