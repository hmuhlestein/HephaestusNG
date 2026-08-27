"""
Autopilot Orchestrator

A continuous multi-agent workflow engine that:
1. Watches a design queue directory for new design documents
2. Picks the next logical design to process
3. Runs the full pipeline: product → architect → developer → review → security → QA → product validation
4. Generates an HTML feature report for human review
5. Repeats until stopped or queue is empty

This __init__ is a re-export surface only -- the pipeline-execution flow
(run_single_workflow, run_continuous_pipeline, etc.) lives in pipeline.py,
the small stateful registries live in runtime_registries.py, and the rest
is split across this package's other modules (config.py, policy.py,
engine_client.py, phase_transitions.py, arbitration.py, ...). Every name
below is re-exported at the package level for callers that predate that
split (`from src.autopilot.orchestrator import X`); new call sites within
this package should import directly from the owning submodule instead.
"""
import asyncio
import copy
import json
import logging
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, NamedTuple, Optional, Tuple

from src.autopilot.orchestrator.agent_registration import _register_orchestrator_agent
from src.autopilot.orchestrator.config import (
    _get_paused_workflow_max_retry_cycles as _get_paused_workflow_max_retry_cycles,
)
from src.autopilot.orchestrator.config import (
    _get_paused_workflow_retry_cooldown_seconds as _get_paused_workflow_retry_cooldown_seconds,
)
from src.autopilot.orchestrator.config import _get_phase0_timeout, _get_workflow_timeout
from src.autopilot.orchestrator.engine_client import (
    _update_orchestrator_status,
    get_active_workflows,
    get_agents,
    get_tasks,
    get_workflow_status,
    pause_workflow_direct,
    peek_agent_output,
    terminate_agent_direct,
)
from src.autopilot.orchestrator.features import (
    _clean_stale_assigned_tasks,
    _create_feature_records,
    _relink_features_to_workflows,
    _resolve_execution_order,
    _update_feature_status,
    _validate_features_json,
)
from src.autopilot.orchestrator.human_escalation import prompt_human
from src.autopilot.orchestrator.phase_transitions import POLL_INTERVAL, _negotiate_validation_fix, _resume_stuck_workflow_tasks, _try_advance_phases
from src.autopilot.orchestrator.pipeline import (
    DESIGN_QUEUE_SCAN_INTERVAL as DESIGN_QUEUE_SCAN_INTERVAL,
)
from src.autopilot.orchestrator.pipeline import (
    HEARTBEAT_INTERVAL as HEARTBEAT_INTERVAL,
)

