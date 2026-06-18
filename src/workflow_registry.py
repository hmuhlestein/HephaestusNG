"""Centralized workflow registry.

Workflow definitions registered here are loaded by the MCP server
on startup and inserted into the database. Only autopilot is active;
example workflows in example_workflows/ are kept as reference but
not registered.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.sdk.models import WorkflowDefinition

# Autopilot workflow
from src.autopilot.phases import AUTOPILOT_PHASES, AUTOPILOT_WORKFLOW_CONFIG, AUTOPILOT_LAUNCH_TEMPLATE, AUTOPILOT_ORCHESTRATOR_CONFIG


def get_all_workflow_definitions() -> list:
    """Return all workflow definitions for registration.

    Only autopilot is registered. Example workflows in example_workflows/
    are kept as reference but not loaded into the system.
    """
    return [
        WorkflowDefinition(
            id="autopilot",
            name="Autopilot Pipeline",
            phases=AUTOPILOT_PHASES,
            orchestrator_config=AUTOPILOT_ORCHESTRATOR_CONFIG,
            config=AUTOPILOT_WORKFLOW_CONFIG,
            description="10-phase automated pipeline: requirements, architecture, development, review, doc review, security, QA, validation, git, forensics",
            launch_template=AUTOPILOT_LAUNCH_TEMPLATE,
        ),
    ]
