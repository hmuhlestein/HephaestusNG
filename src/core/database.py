"""Database models and schema for Hephaestus."""

import logging
import os
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    exc as sqlalchemy_exc,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import backref, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

Base = declarative_base()
logger = logging.getLogger(__name__)


# Status enums - use these instead of string literals for type safety (L-1)
class AgentStatus:
    """Valid agent status values."""
    IDLE = "idle"
    WORKING = "working"
    STUCK = "stuck"
    TERMINATED = "terminated"
    
    ALL = {IDLE, WORKING, STUCK, TERMINATED}


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
    
    ALL = {PENDING, QUEUED, BLOCKED, ASSIGNED, IN_PROGRESS, UNDER_REVIEW,
           VALIDATION_IN_PROGRESS, NEEDS_WORK, DONE, FAILED, DUPLICATED}
    
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
        CheckConstraint("status IN ('idle', 'working', 'stuck', 'terminated')"),
        default="idle",
        nullable=False,
    )
    cli_type = Column(String, nullable=False)  # claude, codex, etc.
    tmux_session_name = Column(String, unique=True)
    current_task_id = Column(String, ForeignKey("tasks.id"))
    last_activity = Column(DateTime, default=datetime.utcnow)
    health_check_failures = Column(Integer, default=0)
    restart_count = Column(Integer, default=0)  # Tracks restart attempts
    cli_model = Column(String, nullable=True)  # Per-agent model override

    # Validation-related fields
    agent_type = Column(
        String,
        CheckConstraint(
            "agent_type IN ('phase', 'validator', 'result_validator', 'monitor', 'diagnostic', 'orchestrator')"
        ),
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
        CheckConstraint(
            "status IN ('pending', 'queued', 'blocked', 'assigned', 'in_progress', 'under_review', 'validation_in_progress', 'needs_work', 'done', 'failed', 'duplicated')"
        ),
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
    workflow_id = Column(
        String, ForeignKey("workflows.id")
    )  # Workflow this task is part of
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    completion_notes = Column(Text)
    failure_reason = Column(Text)
    estimated_complexity = Column(Integer)
    action = Column(String, default="")  # Engine action: 'continue', 'retry', 'goto'

    # Validation-related fields
    review_done = Column(Boolean, default=False)
    validation_enabled = Column(Boolean, default=False)
    validation_iteration = Column(Integer, default=0)
    last_validation_feedback = Column(Text)

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
    priority_boosted = Column(
        Boolean, default=False
    )  # If manually boosted to bypass queue

    # Task dependency and concurrency fields
    depends_on = Column(JSON)  # List of task IDs that must complete before this one
    parallel_group = Column(
        String
    )  # Tasks in same group can run in parallel; different groups are sequential
    max_concurrent = Column(
        Integer, default=1
    )  # Max agents working on this task simultaneously

    # Ticket tracking integration
    ticket_id = Column(
        String, ForeignKey("tickets.id")
    )  # Associated ticket (required when ticket tracking enabled)
    related_ticket_ids = Column(JSON)  # List of related ticket IDs for context

    # Relationships
    assigned_agent = relationship("Agent", foreign_keys=[assigned_agent_id])
    duplicate_of = relationship(
        "Task", remote_side=[id], foreign_keys=[duplicate_of_task_id], post_update=True
    )
    parent_task = relationship(
        "Task", remote_side=[id], foreign_keys=[parent_task_id], backref="subtasks"
    )
    created_by_agent = relationship(
        "Agent", back_populates="created_tasks", foreign_keys=[created_by_agent_id]
    )
    memories = relationship("Memory", back_populates="task")
    phase = relationship("Phase", back_populates="tasks")
    workflow = relationship("Workflow", backref="tasks")
    results = relationship("AgentResult", back_populates="task")
    ticket = relationship("Ticket", foreign_keys=[ticket_id], backref="related_tasks")


class Memory(Base):
    """Memory model for storing agent discoveries and learnings."""

    __tablename__ = "memories"

    id = Column(String, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)
    content = Column(Text, nullable=False)
    memory_type = Column(
        String,
        CheckConstraint(
            "memory_type IN ('error_fix', 'discovery', 'decision', 'learning', 'warning', 'codebase_knowledge')"
        ),
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
    created_at = Column(
        DateTime, default=datetime.utcnow, nullable=False
    )  # Added for compatibility
    agent_id = Column(
        String, ForeignKey("agents.id"), nullable=True
    )  # Made nullable for conductor logs
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


class WorkflowDefinition(Base):
    """Workflow definition model representing a reusable workflow template."""

    __tablename__ = "workflow_definitions"

    id = Column(String, primary_key=True)  # e.g., "prd-to-software"
    name = Column(String, nullable=False)  # "PRD to Software Builder"
    description = Column(String)
    phases_config = Column(JSON)  # Serialized phase definitions
    workflow_config = Column(
        JSON
    )  # has_result, result_criteria, on_result_found, launch_template, etc.
    orchestrator_config = Column(
        JSON
    )  # Orchestrator config for phase evaluation and flow control
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    executions = relationship("Workflow", back_populates="definition")


class Workflow(Base):
    """Workflow model representing a collection of phases (an execution instance)."""

    __tablename__ = "workflows"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(
        String
    )  # User-provided name/description for this execution (e.g., "My URL Shortener")
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
    project_id = Column(
        String, ForeignKey("autopilot_projects.id", ondelete="SET NULL"), nullable=True
    )

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

    # Relationships
    definition = relationship("WorkflowDefinition", back_populates="executions")
    design = relationship(
        "AutopilotDesign", foreign_keys=[design_id], backref="workflows"
    )
    project = relationship(
        "AutopilotProject", foreign_keys=[project_id], backref="workflows"
    )
    phases = relationship("Phase", back_populates="workflow", order_by="Phase.order")
    result = relationship("WorkflowResult", foreign_keys=[result_id])
    all_results = relationship(
        "WorkflowResult", foreign_keys="WorkflowResult.workflow_id"
    )
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
    working_directory = Column(
        String
    )  # Default working directory for agents in this phase

    # Validation configuration
    validation = Column(JSON)  # Stores validation criteria and settings

    # Per-phase CLI configuration (optional - falls back to global defaults)
    cli_tool = Column(
        String, nullable=True
    )  # "claude", "opencode", "droid", "codex", "pi", "swarm"
    cli_model = Column(
        String, nullable=True
    )  # "sonnet", "opus", "haiku", "GLM-4.6", etc.
    glm_api_token_env = Column(
        String, nullable=True
    )  # Environment variable name for GLM token
    thinking_level = Column(
        String, nullable=True
    )  # pi reasoning budget: off|minimal|low|medium|high|xhigh

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
        CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')"
        ),
        default="pending",
        nullable=False,
    )
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    completion_summary = Column(Text)

    # Relationships
    phase = relationship("Phase", back_populates="executions")


