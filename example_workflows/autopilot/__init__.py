"""
Autopilot Multi-Agent Workflow

A fully automated pipeline that takes design documents and iterates through:
1. Product Requirements Extraction
2. Architecture & Design
3. Development
4. Adversarial Code Review
5. Security Review
6. QA Testing & Validation

The workflow loops until the original intent is satisfied or a hard stop condition is met.
"""

from example_workflows.autopilot.phases import (
    AUTOPILOT_PHASES,
    AUTOPILOT_WORKFLOW_CONFIG,
    AUTOPILOT_LAUNCH_TEMPLATE,
)

__all__ = ["AUTOPILOT_PHASES", "AUTOPILOT_WORKFLOW_CONFIG", "AUTOPILOT_LAUNCH_TEMPLATE"]
