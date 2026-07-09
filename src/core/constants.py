"""Shared constants for Hephaestus."""

import os

# Home-dir state directories
AUTOPILOT_STATE_DIR = os.path.expanduser("~/.hephaestus/autopilot")
HEPHAESTUS_LOGS_DIR = os.path.expanduser("~/.hephaestus/logs")
HEPHAESTUS_PIDS_DIR = os.path.expanduser("~/.hephaestus/pids")

# Per-project context directory (git-excluded, lives inside each repo/worktree root)
CONTEXT_DIR_NAME = ".hephaestus"

# Worktrees subdirectory (lives inside each repo root)
WORKTREES_SUBDIR = ".worktrees"

# Marks a Task.raw_description as synthetic monitor-created diagnostic work
# (see src.monitoring.monitor._create_diagnostic_agent) that must never count
# toward phase/workflow completion checks -- an orphaned diagnostic task left
# "pending" after its agent died would otherwise permanently block whichever
# completion check didn't know to exclude it. Centralized here after this
# exact convention was independently duplicated as a raw string literal in
# three separate places (orchestrator.py, status_derivation.py).
DIAGNOSTIC_TASK_PREFIX = "DIAGNOSTIC:"

# Design document queue/inbox (relative to project root)
DESIGN_SUBDIR = "docs/design"
DESIGN_CONTEXT_SUBDIR = ".hephaestus/designs"

# Pipeline metrics filename
PIPELINE_METRICS_FILE = "pipeline_metrics.json"
