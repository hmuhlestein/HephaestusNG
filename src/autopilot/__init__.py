"""
Autopilot Multi-Agent Pipeline

A continuous workflow engine that watches a design queue and processes
designs through the full 10-phase pipeline: product requirements,
architecture, development, adversarial review, doc review, security review,
QA, product validation, git commit, and forensics analysis.
"""

from src.autopilot.orchestrator import (
    DesignStatus,
    PipelineState,
    StopReason,
    run_continuous_pipeline,
)
from src.autopilot.phases import (
    AUTOPILOT_LAUNCH_TEMPLATE,
    AUTOPILOT_PHASES,
    AUTOPILOT_WORKFLOW_CONFIG,
)

__all__ = [
    "run_continuous_pipeline",
    "PipelineState",
    "StopReason",
    "DesignStatus",
    "AUTOPILOT_PHASES",
    "AUTOPILOT_WORKFLOW_CONFIG",
    "AUTOPILOT_LAUNCH_TEMPLATE",
]
