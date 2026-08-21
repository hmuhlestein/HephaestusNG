"""Cross-cutting helpers, request/response models, and server state for the MCP server.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import (
    FastAPI,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from git import Repo
from pydantic import BaseModel, Field, validator

from src.agents.manager import AgentManager
from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    Workflow,
)
from src.core.simple_config import get_config
from src.core.worktree_manager import WorktreeManager
from src.mcp.agents_api import router as agents_router
from src.mcp.memory_api import (
    router as memory_router,
)
from src.mcp.messaging_api import router as messaging_router
from src.mcp.server.connection_broadcaster import ConnectionBroadcaster
from src.mcp.server.state_bootstrap import load_active_project, migrate_is_active_column

# Import routers at module level for test compatibility
from src.mcp.tickets_api import router as tickets_router
from src.memory.embedding_factory import EmbeddingProvider
from src.memory.rag import RAGSystem
from src.memory.store_factory import VectorStoreProtocol, create_vector_store
from src.phases import PhaseManager
from src.prompts.loader import get_prompt
from src.services.queue_service import QueueService
from src.services.result_validator_service import ResultValidatorService
from src.services.task_similarity_service import TaskSimilarityService

logger = logging.getLogger(__name__)

SELF_REVIEW_CHECKLIST_PROMPT = "\n" + get_prompt("self_review_checklist")

def _resolve_worktree_path(session, task) -> Optional[str]:
    """The workflow's shared worktree, for self-review telemetry.

    Deliberately does NOT fall back to the task's agent's own isolated
    worktree if the shared one is missing -- that class of fallback is what
    let a worktree-tracking bug in cleanup_all_stale_branches go unnoticed
    (see worktree_manager.py's fix and its regression test). A missing
    working_directory here means telemetry just can't compute a diff for
    this round, logged plainly by the caller; it should not silently
    resolve against a different worktree instead.
    """
    if task.workflow_id:
        wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
        if wf and wf.working_directory:
            return wf.working_directory
    return None

def _resolve_worktree_head_sha(session, task) -> Optional[str]:
    """Current git HEAD commit of the worktree the task's agent is working
    in, for self-review telemetry (see docs/GAP_CHECK_SELF_LOOP_DESIGN.md).
    Best-effort: returns None if the worktree can't be resolved or read.
    """
    worktree_path = _resolve_worktree_path(session, task)
    if not worktree_path:
        return None
    try:
        return Repo(worktree_path).head.commit.hexsha
    except Exception as e:
        logger.debug(f"[SELF-REVIEW] Could not read worktree HEAD for task {task.id[:8]}: {e}")
        return None



# Initialize FastAPI app

app = FastAPI(
    title="Hephaestus MCP Server",
    description="Model Context Protocol server for AI agent orchestration",
    version="1.0.0",
)


# Add CORS middleware

config = get_config()

if config.server.enable_cors:
    # SECURITY: Use explicit origins instead of wildcard '*' when credentials are allowed.
    # Wildcard + credentials is a security risk (allows credential theft from any origin).
    # Default to localhost origins for development; set CORS_ORIGINS env var for production.
    import os

    _cors_origins_str = os.environ.get("CORS_ORIGINS", "")
    if _cors_origins_str:
        _cors_origins = [o.strip() for o in _cors_origins_str.split(",") if o.strip()]
    else:
        # Development defaults: localhost only
        _config = get_config()
        _frontend_port = _config.server.frontend_port
        _backend_port = _config.server.mcp_port
        _cors_origins = [f"http://localhost:{_frontend_port}",
                         "http://localhost:3000",
                         f"http://localhost:{_backend_port}",
                         f"http://127.0.0.1:{_frontend_port}",
                         "http://127.0.0.1:3000",
                         f"http://127.0.0.1:{_backend_port}",
                         ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

# Include routers at module level (needed for test compatibility)
app.include_router(tickets_router)
app.include_router(agents_router)
app.include_router(messaging_router)
app.include_router(memory_router)


# Request/Response Models



class CreateTaskRequest(BaseModel):
    """Request model for creating a task."""

    task_description: str = Field(..., description="Raw task description", max_length=50000)
    done_definition: str = Field(..., description="What constitutes completion", max_length=10000)
    ai_agent_id: str = Field(..., description="ID of requesting agent")
    workflow_id: Optional[str] = Field(default=None, description="ID of the workflow this task belongs to")
    priority: Optional[str] = Field(default="medium", pattern="^(low|medium|high)$")
    parent_task_id: Optional[str] = Field(default=None, description="Parent task ID for sub-tasks")
    phase_id: Optional[str] = Field(default=None, description="Phase ID for workflow-based tasks")
    phase_order: Optional[int] = Field(default=None, description="Phase order number (alternative to phase_id)")
    cwd: Optional[str] = Field(default=None, description="Working directory for the task")
    ticket_id: Optional[str] = Field(
        default=None,
        description="Associated ticket ID (required when ticket tracking enabled)",
    )
    depends_on: Optional[List[str]] = Field(default=None, description="List of task IDs that must complete before this one")
    parallel_group: Optional[str] = Field(
        default=None,
        description="Tasks in same group can run in parallel; different groups are sequential",
    )
    max_concurrent: Optional[int] = Field(default=1, description="Max agents working on this task simultaneously")
    context: Optional[str] = Field(
        default=None,
        description="Additional context for the agent (e.g., design document content, requirements summary)",
        max_length=100000,
    )

    @validator("ticket_id", pre=True, always=True)
    @classmethod
    def validate_ticket_id(cls, v):
        """Strip whitespace and reject whitespace-only ticket_id values."""
        if v is None:
            return v
        # Strip leading and trailing whitespace
        stripped = v.strip()
        # Reject whitespace-only values (after stripping, result is empty)
        if v and not stripped:
            raise ValueError("ticket_id cannot be whitespace-only. Provide a valid ticket identifier or omit the field.")
        # Return the stripped value (trimmed)
        return stripped if stripped else v

class CreateTaskResponse(BaseModel):
    """Response model for task creation."""

    task_id: str
    enriched_description: str
    assigned_agent_id: str
    estimated_completion_time: int  # minutes
    status: str

class UpdateTaskStatusRequest(BaseModel):
    """Request model for updating task status."""

    task_id: str
    status: str = Field(..., pattern="^(done|failed)$")
    summary: str = Field(default="", description="What was accomplished. Required if status is 'done'")
    key_learnings: List[str] = Field(default=[], description="Important discoveries")
    code_changes: Optional[List[str]] = Field(default=None, description="Files modified/created")
    failure_reason: Optional[str] = Field(default=None, description="Required if status is 'failed'")
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured data (verdict, counts, etc.) — folded into summary",
    )

class UpdateTaskStatusResponse(BaseModel):
    """Response model for task status update."""

    success: bool
    message: str
    termination_scheduled: bool

class RegisterWorkflowDefinitionRequest(BaseModel):
    """Request model for registering a workflow definition."""

    id: str = Field(..., description="Unique ID for the workflow definition")
    name: str = Field(..., description="Human-readable name")
    description: str = Field(default="", description="Description of the workflow")
    phases_config: List[Dict[str, Any]] = Field(..., description="Phase configurations")
    workflow_config: Optional[Dict[str, Any]] = Field(default=None, description="Workflow configuration")

class StartWorkflowRequest(BaseModel):
    """Request model for starting a workflow execution."""

    definition_id: str = Field(..., description="ID of the workflow definition to execute")
    description: str = Field(..., description="Description/name of this workflow execution")
    working_directory: Optional[str] = Field(default=None, description="Working directory for the workflow")
    launch_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Parameters from launch template to substitute into phases",
    )
    design_id: Optional[str] = Field(
        default=None,
        description="autopilot_designs.id that spawned this execution (§9.7)",
    )

class ServerState:
    """Global server state."""

    def __init__(self) -> None:
        # Annotated: an unannotated __init__ is "untyped" to mypy, which
        # skips checking its body entirely (see
        # ConnectionBroadcaster.__init__'s comment) -- meaning it never
        # learned self._broadcaster's type, and the active_websockets/
        # sse_queues properties below both read as "Returning Any" despite
        # being correctly typed at runtime.
        self.db_manager: Optional[DatabaseManager] = None
        self.vector_store: Optional[VectorStoreProtocol] = None
        self.llm_provider = None
        self.agent_manager: Optional[AgentManager] = None
        self.rag_system: Optional[RAGSystem] = None
        self.phase_manager: Optional[PhaseManager] = None
        self.branch_manager: Optional[WorktreeManager] = None
        self.result_validator_service: Optional[ResultValidatorService] = None
        self.embedding_service: Optional[EmbeddingProvider] = None
        self.task_similarity_service: Optional[TaskSimilarityService] = None
        self.queue_service: Optional[QueueService] = None
        # Connection fan-out (SOLID review 1.6): a distinct responsibility
        # from composing the service instances above, extracted to
        # ConnectionBroadcaster. active_websockets/sse_queues/broadcast_update
        # stay on ServerState as properties/a delegator -- several call sites
        # mutate or reassign them directly on server_state (append/remove, and
        # one test's monkeypatch.setattr), and this keeps that surface
        # unchanged rather than pushing the migration onto every caller.
        self._broadcaster = ConnectionBroadcaster()
        self.background_queue_processor_task: Optional[asyncio.Task] = None
        self.phase_advancement_sweep_task: Optional[asyncio.Task] = None
        self.shutdown_event: asyncio.Event = asyncio.Event()

    @property
    def active_websockets(self) -> List[WebSocket]:
        return self._broadcaster.active_websockets

    @active_websockets.setter
    def active_websockets(self, value: List[WebSocket]) -> None:
        self._broadcaster.active_websockets = value

    @property
    def sse_queues(self) -> List[asyncio.Queue]:
        return self._broadcaster.sse_queues

    @sse_queues.setter
    def sse_queues(self, value: List[asyncio.Queue]) -> None:
        self._broadcaster.sse_queues = value

    async def initialize(self):
        """Initialize server components."""
        config = get_config()

        # Initialize database
        self.db_manager = DatabaseManager(str(config.paths.database_path))
        self.db_manager.create_tables()

        # Migrate: add is_active column to existing autopilot_projects table
        migrate_is_active_column(self.db_manager)

        # Load active project from DB and apply to config BEFORE creating managers
        load_active_project(self.db_manager, config)

        # Initialize vector store via the backend factory (turbovec python-only by
        # default per VECTOR_STORE_BACKEND / config). Do NOT hardcode the Qdrant
        # VectorStoreManager — that ignored the config and produced
        # 'QdrantClient has no attribute search' / connection-refused errors when
        # Qdrant wasn't running.
        self.vector_store = create_vector_store()

        # Initialize LLM provider using get_llm_provider()
        # This automatically handles multi-provider config or falls back to legacy single-provider
        from src.interfaces.llm_interface import get_llm_provider

        self.llm_provider = get_llm_provider()

        # Initialize phase manager first (needed by agent manager)
        self.phase_manager = PhaseManager(db_manager=self.db_manager)

        # Initialize worktree manager
        self.branch_manager = WorktreeManager(db_manager=self.db_manager)

        # Initialize agent manager with phase manager
        self.agent_manager = AgentManager(
            db_manager=self.db_manager,
            llm_provider=self.llm_provider,
            phase_manager=self.phase_manager,
        )

        # Initialize result validator service
        self.result_validator_service = ResultValidatorService(
            db_manager=self.db_manager,
            phase_manager=self.phase_manager,
        )

        # Initialize embedding and similarity services for task dedup using the
        # configurable embedding provider (fastembed by default — no OpenAI key needed).
        # Previously this was gated on config.openai_api_key, which silently disabled
        # dedup for python-only (openrouter) setups even though it's enabled by config.
        if config.task_dedup.task_dedup_enabled:
            try:
                from src.memory.embedding_factory import create_embedding_provider
                from src.memory.store_factory import validate_embedding_dimension_compatibility
                from src.services.ticket_search_service import TicketSearchService

                self.embedding_service = create_embedding_provider()
                # Fail fast here, at startup, rather than letting every
                # store_memory call fail silently later (see the
                # function's own docstring -- every current caller
                # swallows that per-call ValueError).
                validate_embedding_dimension_compatibility(
                    self.vector_store, self.embedding_service.get_dim()
                )
                # Share the same embedding provider instance with TicketSearchService
                # instead of letting it create its own separate model load.
                TicketSearchService._embedding_provider = self.embedding_service
                self.task_similarity_service = TaskSimilarityService(self.db_manager, self.embedding_service)
                logger.info("Task deduplication service initialized (embedding via configurable provider)")
            except Exception as e:
                logger.warning(f"Task deduplication disabled — embedding provider init failed: {e}")
        else:
            logger.info("Task deduplication disabled by configuration")

        # Initialize RAG system. Constructed *after* the embedding provider
        # above so it can share that one instance -- Phase 2 §4.7's goal (a).
        # Built earlier, self.embedding_service was still None and RAGSystem
        # silently fell back to loading a third copy of the same model.
        # Still None when task dedup is off; the fallback covers that, since
        # RAG must not be disabled by a dedup toggle.
        self.rag_system = RAGSystem(
            vector_store=self.vector_store,
            llm_provider=self.llm_provider,
            embedding_provider=self.embedding_service,
        )

        # Initialize queue service
        self.queue_service = QueueService(
            db_manager=self.db_manager,
            max_concurrent_agents=config.mcp.max_concurrent_agents,
            cli_model_concurrency_limits=config.agents.cli_model_concurrency_limits,
            default_cli_tool=config.agents.default_cli_tool,
            default_cli_model=config.agents.cli_model,
            cli_model_fallback=config.agents.cli_model_fallback,
            secondary_cli_model_fallback=config.agents.secondary_cli_model_fallback,
        )
        logger.info(f"Queue service initialized with max_concurrent_agents={config.mcp.max_concurrent_agents}")

        logger.info("Server state initialized successfully")

    async def broadcast_update(
        self,
        message: Dict[str, Any],
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
    ):
        """Broadcast update to all connected WebSocket and SSE clients.

        Delegates to ConnectionBroadcaster -- see its docstring for the
        project_id/project_name behaviour.
        """
        return await self._broadcaster.broadcast_update(message, project_id, project_name)



# Initialize server state

server_state = ServerState()


# Register with app_context so other modules can reach shared state without
# importing this route module (breaks the circular-import workaround used
# throughout the service layer — see docs/SOLID_OO_REVIEW.md 1.6/3.11).
from src.core.app_context import set_app_state as _set_app_state  # noqa: E402

_set_app_state(server_state)


# ==================== SECURITY: Agent Authentication ====================

# Known system agents that don't require token validation

# Canonical definitions live in src/core/agent_identity -- this module is
# imported by src/mcp/agents_api.py, which also needs them, so they cannot
# live here without a cycle. Re-exported for the existing importers.
from src.core.agent_identity import (  # noqa: E402
    KNOWN_SYSTEM_AGENTS,
    MCP_AGENT_PREFIX,
    ROOT_AGENT_ID,
    SDK_AGENT_PREFIX,
    is_known_system_identity,
    is_root_agent,
    is_sdk_or_root_agent,
)

__all__ = [
    "KNOWN_SYSTEM_AGENTS",
    "MCP_AGENT_PREFIX",
    "ROOT_AGENT_ID",
    "SDK_AGENT_PREFIX",
    "is_known_system_identity",
    "is_root_agent",
    "is_sdk_or_root_agent",
]

async def verify_agent_authentication(agent_id: str) -> bool:
    """Verify agent is authenticated and authorized.

    SECURITY: Validates agent identity before allowing operations.
    Known system agents are trusted; others must be registered.

    Args:
        agent_id: The agent ID from X-Agent-ID header

    Returns:
        True if agent is authenticated, False otherwise
    """
    # System agents are always trusted
    if agent_id in KNOWN_SYSTEM_AGENTS:
        return True

    # SDK agents are trusted (started by SDK)
    if agent_id.startswith("sdk-") or agent_id.startswith("mcp-"):
        return True

    # Check if agent exists in database. create_agent_for_task_direct runs
    # agent creation in a background thread via asyncio.run() (a separate
    # thread from this request-handling one), sharing the same StaticPool
    # SQLite connection — under load, a freshly-committed Agent row has
    # occasionally not been visible yet to a query landing microseconds
    # later on this thread (observed live: "Rejected unknown agent" for an
    # agent whose row demonstrably existed seconds later). One short retry
    # bridges that race without weakening the check itself — an agent that
    # is genuinely unknown or terminated still gets rejected either way.

    for attempt in range(2):
        try:
            session = server_state.db_manager.get_session()
            try:
                agent = session.query(Agent).filter_by(id=agent_id).first()
                if agent and agent.status in ["idle", "working", "starting"]:
                    # Agent exists and is active - trusted
                    return True
                elif agent and agent.status == "terminated":
                    # Agent was terminated - reject (not a visibility race, no retry)
                    logger.warning(f"Rejected terminated agent: {agent_id[:8]}")
                    return False
                elif attempt == 0:
                    # Possibly a transient cross-thread commit-visibility race —
                    # retry once before rejecting as genuinely unknown.
                    import asyncio as _asyncio

                    await _asyncio.sleep(0.3)
                    continue
                else:
                    logger.warning(f"Rejected unknown agent: {agent_id[:8]}")
                    return False
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Agent auth check failed: {e}")
            return False
    return False

def _git_expert_already_landed(session, task, config) -> bool:
    """Check whether a git_expert task's actual git work (commit +
    merge + push) already succeeded, even though the agent never reported
    "done" -- e.g. its final complete_my_task call was lost to a
    connection drop during a backend restart. Observed live: task
    2b98bf74/agent ada62108 did the real merge+push (verified after the
    fact via `git log`), but the connection dropped exactly as the
    backend restarted, so the completion call never reached the handler.
    Without this check, the orphan-resume loop below blindly redispatches
    a fresh agent to redo the whole git sequence from scratch -- usually
    a harmless no-op merge, but a wasted retry cycle every time this
    collision happens.

    Scoped to git_expert specifically: it's the one phase whose real
    "done" signal is external git state, not a file artifact
    verify_output_artifact can check (spec.py lists it in
    OPTIONAL_PHASES, with no required output at all).
    """
    from pathlib import Path

    from src.core.database import Workflow

    phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if not phase or phase.name != "git_expert":
        return False

    wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
    wd = wf.working_directory if wf else None
    if not wd or not Path(wd).exists():
        return False

    try:
        import subprocess

        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=wd, capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        if not branch or branch == "HEAD":
            return False
        # <project>/.worktrees/wt_X -> <project>, the base repo the branch
        # would be merged into.
        base_repo = Path(wd).parent.parent
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch, config.git.base_branch],
            cwd=base_repo, capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False

def _tmux_session_alive(session_name: str) -> bool:
    """True if the named tmux session currently exists."""
    if not session_name:
        return False
    try:
        import subprocess

        r = subprocess.run(["tmux", "has-session", "-t", session_name], capture_output=True, timeout=3)
        return r.returncode == 0
    except Exception:
        return False

def _build_phase_dict(phase) -> dict:
    """Serialize a source sdk.models.Phase into the dict form stored in
    WorkflowDefinition.phases_config, refreshed from YAML at every startup."""
    phase_dict = {
        "id": phase.id,
        "name": phase.name,
        "description": phase.description,
        "done_definitions": phase.done_definitions,
        "working_directory": phase.working_directory,
    }
    if phase.additional_notes:
        phase_dict["additional_notes"] = phase.additional_notes
    if phase.outputs:
        phase_dict["outputs"] = phase.outputs
    if phase.next_steps:
        phase_dict["next_steps"] = phase.next_steps
    # NOTE: `phase.validation` is NOT carried through here even
    # though phase_manager.py reads phase_config.get("validation")
    # when building Phase DB rows -- a pre-existing gap (not
    # introduced by self_review below) that's why validation has
    # never actually fired for any phase despite the plumbing
    # existing. Not fixed here — out of scope for self_review,
    # flagging so it isn't mistaken for "already working."
    if phase.self_review:
        phase_dict["self_review"] = phase.self_review
    if phase.cli_tool:
        phase_dict["cli_tool"] = phase.cli_tool
    if phase.cli_model:
        phase_dict["cli_model"] = phase.cli_model
    if phase.fallback_cli_tool:
        phase_dict["fallback_cli_tool"] = phase.fallback_cli_tool
    if phase.fallback_cli_model:
        phase_dict["fallback_cli_model"] = phase.fallback_cli_model
    if phase.glm_api_token_env:
        phase_dict["glm_api_token_env"] = phase.glm_api_token_env
    if phase.thinking_level:
        phase_dict["thinking_level"] = phase.thinking_level
    return phase_dict

def _touch_agent_activity(agent_id: str) -> None:
    """Update agent's last_activity timestamp (best-effort, never raises)."""
    try:
        session = server_state.db_manager.get_session()
        try:
            from src.core.database import Agent

            agent = session.query(Agent).filter_by(id=agent_id).first()
            if agent:
                from datetime import datetime

                agent.last_activity = datetime.utcnow()
                session.commit()
        finally:
            session.close()
    except Exception:
        pass  # non-critical