class AgentWorktree(Base):
    """Track git worktree isolation for agents."""

    # TODO: Rename column worktree_path → branch_path (pending migration)

    __tablename__ = "agent_worktrees"

    agent_id = Column(String, ForeignKey("agents.id"), primary_key=True)
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
        CheckConstraint(
            "commit_type IN ('parent_checkpoint', 'validation_ready', 'final', 'auto_save', 'conflict_resolution')"
        ),
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
    agent = relationship(
        "Agent", backref="conflict_resolutions", overlaps="conflict_resolutions"
    )
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
        CheckConstraint(
            "result_type IN ('implementation', 'analysis', 'fix', 'design', 'test', 'documentation')"
        ),
        nullable=False,
    )
    summary = Column(Text, nullable=False)
    verification_status = Column(
        String,
        CheckConstraint(
            "verification_status IN ('unverified', 'verified', 'disputed')"
        ),
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
    extra_files = Column(
        JSON, nullable=True, default=list
    )  # List of additional file paths (e.g., patches, reproduction scripts)
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
    workflow = relationship(
        "Workflow", foreign_keys=[workflow_id], back_populates="all_results"
    )
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
    last_claude_message_marker = Column(
        String(100)
    )  # NEW: Marker for next cycle to identify new content

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
    agent1 = relationship(
        "Agent", foreign_keys=[agent1_id], backref="duplicates_as_agent1"
    )
    agent2 = relationship(
        "Agent", foreign_keys=[agent2_id], backref="duplicates_as_agent2"
    )


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
    agent = relationship(
        "Agent", foreign_keys=[diagnostic_agent_id], backref="diagnostic_runs"
    )
    task = relationship(
        "Task", foreign_keys=[diagnostic_task_id], backref="diagnostic_runs"
    )


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
    ticket_type = Column(
        String(50), nullable=False
    )  # bug, feature, improvement, task, spike, etc.
    priority = Column(String(20), nullable=False)  # low, medium, high, critical
    status = Column(
        String(50), nullable=False
    )  # Based on board_config columns (fully configurable)

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    started_at = Column(DateTime)  # When work begins
    completed_at = Column(DateTime)  # When marked complete

    # Links & References
    parent_ticket_id = Column(String, ForeignKey("tickets.id"))
    task_id = Column(
        String, ForeignKey("tasks.id")
    )  # Primary task this ticket relates to
    phase_id = Column(
        String, ForeignKey("phases.id")
    )  # Phase where this ticket was created
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
    approval_status = Column(
        String(20), default="auto_approved", nullable=False
    )  # auto_approved, pending_review, approved, rejected
    approval_requested_at = Column(DateTime)  # When approval was requested
    approval_decided_at = Column(DateTime)  # When human made decision
    approval_decided_by = Column(String)  # User/agent who approved/rejected
    rejection_reason = Column(Text)  # Why ticket was rejected

    # Relationships
    workflow = relationship("Workflow", backref="tickets")
    created_by_agent = relationship(
        "Agent", foreign_keys=[created_by_agent_id], backref="created_tickets"
    )
    assigned_agent = relationship(
        "Agent", foreign_keys=[assigned_agent_id], backref="assigned_tickets"
    )
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
    ticket_id = Column(
        String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)

    # Content
    comment_text = Column(Text, nullable=False)
    comment_type = Column(
        String(50), default="general"
    )  # general, status_change, assignment, blocker, resolution

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
    ticket_id = Column(
        String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)

    # Change Information
    change_type = Column(
        String(50), nullable=False
    )  # created, status_changed, assigned, commented, field_updated, commit_linked, reopened, blocked, unblocked
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
    ticket_id = Column(
        String, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False
    )
    agent_id = Column(String, ForeignKey("agents.id"), nullable=False)

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
    link_method = Column(
        String(50), default="manual"
    )  # manual, auto_detected, worktree

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
    initial_status = Column(
        String(50), nullable=False
    )  # Default status for new tickets

    # Settings
    auto_assign = Column(Boolean, default=False)
    require_comments_on_status_change = Column(Boolean, default=False)
    allow_reopen = Column(Boolean, default=True)
    track_time = Column(Boolean, default=False)

    # Human Review Settings
    ticket_human_review = Column(
        Boolean, default=False
    )  # Enable human approval for tickets
    approval_timeout_seconds = Column(Integer, default=1800)  # 30 minutes default

    # Metadata
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

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
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    designs = relationship(
        "AutopilotDesign", back_populates="project", cascade="all, delete-orphan"
    )


class Feature(Base):
    """Feature model representing a decomposed feature from a design document."""

    __tablename__ = "features"

    id = Column(String, primary_key=True)  # feat-<uuid8>
    design_id = Column(String, ForeignKey("autopilot_designs.id"), nullable=False)
    feature_key = Column(
        String(100), nullable=False
    )  # slug from features.json "id" field
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
        CheckConstraint(
            "status IN ('pending', 'active', 'completed', 'failed', 'skipped', 'paused')"
        ),
        nullable=False,
        default="pending",
    )
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    scope_doc_path = Column(
        Text, nullable=True
    )  # abs path to scope.md in permanent record
    feature_record_path = Column(
        Text, nullable=True
    )  # abs path to designs/.../features/<key>/
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)

    # Relationships
    design = relationship("AutopilotDesign", back_populates="features")
    workflow = relationship("Workflow", foreign_keys=[workflow_id])


