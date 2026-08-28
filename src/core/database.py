"""Database models and schema for Hephaestus."""

import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import backref, relationship, sessionmaker
from sqlalchemy.pool import QueuePool, StaticPool

Base = declarative_base()
logger = logging.getLogger(__name__)


# Status enums - use these instead of string literals for type safety (L-1)
class AgentStatus:
    """Valid agent status values."""

    IDLE = "idle"
    WORKING = "working"
    STUCK = "stuck"
    TERMINATED = "terminated"
    STARTING = "starting"  # Initial state before tmux session confirmed

    ALL = {IDLE, WORKING, STUCK, TERMINATED, STARTING}


class TaskStatus:
    """Valid task status values."""

    PENDING = "pending"
    QUEUED = "queued"
    BLOCKED = "blocked"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    UNDER_REVIEW = "under_review"
    VALIDATION_IN_PROGRESS = "validation_in_progress"
    NEEDS_WORK = "needs_work"
    DONE = "done"
    FAILED = "failed"
    DUPLICATED = "duplicated"

    ALL = {PENDING, QUEUED, BLOCKED, ASSIGNED, IN_PROGRESS, UNDER_REVIEW, VALIDATION_IN_PROGRESS, NEEDS_WORK, DONE, FAILED, DUPLICATED}

    # Terminal states (no further transitions expected)
    TERMINAL = {DONE, FAILED, DUPLICATED}

    # Active states (work in progress)
    ACTIVE = {ASSIGNED, IN_PROGRESS, UNDER_REVIEW, VALIDATION_IN_PROGRESS, NEEDS_WORK}


class WorkflowStatus:
    """Valid workflow status values."""

    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"

    ALL = {ACTIVE, PAUSED, COMPLETED, FAILED}


class FeatureStatus:
    """Valid feature status values."""

    PENDING = "pending"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    VALIDATED = "validated"

    ALL = {PENDING, ACTIVE, PAUSED, COMPLETED, FAILED, SKIPPED, VALIDATED}


