"""
Hephaestus SDK - Python SDK for the Hephaestus AI agent orchestration system.

This SDK provides a programmatic interface to control Hephaestus, including:
- Starting backend and monitoring services
- Creating and managing tasks
- Defining workflow phases in Python or YAML
- Optional TUI for interactive control
- Headless operation for automation
"""

from src.sdk.client import HephaestusSDK
from src.sdk.config import HephaestusConfig
from src.sdk.exceptions import (
    HephaestusError,
    HephaestusStartupError,
    InvalidPhaseError,
    ProcessSpawnError,
    QdrantConnectionError,
    RestartError,
    SDKNotRunningError,
    TaskCreationError,
    TaskNotFoundError,
    TaskTimeoutError,
)
from src.sdk.models import (
    AgentStatus,
    Phase,
    TaskStatus,
    TaskUpdate,
    ValidationCriteria,
    WorkflowConfig,
    WorkflowResult,
)

__version__ = "0.1.0"

__all__ = [
    "HephaestusSDK",
    "HephaestusConfig",
    "Phase",
    "WorkflowConfig",
    "TaskStatus",
    "TaskUpdate",
    "WorkflowResult",
    "ValidationCriteria",
    "AgentStatus",
    "HephaestusError",
    "HephaestusStartupError",
    "SDKNotRunningError",
    "InvalidPhaseError",
    "TaskCreationError",
    "TaskNotFoundError",
    "TaskTimeoutError",
    "QdrantConnectionError",
    "ProcessSpawnError",
    "RestartError",
]