class AutopilotDesign(Base):
    """A design document within a project's design queue."""

    __tablename__ = "autopilot_designs"

    id = Column(String, primary_key=True)  # Format: des-{uuid}
    project_id = Column(
        String, ForeignKey("autopilot_projects.id", ondelete="CASCADE"), nullable=False
    )
    filename = Column(String(500), nullable=False)
    name = Column(String(500), nullable=False)
    ordinal = Column(Integer, nullable=False, default=0)
    size_bytes = Column(Integer, nullable=False, default=0)
    extension = Column(String(10), nullable=False, default=".md")
    content_hash = Column(String(64), nullable=True)  # SHA-256 for dedup
    status = Column(
        String(20), nullable=False, default="pending"
    )  # pending, processing, decomposing, active, completed, failed, skipped
    feature_folder = Column(
        Text, nullable=True
    )  # Path to feature folder after processing
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    modified_at = Column(DateTime, default=datetime.utcnow)

    # New columns for Feature Model
    file_path = Column(Text, nullable=True)  # absolute path to design file
    designs_folder = Column(Text, nullable=True)  # path to designs/<ts>_<name>_<id>/
    phase0_workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)

    # Relationships
    project = relationship("AutopilotProject", back_populates="designs")
    features = relationship(
        "Feature", back_populates="design", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("project_id", "filename", name="uq_design_project_filename"),
    )


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
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
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
    resolver = Column(
        String, nullable=False
    )  # Python path, e.g. "src.prompts.resolvers.project_name"
    category = Column(String, default="general")  # general, workflow, phase, task
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DatabaseManager:
    """Manager for database operations."""

    def __init__(self, database_path: str = "hephaestus.db"):
        """Initialize database connection."""
        self.database_path = database_path
        self.engine = create_engine(
            f"sqlite:///{database_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False,
        )
        
        # Set SQLite pragmas for concurrent access
        # WAL mode allows concurrent readers alongside a single writer
        # busy_timeout makes writers block-and-retry instead of failing immediately
        @event.listens_for(self.engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
        
        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine,
            expire_on_commit=False  # Prevent DetachedInstanceError bugs (H-0*)
        )

    def create_tables(self):
        """Create all database tables."""
        Base.metadata.create_all(bind=self.engine)

        # Create FTS5 virtual table for ticket search
        self._create_fts5_tables()

        # Create indexes for performance optimization
        self._create_indexes()

        # Migrate new columns for existing databases
        self._migrate_task_dependency_columns()
        self._migrate_autopilot_designs_columns()
        self._migrate_feature_model_columns()
        self._migrate_total_gotos_column()

    def _create_fts5_tables(self):
        """Create FTS5 virtual tables and triggers for ticket search."""
        try:
            with self.engine.connect() as conn:
                # Create FTS5 virtual table for tickets
                conn.execute(
                    text(
                        """
                    CREATE VIRTUAL TABLE IF NOT EXISTS ticket_fts USING fts5(
                        ticket_id UNINDEXED,
                        title,
                        description,
                        tags
                    )
                """
                    )
                )

                # Create triggers to keep FTS5 in sync with tickets table
                # Trigger for INSERT
                conn.execute(
                    text(
                        """
                    CREATE TRIGGER IF NOT EXISTS tickets_fts_insert AFTER INSERT ON tickets BEGIN
                        INSERT INTO ticket_fts(ticket_id, title, description, tags)
                        VALUES (new.id, new.title, new.description,
                                COALESCE(json_extract(new.tags, '$'), ''));
                    END
                """
                    )
                )

                # Trigger for UPDATE
                conn.execute(
                    text(
                        """
                    CREATE TRIGGER IF NOT EXISTS tickets_fts_update AFTER UPDATE ON tickets BEGIN
                        DELETE FROM ticket_fts WHERE ticket_id = old.id;
                        INSERT INTO ticket_fts(ticket_id, title, description, tags)
                        VALUES (new.id, new.title, new.description,
                                COALESCE(json_extract(new.tags, '$'), ''));
                    END
                """
                    )
                )

                # Trigger for DELETE
                conn.execute(
                    text(
                        """
                    CREATE TRIGGER IF NOT EXISTS tickets_fts_delete AFTER DELETE ON tickets BEGIN
                        DELETE FROM ticket_fts WHERE ticket_id = old.id;
                    END
                """
                    )
                )

                conn.commit()
                logger.info("Created FTS5 virtual table and triggers for ticket search")
        except Exception as e:
            logger.debug(f"FTS5 table setup (may already exist): {e}")

    def _create_indexes(self):
        """Create database indexes for performance optimization."""
        try:
            with self.engine.connect() as conn:
                # Tickets table indexes
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_tickets_workflow_status
                    ON tickets(workflow_id, status)
                """
                    )
                )

                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_tickets_workflow_priority
                    ON tickets(workflow_id, priority)
                """
                    )
                )

                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_tickets_assigned_agent
                    ON tickets(assigned_agent_id)
                """
                    )
                )

                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_tickets_created_at
                    ON tickets(created_at)
                """
                    )
                )

                # Ticket comments index
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_ticket_comments_ticket_id
                    ON ticket_comments(ticket_id)
                """
                    )
                )

                # Ticket history index
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_ticket_history_ticket_id
                    ON ticket_history(ticket_id)
                """
                    )
                )

                # Ticket commits index
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_ticket_commits_ticket_id
                    ON ticket_commits(ticket_id)
                """
                    )
                )

                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_ticket_commits_sha
                    ON ticket_commits(commit_sha)
                """
                    )
                )

                # Tasks table indexes for ticket tracking
                conn.execute(
                    text(
                        """
                    CREATE INDEX IF NOT EXISTS idx_tasks_ticket_id
                    ON tasks(ticket_id)
                """
                    )
                )

                conn.commit()
                logger.info("Created performance indexes for ticket tracking system")
        except Exception as e:
            logger.debug(f"Index creation (may already exist): {e}")

    def _migrate_task_dependency_columns(self):
        """Add dependency columns to tasks table for existing databases."""
        try:
            with self.engine.connect() as conn:
                # Add depends_on column
                try:
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN depends_on TEXT"))
                except Exception:
                    pass  # Column already exists

                # Add parallel_group column
                try:
                    conn.execute(
                        text("ALTER TABLE tasks ADD COLUMN parallel_group TEXT")
                    )
                except Exception:
                    pass  # Column already exists

                # Add max_concurrent column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE tasks ADD COLUMN max_concurrent INTEGER DEFAULT 1"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                conn.commit()
                logger.info("Migrated task dependency columns")
        except Exception as e:
            logger.debug(f"Task dependency migration (may already exist): {e}")

    def _migrate_autopilot_designs_columns(self):
        """Add status/content_hash/feature_folder/completed_at to autopilot_designs for existing databases."""
        try:
            with self.engine.connect() as conn:
                # Add content_hash column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE autopilot_designs ADD COLUMN content_hash VARCHAR(64)"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                # Add status column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE autopilot_designs ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'pending'"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                # Add feature_folder column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE autopilot_designs ADD COLUMN feature_folder TEXT"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                # Add completed_at column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE autopilot_designs ADD COLUMN completed_at DATETIME"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                conn.commit()
                logger.info("Migrated autopilot_designs columns")
        except Exception as e:
            logger.debug(f"autopilot_designs migration (may already exist): {e}")

        # Add thinking_level to phases for existing databases
        try:
            with self.engine.connect() as conn:
                try:
                    conn.execute(
                        text("ALTER TABLE phases ADD COLUMN thinking_level VARCHAR")
                    )
                except Exception:
                    pass  # Column already exists
                conn.commit()
        except Exception as e:
            logger.debug(f"phases.thinking_level migration (may already exist): {e}")

        # Add design_id FK to workflows for existing databases (§9.7)
        try:
            with self.engine.connect() as conn:
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE workflows ADD COLUMN design_id VARCHAR REFERENCES autopilot_designs(id)"
                        )
                    )
                except Exception:
                    pass  # Column already exists
                conn.commit()
        except Exception as e:
            logger.debug(f"workflows.design_id migration (may already exist): {e}")

        # Add cli_model to agents table if missing
        try:
            with self.engine.connect() as conn:
                try:
                    conn.execute(text("ALTER TABLE agents ADD COLUMN cli_model TEXT"))
                except sqlalchemy_exc.OperationalError:
                    pass  # Column already exists
                conn.commit()
                logger.info("Migrated agents.cli_model column")
        except Exception as e:
            logger.debug(f"agents.cli_model migration (may already exist): {e}")

    def _migrate_feature_model_columns(self):
        """Add Feature model columns to autopilot_designs and workflows for existing databases.

        Idempotent - safe to call on every startup.
        """
        # Add new columns to autopilot_designs
        try:
            with self.engine.connect() as conn:
                # Add file_path column
                try:
                    conn.execute(
                        text("ALTER TABLE autopilot_designs ADD COLUMN file_path TEXT")
                    )
                except Exception:
                    pass  # Column already exists

                # Add designs_folder column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE autopilot_designs ADD COLUMN designs_folder TEXT"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                # Add phase0_workflow_id column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE autopilot_designs ADD COLUMN phase0_workflow_id VARCHAR REFERENCES workflows(id)"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                conn.commit()
                logger.info("Migrated autopilot_designs feature model columns")
        except Exception as e:
            logger.debug(
                f"autopilot_designs feature model migration (may already exist): {e}"
            )

        # Add new columns to workflows
        try:
            with self.engine.connect() as conn:
                # Add workflow_type column
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE workflows ADD COLUMN workflow_type VARCHAR DEFAULT NULL"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                # Add feature_id column.
                # Note: SQLite silently ignores FK declarations in ALTER TABLE, so
                # the REFERENCES clause is documentation only — cascade deletes and
                # constraint checks are enforced by the ORM layer, not the DB engine.
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE workflows ADD COLUMN feature_id VARCHAR REFERENCES features(id)"
                        )
                    )
                except Exception:
                    pass  # Column already exists

                conn.commit()
                logger.info("Migrated workflows feature model columns")
        except Exception as e:
            logger.debug(f"workflows feature model migration (may already exist): {e}")

        # Create features table if it doesn't exist
        try:
            Base.metadata.create_all(
                self.engine, tables=[Feature.__table__], checkfirst=True
            )
            logger.info("Ensured features table exists")
        except Exception as e:
            logger.debug(f"features table creation (may already exist): {e}")

    def _migrate_total_gotos_column(self):
        """Add workflows.total_gotos for existing databases.

        Idempotent - safe to call on every startup.
        """
        try:
            with self.engine.connect() as conn:
                try:
                    conn.execute(
                        text(
                            "ALTER TABLE workflows ADD COLUMN total_gotos INTEGER DEFAULT 0 NOT NULL"
                        )
                    )
                except Exception:
                    pass  # Column already exists
                conn.commit()
                logger.info("Migrated workflows.total_gotos column")
        except Exception as e:
            logger.debug(f"workflows.total_gotos migration (may already exist): {e}")

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


# Context manager for database sessions
from sqlalchemy.sql import text


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