class PhaseExecutionStatus:
    """Valid phase execution status values."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"

    ALL = {PENDING, IN_PROGRESS, COMPLETED, FAILED, SKIPPED}


class DesignStatus:
    """Valid autopilot design status values."""

    PENDING = "pending"
    QUEUED = "queued"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    VALIDATED = "validated"

    ALL = {PENDING, QUEUED, ACTIVE, PAUSED, COMPLETED, FAILED, VALIDATED}


class Agent(Base):
    """Agent model representing an AI agent instance."""

    __tablename__ = "agents"

    id = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    system_prompt = Column(Text, nullable=False)
    status = Column(
        String,
        CheckConstraint("status IN ('idle', 'working', 'stuck', 'terminated', 'starting')"),
        default="idle",
        nullable=False,
    )
    cli_type = Column(String, nullable=False)  # claude, codex, etc.
    tmux_session_name = Column(String, unique=True)
    # Must be set to None on every path that sets status="terminated" (see
    # CLAUDE.md's Critical Invariants / Forbidden lists). This column means
    # "the task this LIVE agent is currently working on" -- a dozen+ call
    # sites across the codebase query it (Agent.current_task_id.in_(...),
    # == task_id) to find the agent associated with a task, for bulk
    # termination sweeps, duplicate-agent prevention, inter-agent message
    # routing, and self-heal/retry detection, and most of them don't
    # separately filter status != "terminated". Leaving a stale value here
    # after termination makes a dead agent indistinguishable from a live
    # one to all of them -- a task can appear to already have an agent
    # (a stale pointer to a corpse) and never get picked up by retry
    # logic, stalling silently.
    #
    # To verify what task a TERMINATED agent was assigned to (e.g. an
    # authorization check for a terminated-but-still-reporting tmux
    # session), don't repurpose this column -- query AgentLog instead
    # (log_type="created", details["task_id"]), which is exactly what
    # src/mcp/server.py's update_task_status tertiary check already does.
    # That mechanism predates and is unrelated to this column's clearing;
    # it works precisely because it doesn't depend on current_task_id
    # surviving termination.
    current_task_id = Column(String, ForeignKey("tasks.id"))
    # The worktree/project directory this agent's tmux session runs in.
    # Set ONCE at creation (from the resolved worktree path) and never
    # cleared or reassigned afterward -- unlike current_task_id, tracing an
    # agent back to its own working directory must keep working after
    # termination (get_agent_output/_resolve_tmux_transcript_dir need it to
    # find .hephaestus/tmux/). Reading it straight off this column avoids
    # having to rederive it via task->workflow.working_directory, which
    # breaks the moment current_task_id is cleared.
    working_directory = Column(String)
    last_activity = Column(DateTime, default=datetime.utcnow)
    # Stamped at the start of the CURRENT attempt -- both
    # create_agent_for_task and restart_agent set this to datetime.utcnow()
    # at launch, unlike created_at (fixed at first creation, never touched
    # by a restart) or last_activity (also stamped at launch, but then
    # overwritten by real progress too -- so it alone can't distinguish
    # "just launched, zero progress yet" from "made progress a while ago").
    # monitor.py's _detect_agent_never_started compares last_activity
    # against THIS field specifically so a restarted agent that hangs
    # again is still caught -- comparing against created_at would always
    # see restarted agents as "already had activity" since created_at
    # predates every restart.
    launched_at = Column(DateTime, nullable=True)
    health_check_failures = Column(Integer, default=0)
    restart_count = Column(Integer, default=0)  # Tracks restart attempts
    cli_model = Column(String, nullable=True)  # Per-agent model override
    # Set by AgentMessenger.send_message_to_agent whenever a message is
    # delivered, cleared once Terminator's grace-period wait consumes it.
    # NOT last_activity: that column is also overwritten by ordinary agent
    # progress unrelated to messaging, so it can't distinguish "a message
    # is sitting unaddressed" from "the agent just did something." Exists
    # so terminate_agent can give a genuinely just-messaged agent a short
    # window to notice before its tmux session is killed out from under
    # it -- see Terminator._terminate_agent_sync's grace-period check.
    pending_message_sent_at = Column(DateTime, nullable=True)

    # Validation-related fields
    agent_type = Column(
        String,
        CheckConstraint("agent_type IN ('phase', 'validator', 'result_validator', 'monitor', 'diagnostic', 'orchestrator')"),
        default="phase",
        nullable=False,
    )
    kept_alive_for_validation = Column(Boolean, default=False)
    terminated_at = Column(DateTime, nullable=True)  # When agent was terminated

    # Relationships
    created_tasks = relationship(
        "Task",
        back_populates="created_by_agent",
        foreign_keys="Task.created_by_agent_id",
    )
    assigned_tasks = relationship("Task", foreign_keys="Task.assigned_agent_id")
    memories = relationship("Memory", back_populates="agent")
    logs = relationship("AgentLog", back_populates="agent")


class Task(Base):
    """Task model representing work to be done."""

    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    raw_description = Column(Text, nullable=False)
    enriched_description = Column(Text)
    done_definition = Column(Text, nullable=False)
    status = Column(
        String,
        CheckConstraint("status IN ('pending', 'queued', 'blocked', 'assigned', 'in_progress', 'under_review', 'validation_in_progress', 'needs_work', 'done', 'failed', 'duplicated')"),
        default="pending",
        nullable=False,
    )
    priority = Column(
        String,
        CheckConstraint("priority IN ('low', 'medium', 'high')"),
        default="medium",
    )
    assigned_agent_id = Column(String, ForeignKey("agents.id"))
    parent_task_id = Column(String, ForeignKey("tasks.id"))
    created_by_agent_id = Column(String, ForeignKey("agents.id"))
    phase_id = Column(String, ForeignKey("phases.id"))  # Phase this task belongs to
    workflow_id = Column(String, ForeignKey("workflows.id"))  # Workflow this task is part of
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    completion_notes = Column(Text)
    failure_reason = Column(Text)
    estimated_complexity = Column(Integer)
    action = Column(String, default="")  # Engine action: 'continue', 'retry', 'goto'
    # Phase name the engine returned to/retried, when action is 'goto' or
    # 'retry' -- set alongside action by PhaseManager.mark_phase_complete's
    # single choke point, so the frontend can show WHICH phase (and, via
    # SESSION_ROLES, which agent) a goto is actually returning to instead
    # of a bare, contextless "goto" badge.
    action_target_phase = Column(String)

    # Persisted count of orchestrator.attempt_recovery's automatic retries.
    # attempt_recovery previously read this from the get_tasks() dict, which
    # never included it (no such column existed) -- task.get("retry_count", 0)
    # silently always returned 0, so the "skip retry after 2 attempts" guard
    # never engaged and a permanently-broken task (e.g. its worktree deleted
    # out from under it) retried forever, every ~60s, indefinitely.
    retry_count = Column(Integer, default=0, nullable=False)

    # Validation-related fields
    review_done = Column(Boolean, default=False)
    validation_enabled = Column(Boolean, default=False)
    validation_iteration = Column(Integer, default=0)
    last_validation_feedback = Column(Text)

    # One-shot self-review (see docs/GAP_CHECK_SELF_LOOP_DESIGN.md). Distinct
    # from review_done above, which marks a *separate validator's* approval —
    # this marks whether the same agent has already been sent the self-review
    # checklist once. Set True BEFORE messaging the agent (not after), so a
    # crash between send and commit can't re-trigger the prompt.
    self_review_done = Column(Boolean, default=False, nullable=False)
    # Telemetry only (see design doc "Telemetry" section): when self-review
    # fired and the worktree HEAD at that moment, so the second "done" call
    # can log elapsed time + a diff-stat of what actually changed during the
    # review pass -- the signal for whether one pass is worth the extra turn.
    self_review_started_at = Column(DateTime)
    self_review_started_commit = Column(String)

    # Results tracking
    has_results = Column(Boolean, default=False)

    # Task deduplication fields
    embedding = Column(JSON)  # Store embedding vector as list of floats
    related_task_ids = Column(JSON)  # List of related task IDs
    duplicate_of_task_id = Column(String, ForeignKey("tasks.id"))
    similarity_score = Column(Float)  # Similarity score to duplicate_of task

    # Queue management fields
    queued_at = Column(DateTime)  # When task was queued
    queue_position = Column(Integer)  # Position in queue (for UI display)
    priority_boosted = Column(Boolean, default=False)  # If manually boosted to bypass queue

    # Task dependency and concurrency fields
    depends_on = Column(JSON)  # List of task IDs that must complete before this one
    parallel_group = Column(String)  # Tasks in same group can run in parallel; different groups are sequential
    max_concurrent = Column(Integer, default=1)  # Max agents working on this task simultaneously

    # Ticket tracking integration
    ticket_id = Column(String, ForeignKey("tickets.id"))  # Associated ticket (required when ticket tracking enabled)
    related_ticket_ids = Column(JSON)  # List of related ticket IDs for context
    repo_id = Column(String, ForeignKey("project_repos.id"))  # ProjectRepo this task is scoped to (multi-repo projects)

    # Cost tracking - denormalized rollup (self-healed by cost_derivation.py)
    cost_total_usd = Column(Float, default=0.0, nullable=False)

    # Set only for a phase with a registered pre-dispatch blocking step
    # (PRE_DISPATCH_BLOCKING_STEPS in launch_pipeline.py, e.g.
    # security_review's mandatory ash scan) -- the agent/tmux session and
    # this Task row already exist and are genuinely alive at this point,
    # but the agent's first real prompt is deliberately held back until
    # that blocking step finishes. Every stuck/orphan/idle detector that
    # judges elapsed time since Task.created_at or Agent.launched_at (see
    # _create_phase_task's own orphan check,
    # _mark_orphaned_and_stale_pending_tasks_failed,
    # _resume_stuck_workflow_tasks, and mechanical_recovery.py's
    # detect_agent_never_started) must treat "now < dispatch_grace_until"
    # as "not stuck yet, this delay is expected" instead of judging
    # elapsed time against their own, shorter defaults -- without this,
    # a legitimately slow blocking step reads identically to genuine
    # staleness and gets the agent killed / task marked orphaned mid-step.
    dispatch_grace_until = Column(DateTime, nullable=True)

    # Relationships
    assigned_agent = relationship("Agent", foreign_keys=[assigned_agent_id])
    duplicate_of = relationship("Task", remote_side=[id], foreign_keys=[duplicate_of_task_id], post_update=True)
    parent_task = relationship("Task", remote_side=[id], foreign_keys=[parent_task_id], backref="subtasks")
    created_by_agent = relationship("Agent", back_populates="created_tasks", foreign_keys=[created_by_agent_id])
    memories = relationship("Memory", back_populates="task")
    phase = relationship("Phase", back_populates="tasks")
    workflow = relationship("Workflow", backref="tasks")
    results = relationship("AgentResult", back_populates="task")
    ticket = relationship("Ticket", foreign_keys=[ticket_id], backref="related_tasks")


@event.listens_for(Task, "after_insert")
def _log_task_insert(mapper, connection, target):
    """Diagnostic: log every Task row's creation site (id/phase_id/
    workflow_id + the calling src/ frames), to catch a phase-1 task-
    duplication race whose actual creator doesn't go through any of the
    logged creation paths (_create_phase_task, agent_task_routes.create_task)
    -- observed live, workflow e9019930's product_requirements phase got a
    second task 16s after the first with no matching log line from either
    known creator. Remove once that creator is identified."""
    import traceback

    frames = [
        f"{Path(f.filename).name}:{f.lineno}:{f.name}"
        for f in traceback.extract_stack()
        if "src/" in f.filename or "src\\" in f.filename
    ]
    logger.info(
        f"[TASK-CREATED] {target.id[:8]} phase_id={(target.phase_id or '')[:8]} "
        f"workflow_id={(target.workflow_id or '')[:8]} status={target.status} "
        f"via: {' <- '.join(reversed(frames[-6:]))}"
    )


class Memory(Base):
    """Memory model for storing agent discoveries and learnings."""

    __tablename__ = "memories"

    id = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    content = Column(Text, nullable=False)
    memory_type = Column(
        String,
        CheckConstraint("memory_type IN ('error_fix', 'discovery', 'decision', 'learning', 'warning', 'codebase_knowledge')"),
        nullable=False,
    )
    embedding_id = Column(String)  # Reference to vector store
    related_task_id = Column(String, ForeignKey("tasks.id"))
    tags = Column(JSON)  # JSON array of tags
    related_files = Column(JSON)  # JSON array of file paths
    extra_data = Column(JSON)  # Additional metadata (renamed from metadata)

    # Relationships
    agent = relationship("Agent", back_populates="memories")
    task = relationship("Task", back_populates="memories")


class AgentLog(Base):
    """Log entries for agent activities and interventions."""

    __tablename__ = "agent_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)  # Added for compatibility
    agent_id = Column(String, ForeignKey("agents.id"), nullable=True)  # Made nullable for conductor logs
    log_type = Column(
        String,
        nullable=False,
    )  # Removed constraint to allow more types
    message = Column(Text)
    details = Column(JSON)  # Additional structured data

    # Relationships
    agent = relationship("Agent", back_populates="logs")


class ProjectContext(Base):
    """Project-wide context and configuration."""

    __tablename__ = "project_context"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key = Column(String, unique=True, nullable=False)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    description = Column(Text)


class AutopilotPipelineEvent(Base):
    """Append-only milestone log for an autopilot pipeline run -- replaces
    the old per-run events.jsonl file (OrchestratorLogger.event()).

    Low-volume by nature: one row per workflow launch, design completion,
    pipeline stop, or human-escalation prompt/response -- not per-poll or
    per-second telemetry. project_id is nullable only for the one
    inherently cross-project writer (background_loops.py's phase-
    advancement sweep, which has no single project in scope); every
    per-project pipeline run always sets it, which is what makes this
    table -- unlike the old file, located by a global "latest run dir"
    scan -- actually safe to query under concurrent multi-project runs.
    """

    __tablename__ = "autopilot_pipeline_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(String, nullable=True)
    run_id = Column(String, nullable=True)
    event_type = Column(String, nullable=False)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        Index("ix_autopilot_pipeline_events_project_id", "project_id"),
        Index("ix_autopilot_pipeline_events_created_at", "created_at"),
    )


class WorkflowDefinition(Base):
    """Workflow definition model representing a reusable workflow template."""

    __tablename__ = "workflow_definitions"

    id = Column(String, primary_key=True)  # e.g., "prd-to-software"
    name = Column(String, nullable=False)  # "PRD to Software Builder"
    description = Column(String)
    phases_config = Column(JSON)  # Serialized phase definitions
    workflow_config = Column(JSON)  # has_result, result_criteria, on_result_found, launch_template, etc.
    orchestrator_config = Column(JSON)  # Orchestrator config for phase evaluation and flow control
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    executions = relationship("Workflow", back_populates="definition")


class Workflow(Base):
    """Workflow model representing a collection of phases (an execution instance)."""

    __tablename__ = "workflows"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String)  # User-provided name/description for this execution (e.g., "My URL Shortener")
    phases_folder_path = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    status = Column(
        String,
        CheckConstraint("status IN ('active', 'completed', 'paused', 'failed')"),
        default="active",
        nullable=False,
    )

    # Link to workflow definition
    definition_id = Column(String, ForeignKey("workflow_definitions.id"))

    # Link to the autopilot design that spawned this execution (1 Design : N Workflows)
    design_id = Column(String, ForeignKey("autopilot_designs.id"), nullable=True)

    # Denormalized project_id for direct filtering (set from design.project_id)
    project_id = Column(String, ForeignKey("autopilot_projects.id", ondelete="SET NULL"), nullable=True)

    # Working directory for this execution (can override default)
    working_directory = Column(String)

    # Launch parameters used to start this execution (for UI-launched workflows)
    launch_params = Column(JSON)

    # Result tracking fields
    result_found = Column(Boolean, default=False)
    result_id = Column(String, ForeignKey("workflow_results.id"))
    completed_by_result = Column(Boolean, default=False)

    # New columns for Feature Model
    workflow_type = Column(
        String,
        CheckConstraint("workflow_type IN ('design', 'feature')"),
        nullable=True,
        default=None,  # Explicitly set via _set_workflow_type; NULL = pre-feature-model row
    )
    feature_id = Column(String, ForeignKey("features.id"), nullable=True)

    # Persisted GOTO counter for the evaluating orchestrator's max_total_gotos
    # safety limit. WorkflowOrchestrator instances are recreated on almost
    # every mark_phase_complete call (fresh PhaseManager() in
    # task_completion_service.py and autopilot/orchestrator.py), so an
    # in-memory-only counter reset to 0 every time — the limit never actually
    # fired, letting a phase goto-loop forever. PhaseManager.mark_phase_complete
    # now syncs orchestrator.total_gotos to/from this column around each call.
    total_gotos = Column(Integer, default=0, nullable=False)

    # When total_gotos was last reset to 0 by an on-demand Retry
    # (_resume_interrupted_workflows(reactivate=True)). _trigger_arbitration's
    # own per-phase arbitration cap (max 3) counts historical arbitration
    # Task rows, which never get deleted -- without this cutoff, a workflow
    # that already exhausted that cap would immediately re-exhaust it on
    # every future retry too, permanently unrecoverable via Retry even
    # after total_gotos itself was reset to give the phase a genuinely
    # fresh goto budget. NULL (rows from before this column existed, or a
    # workflow never yet retried) -> count all-time, the original behavior.
    gotos_reset_at = Column(DateTime, nullable=True)

    # Who/what paused this workflow, distinguishing a deliberate user pause
    # (via the stop endpoint) from a defensive system pause (e.g. hitting
    # MAX_PHASE_ATTEMPTS). _try_auto_resume_paused_workflow reads this to
    # avoid silently reactivating a workflow the user just paused -- see
    # that function's docstring for the bug this prevents. NULL/"system" ->
    # eligible for auto-resume; "user" -> left alone until manually resumed.
    paused_by = Column(String, nullable=True)

    # When this workflow was last paused by _maybe_retry_failed_tasks
    # exhausting its retry cap (paused_by="system"). Read by
    # _retry_exhausted_paused_workflows's cooldown gate -- NULL (rows from
    # before this column existed) is treated as immediately eligible, not
    # skipped. Cleared whenever the workflow leaves "paused", by any path.
    paused_at = Column(DateTime, nullable=True)

    # How many times _retry_exhausted_paused_workflows has already given
    # this workflow another shot after an exhausted-retry pause. Capped at
    # paused_workflow_max_retry_cycles (hephaestus_config.yaml) -- once hit,
    # paused_by flips to "system-exhausted" (excluded from further retries,
    # same as an unrecoverable exception: a human has to look at it).
    paused_retry_count = Column(Integer, default=0, nullable=False)

    # Human-readable explanation for the current status -- e.g. why the
    # workflow paused/failed, or that it's awaiting an arbiter decision.
    # Without this, a defensive pause/fail was only ever explained in a log
    # line buried among thousands of others; the DB row itself gave no clue
    # why it stopped. Cleared when the workflow becomes active/completed.
    status_reason = Column(String, nullable=True)

    # Cost tracking - denormalized rollup (self-healed by cost_derivation.py)
    cost_total_usd = Column(Float, default=0.0, nullable=False)

    # Relationships
    definition = relationship("WorkflowDefinition", back_populates="executions")
    design = relationship("AutopilotDesign", foreign_keys=[design_id], backref="workflows")
    project = relationship("AutopilotProject", foreign_keys=[project_id], backref="workflows")
    phases = relationship("Phase", back_populates="workflow", order_by="Phase.order")
    result = relationship("WorkflowResult", foreign_keys=[result_id])
    all_results = relationship("WorkflowResult", foreign_keys="WorkflowResult.workflow_id")
    feature = relationship("Feature", foreign_keys=[feature_id])


class Phase(Base):
    """Phase model representing a workflow phase."""

    __tablename__ = "phases"

    id = Column(String, primary_key=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=False)
    order = Column(Integer, nullable=False)  # From XX_ prefix
    name = Column(String, nullable=False)  # From filename
    description = Column(Text, nullable=False)
    done_definitions = Column(JSON, nullable=False)  # List of criteria
    additional_notes = Column(Text)
    outputs = Column(Text)  # Expected outputs description
    next_steps = Column(Text)  # Instructions for next phase
    working_directory = Column(String)  # Default working directory for agents in this phase

    # Validation configuration
    validation = Column(JSON)  # Stores validation criteria and settings

    # One-shot self-review configuration, e.g. {"enabled": true} (see
    # docs/GAP_CHECK_SELF_LOOP_DESIGN.md). Read from this phase's own YAML
    # `self_review:` key at task-enrichment time.
    self_review = Column(JSON)

    # Per-phase CLI configuration (optional - falls back to global defaults)
    cli_tool = Column(String, nullable=True)  # "claude", "opencode", "droid", "codex", "pi", "swarm"
    cli_model = Column(String, nullable=True)  # "sonnet", "opus", "haiku", "GLM-4.6", etc.
    fallback_cli_tool = Column(String, nullable=True)  # Fallback CLI tool when primary fails
    fallback_cli_model = Column(String, nullable=True)  # Fallback model when primary fails
    glm_api_token_env = Column(String, nullable=True)  # Environment variable name for GLM token
    thinking_level = Column(String, nullable=True)  # pi reasoning budget: off|minimal|low|medium|high|xhigh

    # Persisted count of WorkflowOrchestrator's per-phase RETRY evaluations
    # (eval_point.max_retries). Same architectural issue as
    # Workflow.total_gotos: WorkflowOrchestrator.phase_retry_counts is an
    # in-memory dict that resets to {} every time a fresh orchestrator gets
    # constructed, which happens on nearly every mark_phase_complete call
    # (see PhaseManager._get_orchestrator callers) -- without persisting
    # this, a phase's RETRY budget never actually ran out.
    retry_count = Column(Integer, default=0, nullable=False)

    # Relationships
    workflow = relationship("Workflow", back_populates="phases")
    tasks = relationship("Task", back_populates="phase")
    executions = relationship("PhaseExecution", back_populates="phase")


class PhaseExecution(Base):
    """Track execution of phases."""

    __tablename__ = "phase_executions"

    id = Column(String, primary_key=True)
    phase_id = Column(String, ForeignKey("phases.id"), nullable=False)
    workflow_execution_id = Column(String)  # For tracking multiple workflow runs
    status = Column(
        String,
        CheckConstraint("status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')"),
        default="pending",
        nullable=False,
    )
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    completion_summary = Column(Text)
    # Atomic claim for "who gets to create this phase's first task" -- set the
    # instant a task-creation attempt begins (before any of the slow work:
    # agent spawning, LLM enrichment), via a single UPDATE ... WHERE column
    # IS NULL. Two independent code paths can decide to create phase 1's
    # first task (server.py's synchronous "create initial task" step when a
    # workflow launches, and the orchestrator's background self-heal for
    # "in_progress phase with no tasks") -- a plain Task.count()==0 check
    # raced between them and produced a live duplicate task+agent. See
    # _claim_phase_task_creation in orchestrator.py.
    task_creation_claimed_at = Column(DateTime, nullable=True)

    # Relationships
    phase = relationship("Phase", back_populates="executions")


class AgentWorktree(Base):
    """Track git worktree isolation for agents."""

    # TODO(deferred): rename worktree_path -> branch_path once a migration is scoped; not done here (tech-debt pass 2026-08-25 — no migration in scope, see des-c7b9 requirements.md).

    __tablename__ = "agent_worktrees"

    agent_id = Column(String, ForeignKey("agents.id"), primary_key=True)
    repo_id = Column(String, ForeignKey("project_repos.id"))  # ProjectRepo this worktree is scoped to (multi-repo projects)
    worktree_path = Column(Text, nullable=False)
    branch_name = Column(String, unique=True, nullable=False)
    parent_agent_id = Column(String, ForeignKey("agents.id"))
    parent_commit_sha = Column(String, nullable=False)
    base_commit_sha = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    merged_at = Column(DateTime)
    merge_status = Column(
        String,
        CheckConstraint("merge_status IN ('active', 'merged', 'abandoned', 'cleaned')"),
        default="active",
        nullable=False,
    )
    merge_commit_sha = Column(String)
    disk_usage_mb = Column(Integer)

    # Relationships
    agent = relationship("Agent", foreign_keys=[agent_id], backref="worktree")
    parent_agent = relationship("Agent", foreign_keys=[parent_agent_id])
    commits = relationship(
        "WorktreeCommit",
        back_populates="worktree",
        foreign_keys="WorktreeCommit.agent_id",
        primaryjoin="AgentWorktree.agent_id==WorktreeCommit.agent_id",
    )
    conflict_resolutions = relationship(
        "MergeConflictResolution",
        back_populates="worktree",
        foreign_keys="MergeConflictResolution.agent_id",
        primaryjoin="AgentWorktree.agent_id==MergeConflictResolution.agent_id",
    )


# Alias for consistent naming (DB table stays agent_worktrees)
AgentBranch = AgentWorktree


class WorktreeCommit(Base):
    """Track commits within agent worktrees for traceability."""

    __tablename__ = "worktree_commits"

    id = Column(String, primary_key=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    commit_sha = Column(String, unique=True, nullable=False)
    commit_type = Column(
        String,
        CheckConstraint("commit_type IN ('parent_checkpoint', 'validation_ready', 'final', 'auto_save', 'conflict_resolution')"),
        nullable=False,
    )
    commit_message = Column(Text, nullable=False)
    files_changed = Column(Integer)
    insertions = Column(Integer)
    deletions = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    agent = relationship("Agent", backref="worktree_commits", overlaps="commits")
    worktree = relationship(
        "AgentWorktree",
        back_populates="commits",
        foreign_keys=[agent_id],
        primaryjoin="WorktreeCommit.agent_id==AgentWorktree.agent_id",
        overlaps="agent,worktree_commits",
    )


class ValidationReview(Base):
    """Track validation reviews for tasks."""

    __tablename__ = "validation_reviews"

    id = Column(String, primary_key=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    validator_agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    iteration_number = Column(Integer, nullable=False)
    validation_passed = Column(Boolean, nullable=False)
    feedback = Column(Text, nullable=False)
    evidence = Column(JSON)  # Array of evidence items
    recommendations = Column(JSON)  # Array of follow-up tasks
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    task = relationship("Task", backref="validation_reviews")
    validator_agent = relationship("Agent", backref="validation_reviews")


class MergeConflictResolution(Base):
    """Track automatic conflict resolutions during merges."""

    __tablename__ = "merge_conflict_resolutions"

    id = Column(String, primary_key=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    file_path = Column(Text, nullable=False)
    parent_modified_at = Column(DateTime)
    child_modified_at = Column(DateTime)
    resolution_choice = Column(
        String,
        CheckConstraint("resolution_choice IN ('parent', 'child', 'tie_child')"),
        nullable=False,
    )
    resolved_at = Column(DateTime, default=datetime.utcnow)
    commit_sha = Column(String, ForeignKey("worktree_commits.commit_sha"))

    # Relationships
    agent = relationship("Agent", backref="conflict_resolutions", overlaps="conflict_resolutions")
    worktree = relationship(
        "AgentWorktree",
        back_populates="conflict_resolutions",
        foreign_keys=[agent_id],
        primaryjoin="MergeConflictResolution.agent_id==AgentWorktree.agent_id",
        overlaps="agent,conflict_resolutions",
    )
    commit = relationship("WorktreeCommit", backref="resolutions")


class AgentResult(Base):
    """Store formal results reported by agents for their completed tasks."""

    __tablename__ = "agent_results"

    id = Column(String, primary_key=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    markdown_content = Column(Text, nullable=False)
    markdown_file_path = Column(Text, nullable=False)
    result_type = Column(
        String,
        CheckConstraint("result_type IN ('implementation', 'analysis', 'fix', 'design', 'test', 'documentation')"),
        nullable=False,
    )
    summary = Column(Text, nullable=False)
    verification_status = Column(
        String,
        CheckConstraint("verification_status IN ('unverified', 'verified', 'disputed')"),
        default="unverified",
        nullable=False,
    )
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    verified_at = Column(DateTime)
    verified_by_validation_id = Column(String, ForeignKey("validation_reviews.id"))

    # Relationships
    agent = relationship("Agent", backref="results")
    task = relationship("Task", back_populates="results")
    validation_review = relationship("ValidationReview", backref="verified_results")


class WorkflowResult(Base):
    """Store workflow-level results with evidence and validation status."""

    __tablename__ = "workflow_results"

    id = Column(String, primary_key=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    result_file_path = Column(Text, nullable=False)
    result_content = Column(Text, nullable=False)
    extra_files = Column(JSON, nullable=True, default=list)  # List of additional file paths (e.g., patches, reproduction scripts)
    status = Column(
        String,
        CheckConstraint("status IN ('pending_validation', 'validated', 'rejected')"),
        default="pending_validation",
        nullable=False,
    )
    validation_feedback = Column(Text)
    validation_evidence = Column(JSON)
    validated_by_agent_id = Column(String, ForeignKey("agents.id"))
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    validated_at = Column(DateTime)

    # Relationships
    workflow = relationship("Workflow", foreign_keys=[workflow_id], back_populates="all_results")
    agent = relationship("Agent", foreign_keys=[agent_id], backref="workflow_results")
    validator_agent = relationship("Agent", foreign_keys=[validated_by_agent_id])


class GuardianAnalysis(Base):
    """Dedicated table for Guardian trajectory analyses."""

    __tablename__ = "guardian_analyses"

    id = Column(Integer, primary_key=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Trajectory analysis fields
    current_phase = Column(String)
    trajectory_aligned = Column(Boolean)
    alignment_score = Column(Float, index=True)
    needs_steering = Column(Boolean, index=True)
    steering_type = Column(String)
    steering_recommendation = Column(Text)
    trajectory_summary = Column(Text)
    last_claude_message_marker = Column(String(100))  # NEW: Marker for next cycle to identify new content

    # Accumulated context fields
    accumulated_goal = Column(Text)
    current_focus = Column(String)
    session_duration = Column(String)
    conversation_length = Column(Integer)

    # Full analysis details as JSON for reference
    details = Column(JSON)

    # Relationships
    agent = relationship("Agent", backref="guardian_analyses", overlaps="logs")


class ConductorAnalysis(Base):
    """Dedicated table for Conductor system analyses."""

    __tablename__ = "conductor_analyses"

    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)

    # System coherence fields
    coherence_score = Column(Float, index=True)
    num_agents = Column(Integer)
    system_status = Column(Text)

    # Duplicate detection
    duplicate_count = Column(Integer)

    # Decision counts
    termination_count = Column(Integer)
    coordination_count = Column(Integer)

    # Full analysis as JSON
    details = Column(JSON)


class DetectedDuplicate(Base):
    """Table for tracking detected duplicate work."""

    __tablename__ = "detected_duplicates"

    id = Column(Integer, primary_key=True)
    conductor_analysis_id = Column(Integer, ForeignKey("conductor_analyses.id"))
    agent1_id = Column(String, ForeignKey("agents.id"))
    agent2_id = Column(String, ForeignKey("agents.id"))
    similarity_score = Column(Float)
    work_description = Column(Text)
    timestamp = Column(DateTime, default=datetime.utcnow)

    # Relationships
    conductor_analysis = relationship("ConductorAnalysis", backref="duplicates")
    agent1 = relationship("Agent", foreign_keys=[agent1_id], backref="duplicates_as_agent1")
    agent2 = relationship("Agent", foreign_keys=[agent2_id], backref="duplicates_as_agent2")


class SteeringIntervention(Base):
    """Table for tracking steering interventions."""

    __tablename__ = "steering_interventions"

    id = Column(Integer, primary_key=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    guardian_analysis_id = Column(Integer, ForeignKey("guardian_analyses.id"))
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    steering_type = Column(String)
    message = Column(Text)
    was_successful = Column(Boolean)

    # Relationships
    agent = relationship("Agent", backref="interventions")
    guardian_analysis = relationship("GuardianAnalysis", backref="interventions")


class DiagnosticRun(Base):
    """Track diagnostic agent executions for workflow stuck detection."""

    __tablename__ = "diagnostic_runs"

    id = Column(String, primary_key=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=False)
    diagnostic_agent_id = Column(String, ForeignKey("agents.id"))
    diagnostic_task_id = Column(String, ForeignKey("tasks.id"))

    # Trigger conditions
    triggered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    total_tasks_at_trigger = Column(Integer, nullable=False)
    done_tasks_at_trigger = Column(Integer, nullable=False)
    failed_tasks_at_trigger = Column(Integer, nullable=False)
    time_since_last_task_seconds = Column(Integer, nullable=False)

    # Results
    tasks_created_count = Column(Integer, default=0)
    tasks_created_ids = Column(JSON)  # List of task IDs created
    completed_at = Column(DateTime)
    status = Column(
        String,
        CheckConstraint("status IN ('created', 'running', 'completed', 'failed')"),
        default="created",
        nullable=False,
    )

    # Analysis context snapshot
    workflow_goal = Column(Text)
    phases_analyzed = Column(JSON)  # List of phase info
    agents_reviewed = Column(JSON)  # List of agent summaries
    diagnosis = Column(Text)  # What the diagnostic agent concluded

    # Relationships
    workflow = relationship("Workflow", backref="diagnostic_runs")
    agent = relationship("Agent", foreign_keys=[diagnostic_agent_id], backref="diagnostic_runs")
    task = relationship("Task", foreign_keys=[diagnostic_task_id], backref="diagnostic_runs")


class Ticket(Base):
    """Ticket model for ticket tracking system."""

    __tablename__ = "tickets"

    id = Column(String, primary_key=True)  # Format: ticket-{uuid}
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=False)
    created_by_agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    assigned_agent_id = Column(String, ForeignKey("agents.id"))

    # Core Fields
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=False)
    ticket_type = Column(String(50), nullable=False)  # bug, feature, improvement, task, spike, etc.
    priority = Column(String(20), nullable=False)  # low, medium, high, critical
    status = Column(String(50), nullable=False)  # Based on board_config columns (fully configurable)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    started_at = Column(DateTime)  # When work begins
    completed_at = Column(DateTime)  # When marked complete

    # Links & References
    parent_ticket_id = Column(String, ForeignKey("tickets.id"))
    task_id = Column(String, ForeignKey("tasks.id"))  # Primary task this ticket relates to
    phase_id = Column(String, ForeignKey("phases.id"))  # Phase where this ticket was created
    repo_id = Column(String, ForeignKey("project_repos.id"))  # ProjectRepo this ticket is scoped to (multi-repo projects)
    related_task_ids = Column(JSON)  # List of related task IDs
    related_ticket_ids = Column(JSON)  # List of related ticket IDs for context
    tags = Column(JSON)  # List of tags

    # Search & Discovery
    embedding = Column(JSON)  # Cached embedding for quick access
    embedding_id = Column(String)  # Reference to Qdrant

    # Blocking & Dependencies
    blocked_by_ticket_ids = Column(JSON)  # List of ticket IDs blocking this ticket
    is_resolved = Column(Boolean, default=False)  # Whether this ticket is resolved
    resolved_at = Column(DateTime)  # When ticket was resolved

    # Human Approval
    approval_status = Column(String(20), default="auto_approved", nullable=False)  # auto_approved, pending_review, approved, rejected
    approval_requested_at = Column(DateTime)  # When approval was requested
    approval_decided_at = Column(DateTime)  # When human made decision
    approval_decided_by = Column(String)  # User/agent who approved/rejected
    rejection_reason = Column(Text)  # Why ticket was rejected

    # Relationships
    workflow = relationship("Workflow", backref="tickets")
    created_by_agent = relationship("Agent", foreign_keys=[created_by_agent_id], backref="created_tickets")
    assigned_agent = relationship("Agent", foreign_keys=[assigned_agent_id], backref="assigned_tickets")
    parent_ticket = relationship(
        "Ticket",
        remote_side=[id],
        foreign_keys=[parent_ticket_id],
        backref="sub_tickets",
    )
    task = relationship("Task", foreign_keys=[task_id], backref="tickets")
    phase = relationship("Phase", foreign_keys=[phase_id], backref="tickets")
    comments = relationship("TicketComment", back_populates="ticket")
    history = relationship("TicketHistory", back_populates="ticket")
    commits = relationship("TicketCommit", back_populates="ticket")

    # Create indexes
    __table_args__ = (
        # Note: Indexes are created separately in create_tables() for better compatibility
    )


class TicketComment(Base):
    """Comments and discussions on tickets."""

    __tablename__ = "ticket_comments"

    id = Column(String, primary_key=True)  # Format: comment-{uuid}
    ticket_id = Column(String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)

    # Content
    comment_text = Column(Text, nullable=False)
    comment_type = Column(String(50), default="general")  # general, status_change, assignment, blocker, resolution

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime)  # If edited
    is_edited = Column(Boolean, default=False)

    # Rich Content
    mentions = Column(JSON)  # List of mentioned agent/ticket IDs
    attachments = Column(JSON)  # List of file paths or URLs

    # Relationships
    ticket = relationship("Ticket", back_populates="comments")
    agent = relationship("Agent", backref="ticket_comments")


class TicketHistory(Base):
    """Track all changes to tickets for audit trail."""

    __tablename__ = "ticket_history"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)

    # Change Information
    change_type = Column(String(50), nullable=False)  # created, status_changed, assigned, commented, field_updated, commit_linked, reopened, blocked, unblocked
    field_name = Column(String(100))  # Which field changed (if applicable)
    old_value = Column(Text)  # Previous value (JSON for complex types)
    new_value = Column(Text)  # New value (JSON for complex types)

    # Context
    change_description = Column(Text)  # Human-readable description
    change_metadata = Column(JSON)  # Additional context (e.g., commit info, file paths)

    # Timing
    changed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    ticket = relationship("Ticket", back_populates="history")
    agent = relationship("Agent", backref="ticket_history")


class TicketCommit(Base):
    """Link git commits to tickets for traceability."""

    __tablename__ = "ticket_commits"

    id = Column(String, primary_key=True)  # Format: tc-{uuid}
    ticket_id = Column(String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    repo_id = Column(String, ForeignKey("project_repos.id"))  # ProjectRepo this commit belongs to (multi-repo projects)

    # Commit Information
    commit_sha = Column(String(40), nullable=False)
    commit_message = Column(Text, nullable=False)
    commit_timestamp = Column(DateTime, nullable=False)

    # Change Stats
    files_changed = Column(Integer)
    insertions = Column(Integer)
    deletions = Column(Integer)
    files_list = Column(JSON)  # List of changed file paths

    # Linking
    linked_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    link_method = Column(String(50), default="manual")  # manual, auto_detected, worktree

    # Relationships
    ticket = relationship("Ticket", back_populates="commits")
    agent = relationship("Agent", backref="ticket_commits")


class BoardConfig(Base):
    """Kanban board configurations per workflow."""

    __tablename__ = "board_configs"

    id = Column(String, primary_key=True)  # Format: board-{uuid}
    workflow_id = Column(
        String,
        ForeignKey("workflows.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )

    # Board Configuration
    name = Column(String(200), nullable=False)
    columns = Column(JSON, nullable=False)  # Array of {id, name, order, color}
    ticket_types = Column(JSON, nullable=False)  # Array of allowed ticket types
    default_ticket_type = Column(String(50))
    initial_status = Column(String(50), nullable=False)  # Default status for new tickets

    # Settings
    auto_assign = Column(Boolean, default=False)
    require_comments_on_status_change = Column(Boolean, default=False)
    allow_reopen = Column(Boolean, default=True)
    track_time = Column(Boolean, default=False)

    # Human Review Settings
    ticket_human_review = Column(Boolean, default=False)  # Enable human approval for tickets
    approval_timeout_seconds = Column(Integer, default=1800)  # 30 minutes default

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationships
    workflow = relationship("Workflow", backref="board_config")


class AutopilotProject(Base):
    """A project directory that Autopilot scans for design documents."""

    __tablename__ = "autopilot_projects"

    id = Column(String, primary_key=True)  # Format: proj-{uuid}
    name = Column(String(200), nullable=False)
    base_dir = Column(Text, nullable=False, unique=True)
    is_default = Column(Boolean, default=False)
    is_active = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Cost tracking - denormalized rollup (self-healed by cost_derivation.py)
    cost_total_usd = Column(Float, default=0.0, nullable=False)
    # Budget limit - None means no limit
    cost_limit_usd = Column(Float, nullable=True)

    # Review mode: when True, pipeline pauses after each feature's deploy phase
    # and waits for the user to approve or request changes before continuing.
    review_mode = Column(Boolean, default=False, nullable=False)

    # Spec Kit auto-scan: when True, the design-queue scan also auto-queues
    # ready (has_plan=True) specs/<NNN>-<name>/ Spec Kit feature directories.
    speckit_auto_scan_enabled = Column(Boolean, default=False, nullable=False)

    designs = relationship("AutopilotDesign", back_populates="project", cascade="all, delete-orphan")
    repos = relationship("ProjectRepo", back_populates="project", cascade="all, delete-orphan")


class ProjectRepo(Base):
    """One git repo belonging to a project. A project spans N sibling repos;
    the primary repo (is_primary=True) is what single-repo projects have
    always had via AutopilotProject.base_dir."""

    __tablename__ = "project_repos"

    id = Column(String, primary_key=True)  # Format: repo-{uuid}
    project_id = Column(String, ForeignKey("autopilot_projects.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(100), nullable=False)  # "backend", "frontend"
    path = Column(Text, nullable=False)  # absolute path, not required under base_dir
    is_primary = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    project = relationship("AutopilotProject", back_populates="repos")

    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_project_repos_project_path"),
        UniqueConstraint("project_id", "label", name="uq_project_repos_project_label"),
    )


class PromptProposal(Base):
    """A forensics-proposed rewrite of one phase-prompt field, awaiting human
    review (design_docs/agent_prompt_analysis.md finding 8).

    forensics_analysis runs after a pipeline finishes and proposes prompt
    improvements for FUTURE runs. Those proposals used to exist only as prose
    in forensics.md and as `improvement` tickets carrying no before/after
    text, so nothing could tell which had been applied or what they changed.

    `previous_value` is captured at APPLY time, not at proposal time: the file
    can change between a proposal being filed and approved, and revert has to
    restore what was actually there, not what the agent once quoted.
    """

    __tablename__ = "prompt_proposals"

    id = Column(String, primary_key=True)  # prop-<uuid8>
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    created_by_agent_id = Column(String, nullable=True)

    # What it wants to change. workflow_definition + phase_name + field
    # locate the target; the service enforces which fields are reachable.
    workflow_definition = Column(String(100), nullable=False, default="autopilot")
    phase_name = Column(String(100), nullable=False)
    field = Column(String(50), nullable=False)
    proposing_phase = Column(String(100), nullable=True)  # for the self-edit guard

    proposed_value = Column(JSON, nullable=False)  # str, or list[str] for done_definitions
    quoted_current_value = Column(JSON, nullable=True)  # what the agent believed it was
    previous_value = Column(JSON, nullable=True)  # what was actually replaced, set on apply

    rationale = Column(Text, nullable=False)  # why -- the evidence from the run
    evidence = Column(Text, nullable=True)  # optional citation (log lines, artifact quotes)

    status = Column(
        String,
        CheckConstraint(
            # No 'approved' state: approval and application are one action, so a
            # row that was approved is already 'applied' (or 'failed' if the
            # write did not land). An unreachable state in a constraint reads
            # like a capability that exists.
            "status IN ('pending', 'rejected', 'applied', 'reverted', 'failed')"
        ),
        nullable=False,
        default="pending",
    )
    review_note = Column(Text, nullable=True)  # human's reason on reject
    applied_commit_sha = Column(String(64), nullable=True)
    reverted_commit_sha = Column(String(64), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)
    applied_at = Column(DateTime, nullable=True)
    reverted_at = Column(DateTime, nullable=True)


class Feature(Base):
    """Feature model representing a decomposed feature from a design document."""

    __tablename__ = "features"

    id = Column(String, primary_key=True)  # feat-<uuid8>
    design_id = Column(String, ForeignKey("autopilot_designs.id"), nullable=False)
    feature_key = Column(String(100), nullable=False)  # slug from features.json "id" field
    repo_id = Column(String, ForeignKey("project_repos.id"))  # ProjectRepo this feature is scoped to (multi-repo projects)
    name = Column(String, nullable=False)
    scope = Column(Text, nullable=False)  # one-paragraph summary
    files = Column(JSON, nullable=True)  # list of file paths owned
    depends_on = Column(JSON, nullable=True)  # list of feature_key strings
    execution = Column(
        String,
        CheckConstraint("execution IN ('parallel', 'sequential')"),
        nullable=False,
        default="parallel",
    )
    status = Column(
        String,
        CheckConstraint("status IN ('pending', 'active', 'completed', 'failed', 'skipped', 'paused')"),
        nullable=False,
        default="pending",
    )
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    scope_doc_path = Column(Text, nullable=True)  # abs path to scope.md in permanent record
    feature_record_path = Column(Text, nullable=True)  # abs path to designs/.../features/<key>/
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    # Cost tracking - denormalized rollup (self-healed by cost_derivation.py)
    cost_total_usd = Column(Float, default=0.0, nullable=False)

    # Review mode columns — populated when project.review_mode is True and
    # the pipeline pauses this feature after its deploy phase for human sign-off.
    review_status = Column(
        String,
        CheckConstraint("review_status IN ('pending', 'approved', 'changes_requested')"),
        nullable=True,
        default=None,
    )
    review_feedback = Column(Text, nullable=True)   # user's change-request text
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by = Column(String(100), nullable=True, default=None)

    # Pull request URL — populated by git_expert phase after creating PR
    pr_url = Column(Text, nullable=True)

    # Denormalized copy of the parent AutopilotDesign.workflow_type at
    # decomposition time -- not a join, since this feature's pipeline can be
    # resumed long after the parent design row's own lifecycle is otherwise
    # irrelevant. Selects which workflow definition_id _run_one_feature
    # launches (see docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md).
    workflow_type = Column(String(20), nullable=False, default="feature")

    # Relationships
    design = relationship("AutopilotDesign", back_populates="features")
    workflow = relationship("Workflow", foreign_keys=[workflow_id])


class AutopilotDesign(Base):
    """A design document within a project's design queue."""

    __tablename__ = "autopilot_designs"

    id = Column(String, primary_key=True)  # Format: des-{uuid}
    project_id = Column(String, ForeignKey("autopilot_projects.id", ondelete="CASCADE"), nullable=False)
    # Nullable: a Spec-Kit directory-sourced design (source_dir set below) has
    # no single filename. filename/file_path and source_dir are mutually
    # exclusive per row (NFR-02).
    filename = Column(String(500), nullable=True)
    name = Column(String(500), nullable=False)
    ordinal = Column(Integer, nullable=False, default=0)
    size_bytes = Column(Integer, nullable=False, default=0)
    extension = Column(String(10), nullable=False, default=".md")
    content_hash = Column(String(64), nullable=True)  # SHA-256 for dedup
    status = Column(String(20), nullable=False, default="pending")  # pending, processing, decomposing, active, completed, failed, skipped
    feature_folder = Column(Text, nullable=True)  # Path to feature folder after processing
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.utcnow)

    # New columns for Feature Model
    file_path = Column(Text, nullable=True)  # absolute path to design file
    designs_folder = Column(Text, nullable=True)  # path to designs/<ts>_<name>_<id>/
    phase0_workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    # Why status == "failed" -- orchestrator.py's run_phase0/_update_design_status
    # has always passed error=... on every failure path, but there was no
    # column to store it: _update_design_status silently dropped it (logging
    # "unknown field 'error'") and the design modal had nothing to show,
    # even for a specific, actionable reason like "Invalid features.json:
    # features array must have 1-5 entries, got 6".
    error = Column(Text, nullable=True)

    # Cost tracking - denormalized rollup (self-healed by cost_derivation.py)
    cost_total_usd = Column(Float, default=0.0, nullable=False)

    # Which pipeline this design runs through -- "feature" (full pipeline,
    # definition_id "autopilot") or "bugfix" (shorter pipeline, definition_id
    # "bugfix"). Set at add-time, either from the user's explicit choice or
    # detect_workflow_type()'s heuristic. See docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md.
    workflow_type = Column(String(20), nullable=False, default="feature")

    # Which ProjectRepo (of a multi-repo project) this design belongs to.
    # None for single-repo projects' legacy queue path, or a file-sourced
    # design never repo-scoped to begin with. Resolved via repo_id_for_path
    # (REQ-01/REQ-06).
    repo_id = Column(String, ForeignKey("project_repos.id"))
    # Absolute path to a Spec Kit specs/<NNN>-<name>/ directory. None for
    # single-file designs. Mutually exclusive with filename/file_path
    # (REQ-02/NFR-02).
    source_dir = Column(Text, nullable=True)

    # Set when the user archives this design from the queue panel -- hides
    # it from the default design list without touching its file, tasks,
    # workflows, or features (unlike remove_project_design's destructive
    # delete). NULL means active/visible.
    archived_at = Column(DateTime, nullable=True)

    # Relationships
    project = relationship("AutopilotProject", back_populates="designs")
    features = relationship("Feature", back_populates="design", cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("project_id", "filename", name="uq_design_project_filename"),)


