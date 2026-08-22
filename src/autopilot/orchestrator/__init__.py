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
from src.autopilot.orchestrator.policy import ACTIVE_AGENT_STATUSES
import json
import shutil
import sys
from src.core.constants import AUTOPILOT_STATE_DIR
from typing import Any
from src.core.constants import CONTEXT_DIR_NAME
from src.core.constants import DESIGN_CONTEXT_SUBDIR
from src.core.database import DatabaseManager
from src.core.database import Workflow
from src.core.database import get_db
from src.core.simple_config import get_config
from src.autopilot.orchestrator.state import DesignEntry
from src.autopilot.orchestrator.state import DesignStatus
from typing import Dict
from typing import NamedTuple
from src.autopilot.orchestrator.state import FeatureReport
from src.autopilot.orchestrator.state import FeatureRunStatus
from typing import Optional
from src.core.constants import PHASE0_DEFINITION_IDS
from src.autopilot.orchestrator.phase_transitions import POLL_INTERVAL
from pathlib import Path
from src.autopilot.orchestrator.state import PersistentPipelineState
from src.autopilot.orchestrator.state import PipelineState
from src.autopilot.orchestrator.state import StopReason
from typing import Tuple
from src.autopilot.orchestrator.features import _clean_stale_assigned_tasks
from src.autopilot.orchestrator.worktree_integration import _cleanup_worktree
from src.autopilot.orchestrator.worktree_integration import _create_designs_folder
from src.autopilot.orchestrator.features import _create_feature_records
from src.autopilot.orchestrator.worktree_integration import _create_integration_worktree
from src.autopilot.orchestrator.state import _delete_project_context
from src.autopilot.orchestrator.reporting import _empty_report
from src.autopilot.orchestrator.policy import _escalate_stale_active_workflows
from src.autopilot.orchestrator.reporting import _generate_design_report_html
from src.autopilot.orchestrator.config import (
    _get_paused_workflow_max_retry_cycles as _get_paused_workflow_max_retry_cycles,
)
from src.autopilot.orchestrator.config import (
    _get_paused_workflow_retry_cooldown_seconds as _get_paused_workflow_retry_cooldown_seconds,
)
from src.autopilot.orchestrator.queue import _get_phase0_completion
from src.autopilot.orchestrator.config import _get_phase0_timeout
from src.autopilot.orchestrator.config import _get_workflow_timeout
from src.autopilot.orchestrator.queue import _has_resumable_active_design
from src.autopilot.orchestrator.runtime_registries import _interruptible_sleep
from src.autopilot.orchestrator.runtime_registries import (
    _is_workflow_monitored as _is_workflow_monitored,
)
from src.autopilot.orchestrator.phase_transitions import _negotiate_validation_fix
from src.autopilot.orchestrator.runtime_registries import _get_orchestrator_agent_id
from src.autopilot.orchestrator.runtime_registries import _orchestrator_agent_ids
from src.autopilot.orchestrator.runtime_registries import _register_monitored_workflow
from src.autopilot.orchestrator.agent_registration import _register_orchestrator_agent
from src.autopilot.orchestrator.features import _relink_features_to_workflows
from src.autopilot.orchestrator.features import _resolve_execution_order
from src.autopilot.orchestrator.phase_transitions import _resume_stuck_workflow_tasks
from src.autopilot.orchestrator.queue import _set_workflow_type
from src.autopilot.orchestrator.runtime_registries import _should_stop
from src.autopilot.orchestrator.runtime_registries import _stop_events as _stop_events
from src.autopilot.orchestrator.phase_transitions import _try_advance_phases
from src.autopilot.orchestrator.runtime_registries import _unregister_monitored_workflow
from src.autopilot.orchestrator.queue import _update_design_status
from src.autopilot.orchestrator.features import _update_feature_status
from src.autopilot.orchestrator.engine_client import _update_orchestrator_status
from src.autopilot.orchestrator.policy import _update_resumed_workflow_recovery_attempts
from src.autopilot.orchestrator.features import _validate_features_json
from src.autopilot.orchestrator.state import _workflow_belongs_to_project
import asyncio
from src.autopilot.orchestrator.policy import attempt_recovery
from src.autopilot.orchestrator.policy import check_api_credits
import copy
from datetime import datetime
from src.autopilot.orchestrator.policy import detect_hard_error
from src.autopilot.orchestrator.policy import detect_impasse
from src.autopilot.orchestrator.engine_client import get_active_workflows
from src.autopilot.orchestrator.engine_client import get_agents
from src.autopilot.orchestrator.engine_client import get_tasks
from src.autopilot.orchestrator.engine_client import get_workflow_status
from src.autopilot.orchestrator.queue import is_design_fully_complete
import logging
import os
from src.autopilot.orchestrator.engine_client import pause_workflow_direct
from src.autopilot.orchestrator.engine_client import peek_agent_output
from src.autopilot.orchestrator.queue import pick_next_design
from src.autopilot.orchestrator.human_escalation import prompt_human
from src.autopilot.orchestrator.engine_client import terminate_agent_direct
import threading
import time

# The package's own pipeline-execution flow -- see pipeline.py's docstring.
from src.autopilot.orchestrator.pipeline import (
    HEPHAESTUS_DIR as HEPHAESTUS_DIR,
    STUCK_THRESHOLD as STUCK_THRESHOLD,
    DESIGN_QUEUE_SCAN_INTERVAL as DESIGN_QUEUE_SCAN_INTERVAL,
    HEARTBEAT_INTERVAL as HEARTBEAT_INTERVAL,
    PARENT_PEEK_INTERVAL as PARENT_PEEK_INTERVAL,
    MAX_PARALLEL_FEATURES as MAX_PARALLEL_FEATURES,
    OrchestratorLogger as OrchestratorLogger,
    _resync_pipeline_registry as _resync_pipeline_registry,
    _WorkflowActivity as _WorkflowActivity,
    _snapshot_workflow_activity as _snapshot_workflow_activity,
    _log_agent_state_changes as _log_agent_state_changes,
    _peek_active_agent_output as _peek_active_agent_output,
    _has_unfinished_phases as _has_unfinished_phases,
    _merge_design_branch_into_main as _merge_design_branch_into_main,
    run_single_workflow as run_single_workflow,
    run_phase0 as run_phase0,
    run_bugfix_single_feature as run_bugfix_single_feature,
    _should_pause_for_review as _should_pause_for_review,
    _pause_feature_for_review as _pause_feature_for_review,
    _wait_for_review_clearance as _wait_for_review_clearance,
    _restore_phase0_completed_status as _restore_phase0_completed_status,
    _pause_phase0_for_review as _pause_phase0_for_review,
    _wait_for_phase0_review_clearance as _wait_for_phase0_review_clearance,
    finalize_phase0_workflow as finalize_phase0_workflow,
    _wait_for_pending_reviews as _wait_for_pending_reviews,
    _run_one_feature as _run_one_feature,
    run_feature_pipelines as run_feature_pipelines,
    run_design_aggregate as run_design_aggregate,
    run_single_design as run_single_design,
    _build_and_start_pipeline_sdk as _build_and_start_pipeline_sdk,
    _persist_design_outcome as _persist_design_outcome,
    _shutdown_pipeline as _shutdown_pipeline,
    run_continuous_pipeline as run_continuous_pipeline,
    main as main,
)

