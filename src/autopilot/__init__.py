"""
Autopilot Multi-Agent Pipeline

A continuous workflow engine that watches a design queue and processes
designs through the full 9-phase pipeline: product requirements,
architecture, development, adversarial review, security review, QA,
product validation, git commit, and forensics analysis.
"""

from src.autopilot.orchestrator import run_continuous_pipeline, PipelineState, StopReason, DesignStatus
from src.autopilot.phases import AUTOPILOT_PHASES, AUTOPILOT_WORKFLOW_CONFIG, AUTOPILOT_LAUNCH_TEMPLATE

__all__ = [
    "run_continuous_pipeline", "PipelineState", "StopReason", "DesignStatus",
    "AUTOPILOT_PHASES", "AUTOPILOT_WORKFLOW_CONFIG", "AUTOPILOT_LAUNCH_TEMPLATE",
]