# The package's own pipeline-execution flow -- see pipeline.py's docstring.
from src.autopilot.orchestrator.pipeline import (
    HEPHAESTUS_DIR as HEPHAESTUS_DIR,
)
from src.autopilot.orchestrator.pipeline import (
    MAX_PARALLEL_FEATURES as MAX_PARALLEL_FEATURES,
)
from src.autopilot.orchestrator.pipeline import (
    PARENT_PEEK_INTERVAL as PARENT_PEEK_INTERVAL,
)
from src.autopilot.orchestrator.pipeline import (
    STUCK_THRESHOLD as STUCK_THRESHOLD,
)
from src.autopilot.orchestrator.pipeline import (
    OrchestratorLogger as OrchestratorLogger,
)
from src.autopilot.orchestrator.pipeline import (
    _build_and_start_pipeline_sdk as _build_and_start_pipeline_sdk,
)
from src.autopilot.orchestrator.pipeline import (
    _has_unfinished_phases as _has_unfinished_phases,
)
from src.autopilot.orchestrator.pipeline import (
    _log_agent_state_changes as _log_agent_state_changes,
)
from src.autopilot.orchestrator.pipeline import (
    _merge_design_branch_into_main as _merge_design_branch_into_main,
)
from src.autopilot.orchestrator.pipeline import (
    _pause_feature_for_review as _pause_feature_for_review,
)
from src.autopilot.orchestrator.pipeline import (
    _pause_phase0_for_review as _pause_phase0_for_review,
)
from src.autopilot.orchestrator.pipeline import (
    _peek_active_agent_output as _peek_active_agent_output,
)
from src.autopilot.orchestrator.pipeline import (
    _persist_design_outcome as _persist_design_outcome,
)
from src.autopilot.orchestrator.pipeline import (
    _restore_phase0_completed_status as _restore_phase0_completed_status,
)
from src.autopilot.orchestrator.pipeline import (
    _resync_pipeline_registry as _resync_pipeline_registry,
)
from src.autopilot.orchestrator.pipeline import (
    _run_one_feature as _run_one_feature,
)
from src.autopilot.orchestrator.pipeline import (
    _should_pause_for_review as _should_pause_for_review,
)
from src.autopilot.orchestrator.pipeline import (
    _shutdown_pipeline as _shutdown_pipeline,
)
from src.autopilot.orchestrator.pipeline import (
    _snapshot_workflow_activity as _snapshot_workflow_activity,
)
from src.autopilot.orchestrator.pipeline import (
    _wait_for_pending_reviews as _wait_for_pending_reviews,
)
from src.autopilot.orchestrator.pipeline import (
    _wait_for_phase0_review_clearance as _wait_for_phase0_review_clearance,
)
from src.autopilot.orchestrator.pipeline import (
    _wait_for_review_clearance as _wait_for_review_clearance,
)
from src.autopilot.orchestrator.pipeline import (
    _WorkflowActivity as _WorkflowActivity,
)
from src.autopilot.orchestrator.pipeline import (
    finalize_phase0_workflow as finalize_phase0_workflow,
)
from src.autopilot.orchestrator.pipeline import (
    main as main,
)
from src.autopilot.orchestrator.pipeline import (
    run_bugfix_single_feature as run_bugfix_single_feature,
)
from src.autopilot.orchestrator.pipeline import (
    run_continuous_pipeline as run_continuous_pipeline,
)
from src.autopilot.orchestrator.pipeline import (
    run_design_aggregate as run_design_aggregate,
)
from src.autopilot.orchestrator.pipeline import (
    run_feature_pipelines as run_feature_pipelines,
)
from src.autopilot.orchestrator.pipeline import (
    run_phase0 as run_phase0,
)
from src.autopilot.orchestrator.pipeline import (
    run_single_design as run_single_design,
)
from src.autopilot.orchestrator.pipeline import (
    run_single_workflow as run_single_workflow,
)
from src.autopilot.orchestrator.policy import (
    ACTIVE_AGENT_STATUSES,
    _escalate_stale_active_workflows,
    _update_resumed_workflow_recovery_attempts,
    attempt_recovery,
    check_api_credits,
    detect_hard_error,
    detect_impasse,
)
from src.autopilot.orchestrator.queue import _get_phase0_completion, _has_resumable_active_design, _set_workflow_type, _update_design_status, is_design_fully_complete, pick_next_design
from src.autopilot.orchestrator.reporting import _empty_report, _generate_design_report_html
from src.autopilot.orchestrator.runtime_registries import (
    _get_orchestrator_agent_id,
    _interruptible_sleep,
    _orchestrator_agent_ids,
    _register_monitored_workflow,
    _should_stop,
    _unregister_monitored_workflow,
)
from src.autopilot.orchestrator.runtime_registries import (
    _is_workflow_monitored as _is_workflow_monitored,
)
from src.autopilot.orchestrator.runtime_registries import _stop_events as _stop_events
from src.autopilot.orchestrator.state import (
    DesignEntry,
    DesignStatus,
    FeatureReport,
    FeatureRunStatus,
    PersistentPipelineState,
    PipelineState,
    StopReason,
    _delete_project_context,
    _workflow_belongs_to_project,
)
from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree, _create_designs_folder, _create_integration_worktree
from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME, DESIGN_CONTEXT_SUBDIR, PHASE0_DEFINITION_IDS
from src.core.database import DatabaseManager, Workflow, get_db
from src.core.simple_config import get_config

