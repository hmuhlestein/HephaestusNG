"""
Autopilot Multi-Agent Pipeline

A continuous workflow engine that watches a design queue and processes
designs through the full 8-phase pipeline: product requirements,
architecture, development, adversarial review, security review, QA,
product validation, and git commit.
"""

from example_workflows.autopilot.phases import (
    AUTOPILOT_PHASES,
    AUTOPILOT_WORKFLOW_CONFIG,
    AUTOPILOT_LAUNCH_TEMPLATE,
)

__all__ = ["AUTOPILOT_PHASES", "AUTOPILOT_WORKFLOW_CONFIG", "AUTOPILOT_LAUNCH_TEMPLATE"]
