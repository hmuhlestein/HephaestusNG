"""Shared constants for Hephaestus."""

import os
import subprocess
from pathlib import Path

# Home-dir state directories
AUTOPILOT_STATE_DIR = os.path.expanduser("~/.hephaestus/autopilot")
HEPHAESTUS_LOGS_DIR = os.path.expanduser("~/.hephaestus/logs")
HEPHAESTUS_PIDS_DIR = os.path.expanduser("~/.hephaestus/pids")

# Per-project context directory (git-excluded, lives inside each repo/worktree root)
CONTEXT_DIR_NAME = ".hephaestus"

# Worktrees subdirectory (lives inside each repo root)
WORKTREES_SUBDIR = ".worktrees"

# Marks a Task.raw_description as synthetic monitor-created diagnostic work
# (see src.monitoring.diagnostic_agent.WorkflowStuckDiagnostics.create_diagnostic_agent) that must never count
# toward phase/workflow completion checks -- an orphaned diagnostic task left
# "pending" after its agent died would otherwise permanently block whichever
# completion check didn't know to exclude it. Centralized here after this
# exact convention was independently duplicated as a raw string literal in
# three separate places (orchestrator.py, status_derivation.py).
DIAGNOSTIC_TASK_PREFIX = "DIAGNOSTIC:"

# Design document queue/inbox (relative to project root)
DESIGN_SUBDIR = "docs/spec"
# Default destination folder for the "Report Bug" flow's uploads -- see
# add_project_design's destination handling and DESIGN_SUBDIR's identical
# role for the "New Feature" flow.
BUGFIX_SUBDIR = "docs/bugfix"
DESIGN_CONTEXT_SUBDIR = ".hephaestus/designs"
DESIGN_QUEUE_FALLBACK_DIR = "docs/spec-queue"

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
# "bugfix" is the shorter, per-feature pipeline for AutopilotDesign.workflow_type
# == "bugfix" (see docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md and
# WORKFLOW_TYPE_DEFINITION_IDS below) -- included here so every status/repair/
# scoping check that treats DESIGN_WORKFLOW_DEFINITION_IDS as "any workflow
# belonging to a design's pipeline" (queue_routes, design_file_routes,
# control_routes, repair_service, engine_client, design_status_service) also
# recognizes a bugfix-typed feature's workflow, not just "autopilot" ones.
DESIGN_WORKFLOW_DEFINITION_IDS = ("autopilot", "bugfix") + PHASE0_DEFINITION_IDS

# AutopilotDesign.workflow_type / Feature.workflow_type -> the workflow
# definition_id that type actually launches. "feature" keeps mapping to the
# pre-existing "autopilot" id (not renamed) so every other place matching on
# definition_id == "autopilot" doesn't need to change.
WORKFLOW_TYPE_DEFINITION_IDS = {"feature": "autopilot", "bugfix": "bugfix"}


def _resolve_install_dir(anchor: "Path | None" = None) -> Path:
    """The one true Hephaestus installation directory -- never a worktree's.

    anchor: directory to run `git rev-parse` from. Defaults to this file's
    own directory; overridable so tests can point it at a throwaway repo
    without faking __file__.

    HephaestusNG is self-hosting: its own autopilot runs agents against
    copies of itself checked out under .worktrees/, each a full git
    worktree with its own src/core/constants.py. A plain
    Path(__file__).parent-chain -- what every former call site of this
    (start.py, config.py, init.py, project.py, pipeline.py) computed
    independently -- resolves to whichever COPY of the source tree is
    actually executing. Invoked from inside a worktree, that's the
    worktree itself: `heph start` launched that way spawns the backend
    with a worktree cwd, and since DatabaseManager's default db path
    ("hephaestus.db") is a bare relative path resolved against process
    cwd, the backend then reads/writes an empty per-worktree database
    instead of the real one -- confirmed live via a worktree copy whose
    hephaestus.db had the full schema (freshly migrated) but zero rows,
    while the /autopilot page it was serving showed no projects at all.

    All worktrees of one repo share a single underlying object/ref store,
    and `git rev-parse --git-common-dir` resolves it from ANY worktree
    back to the one true repo's .git -- unlike --show-toplevel, which
    (correctly, for other purposes) returns the worktree's own path. That
    makes it the right anchor regardless of which copy of this file is
    executing or what the caller's cwd is.
    """
    file_dir = anchor if anchor is not None else Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=file_dir,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        # --git-common-dir is always the ONE shared ".git" (unlike
        # --git-dir, which differs per linked worktree) -- its parent is
        # the main repo root, from any worktree.
        git_common_dir = Path(result.stdout.strip())
        return git_common_dir.parent
    except Exception:
        # Not a git checkout at all (e.g. a packaged/pip-installed
        # deployment with no .git present) -- fall back to the old
        # __file__-relative guess, matching every prior call site's
        # pre-existing behavior in that case.
        return file_dir.parent.parent


HEPHAESTUS_INSTALL_DIR = _resolve_install_dir()
