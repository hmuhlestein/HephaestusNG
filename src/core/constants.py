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

# Prefix _create_phase_task (orchestrator.py) embeds in a goto/retry task's
# description ahead of the gate's actual finding -- centralized here so the
# one place that needs to strip it back out for display (autopilot_api.py's
# get_project_design_status) doesn't hardcode a second, driftable copy of
# the exact same label text.
GOTO_REASON_PREFIX = "WHY YOU'RE HERE: "

# Workflow definition IDs for design pipelines
# "autopilot-phase0" is the pre-rename Phase 0 definition_id
# "feature_architect" is the current Phase 0 definition_id
PHASE0_DEFINITION_IDS = ("autopilot-phase0", "feature_architect")
DESIGN_WORKFLOW_DEFINITION_IDS = ("autopilot",) + PHASE0_DEFINITION_IDS
