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

# Design document queue/inbox (relative to project root)
DESIGN_SUBDIR = "docs/design"

# Pipeline metrics filename
PIPELINE_METRICS_FILE = "pipeline_metrics.json"
