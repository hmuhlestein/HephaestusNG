"""
Autopilot Multi-Agent Pipeline

A continuous workflow engine that watches a design queue and processes
designs through the full pipeline: product → architect → developer →
review → security → QA → product validation.
"""

from src.autopilot.orchestrator import run_continuous_pipeline, PipelineState, StopReason, DesignStatus

__all__ = ["run_continuous_pipeline", "PipelineState", "StopReason", "DesignStatus"]