def _resolve_agent_current_phase(agent_id: str, workflow_id: str) -> Optional[str]:
    """Resolve the agent's current phase ID from their assigned task.

    M-6 fix: Make phase context implicit for subtask creation.
    The server already knows the agent's current phase from its assigned task,
    so agents don't need to specify phase_id for subtasks within their own phase.

    Returns:
        Phase ID string if found, None otherwise.
    """
    if not agent_id or not workflow_id:
        return None

    from src.core.database import Task as TaskModel

    session = server_state.db_manager.get_session()
    try:
        # Find the agent's most recent assigned task in this workflow
        own_task = (
            session.query(TaskModel)
            .filter(
                TaskModel.assigned_agent_id == agent_id,
                TaskModel.workflow_id == workflow_id,
            )
            .order_by(TaskModel.created_at.desc())
            .first()
        )
        if own_task and own_task.phase_id:
            return own_task.phase_id
    except Exception as e:
        # A DB failure here (not just "no assigned task found") previously
        # logged at debug -- invisible at production log levels -- and
        # then re-surfaced downstream as a misleading client-input-
        # validation error telling the agent to supply phase_id explicitly,
        # with no trace back to the real cause.
        logger.warning(f"[_resolve_agent_current_phase] Failed: {e}")
    finally:
        session.close()
    return None