class PhasePromptVersion(Base):
    """Versioned prompt content for a phase.

    Every save creates a new row. Exactly one row per phase is marked
    ``active``; the rest are ``draft`` or ``archived`` (replaced).
    """

    __tablename__ = "phase_prompt_versions"

    id = Column(String, primary_key=True)
    phase_id = Column(String, ForeignKey("phases.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(
        String,
        CheckConstraint("status IN ('active', 'draft', 'archived')"),
        default="draft",
        nullable=False,
    )

    # Snapshot of editable fields
    description = Column(Text, nullable=False, default="")
    done_definitions = Column(JSON, nullable=False, default=list)
    additional_notes = Column(Text)
    outputs = Column(Text)
    next_steps = Column(Text)

    # Metadata
    change_summary = Column(Text)  # Human-readable change note from editor
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_by = Column(String, nullable=False, default="ui-user")
    parent_version = Column(Integer, nullable=True)  # Version this was edited from

    # Relationships
    phase = relationship("Phase", backref="prompt_versions")

    __table_args__ = (UniqueConstraint("phase_id", "version", name="uq_phase_version"),)


class TaskPromptOverride(Base):
    """Per-task prompt overrides.

    Empty / NULL values fall back to the phase default. Non-empty values
    replace the corresponding section in the assembled prompt.
    """

    __tablename__ = "task_prompt_overrides"

    task_id = Column(String, ForeignKey("tasks.id"), primary_key=True)
    system_prompt = Column(Text)  # NULL = use phase default
    user_prompt = Column(Text)  # NULL = use phase default
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    updated_by = Column(String, nullable=False, default="ui-user")

    # Relationships
    task = relationship("Task", backref=backref("prompt_override", uselist=False))


class PhasePromptTemplate(Base):
    """Available template variables for phase prompts.

    Documents which ``{var_name}`` tokens the assembler recognizes and
    how to resolve them.
    """

    __tablename__ = "phase_prompt_templates"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)  # e.g. "project_name"
    description = Column(Text, nullable=False)
    example_value = Column(Text)  # e.g. "hephaestus"
    resolver = Column(String, nullable=False)  # Python path, e.g. "src.prompts.resolvers.project_name"
    category = Column(String, default="general")  # general, workflow, phase, task
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class CostEntry(Base):
    """Append-only ledger of LLM costs. One row per turn/call.

    Source of truth for all cost data. Aggregates are derived from this
    table via cost_derivation.py, not hand-maintained.
    """

    __tablename__ = "cost_entries"

    id = Column(String, primary_key=True)  # cost-<uuid8>
    task_id = Column(String, ForeignKey("tasks.id"), nullable=True)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)

    # 'pi' | 'claude_code' | 'opencode' | 'codex' | 'openrouter_direct'
    source = Column(String, nullable=False)
    model = Column(String, nullable=True)  # e.g. "anthropic/claude-sonnet-4"

    input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cache_read_tokens = Column(Integer, default=0)
    cache_write_tokens = Column(Integer, default=0)
    reasoning_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, nullable=False)

    recorded_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # Raw source line/turn for debugging discrepancies
    raw_usage = Column(JSON, nullable=True)

    # Relationships
    task = relationship("Task", foreign_keys=[task_id], backref="cost_entries")
    agent = relationship("Agent", foreign_keys=[agent_id], backref="cost_entries")
    workflow = relationship("Workflow", foreign_keys=[workflow_id], backref="cost_entries")

    __table_args__ = (
        Index("ix_cost_entries_task_id", "task_id"),
        Index("ix_cost_entries_workflow_id", "workflow_id"),
        Index("ix_cost_entries_recorded_at", "recorded_at"),
    )


class SessionCostCheckpoint(Base):
    """Checkpoint for transcript-tailing cost collectors.

    Keyed by session_id (not Agent.id) because the session outlives any
    single agent row -- an agent retry reuses the same session file,
    and a checkpoint on the new agent would re-read dead agent's turns.
    """

    __tablename__ = "session_cost_checkpoints"

    session_id = Column(String, primary_key=True)
    lines_processed = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SchemaMigration(Base):
    """Records that DatabaseManager's own schema migration `id` has been
    attempted, so create_tables() doesn't re-run and re-log every one of
    them on every single app startup forever (SOLID review 4.1).

    Deliberately tracks "attempted", not "fully succeeded": each migration
    function still owns its own internal resilience (multiple independent
    ALTER-TABLE-or-skip-if-exists sub-steps per function, several already
    isolating their own failures from each other) and still never raises
    to its caller -- changing that contract is a separate, larger change
    this pass doesn't make. What changes here is purely bookkeeping +
    making a genuine failure visible (see _run_schema_migration's own
    warning-level log, replacing the previous debug-level one that could
    silently mask a real bug until it resurfaced later as a confusing
    "no such column" error somewhere unrelated).
    """

    __tablename__ = "schema_migrations"

    id = Column(String, primary_key=True)
    applied_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DatabaseManager:
    """Manager for database operations.

    Uses engine caching to avoid creating duplicate engines for the same
    database path. Each engine uses QueuePool for connection pooling,
    allowing concurrent reads alongside writes (SQLite WAL mode).
    """

    _engines: Dict[str, Any] = {}
    _sessions: Dict[str, sessionmaker] = {}
    _lock = threading.Lock()

    def __init__(self, database_path: str = "hephaestus.db"):
        """Initialize database connection (reuses cached engine if available)."""
        if database_path is None:
            database_path = os.environ.get("HEPHAESTUS_TEST_DB", "hephaestus.db")
        self.database_path = database_path

        # ":memory:" means "give me a fresh, isolated in-memory database,"
        # not "give me the shared one at this path" -- caching it by the
        # literal string like every other path would make every caller
        # process-wide share ONE engine/connection pool forever, with one
        # test's leftover rows bleeding into an unrelated later test.
        # Never cache or reuse it.
        is_memory = database_path == ":memory:"

        with DatabaseManager._lock:
            if is_memory or database_path not in DatabaseManager._engines:
                if is_memory:
                    # StaticPool: a single connection, reused for every
                    # checkout. QueuePool hands out whichever of its several
                    # connections is free -- fine for a real file (they all
                    # see the same on-disk data) but wrong for ":memory:",
                    # where each connection IS its own separate database.
                    engine = create_engine(
                        f"sqlite:///{database_path}",
                        connect_args={"check_same_thread": False},
                        poolclass=StaticPool,
                        echo=False,
                    )
                else:
                    engine = create_engine(
                        f"sqlite:///{database_path}",
                        connect_args={"check_same_thread": False},
                        poolclass=QueuePool,
                        pool_size=5,
                        max_overflow=10,
                        pool_timeout=30,
                        pool_recycle=300,
                        echo=False,
                    )

                # Set SQLite pragmas for concurrent access
                # WAL mode allows concurrent readers alongside a single writer
                # busy_timeout makes writers block-and-retry instead of failing
                # foreign_keys=ON enforces foreign key constraints
                @event.listens_for(engine, "connect")
                def _set_sqlite_pragma(dbapi_connection, connection_record):
                    cursor = dbapi_connection.cursor()
                    cursor.execute("PRAGMA foreign_keys=ON")  # Enforce FK constraints
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA busy_timeout=30000")  # 30s
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.close()

                session_factory = sessionmaker(
                    autocommit=False,
                    autoflush=False,
                    bind=engine,
                    expire_on_commit=False,  # Prevent DetachedInstanceError bugs (H-0*)
                )
                if is_memory:
                    # Deliberately not written into the class-level caches
                    # above -- see the comment on is_memory.
                    self.engine = engine
                    self.SessionLocal = session_factory
                    return
                DatabaseManager._engines[database_path] = engine
                DatabaseManager._sessions[database_path] = session_factory

            self.engine = DatabaseManager._engines[database_path]
            self.SessionLocal = DatabaseManager._sessions[database_path]

    def create_tables(self):
        """Create all database tables."""
        Base.metadata.create_all(bind=self.engine)

        # FTS5 search tables and performance indexes live in
        # src/core/schema_ddl.py (SOLID review 4.1) -- ~150 lines of raw SQL
        # that only need the engine, same as the migrations below.
        from src.core.schema_ddl import create_fts5_tables, create_indexes

        create_fts5_tables(self.engine)
        create_indexes(self.engine)

        # Migrate new columns for existing databases. Each migration still
        # owns its own internal resilience (SOLID review 4.1) -- this just
        # adds "have we attempted this before" bookkeeping in
        # schema_migrations so create_tables() doesn't re-run and re-log
        # every one of these on every single app startup forever.
        #
        # The migrations themselves live in src/core/schema_migrations.py
        # (SOLID review 4.1: they were ~590 of this class's ~940 lines).
        # Imported here rather than at module scope because that module
        # imports model classes back from this one.
        from src.core.schema_migrations import SCHEMA_MIGRATIONS

        for migration_id, fn in SCHEMA_MIGRATIONS:
            # Bind fn per-iteration: a bare `lambda: fn(self.engine)` would
            # capture the loop variable and run the last migration 18 times.
            self._run_schema_migration(migration_id, lambda fn=fn: fn(self.engine))

        # Recurring reconciliation, NOT a one-shot migration -- deliberately
        # outside the loop above so it is never recorded-and-skipped. A
        # self-review-enabled phase whose row lost the flag silently stops
        # gating task completion, and that can drift back at any time as new
        # per-workflow Phase rows are seeded. See the function's docstring.
        from src.core.schema_migrations import backfill_self_review_defaults

        backfill_self_review_defaults(self.engine)

    def _run_schema_migration(self, migration_id: str, fn) -> None:
        """Run one schema migration at most once per database, recorded in
        schema_migrations (SOLID review 4.1).

        `fn` (one of the _migrate_* methods below) keeps its own existing
        internal resilience unchanged -- multiple independent ALTER-TABLE-
        or-skip-if-already-exists sub-steps per method, several already
        isolating their own failures from each other -- and still never
        raises to this wrapper under normal operation. What this adds:

        1. Skips re-running (and re-logging) a migration already recorded
           as attempted, instead of unconditionally re-running all 18 of
           these on every single app startup forever.
        2. If checking/recording schema_migrations itself fails (e.g. the
           table doesn't exist yet on a very first run before
           Base.metadata.create_all has committed), falls through to
           running fn() anyway -- matching every prior startup's behavior
           of "just run the migration," never skipping one due to
           bookkeeping trouble.
        3. If fn() raises despite its own internal handling, logs at
           WARNING (not silently) and does not record it as applied, so
           it retries next startup instead of being masked forever.
        """
        try:
            with self.session_scope() as session:
                if session.query(SchemaMigration).filter_by(id=migration_id).first():
                    return
        except Exception as e:
            logger.warning(
                f"Could not check schema_migrations for {migration_id}, running it anyway: {e}"
            )

        try:
            fn()
        except Exception as e:
            logger.warning(f"Schema migration {migration_id} failed: {e}")
            return  # Don't record as applied -- retry next startup.

        try:
            with self.session_scope() as session:
                session.add(SchemaMigration(id=migration_id))
        except Exception as e:
            logger.warning(f"Could not record schema_migrations entry for {migration_id}: {e}")

    def get_session(self):
        """Get a database session."""
        return self.SessionLocal()

    @contextmanager
    def session_scope(self):
        """Provide a transactional scope around a series of operations.

        Use this instead of raw get_session() to ensure proper
        commit/rollback/close handling (H-1 fix).
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def drop_tables(self):
        """Drop all database tables (for testing)."""
        Base.metadata.drop_all(bind=self.engine)

    def dispose(self) -> None:
        """Dispose of the engine's connection pool.

        Call this when done with a short-lived DatabaseManager to release
        connections back to the pool. Safe to call multiple times.
        """
        try:
            self.engine.dispose()
        except Exception as e:
            logger.warning(f"Error disposing engine: {e}")


@contextmanager
def get_db(database_path: Optional[str] = None):
    """Provide a transactional scope around a series of operations."""
    if database_path is None:
        # Check environment variable for test database
        database_path = os.environ.get("HEPHAESTUS_TEST_DB", "hephaestus.db")
    db_manager = DatabaseManager(database_path)
    db = db_manager.get_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def utc_now() -> datetime:
    """Return the current UTC time as a naive datetime.

    Replaces all datetime.utcnow() calls (deprecated in Python 3.12).
    Uses datetime.now(timezone.utc) and strips tzinfo so the rest of the
    codebase continues to work with naive datetimes — every stored/
    compared timestamp uses UTC, and mixing aware/naive in the same
    comparison raises TypeError.
    """
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def get_default_db_manager() -> DatabaseManager:
    """Return a DatabaseManager using the default database path.

    Consolidates the repeated DatabaseManager(None) pattern — callers
    that need a specific path should construct DatabaseManager directly.
    """
    return DatabaseManager(None)


def get_project_info_for_workflow(session, workflow_id: Optional[str]):
    """Resolve a workflow's (project_id, project_name), or (None, None) if
    workflow_id is missing, the workflow has no project_id, or nothing
    matches. Never raises -- callers (e.g. WebSocket/SSE broadcast sites)
    use this to label events by project without letting a lookup failure
    take down an otherwise-successful action.
    """
    if not workflow_id:
        return None, None
    try:
        wf = session.query(Workflow).filter_by(id=workflow_id).first()
        if not wf or not wf.project_id:
            return None, None
        proj = session.query(AutopilotProject).filter_by(id=wf.project_id).first()
        return wf.project_id, (proj.name if proj else None)
    except Exception:
        return None, None


def resolve_project_for_workflow(workflow_id: Optional[str]):
    """Same as get_project_info_for_workflow, but opens and closes its own
    session -- for call sites (mainly WebSocket/SSE broadcast points) that
    don't already have one open. Never raises."""
    if not workflow_id:
        return None, None
    try:
        with get_db() as session:
            return get_project_info_for_workflow(session, workflow_id)
    except Exception:
        return None, None
