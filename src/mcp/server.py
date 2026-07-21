"""MCP Server implementation for Hephaestus."""

import asyncio
import json
import logging
import os
import time
import uuid
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (
    Body,
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from git import Repo
from pydantic import BaseModel, Field, validator

from src.agents.manager import AgentManager
from src.auth.auth_api import router as auth_router
from src.core.database import (
    Agent,
    DatabaseManager,
    Phase,
    Task,
    Workflow,
)
from src.core.simple_config import get_config
from src.core.worktree_manager import WorktreeManager
from src.mcp.api import create_frontend_routes

# Import routers at module level for test compatibility
from src.mcp.tickets_api import router as tickets_router
from src.mcp.agents_api import router as agents_router
from src.mcp.messaging_api import router as messaging_router
from src.mcp.memory_api import (
    router as memory_router,
    save_memory, search_memory,
    SaveMemoryRequest, SearchMemoryRequest,
)
from src.memory.rag import RAGSystem
from src.memory.store_factory import VectorStoreProtocol, create_vector_store
from src.phases import PhaseManager
from src.services.embedding_service import EmbeddingService
from src.services.queue_service import QueueService
from src.services.result_validator_service import ResultValidatorService
from src.services.task_similarity_service import TaskSimilarityService
from src.services.ticket_search_service import TicketSearchService
from src.services.ticket_service import TicketService
from src.services.workflow_result_service import WorkflowResultService

logger = logging.getLogger(__name__)

# One-shot self-review checklist (see docs/GAP_CHECK_SELF_LOOP_DESIGN.md).
# Concrete and checkable by design, not an open-ended "find your own gaps" —
# makes a no-op pass (nothing changed, same completion_notes) easy to spot
# later against the before/after diff.
SELF_REVIEW_CHECKLIST_PROMPT = """
Before this is actually done, re-check your own work:
- Re-read the design/requirements — is every requirement implemented?
- Edge cases and error handling — anything unhandled?
- Tests exist for new code, and they pass?
- Any TODOs, stubs, or dead code left behind?

Fix anything real you find, then call hephaestus_update_task_status
with status="done" again — record what you changed (if anything) in the
summary.
"""


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
if config.enable_cors:
    # SECURITY: Use explicit origins instead of wildcard '*' when credentials are allowed.
    # Wildcard + credentials is a security risk (allows credential theft from any origin).
    # Default to localhost origins for development; set CORS_ORIGINS env var for production.
    import os

    _cors_origins_str = os.environ.get("CORS_ORIGINS", "")
    if _cors_origins_str:
        _cors_origins = [o.strip() for o in _cors_origins_str.split(",") if o.strip()]
    else:
        # Development defaults: localhost only
        _cors_origins = [
            "http://localhost:5173",
            "http://localhost:3000",
            "http://localhost:8300",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:3000",
            "http://127.0.0.1:8300",
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
from pydantic import BaseModel, Field, validator


class CreateTaskRequest(BaseModel):
    """Request model for creating a task."""

    task_description: str = Field(
        ..., description="Raw task description", max_length=50000
    )
    done_definition: str = Field(
        ..., description="What constitutes completion", max_length=10000
    )
    ai_agent_id: str = Field(..., description="ID of requesting agent")
    workflow_id: Optional[str] = Field(default=None, description="ID of the workflow this task belongs to")
    priority: Optional[str] = Field(default="medium", pattern="^(low|medium|high)$")
    parent_task_id: Optional[str] = Field(
        default=None, description="Parent task ID for sub-tasks"
    )
    phase_id: Optional[str] = Field(
        default=None, description="Phase ID for workflow-based tasks"
    )
    phase_order: Optional[int] = Field(
        default=None, description="Phase order number (alternative to phase_id)"
    )
    cwd: Optional[str] = Field(
        default=None, description="Working directory for the task"
    )
    ticket_id: Optional[str] = Field(
        default=None,
        description="Associated ticket ID (required when ticket tracking enabled)",
    )
    depends_on: Optional[List[str]] = Field(
        default=None, description="List of task IDs that must complete before this one"
    )
    parallel_group: Optional[str] = Field(
        default=None,
        description="Tasks in same group can run in parallel; different groups are sequential",
    )
    max_concurrent: Optional[int] = Field(
        default=1, description="Max agents working on this task simultaneously"
    )
    context: Optional[str] = Field(
        default=None,
        description="Additional context for the agent (e.g., design document content, requirements summary)",
        max_length=100000,
    )

    @validator('ticket_id', pre=True, always=True)
    @classmethod
    def validate_ticket_id(cls, v):
        """Strip whitespace and reject whitespace-only ticket_id values."""
        if v is None:
            return v
        # Strip leading and trailing whitespace
        stripped = v.strip()
        # Reject whitespace-only values (after stripping, result is empty)
        if v and not stripped:
            raise ValueError(
                "ticket_id cannot be whitespace-only. "
                "Provide a valid ticket identifier or omit the field."
            )
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
    summary: str = Field(default="", description="What was accomplished")
    key_learnings: List[str] = Field(default=[], description="Important discoveries")
    code_changes: Optional[List[str]] = Field(
        default=None, description="Files modified/created"
    )
    failure_reason: Optional[str] = Field(
        default=None, description="Required if status is 'failed'"
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional structured data (verdict, counts, etc.) — folded into summary",
    )


class UpdateTaskStatusResponse(BaseModel):
    """Response model for task status update."""

    success: bool
    message: str
    termination_scheduled: bool


# ApproveTicketResponse, RejectTicketResponse, FileDiff, CommitDiffResponse
# now live in tickets_api.py


# Workflow Management Request/Response Models
class RegisterWorkflowDefinitionRequest(BaseModel):
    """Request model for registering a workflow definition."""

    id: str = Field(..., description="Unique ID for the workflow definition")
    name: str = Field(..., description="Human-readable name")
    description: str = Field(default="", description="Description of the workflow")
    phases_config: List[Dict[str, Any]] = Field(..., description="Phase configurations")
    workflow_config: Optional[Dict[str, Any]] = Field(
        default=None, description="Workflow configuration"
    )


class StartWorkflowRequest(BaseModel):
    """Request model for starting a workflow execution."""

    definition_id: str = Field(
        ..., description="ID of the workflow definition to execute"
    )
    description: str = Field(
        ..., description="Description/name of this workflow execution"
    )
    working_directory: Optional[str] = Field(
        default=None, description="Working directory for the workflow"
    )
    launch_params: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Parameters from launch template to substitute into phases",
    )
    design_id: Optional[str] = Field(
        default=None,
        description="autopilot_designs.id that spawned this execution (§9.7)",
    )


# Server state
class ServerState:
    """Global server state."""

    def __init__(self):
        self.db_manager: Optional[DatabaseManager] = None
        self.vector_store: Optional[VectorStoreProtocol] = None
        self.llm_provider = None
        self.agent_manager: Optional[AgentManager] = None
        self.rag_system: Optional[RAGSystem] = None
        self.phase_manager: Optional[PhaseManager] = None
        self.branch_manager: Optional[WorktreeManager] = None
        self.result_validator_service: Optional[ResultValidatorService] = None
        self.embedding_service: Optional[EmbeddingService] = None
        self.task_similarity_service: Optional[TaskSimilarityService] = None
        self.queue_service: Optional[QueueService] = None
        self.active_websockets: List[WebSocket] = []
        self.sse_queues: List[asyncio.Queue] = []
        self.background_queue_processor_task: Optional[asyncio.Task] = None
        self.phase_advancement_sweep_task: Optional[asyncio.Task] = None
        self.shutdown_event: asyncio.Event = asyncio.Event()

    async def initialize(self):
        """Initialize server components."""
        config = get_config()

        # Initialize database
        self.db_manager = DatabaseManager(str(config.database_path))
        self.db_manager.create_tables()

        # Migrate: add is_active column to existing autopilot_projects table
        self._migrate_is_active_column()

        # Load active project from DB and apply to config BEFORE creating managers
        self._load_active_project(config)

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

        # Initialize RAG system
        self.rag_system = RAGSystem(
            vector_store=self.vector_store,
            llm_provider=self.llm_provider,
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
        if config.task_dedup_enabled:
            try:
                from src.memory.embedding_factory import create_embedding_provider

                self.embedding_service = create_embedding_provider()
                self.task_similarity_service = TaskSimilarityService(
                    self.db_manager, self.embedding_service
                )
                logger.info(
                    "Task deduplication service initialized (embedding via configurable provider)"
                )
            except Exception as e:
                logger.warning(
                    f"Task deduplication disabled — embedding provider init failed: {e}"
                )
        else:
            logger.info("Task deduplication disabled by configuration")

        # Initialize queue service
        self.queue_service = QueueService(
            db_manager=self.db_manager,
            max_concurrent_agents=config.max_concurrent_agents,
        )
        logger.info(
            f"Queue service initialized with max_concurrent_agents={config.max_concurrent_agents}"
        )

        logger.info("Server state initialized successfully")

    def _migrate_is_active_column(self):
        """Add is_active column to autopilot_projects if missing."""
        import sqlalchemy

        try:
            with self.db_manager.get_session() as session:
                session.execute(
                    sqlalchemy.text(
                        "ALTER TABLE autopilot_projects ADD COLUMN is_active BOOLEAN DEFAULT 0"
                    )
                )
                session.commit()
                logger.info("Migrated: added is_active column to autopilot_projects")
        except Exception:
            pass  # Column already exists

    def _load_active_project(self, config):
        """Load active project from DB and apply to config before managers init."""
        from src.core.database import AutopilotProject

        try:
            with self.db_manager.get_session() as session:
                active = (
                    session.query(AutopilotProject).filter_by(is_active=True).first()
                )
                if active:
                    from pathlib import Path

                    config.main_repo_path = Path(active.base_dir)
                    config.project_root = Path(active.base_dir)
                    logger.info(
                        f"Active project loaded: {active.name} ({active.base_dir})"
                    )
                else:
                    # Auto-activate the default or first project
                    proj = (
                        session.query(AutopilotProject)
                        .filter_by(is_default=True)
                        .first()
                    )
                    if not proj:
                        proj = session.query(AutopilotProject).first()
                    if proj:
                        proj.is_active = True
                        session.commit()
                        from pathlib import Path

                        config.main_repo_path = Path(proj.base_dir)
                        config.project_root = Path(proj.base_dir)
                        logger.info(
                            f"Auto-activated project: {proj.name} ({proj.base_dir})"
                        )
        except Exception as e:
            logger.warning(f"Could not load active project: {e}")

    async def broadcast_update(self, message: Dict[str, Any]):
        """Broadcast update to all connected WebSocket and SSE clients."""
        disconnected = []
        for websocket in self.active_websockets:
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)

        # Remove disconnected clients
        for ws in disconnected:
            self.active_websockets.remove(ws)

        # Send to SSE clients
        for queue in self.sse_queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("SSE queue full, skipping event")


# Initialize server state
server_state = ServerState()

# Register with app_context so other modules can reach shared state without
# importing this route module (breaks the circular-import workaround used
# throughout the service layer — see docs/SOLID_OO_REVIEW.md 1.6/3.11).
from src.core.app_context import set_app_state as _set_app_state

_set_app_state(server_state)


# ==================== SECURITY: Agent Authentication ====================

# Known system agents that don't require token validation
KNOWN_SYSTEM_AGENTS = {
    "main-session-agent",
    "sdk-agent",
    "system",
    "ui-user",
    "sdk-repair-agent",
    "orchestrator",
    "monitor",
}


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
    from src.core.database import Agent

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


def _tmux_session_alive(session_name: str) -> bool:
    """True if the named tmux session currently exists."""
    if not session_name:
        return False
    try:
        import subprocess

        r = subprocess.run(
            ["tmux", "has-session", "-t", session_name], capture_output=True, timeout=3
        )
        return r.returncode == 0
    except Exception:
        return False


async def _resume_interrupted_workflows(
    workflow_id: Optional[str] = None, reactivate: bool = False
):
    """Re-drive workflows that were mid-flight when the server last stopped.

    Completed phases are durable (committed to the integration branch) and the DB
    records exactly where each run is. The volatile part is the in-flight agent: its
    tmux session dies with the server. We find phase agents that still think they're
    working but whose tmux is gone, and restart them — restart_agent re-attaches to
    the agent's existing worktree branch (prior commits + context intact) with a
    'continue where you left off' prompt. WIP is preserved because terminate_agent
    auto-commits, and the worktree dir survives a crash regardless.

    Runs on startup (all interrupted workflows) and on demand via the recover
    endpoint (optionally scoped to one workflow_id; reactivate=True flips a
    paused/failed workflow back to active first — the UI "Retry" path).

    Returns {"resumed": int, "workflows": [ids]}.
    """
    from src.core.database import Agent, Task, Workflow

    session = server_state.db_manager.get_session()
    result = {"resumed": 0, "workflows": []}
    try:
        if not getattr(server_state, "agent_manager", None):
            logger.warning("[RESUME] agent_manager not ready — skipping resume scan")
            return result

        statuses = ["active", "paused"] + (["failed"] if reactivate else [])
        q = session.query(Workflow).filter(Workflow.status.in_(statuses))
        if workflow_id:
            q = q.filter(Workflow.id == workflow_id)
        active = q.all()
        if not active:
            return result

        # On-demand retry can flip a paused/failed workflow back to active so the
        # monitor re-drives it (and the scan below restarts any orphaned agents).
        if reactivate:
            for wf in active:
                if wf.status in ("paused", "failed"):
                    wf.status = "active"
                    wf.paused_by = None
            session.commit()

        resumed = 0
        for wf in active:
            # On-demand retry only (never the passive startup-wide scan, which
            # runs with reactivate=False): also reset tasks that outright
            # failed, not just ones whose agent process died mid-flight.
            # Without this, clicking Resume/Rerun on a workflow with a genuinely
            # failed task flips the workflow back to "active" but leaves the
            # failed task untouched — status derivation then flips it straight
            # back to "failed" and nothing appears to have happened.
            if reactivate:
                failed_tasks = (
                    session.query(Task)
                    .filter(Task.workflow_id == wf.id, Task.status == "failed")
                    .all()
                )
                for t in failed_tasks:
                    t.status = "pending"
                    t.failure_reason = None
                    t.assigned_agent_id = None
                    # This row is reused for the retry -- clear any stale
                    # goto/retry tag from a previous life (see the matching
                    # fix in restart_task_endpoint / orchestrator.py's
                    # per-phase failed-task retry).
                    t.action = ""
                    t.action_target_phase = None
                if failed_tasks:
                    session.commit()
                    logger.info(
                        f"[RESUME] Workflow {wf.id[:8]}: resetting "
                        f"{len(failed_tasks)} failed task(s) for on-demand retry"
                    )
                for t in failed_tasks:
                    try:
                        if server_state.queue_service.should_queue_task():
                            server_state.queue_service.enqueue_task(t.id)
                        else:
                            from src.services.agent_dispatch_service import (
                                AgentDispatchService,
                            )

                            dispatch_context = (
                                await AgentDispatchService.build_dispatch_context(
                                    task_description_for_rag=t.enriched_description
                                    or t.raw_description,
                                    phase_id=t.phase_id,
                                )
                            )
                            agent = await AgentDispatchService.dispatch(
                                task=t,
                                enriched_data={
                                    "enriched_description": t.enriched_description
                                },
                                dispatch_context=dispatch_context,
                            )
                            AgentDispatchService.mark_assigned(
                                t.id, agent.id, status="assigned"
                            )
                        resumed += 1
                    except Exception as e:
                        logger.warning(
                            f"[RESUME] Failed to restart failed task {t.id[:8]}: {e}"
                        )

            # Only tasks that still need work — a 'done' task advances via the
            # monitor's phase-completion check, not by restarting its old agent.
            task_ids = [
                t.id
                for t in session.query(Task)
                .filter(
                    Task.workflow_id == wf.id,
                    Task.status.in_(["pending", "assigned", "in_progress", "queued"]),
                )
                .all()
            ]
            if not task_ids:
                continue
            orphans = (
                session.query(Agent)
                .filter(
                    Agent.current_task_id.in_(task_ids),
                    Agent.agent_type == "phase",
                    Agent.status.in_(["working", "idle", "starting"]),
                )
                .all()
            )
            for agent in orphans:
                if _tmux_session_alive(agent.tmux_session_name):
                    continue  # still alive (e.g., only the monitor restarted) — leave it
                logger.info(
                    f"[RESUME] Workflow {wf.id[:8]}: restarting orphaned phase agent "
                    f"{agent.id[:8]} (dead tmux session) to continue from committed state"
                )
                try:
                    await server_state.agent_manager.restart_agent(
                        agent.id, reason="server restarted — resuming interrupted work"
                    )
                    resumed += 1
                except Exception as e:
                    logger.warning(
                        f"[RESUME] Failed to restart agent {agent.id[:8]}: {e}"
                    )
        result["resumed"] = resumed
        result["workflows"] = [wf.id for wf in active]
        if resumed:
            logger.info(
                f"[RESUME] Resumed {resumed} interrupted phase agent(s) across "
                f"{len(active)} workflow(s)"
            )
        return result
    finally:
        session.close()


@app.on_event("startup")
async def startup_event():
    """Initialize server on startup."""
    logger.info("Starting Hephaestus MCP Server...")

    # Several handlers (e.g. autopilot_api.py's repair endpoint) write files
    # under AUTOPILOT_STATE_DIR without their own mkdir guard, previously
    # relying on PersistentPipelineState's constructor having created it as
    # a side effect on first use -- fragile even then, since it depended on
    # a pipeline having started first. Guarantee it exists unconditionally,
    # once, here.
    from pathlib import Path

    from src.core.constants import AUTOPILOT_STATE_DIR

    Path(AUTOPILOT_STATE_DIR).mkdir(parents=True, exist_ok=True)

    await server_state.initialize()

    # Add frontend API routes
    api_router = create_frontend_routes(
        server_state.db_manager, server_state.agent_manager, server_state.phase_manager
    )
    app.include_router(api_router)

    # Add authentication routes
    app.include_router(auth_router)

    # Add autopilot routes (configure BEFORE including)
    from src.mcp.autopilot_api import configure_autopilot_api
    from src.mcp.autopilot_api import router as autopilot_router

    configure_autopilot_api(
        design_queue_dir=os.environ.get("DESIGN_QUEUE_DIR", ""),
        features_dir=os.environ.get("FEATURES_DIR", ""),
    )
    app.include_router(autopilot_router)

    # Add project management routes
    from src.mcp.projects_api import router as projects_router

    app.include_router(projects_router)

    # Note: tickets_router (M-1: extracted from server.py) is included at
    # module level above, not here — TestClient(app) used without the
    # `with TestClient(app) as client:` context manager never fires this
    # startup event, so including it only here would break those tests.

    # Load phases if folder is specified
    from pathlib import Path

    logger.info("=== PHASE LOADING DEBUG ===")
    logger.info(f"Current working directory: {os.getcwd()}")
    logger.info(
        f"Environment variables starting with HEPHAESTUS: {[k for k in os.environ.keys() if 'HEPHAESTUS' in k]}"
    )

    phases_folder = os.environ.get("HEPHAESTUS_PHASES_FOLDER")
    logger.info(f"HEPHAESTUS_PHASES_FOLDER value: '{phases_folder}'")

    if phases_folder:
        logger.info(f"Attempting to load workflow phases from: {phases_folder}")

        # Check if folder exists
        full_path = Path(phases_folder)
        if not full_path.is_absolute():
            full_path = Path(os.getcwd()) / phases_folder

        logger.info(f"Full path to phases folder: {full_path}")
        logger.info(f"Folder exists: {full_path.exists()}")
        logger.info(
            f"Is directory: {full_path.is_dir() if full_path.exists() else 'N/A'}"
        )

        if full_path.exists() and full_path.is_dir():
            # List files in directory
            files = list(full_path.glob("*.yaml"))
            logger.info(f"YAML files found: {len(files)}")
            for f in files:
                logger.info(f"  - {f.name}")

        try:
            from src.phases import PhaseLoader

            logger.info("PhaseLoader imported successfully")

            # Load phases from folder
            logger.info(
                f"Calling PhaseLoader.load_phases_from_folder('{phases_folder}')"
            )
            workflow_def = PhaseLoader.load_phases_from_folder(phases_folder)
            logger.info(
                f"Loaded workflow '{workflow_def.name}' with {len(workflow_def.phases)} phases"
            )

            # Load phases configuration (for ticket tracking, result handling, etc.)
            logger.info(f"Loading phases_config.yaml from '{phases_folder}'")
            phases_config = PhaseLoader.load_phases_config(phases_folder)
            logger.info(
                f"Loaded phases config: enable_tickets={phases_config.enable_tickets}, has_result={phases_config.has_result}"
            )

            # Workflow initialization is handled by SDK's start_workflow() call
            # The phase definitions are loaded but workflow execution is created on-demand
            logger.info(
                "Phases loaded successfully - workflow execution will be created via start_workflow() call"
            )

            # Log phase names
            logger.info("Loaded phases:")
            for phase in workflow_def.phases:
                logger.info(f"  Phase {phase.id}: {phase.name}")
                logger.info(f"    - Description: {phase.description[:100]}...")
                logger.info(
                    f"    - Done definitions: {len(phase.done_definitions)} items"
                )

        except ImportError as e:
            logger.error(f"Failed to import PhaseLoader: {e}")
            import traceback

            logger.error(traceback.format_exc())
        except Exception as e:
            logger.error(f"Failed to load phases: {e}")
            import traceback

            logger.error(f"Full traceback:\n{traceback.format_exc()}")
            # Don't fail server startup, just run without phases
    else:
        logger.info("No phases folder specified - running in standard mode")
        logger.info("To load phases, set HEPHAESTUS_PHASES_FOLDER environment variable")

    logger.info("=== END PHASE LOADING DEBUG ===")

    # Register all workflow definitions
    try:
        from src.core.database import WorkflowDefinition as DBWorkflowDefinition
        from src.workflow_registry import get_all_workflow_definitions

        all_definitions = get_all_workflow_definitions()

        with server_state.db_manager.get_session() as session:
            for defn in all_definitions:
                # Build phases_config from source
                phases_config = []
                for phase in defn.phases:
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
                    phases_config.append(phase_dict)

                workflow_config = {
                    "has_result": defn.config.has_result,
                    "result_criteria": defn.config.result_criteria,
                    "on_result_found": defn.config.on_result_found,
                    "enable_tickets": defn.config.enable_tickets,
                    "board_config": defn.config.board_config,
                }

                # Include launch_template in workflow_config if present
                if defn.launch_template:
                    from dataclasses import asdict

                    workflow_config["launch_template"] = asdict(defn.launch_template)

                # Get orchestrator_config if present
                orchestrator_config = getattr(defn, "orchestrator_config", None)

                existing = (
                    session.query(DBWorkflowDefinition).filter_by(id=defn.id).first()
                )
                if existing:
                    # Update from source files (source of truth)
                    existing.name = defn.name
                    existing.description = defn.description
                    existing.phases_config = phases_config
                    existing.workflow_config = workflow_config
                    existing.orchestrator_config = orchestrator_config
                    logger.info(f"Updated workflow from source: {defn.id}")
                else:
                    db_def = DBWorkflowDefinition(
                        id=defn.id,
                        name=defn.name,
                        description=defn.description,
                        phases_config=phases_config,
                        workflow_config=workflow_config,
                        orchestrator_config=orchestrator_config,
                    )
                    session.add(db_def)
                    logger.info(f"Registered workflow: {defn.id}")
            # Remove stale definitions that no longer exist on disk
            loaded_ids = {d.id for d in all_definitions}
            stale = (
                session.query(DBWorkflowDefinition)
                .filter(DBWorkflowDefinition.id.notin_(loaded_ids))
                .all()
            )
            for stale_def in stale:
                logger.info(f"Removing stale workflow definition: {stale_def.id}")
                session.delete(stale_def)

            session.commit()
        logger.info(
            f"Workflow registration complete: {len(all_definitions)} definitions"
        )
    except Exception as e:
        logger.error(f"Failed to register workflows: {e}")
        import traceback

        logger.error(traceback.format_exc())

    # Start background queue processor
    logger.info("Starting background queue processor...")
    server_state.background_queue_processor_task = asyncio.create_task(
        background_queue_processor()
    )
    logger.info("Background queue processor task created")

    # Start background phase advancement sweep — the generic, restart-safe
    # replacement for relying on a specific run's own polling loop (see its
    # docstring for why that's necessary).
    logger.info("Starting background phase advancement sweep...")
    server_state.phase_advancement_sweep_task = asyncio.create_task(
        background_phase_advancement_sweep()
    )
    logger.info("Background phase advancement sweep task created")

    # Resume the autopilot pipeline driver itself if it was running when the
    # server last stopped. AutopilotService lives entirely in-process (see
    # src/autopilot/service.py) — its polling loop (which fires phase
    # transitions once a phase's task is marked done) dies with the process
    # and nothing else re-creates it. Without this, a backend restart while a
    # pipeline is active permanently stalls phase advancement: tasks finish,
    # but the next phase's task never gets created, until a much later,
    # cruder fallback (the diagnostic monitor's stuck-workflow detector)
    # eventually notices and manually patches the gap.
    #
    # Done BEFORE _resume_interrupted_workflows below (rather than after, as
    # this used to be ordered) so that if the persisted state says the user
    # last had this running, AutopilotService.running flips true as early as
    # possible in startup -- not after the slower interrupted-workflow scan
    # has already run. Every check elsewhere that reads "is the pipeline
    # active" (status endpoints, the frontend queue page, orphan/recovery
    # logic) should see "active" for as much of the startup window as
    # possible instead of a transient "idle" read.
    try:
        from src.autopilot.service import AutopilotService, get_autopilot_service, get_registry

        # Enumerate every project with a persisted "was running" marker, not
        # just one -- multiple projects can each have their own pipeline to
        # resume now (see docs/MULTI_PROJECT_CONCURRENCY_DESIGN.md). This is
        # also the one and only call site of enumerate_persisted_states'
        # legacy-key migration, so a pipeline that was running before this
        # change deployed self-heals onto the namespaced key right here.
        for resume_project_id, persisted in AutopilotService.enumerate_persisted_states():
            if not persisted.get("project_path"):
                continue

            # Same cap POST /start enforces. Without this, a restart with
            # more persisted "was running" projects than max_concurrent_
            # projects (e.g. the cap was lowered, or that many really were
            # running when the backend went down) would silently resume all
            # of them, permanently exceeding the cap until the next manual
            # stop. try_reserve() always allows a project already counted as
            # running, so this only ever rejects the (N+1)th and later
            # resumes within this same loop, not earlier ones. Using
            # try_reserve (not can_start) here too, not just its atomicity:
            # an incoming POST /start could in principle race this loop if
            # the server starts accepting connections before startup_event
            # finishes.
            can_start, cap_message = get_registry().try_reserve(resume_project_id)
            if not can_start:
                logger.warning(
                    f"[RESUME] Skipping auto-resume for project {resume_project_id}: "
                    f"{cap_message}"
                )
                continue

            logger.info(
                f"[RESUME] Auto-resuming autopilot pipeline for project "
                f"{resume_project_id} ({persisted['project_path']}) "
                "(was running before restart)"
            )
            try:
                await get_autopilot_service(resume_project_id).start(
                    project_path=persisted["project_path"],
                    design_queue=persisted.get("design_queue") or "",
                    max_iterations=persisted.get("max_iterations", 10),
                )
            except Exception as e:
                logger.error(
                    f"[RESUME] Failed to auto-resume project {resume_project_id}: {e}"
                )
            finally:
                get_registry().release_reservation(resume_project_id)
    except Exception as e:
        logger.error(f"[RESUME] Failed to enumerate persisted autopilot state: {e}")

    # Resume any workflows that were mid-flight when the server last stopped
    # (crash / laptop sleep / manual restart) so real work isn't stranded.
    try:
        await _resume_interrupted_workflows()
    except Exception as e:
        logger.error(f"[RESUME] resume scan failed: {e}")

    logger.info("Server started successfully")


@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on shutdown."""
    logger.info("Shutting down Hephaestus MCP Server...")

    # Stop background queue processor
    logger.info("Stopping background queue processor...")
    server_state.shutdown_event.set()
    if server_state.background_queue_processor_task:
        try:
            await asyncio.wait_for(
                server_state.background_queue_processor_task, timeout=5.0
            )
            logger.info("Background queue processor stopped")
        except asyncio.TimeoutError:
            logger.warning(
                "Background queue processor did not stop gracefully, cancelling..."
            )
            server_state.background_queue_processor_task.cancel()

    # Stop background phase advancement sweep (shares the same shutdown_event,
    # already set above)
    logger.info("Stopping background phase advancement sweep...")
    if server_state.phase_advancement_sweep_task:
        try:
            await asyncio.wait_for(
                server_state.phase_advancement_sweep_task, timeout=5.0
            )
            logger.info("Background phase advancement sweep stopped")
        except asyncio.TimeoutError:
            logger.warning(
                "Background phase advancement sweep did not stop gracefully, cancelling..."
            )
            server_state.phase_advancement_sweep_task.cancel()

    # Close all WebSocket connections
    for ws in server_state.active_websockets:
        await ws.close()


def verify_agent_id(agent_id: str = Header(None, alias="X-Agent-ID")) -> str:
    """Verify agent ID from header.

    SECURITY: Validates agent_id format (must be UUID or known SDK identifier).
    Rejects empty/malformed agent IDs.
    """
    if not agent_id:
        raise HTTPException(
            status_code=401, detail="Agent ID required in X-Agent-ID header"
        )

    # Validate format: must be UUID or known SDK/system identifier
    import re

    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
    )
    known_system_ids = {
        "main-session-agent",
        "sdk-agent",
        "system",
        "ui-user",
        "sdk-repair-agent",
        "orchestrator",
        "monitor",
    }

    if not (
        uuid_pattern.match(agent_id)
        or agent_id in known_system_ids
        or agent_id.startswith("sdk-")
        or agent_id.startswith("mcp-")
    ):
        raise HTTPException(
            status_code=401,
            detail=f"Invalid agent ID format: '{agent_id}'. Must be a UUID or known system identifier.",
        )

    return agent_id


# SECURITY: Rate limiting for sensitive endpoints

_rate_limit_store: Dict[str, List[float]] = defaultdict(list)
RATE_LIMIT_WINDOW = 60  # seconds
RATE_LIMIT_MAX = 30  # requests per window


def _check_rate_limit(key: str, max_requests: int = RATE_LIMIT_MAX) -> bool:
    """Check if request is within rate limit. Returns True if allowed."""
    now = time.time()
    # Clean old entries
    _rate_limit_store[key] = [
        t for t in _rate_limit_store[key] if now - t < RATE_LIMIT_WINDOW
    ]
    if len(_rate_limit_store[key]) >= max_requests:
        return False
    _rate_limit_store[key].append(now)
    return True


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


async def process_queue():
    """Process the next queued task by creating an agent for it.

    Only creates an agent if we're under the max concurrent agent limit.
    """
    from src.services.agent_dispatch_service import AgentDispatchService
    from src.services.task_enrichment_service import TaskEnrichmentService

    try:
        # Check if we should queue (i.e., at capacity)
        if server_state.queue_service.should_queue_task():
            logger.debug("At capacity - not processing queue")
            return

        # Get next task from queue
        next_task = server_state.queue_service.get_next_queued_task()

        if not next_task:
            logger.debug("No queued tasks to process")
            return

        logger.info(
            f"Processing queued task {next_task.id} (priority={next_task.priority}, boosted={next_task.priority_boosted})"
        )

        # Dequeue the task
        server_state.queue_service.dequeue_task(next_task.id)

        # Resolve phase_id once up front — reused for both enrichment (if
        # needed) and agent dispatch below. Previously this exact
        # digit-vs-UUID resolution was independently duplicated for each
        # (see docs/SOLID_OO_REVIEW.md findings 1.2/1.3/1.4).
        resolved_phase_id = None
        if next_task.phase_id and server_state.phase_manager:
            resolved_phase_id = TaskEnrichmentService.resolve_phase_id(
                phase_id_raw=next_task.phase_id,
                phase_order=None,
                workflow_id=next_task.workflow_id,
                requesting_agent_id="system",
            )
            if resolved_phase_id != next_task.phase_id:
                next_task.phase_id = resolved_phase_id  # update in-memory object too

        # Tasks created with placeholder "[Processing] ..." (e.g. blocked on
        # creation and enrichment was skipped) need real LLM enrichment.
        needs_enrichment = (
            not next_task.enriched_description
            or next_task.enriched_description.startswith("[Processing]")
        )
        logger.info(
            f"[QUEUE_ENRICHMENT] Task {next_task.id} needs_enrichment={needs_enrichment}"
        )

        if needs_enrichment:
            phase_context_str, ctx_workflow_id = (
                TaskEnrichmentService.get_phase_context_str(resolved_phase_id)
            )
            workflow_id = ctx_workflow_id or next_task.workflow_id

            enrichment_result = await TaskEnrichmentService.enrich(
                raw_description=next_task.raw_description,
                done_definition=next_task.done_definition,
                phase_context_str=phase_context_str,
                requesting_agent_id="system",
            )
            enriched_task = enrichment_result["enriched_task"]

            # FIX #7: Save enrichment context for dispatch reuse.
            next_task._enrichment_context = {
                "context_memories": enrichment_result["context_memories"],
                "project_context": enrichment_result["project_context"],
            }

            session = server_state.db_manager.get_session()
            try:
                task = session.query(Task).filter_by(id=next_task.id).first()
                if task:
                    task.enriched_description = enriched_task["enriched_description"]
                    task.estimated_complexity = enriched_task.get(
                        "estimated_complexity", 5
                    )
                    if resolved_phase_id:
                        task.phase_id = resolved_phase_id
                    if workflow_id:
                        task.workflow_id = workflow_id

                    # Inherit validation from phase, if enabled there
                    if resolved_phase_id:
                        from src.core.database import Phase

                        phase = (
                            session.query(Phase)
                            .filter_by(id=resolved_phase_id)
                            .first()
                        )
                        if phase and phase.validation and phase.validation.get(
                            "enabled", True
                        ):
                            task.validation_enabled = True

                    session.commit()
                    next_task._enriched_task_dict = enriched_task  # for dispatch below
                    logger.info(
                        f"[QUEUE_ENRICHMENT] Enrichment complete for task {next_task.id}"
                    )
                else:
                    logger.error(
                        f"[QUEUE_ENRICHMENT] Task {next_task.id} not found in database!"
                    )
            finally:
                session.close()
        else:
            logger.info(
                f"[QUEUE_ENRICHMENT] Task {next_task.id} already enriched - skipping enrichment pipeline"
            )

        # Refresh task from DB to get post-enrichment data, and build the
        # temp task object used for dispatch (mirrors create_task's pattern).
        session = server_state.db_manager.get_session()
        try:
            refreshed_task = session.query(Task).filter_by(id=next_task.id).first()
            if refreshed_task:
                task_for_agent = Task(
                    id=refreshed_task.id,
                    raw_description=refreshed_task.raw_description,
                    enriched_description=refreshed_task.enriched_description,
                    done_definition=refreshed_task.done_definition,
                    phase_id=resolved_phase_id or refreshed_task.phase_id,
                    created_by_agent_id=refreshed_task.created_by_agent_id,
                    workflow_id=refreshed_task.workflow_id,
                )
                task_description_for_rag = (
                    refreshed_task.enriched_description
                    or refreshed_task.raw_description
                )
            else:
                logger.warning(
                    "[QUEUE_AGENT_CREATE] Could not refresh task from DB - using stale task"
                )
                task_for_agent = next_task
                task_description_for_rag = (
                    next_task.enriched_description or next_task.raw_description
                )
        finally:
            session.close()

        # If enrichment just ran, use the full LLM result dict; otherwise
        # (task was already enriched) build a minimal dict.
        if hasattr(next_task, "_enriched_task_dict"):
            enriched_data_for_agent = next_task._enriched_task_dict
        else:
            enriched_data_for_agent = {
                "enriched_description": task_for_agent.enriched_description,
                "estimated_complexity": task_for_agent.estimated_complexity or 5,
            }

        # FIX #7: Reuse enrichment context if available (avoid double-fetch).
        if hasattr(next_task, "_enrichment_context"):
            dispatch_context = (
                await AgentDispatchService.build_dispatch_context_from_existing(
                    memories=next_task._enrichment_context["context_memories"],
                    project_context=next_task._enrichment_context["project_context"],
                    working_directory="",  # Will fall back to phase cwd
                    phase_id=task_for_agent.phase_id,
                )
            )
        else:
            dispatch_context = await AgentDispatchService.build_dispatch_context(
                task_description_for_rag=task_description_for_rag,
                phase_id=task_for_agent.phase_id,
                requesting_agent_id="system",
            )

        agent = await AgentDispatchService.dispatch(
            task=task_for_agent,
            enriched_data=enriched_data_for_agent,
            dispatch_context=dispatch_context,
        )
        logger.info(f"Created agent {agent.id} for queued task {next_task.id}")

        # Agent is now working — "in_progress" (not "assigned" like the
        # other dispatch call sites), matching original process_queue behavior.
        AgentDispatchService.mark_assigned(next_task.id, agent.id, status="in_progress")

        # Broadcast update
        await server_state.broadcast_update(
            {
                "type": "task_dequeued",
                "task_id": next_task.id,
                "agent_id": agent.id,
                "description": (
                    next_task.enriched_description or next_task.raw_description
                )[:200],
            }
        )

    except Exception as e:
        logger.error(f"Failed to process queue: {e}")
        import traceback

        logger.error(traceback.format_exc())


# FIX #11: Register queue processor with app_context so services can
# trigger queue processing without importing server.py directly.
from src.core.app_context import set_queue_processor as _set_queue_processor

_set_queue_processor(process_queue)


async def background_queue_processor():
    """Background task that processes the queue every minute.

    This ensures that queued tasks (especially newly unblocked ones)
    don't get stuck waiting for another event to trigger queue processing.
    """
    logger.info("Background queue processor started")

    while not server_state.shutdown_event.is_set():
        try:
            # Check if there are any queued tasks
            queue_status = server_state.queue_service.get_queue_status()
            queued_count = queue_status.get("queued_tasks_count", 0)

            if queued_count > 0:
                logger.info(
                    f"[BACKGROUND_QUEUE] Found {queued_count} queued task(s), processing queue..."
                )
                await process_queue()
            else:
                logger.debug("[BACKGROUND_QUEUE] No queued tasks, skipping")

        except Exception as e:
            logger.error(f"[BACKGROUND_QUEUE] Error in background queue processor: {e}")
            import traceback

            logger.error(traceback.format_exc())

        # Wait 60 seconds before next check
        try:
            await asyncio.wait_for(server_state.shutdown_event.wait(), timeout=60.0)
            # If we get here, shutdown was signaled
            break
        except asyncio.TimeoutError:
            # Timeout is expected - continue the loop
            pass

    logger.info("Background queue processor stopped")


async def background_phase_advancement_sweep():
    """Background task that re-drives phase advancement for every active
    workflow, independent of any specific run's own polling loop.

    _advance_phases (src/autopilot/orchestrator.py) is the single source of
    truth for firing phase transitions, but historically it was only ever
    called from inside run_single_workflow's own monitor loop -- a loop
    that lives and dies with that specific async call. A backend restart
    kills it, and nothing re-created it for an already-launched workflow:
    the startup resume path (_resume_interrupted_workflows) only restarts
    orphaned AGENTS, on a stale assumption ("a 'done' task advances via the
    monitor's phase-completion check") that no longer holds -- that
    responsibility moved into the orchestrator's per-workflow loop without
    the resume path being updated to compensate.

    Observed live: a workflow's task finished successfully hours before
    this fix, but its phase never advanced past it, because nothing was
    polling _advance_phases for that workflow anymore after a backend
    restart -- it sat "in_progress" indefinitely until manually kicked.

    This sweep is a generic, restart-safe safety net: every workflow with
    status active/paused gets _advance_phases called for it here, on a
    fixed interval, regardless of how it was launched or whether some
    other loop is also driving it. _advance_phases's own claim guards
    (_claim_phase_task_creation) make concurrent calls from multiple
    sources safe by construction -- this doesn't race with
    run_single_workflow's own loop when both are active for the same
    workflow, it just means the workflow is never orphaned from
    advancement again.

    The per-tick work is synchronous, blocking DB I/O (_advance_phases
    itself, and everything it calls, uses plain SQLAlchemy sessions, not
    async ones) -- run via run_in_executor rather than inline, the same way
    AutopilotService._run_pipeline offloads its own synchronous pipeline
    loop. Calling N sequential blocking DB round-trips directly inside this
    coroutine would stall the whole event loop -- every HTTP request,
    WebSocket push, and SSE stream this same process is serving -- for the
    sweep's full duration, every tick, growing with active-workflow count.
    """
    from pathlib import Path

    from src.autopilot.orchestrator import OrchestratorLogger
    from src.core.constants import AUTOPILOT_STATE_DIR

    logger.info("Background phase advancement sweep started")
    sweep_logger = OrchestratorLogger(
        Path(AUTOPILOT_STATE_DIR) / "phase-advancement-sweep"
    )
    loop = asyncio.get_event_loop()

    while not server_state.shutdown_event.is_set():
        try:
            await loop.run_in_executor(
                None, _run_phase_advancement_sweep_once, sweep_logger
            )
        except Exception as e:
            logger.error(f"[PHASE-SWEEP] Error in phase advancement sweep: {e}")

        try:
            await asyncio.wait_for(server_state.shutdown_event.wait(), timeout=20.0)
            break
        except asyncio.TimeoutError:
            pass

    logger.info("Background phase advancement sweep stopped")


def _run_phase_advancement_sweep_once(sweep_logger) -> None:
    """Synchronous body of one background_phase_advancement_sweep tick --
    see that function's docstring for why this runs in a thread executor
    rather than inline on the event loop."""
    from src.autopilot.orchestrator import (
        _advance_phases,
        _clean_stale_assigned_tasks,
        _maybe_resolve_arbitration,
        _recover_abandoned_workflows_missing_worktree,
        _retry_failed_tasks,
        _sync_stale_feature_statuses,
    )
    from src.core.database import Workflow

    # Feature-table-wide, not scoped to any one workflow -- see its own
    # docstring for why this can't just live inside _run_one_feature.
    try:
        _sync_stale_feature_statuses(sweep_logger)
    except Exception as e:
        logger.error(f"[PHASE-SWEEP] Feature-status sync error: {e}")

    # Runs before the active/paused workflow snapshot below, so a workflow
    # this just resumed is included in this same tick's per-workflow loop
    # (_retry_failed_tasks etc.) instead of waiting a full tick.
    try:
        _recover_abandoned_workflows_missing_worktree(sweep_logger)
    except Exception as e:
        logger.error(f"[PHASE-SWEEP] Abandoned-workflow recovery error: {e}")

    session = server_state.db_manager.get_session()
    try:
        workflows = (
            session.query(Workflow.id, Workflow.status)
            .filter(Workflow.status.in_(["active", "paused"]))
            .all()
        )
    finally:
        session.close()

    for wf_id, wf_status in workflows:
        # Self-healing (dead-agent cleanup + failed-task retry) only while
        # the workflow is actually active, never paused -- these two used
        # to run only once, at pipeline-startup, for whichever single
        # workflow happened to be the last-tracked current_workflow_id (see
        # attempt_recovery's caller in run_continuous_pipeline). Any other
        # in-flight workflow (parallel feature runs, or one resumed outside
        # that one startup check) never got either: a task whose agent died
        # mid-work just sat "assigned"/"in_progress" forever, since nothing
        # else ever notices the agent is dead. Running both here makes it
        # universal instead of tied to one specific caller.
        #
        # _maybe_resolve_arbitration is bundled into this same "active only"
        # guard for the same reason, even though it isn't self-healing: a
        # successful resolution dispatches the next phase's task (see
        # _resolve_arbitration_outcome), which is exactly the "spawn new
        # agent work" side effect a pause is meant to prevent. If the
        # arbitration agent finishes while paused, its decision simply stays
        # unresolved (the claim it holds has no expiry) until the workflow
        # is resumed -- the very next sweep tick after that picks it up and
        # resolves it normally. Not a permanent stall, just deferred.
        if wf_status == "active":
            try:
                _clean_stale_assigned_tasks(wf_id, sweep_logger)
            except Exception as e:
                logger.error(f"[PHASE-SWEEP] Stale-task cleanup error for {wf_id[:8]}: {e}")
            try:
                _retry_failed_tasks(wf_id, sweep_logger)
            except Exception as e:
                logger.error(f"[PHASE-SWEEP] Failed-task retry error for {wf_id[:8]}: {e}")
            try:
                _maybe_resolve_arbitration(wf_id, sweep_logger)
            except Exception as e:
                logger.error(f"[PHASE-SWEEP] Arbitration resolve error for {wf_id[:8]}: {e}")

        try:
            _advance_phases(wf_id, sweep_logger)
        except Exception as e:
            logger.error(f"[PHASE-SWEEP] Error advancing workflow {wf_id[:8]}: {e}")


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
        logger.debug(f"[_resolve_agent_current_phase] Failed: {e}")
    finally:
        session.close()
    return None


# API Endpoints
@app.post("/create_task", response_model=CreateTaskResponse)
async def create_task(
    request: CreateTaskRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Create a new task with automatic enrichment and agent assignment."""
    # SECURITY: Verify agent authentication before allowing task creation
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    _touch_agent_activity(agent_id)
    logger.info(
        f"Creating task from agent {agent_id}: {request.task_description[:100]}..."
    )

    try:
        # Check if ticket tracking is enabled and ticket_id is required
        # EXCEPTION: SDK agents (main-session-agent or agents with 'sdk'/'main' in ID)
        # can create tasks without ticket_id as they are often the ticket creators

        # Check if ticket tracking is enabled in the system (any board config exists)
        session = server_state.db_manager.get_session()
        try:
            from src.core.database import BoardConfig

            # Check if there are any board configs (indicating ticket tracking is active)
            has_ticket_tracking = session.query(BoardConfig).first() is not None

            # If ticket tracking is enabled globally, require ticket_id from MCP agents
            if has_ticket_tracking and not request.ticket_id:
                # Check if this is an SDK agent (allowed to create tasks without tickets)
                is_sdk_agent = (
                    agent_id == "main-session-agent"
                    or "sdk" in agent_id.lower()
                    or "main" in agent_id.lower()
                )

                if not is_sdk_agent:
                    session.close()
                    raise HTTPException(
                        status_code=400,
                        detail="Ticket tracking is enabled. MCP agents MUST provide ticket_id. "
                        "Create a ticket first using create_ticket, then use that ticket_id here. "
                        "Only SDK/root agents can create tasks without tickets.",
                    )
        finally:
            if session.is_active:
                session.close()

        # Generate task ID immediately
        task_id = str(uuid.uuid4())

        # Validate phase_id for workflow tasks
        # M-6 fix: Auto-resolve agent's current phase if not provided for subtasks
        if request.workflow_id:
            logger.info(
                f"[CREATE_TASK] phase_id={repr(request.phase_id)}, phase_order={repr(request.phase_order)}"
            )
            
            # Auto-resolve phase_id from agent's current task if not provided
            if not request.phase_id and not request.phase_order:
                resolved_phase = _resolve_agent_current_phase(agent_id, request.workflow_id)
                if resolved_phase:
                    logger.info(
                        f"[CREATE_TASK] Auto-resolved phase_id for agent {agent_id[:8]}: {resolved_phase}"
                    )
                    request.phase_id = resolved_phase
                else:
                    logger.error(
                        f"[CREATE_TASK] REJECTED: no phase_id for workflow {request.workflow_id}"
                    )
                    raise HTTPException(
                        status_code=400,
                        detail=f"phase_id or phase_order is REQUIRED for workflow tasks. "
                        f"Agent {agent_id} must provide phase_id when workflow_id is set.",
                    )
            if request.phase_id in ("None", "none", "null", ""):
                # Try to auto-resolve before rejecting
                resolved_phase = _resolve_agent_current_phase(agent_id, request.workflow_id)
                if resolved_phase:
                    logger.info(
                        f"[CREATE_TASK] Auto-resolved invalid phase_id for agent {agent_id[:8]}: {resolved_phase}"
                    )
                    request.phase_id = resolved_phase
                else:
                    logger.error(
                        f"[CREATE_TASK] REJECTED: invalid phase_id={repr(request.phase_id)}"
                    )
                    raise HTTPException(
                        status_code=400,
                        detail=f"phase_id cannot be None/null/empty string. "
                        f"Agent {agent_id} must provide a valid phase_id.",
                    )

            # Dedup: don't create duplicate tasks for the same phase
            dedup_phase_id = request.phase_id
            # Resolve an order number to the real Phase UUID — needed both when
            # phase_order was given directly, AND when phase_id itself is a
            # digit string (the MCP create_task tool sends phase order numbers
            # through the phase_id field, e.g. "4"). Without this, phase_id
            # stays as the literal string "4", which matches no real Phase row,
            # silently defeating both the dedup check below and the
            # own-phase guard further down.
            phase_order_to_resolve = request.phase_order
            if not phase_order_to_resolve and dedup_phase_id and str(dedup_phase_id).isdigit():
                phase_order_to_resolve = int(dedup_phase_id)
            if phase_order_to_resolve and (
                not dedup_phase_id or str(dedup_phase_id).isdigit()
            ):
                from src.core.database import Phase as PhaseModel

                _s = server_state.db_manager.get_session()
                try:
                    _phase = (
                        _s.query(PhaseModel)
                        .filter_by(
                            workflow_id=request.workflow_id, order=phase_order_to_resolve
                        )
                        .first()
                    )
                    if _phase:
                        dedup_phase_id = _phase.id
                finally:
                    _s.close()
            if dedup_phase_id:
                _s = server_state.db_manager.get_session()
                try:
                    from src.core.database import Task as TaskModel

                    existing = (
                        _s.query(TaskModel)
                        .filter(
                            TaskModel.phase_id == dedup_phase_id,
                            TaskModel.workflow_id == request.workflow_id,
                            TaskModel.status.in_(
                                ["pending", "assigned", "in_progress", "queued"]
                            ),
                        )
                        .first()
                    )
                    if existing:
                        # Content-aware dedup: a phase like 'development' can
                        # legitimately have many distinct tasks in flight at
                        # once. Matching on phase_id alone (as this used to)
                        # silently swallowed every genuinely-different task
                        # submitted while one was already active — the caller
                        # got back a false "created successfully" pointing at
                        # unrelated existing content, and the new work was
                        # never recorded anywhere. Only treat it as a real
                        # duplicate if the description is actually the same.
                        from difflib import SequenceMatcher

                        similarity = SequenceMatcher(
                            None,
                            (existing.raw_description or "")[:500],
                            request.task_description[:500],
                        ).ratio()
                        if similarity >= 0.85:
                            logger.info(
                                f"[CREATE_TASK] Dedup: phase already has near-identical "
                                f"active task {existing.id[:8]} (similarity={similarity:.2f}), returning it"
                            )
                            _s.close()
                            return CreateTaskResponse(
                                task_id=existing.id,
                                enriched_description=existing.enriched_description
                                or existing.raw_description,
                                assigned_agent_id=existing.assigned_agent_id
                                or "unassigned",
                                estimated_completion_time=30,
                                status="queued",
                            )
                        logger.info(
                            f"[CREATE_TASK] Phase has active task {existing.id[:8]} but "
                            f"new description differs (similarity={similarity:.2f}) — "
                            "creating a new task rather than deduping"
                        )
                finally:
                    if _s.is_active:
                        _s.close()

            # Guard: don't let a phase agent seed the FIRST task for a phase
            # other than its own. Agents have no reliable way to know a
            # workflow's real phase order/names (e.g. assuming "scope review
            # is phase 2, so phase 3 must be implementation" when phase 3 is
            # actually architecture_design) — guessing wrong here has created
            # tasks with content for the wrong phase, pre-empting the
            # orchestrator's own correctly-labeled auto-transition (which
            # only fires if no task exists yet for that phase). Agents with
            # no currently-assigned task (SDK/root/system agents bootstrapping
            # a workflow) are exempt.
            if dedup_phase_id:
                _s = server_state.db_manager.get_session()
                try:
                    from src.core.database import Phase as PhaseModel
                    from src.core.database import Task as TaskModel

                    own_task = (
                        _s.query(TaskModel)
                        .filter(
                            TaskModel.assigned_agent_id == agent_id,
                            TaskModel.workflow_id == request.workflow_id,
                        )
                        .order_by(TaskModel.created_at.desc())
                        .first()
                    )
                    if own_task and own_task.phase_id:
                        own_phase = (
                            _s.query(PhaseModel).filter_by(id=own_task.phase_id).first()
                        )
                        target_phase = (
                            _s.query(PhaseModel).filter_by(id=dedup_phase_id).first()
                        )
                        if (
                            own_phase
                            and target_phase
                            and own_phase.order != target_phase.order
                        ):
                            logger.error(
                                f"[CREATE_TASK] REJECTED: agent {agent_id[:8]} (own phase "
                                f"'{own_phase.name}', order {own_phase.order}) tried to seed "
                                f"the first task for phase '{target_phase.name}' "
                                f"(order {target_phase.order})"
                            )
                            raise HTTPException(
                                status_code=400,
                                detail=(
                                    f"Refusing to create a task for phase '{target_phase.name}' "
                                    f"(order {target_phase.order}) — you are working phase "
                                    f"'{own_phase.name}' (order {own_phase.order}). Only create "
                                    "subtasks within your OWN current phase. The orchestrator "
                                    "automatically creates the next phase's task, with the "
                                    "correct name and required output, once you mark your own "
                                    "task done — do not try to create it yourself."
                                ),
                            )
                finally:
                    if _s.is_active:
                        _s.close()

        # Create initial task in database with pending status
        session = server_state.db_manager.get_session()
        # Validate FK references exist; NULL out if not
        resolved_phase_id = request.phase_id
        if request.phase_id:
            from src.core.database import Phase
            if not session.query(Phase).filter_by(id=request.phase_id).first():
                resolved_phase_id = None
        # Ensure created_by_agent_id FK is satisfied
        from src.core.database import Agent
        if not session.query(Agent).filter_by(id=agent_id).first():
            session.add(Agent(
                id=agent_id,
                system_prompt="auto-created by create_task",
                status="idle",
                cli_type="system",
            ))
            session.flush()
        task = Task(
            id=task_id,
            raw_description=request.task_description,
            enriched_description=f"[Processing] {request.task_description}",  # Placeholder
            done_definition=request.done_definition,
            status="pending",
            priority=request.priority,
            parent_task_id=request.parent_task_id,
            created_by_agent_id=agent_id,
            phase_id=resolved_phase_id,
            workflow_id=request.workflow_id,  # Use workflow_id from request
            estimated_complexity=5,  # Default value
            ticket_id=request.ticket_id,  # Store associated ticket ID
            depends_on=request.depends_on,  # Task dependencies
            parallel_group=request.parallel_group,  # Parallel execution group
            max_concurrent=request.max_concurrent or 1,  # Max concurrent agents
        )
        session.add(task)
        session.commit()
        session.close()

        # Check if task's ticket is blocked
        if request.ticket_id:
            from src.services.task_blocking_service import TaskBlockingService

            blocking_info = TaskBlockingService.check_task_blocked(task_id)

            if blocking_info["is_blocked"]:
                # Ticket is blocked - mark task as blocked immediately
                logger.info(
                    f"Task {task_id} associated with blocked ticket {request.ticket_id}. "
                    f"Marking task as 'blocked'. Blocked by: {blocking_info['blocking_ticket_ids']}"
                )

                session = server_state.db_manager.get_session()
                try:
                    task_obj = session.query(Task).filter_by(id=task_id).first()
                    if task_obj:
                        task_obj.status = "blocked"

                        blocker_titles = [
                            t["title"] for t in blocking_info["blocking_tickets"]
                        ]
                        task_obj.completion_notes = (
                            f"Blocked by tickets: {', '.join(blocker_titles)}"
                        )

                        session.commit()
                finally:
                    session.close()

                # Broadcast blocked status
                await server_state.broadcast_update(
                    {
                        "type": "task_blocked",
                        "task_id": task_id,
                        "description": request.task_description[:200],
                        "blocking_tickets": blocking_info["blocking_ticket_ids"],
                    }
                )

                # Return immediately - don't process this task further
                return {
                    "task_id": task_id,
                    "enriched_description": request.task_description,  # Use raw description for blocked tasks
                    "assigned_agent_id": "none",  # No agent assigned for blocked tasks
                    "estimated_completion_time": 0,  # No estimate for blocked tasks
                    "status": "blocked",
                }

        # Process the rest asynchronously
        async def process_task_async():
            # Import Phase at the top to avoid scope issues
            from src.core.database import Phase
            from src.services.agent_dispatch_service import AgentDispatchService
            from src.services.task_enrichment_service import TaskEnrichmentService

            try:
                # 1. Determine phase if workflow is active. Only attempt
                # resolution if there's a workflow context at all (from the
                # request or the phase_manager singleton) — this guard is
                # specific to create_task (process_queue always has a
                # workflow_id already, since the task was already created).
                phase_id = request.phase_id
                workflow_id = None
                phase_context_str = ""

                target_workflow_id = (
                    request.workflow_id or server_state.phase_manager.workflow_id
                )
                if target_workflow_id:
                    phase_id = TaskEnrichmentService.resolve_phase_id(
                        phase_id_raw=request.phase_id,
                        phase_order=request.phase_order,
                        workflow_id=request.workflow_id,
                        requesting_agent_id=agent_id,
                    )
                    if phase_id:
                        phase_context_str, ctx_workflow_id = (
                            TaskEnrichmentService.get_phase_context_str(phase_id)
                        )
                        if ctx_workflow_id:
                            workflow_id = ctx_workflow_id
                    else:
                        logger.warning("No phase_id determined for task")
                else:
                    logger.warning("No active workflow in phase_manager")

                # 2. Determine working directory (priority: request > phase > server)
                working_directory = request.cwd  # From request
                if not working_directory and phase_id:
                    session = server_state.db_manager.get_session()
                    phase = session.query(Phase).filter_by(id=phase_id).first()
                    if phase and phase.working_directory:
                        working_directory = phase.working_directory
                    session.close()
                if not working_directory:
                    working_directory = os.getcwd()  # Server's current directory

                # 3-5. Retrieve RAG context, project context, and run LLM
                # enrichment (shared with process_queue — see
                # TaskEnrichmentService / docs/SOLID_OO_REVIEW.md 1.2/1.3).
                enrichment_result = await TaskEnrichmentService.enrich(
                    raw_description=request.task_description,
                    done_definition=request.done_definition,
                    phase_context_str=phase_context_str,
                    requesting_agent_id=agent_id,
                )
                enriched_task = enrichment_result["enriched_task"]
                context_memories = enrichment_result["context_memories"]
                project_context = enrichment_result["project_context"]

                # 6. Update task with enriched data
                session = server_state.db_manager.get_session()
                task = session.query(Task).filter_by(id=task_id).first()
                if task:
                    enriched_desc = enriched_task["enriched_description"]
                    if isinstance(enriched_desc, dict):
                        import json

                        enriched_desc = json.dumps(enriched_desc, indent=2)
                    task.enriched_description = enriched_desc
                    task.phase_id = phase_id
                    # Prioritize request.workflow_id for multi-workflow support, fallback to phase context
                    task.workflow_id = request.workflow_id or workflow_id
                    task.estimated_complexity = enriched_task.get(
                        "estimated_complexity", 5
                    )

                    # Check if phase has validation enabled and inherit it
                    if phase_id:
                        phase = session.query(Phase).filter_by(id=phase_id).first()
                        if phase and phase.validation:
                            # Check if validation is explicitly disabled
                            if phase.validation.get(
                                "enabled", True
                            ):  # Default to True if not specified
                                task.validation_enabled = True
                                logger.info(
                                    f"Task {task_id} inheriting validation from phase {phase.name}"
                                )
                            else:
                                logger.info(
                                    f"Task {task_id} validation explicitly disabled in phase {phase.name}"
                                )

                    session.commit()

                    # Store task data before closing session
                    task_data = {
                        "id": task_id,
                        "raw_description": request.task_description,
                        "enriched_description": enriched_task["enriched_description"],
                        "done_definition": request.done_definition,
                        "phase_id": phase_id,
                        "workflow_id": request.workflow_id,  # CRITICAL: Include workflow_id
                    }
                    session.close()

                    # 6.5 Check for duplicates if deduplication is enabled
                    duplicate_info = None
                    if (
                        server_state.embedding_service
                        and server_state.task_similarity_service
                        and get_config().task_dedup_enabled
                    ):
                        try:
                            # Generate embedding for enriched description
                            task_embedding = (
                                await server_state.embedding_service.generate_embedding(
                                    enriched_task["enriched_description"]
                                )
                            )

                            # Check for duplicates within the same phase
                            duplicate_info = await server_state.task_similarity_service.check_for_duplicates(
                                enriched_task["enriched_description"],
                                task_embedding,
                                phase_id=phase_id,  # Only check duplicates within same phase
                            )

                            if duplicate_info["is_duplicate"]:
                                # Update task as duplicate
                                session = server_state.db_manager.get_session()
                                task = session.query(Task).filter_by(id=task_id).first()
                                if task:
                                    task.status = "duplicated"
                                    task.duplicate_of_task_id = duplicate_info[
                                        "duplicate_of"
                                    ]
                                    task.similarity_score = duplicate_info[
                                        "max_similarity"
                                    ]
                                    session.commit()
                                session.close()

                                # Log the duplicate detection
                                logger.warning(
                                    f"Task {task_id} detected as duplicate of {duplicate_info['duplicate_of']} "
                                    f"with similarity {duplicate_info['max_similarity']:.3f}"
                                )

                                # Return early (don't create agent for duplicates)
                                return

                            # Store embedding and related tasks (not a duplicate)
                            await server_state.task_similarity_service.store_task_embedding(
                                task_id,
                                task_embedding,
                                related_tasks_details=duplicate_info.get(
                                    "related_tasks_details", []
                                ),
                            )

                            if duplicate_info.get("related_tasks"):
                                logger.info(
                                    f"Task {task_id} has {len(duplicate_info['related_tasks'])} related tasks"
                                )

                        except Exception as e:
                            logger.error(f"Failed to check for duplicates: {e}")
                            # Continue without deduplication on error

                    # 6.5 Check if we should queue the task
                    if server_state.queue_service.should_queue_task():
                        # At capacity - enqueue the task
                        server_state.queue_service.enqueue_task(task_id)

                        # Get queue status for broadcasting
                        queue_status = server_state.queue_service.get_queue_status()

                        # Broadcast queued status
                        await server_state.broadcast_update(
                            {
                                "type": "task_queued",
                                "task_id": task_id,
                                "description": enriched_task["enriched_description"][
                                    :200
                                ],
                                "queue_position": queue_status.get(
                                    "queued_tasks_count", 0
                                ),
                                "slots_available": queue_status.get(
                                    "slots_available", 0
                                ),
                            }
                        )

                        logger.info(
                            f"Task {task_id} queued (at capacity: {queue_status['active_agents']}/{queue_status['max_concurrent_agents']} agents)"
                        )
                        return  # Don't create agent yet

                    # 7. Create agent for the task (using task data, not the ORM object)
                    # Create a temporary task object for the agent manager
                    logger.info(f"[CREATE_TASK] Creating agent for task {task_id}")
                    logger.info(f"[CREATE_TASK] Task was created by agent: {agent_id}")

                    temp_task = Task(
                        id=task_id,
                        raw_description=task_data["raw_description"],
                        enriched_description=task_data["enriched_description"],
                        done_definition=task_data["done_definition"],
                        phase_id=task_data["phase_id"],
                        workflow_id=task_data[
                            "workflow_id"
                        ],  # CRITICAL: Include workflow_id
                        created_by_agent_id=agent_id,  # Important: Set the parent agent ID
                    )

                    # Dispatch reuses the RAG memories/project context already
                    # fetched during enrichment above (unlike process_queue,
                    # which re-fetches post-enrichment) — only the phase CLI
                    # config lookup is added here.
                    dispatch_context = (
                        await AgentDispatchService.build_dispatch_context_from_existing(
                            memories=context_memories,
                            project_context=project_context,
                            working_directory=working_directory,
                            phase_id=temp_task.phase_id,
                        )
                    )

                    agent = await AgentDispatchService.dispatch(
                        task=temp_task,
                        enriched_data=enriched_task,
                        dispatch_context=dispatch_context,
                    )

                    # Store agent ID immediately (before session issues)
                    agent_id_str = str(agent.id) if agent else None

                    # 8. Update task with assigned agent
                    AgentDispatchService.mark_assigned(
                        task_id, agent_id_str, status="assigned"
                    )

                    # 9. Broadcast update via WebSocket
                    await server_state.broadcast_update(
                        {
                            "type": "task_created",
                            "task_id": task_id,
                            "agent_id": agent_id_str,
                            "description": enriched_task["enriched_description"][:200],
                        }
                    )

                    logger.info(f"Task {task_id} processed successfully in background")
                else:
                    logger.error(f"Task {task_id} not found after creation")

            except Exception as e:
                logger.error(f"Failed to process task {task_id} in background: {e}")
                # Update task status to failed
                session = server_state.db_manager.get_session()
                task = session.query(Task).filter_by(id=task_id).first()
                if task:
                    task.status = "failed"
                    task.failure_reason = str(e)
                    session.commit()
                session.close()

        # Start processing in the background without waiting
        import asyncio

        asyncio.create_task(process_task_async())

        # Return immediately with pending status
        return CreateTaskResponse(
            task_id=task_id,
            enriched_description=f"[Processing] {request.task_description}",
            assigned_agent_id="pending",
            estimated_completion_time=25,
            status="pending",
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/validate_agent_id/{agent_id}")
async def validate_agent_id(agent_id: str):
    """Quick endpoint for agents to validate their ID format.

    Returns:
        Success if ID matches UUID format, error otherwise
    """
    import re

    uuid_pattern = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
    )

    if uuid_pattern.match(agent_id):
        return {
            "valid": True,
            "message": f"✅ Agent ID {agent_id} is valid UUID format",
        }
    else:
        return {
            "valid": False,
            "message": f"❌ Agent ID '{agent_id}' is NOT valid. Use the UUID from your initial prompt.",
            "common_mistakes": [
                "Using 'agent-mcp' instead of actual UUID",
                "Using 'main-session-agent' when you're not the main session",
                "Typo in UUID",
            ],
        }


@app.post("/update_task_status", response_model=UpdateTaskStatusResponse)
async def update_task_status(
    request: UpdateTaskStatusRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Update task status when complete or failed."""
    # SECURITY: Verify agent authentication before allowing status updates
    if not await verify_agent_authentication(agent_id):
        raise HTTPException(
            status_code=401,
            detail="Agent not authenticated. Provide valid X-Agent-ID header.",
        )

    _touch_agent_activity(agent_id)
    logger.info(f"Updating task {request.task_id} status to {request.status}")

    # There's no dedicated column for structured verdict/count data agents
    # sometimes attach (e.g. a scope-review gate's verdict + issue counts) --
    # fold it into the summary text so it's preserved everywhere summary
    # already flows (completion_notes, memories, etc.) instead of adding a
    # new storage path for what's still just descriptive detail.
    if request.metadata:
        request.summary = (
            f"{request.summary}\n\n[metadata] {json.dumps(request.metadata)}"
        ).strip()

    from src.services.task_completion_service import TaskCompletionService

    # FIX #5: Wrap entire handler in try/finally to prevent session leaks
    # on early returns (404, 403, rejection dict).
    session = server_state.db_manager.get_session()
    try:
        # 1. Verify task exists and agent is authorized.
        # Primary check: agent is the currently assigned agent.
        # Secondary check: agent was created for this task (current_task_id match).
        #   This handles retry scenarios where a new agent is dispatched for the
        #   same task but the old agent (still running) tries to report its status.
        #   The old agent's current_task_id still points to this task.
        task = session.query(Task).filter_by(id=request.task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        is_current_assignee = (task.assigned_agent_id == agent_id)
        if not is_current_assignee:
            # Secondary check: does this agent have this task as its current_task_id?
            agent_record = session.query(Agent).filter_by(id=agent_id).first()
            if agent_record and agent_record.current_task_id == request.task_id:
                logger.warning(
                    f"Agent {agent_id[:8]} updating task {request.task_id[:8]} "
                    f"but is not current assignee (current: {task.assigned_agent_id}). "
                    f"Allowing because agent's current_task_id matches."
                )
            else:
                # Tertiary check: was this agent ever assigned to this task?
                # This handles the case where an agent was terminated (current_task_id
                # cleared) but its tmux session is still alive and trying to report.
                from src.core.database import AgentLog
                agent_was_assigned = session.query(AgentLog).filter(
                    AgentLog.agent_id == agent_id,
                    AgentLog.log_type == "created",
                    AgentLog.details["task_id"].as_string() == request.task_id,
                ).first()
                if not agent_was_assigned:
                    # Fallback: check if agent's details contain the task_id
                    agent_logs = session.query(AgentLog).filter(
                        AgentLog.agent_id == agent_id,
                        AgentLog.log_type == "created",
                    ).all()
                    for log in agent_logs:
                        if log.details and log.details.get("task_id") == request.task_id:
                            agent_was_assigned = log
                            break
                
                if agent_was_assigned:
                    logger.warning(
                        f"Agent {agent_id[:8]} updating task {request.task_id[:8]} "
                        f"but is not current assignee (current: {task.assigned_agent_id}). "
                        f"Allowing because agent was previously assigned to this task (terminated agent completing work)."
                    )
                else:
                    raise HTTPException(
                        status_code=403, detail="Agent not authorized for this task"
                    )

        # 2. Save learnings as memories
        await TaskCompletionService.record_learnings(
            session, agent_id, request.task_id, request.key_learnings, request.code_changes
        )

        # 3. Check if task has results reported
        if request.status == "done" and not task.has_results:
            logger.warning(
                f"Task {request.task_id} completed without formal results reported"
            )

        # Fetched once and reused below (self-review gate + the output-artifact
        # floor a few lines down both need this same task's Phase row).
        phase = (
            session.query(Phase).filter_by(id=task.phase_id).first()
            if task.phase_id
            else None
        )

        # 3a. One-shot self-review (docs/GAP_CHECK_SELF_LOOP_DESIGN.md) — the
        # first "done" from a phase with self_review enabled doesn't complete
        # the task; it sends a fixed checklist back to the same (still-running)
        # agent and requires a second "done" call. Runs before the output-artifact
        # floor and the validator loop: self-review is the cheapest, warmest-context
        # gate and should catch what it can before either of those engage.
        if request.status == "done" and task.phase_id and not task.self_review_done:
            if phase and phase.self_review and phase.self_review.get("enabled", False):
                # Set BEFORE messaging -- crash-safe. If the process dies before
                # the message is delivered, the worst case is a skipped prompt,
                # not an infinite re-trigger of this branch on retry.
                task.self_review_done = True
                task.self_review_started_at = datetime.utcnow()
                task.self_review_started_commit = _resolve_worktree_head_sha(session, task)
                task.completion_notes = request.summary
                session.commit()

                logger.info(
                    f"[SELF-REVIEW] Task {task.id[:8]} (phase {phase.name}) fired — "
                    f"agent {agent_id[:8]}, worktree HEAD "
                    f"{(task.self_review_started_commit or 'unknown')[:8]}"
                )

                await server_state.agent_manager.send_message_to_agent(
                    agent_id, SELF_REVIEW_CHECKLIST_PROMPT
                )

                return UpdateTaskStatusResponse(
                    success=True,
                    message="Self-review requested — re-check your work, then call update_task_status(done) again.",
                    termination_scheduled=False,
                )

        # 3a-2. Self-review telemetry — this task went through the gate above
        # on a prior call and is now completing for real. Log elapsed time and
        # a diff-stat of what changed during the review pass: the actual signal
        # for whether one pass is worth the extra LLM turn (design doc "Telemetry").
        if request.status == "done" and task.self_review_started_at is not None:
            elapsed = (datetime.utcnow() - task.self_review_started_at).total_seconds()
            diff_stat = None
            if task.self_review_started_commit:
                worktree_path = _resolve_worktree_path(session, task)
                if worktree_path:
                    try:
                        repo = Repo(worktree_path)
                        diff_stat = repo.git.diff(
                            task.self_review_started_commit, "HEAD", stat=True
                        )
                    except Exception as e:
                        logger.debug(
                            f"[SELF-REVIEW] Could not diff worktree for task {task.id[:8]}: {e}"
                        )
            logger.info(
                f"[SELF-REVIEW] Task {task.id[:8]} completed {elapsed:.0f}s after "
                f"self-review fired. Diff since review: "
                f"{diff_stat.strip() if diff_stat else '(no changes / diff unavailable)'}"
            )
            # Clear so this doesn't re-log if 'done' is ever seen again for the
            # same task (shouldn't normally happen once status is terminal).
            task.self_review_started_at = None
            task.self_review_started_commit = None
            session.commit()

        # 3b. Output-existence hard floor — reject done when declared output
        # artifact is missing. General, not phase-special-cased: drives off
        # PHASE_OUTPUT_ARTIFACTS in spec.py. Same class as ruff/tests: a
        # mechanical, hard floor check.
        if request.status == "done" and task.phase_id:
            rejection = TaskCompletionService.verify_output_artifact(session, task, phase=phase)
            if rejection:
                # rejection is a plain {"status", "message"} dict (not the
                # response_model's shape) — returning it directly makes
                # FastAPI's response_model validation fail with a 500 (missing
                # 'success'/'termination_scheduled'), which hides the actual
                # "missing output artifact" reason from the agent and instead
                # just looks like a broken server, causing blind retries.
                #
                # Persist the reason on the task even though status stays
                # non-terminal: if this agent's session ends (times out,
                # killed) before it retries, _clean_stale_assigned_tasks
                # will mark this task "failed" with only a generic "agent
                # terminated" message -- without this, the specific
                # validation problem is lost, and the orchestrator's retry
                # (_maybe_retry_failed_tasks) respawns a fresh agent with no
                # memory of what actually needs fixing.
                task.failure_reason = rejection.get("message", "Output validation failed")
                session.commit()
                return UpdateTaskStatusResponse(
                    success=False,
                    message=rejection.get("message", "Output validation failed"),
                    termination_scheduled=False,
                )

        # 3b-0-b. Gate-result schema hard floor — for gated phases, reject
        # done when the structured JSON result exists (3b already covers it
        # being missing) but doesn't have any of the keys the gate's score_*
        # function actually reads. Same class of check as 3b: a documented
        # schema alone is compliance-dependent (an agent can write valid
        # JSON in a totally different shape and still pass the existence
        # check), this makes the shape itself enforced.
        if request.status == "done" and task.phase_id:
            rejection = TaskCompletionService.verify_gate_result_schema(session, task, phase=phase)
            if rejection:
                task.failure_reason = rejection.get("message", "Gate result schema invalid")
                session.commit()
                return UpdateTaskStatusResponse(
                    success=False,
                    message=rejection.get("message", "Gate result schema invalid"),
                    termination_scheduled=False,
                )

        # 3b-1. Open-ticket hard floor — reject done on the development phase
        # while unresolved bug tickets (QA/security findings) remain. Same
        # class of check as 3b above: a prompt instruction alone is
        # compliance-dependent, this makes "fixed and resolved" enforced.
        if request.status == "done" and task.phase_id:
            rejection = TaskCompletionService.verify_no_open_tickets(session, task, phase=phase)
            if rejection:
                task.failure_reason = rejection.get("message", "Open tickets remain unresolved")
                session.commit()
                return UpdateTaskStatusResponse(
                    success=False,
                    message=rejection.get("message", "Open tickets remain unresolved"),
                    termination_scheduled=False,
                )

        # 3b-2. Auto-create tickets from forensics_analysis's own report —
        # "Tickets created for actionable findings" is mandated but easily
        # skipped once the agent's analysis work is done (observed live: a
        # thorough report with zero ticket calls). Best-effort side effect,
        # never blocks "done".
        if request.status == "done" and task.phase_id:
            await TaskCompletionService.create_tickets_from_forensics_report(
                session, task
            )

        # 4. Check if task has validation enabled
        validation_spawned = False
        if request.status == "done" and task.validation_enabled:
            # Agent claims done but needs validation
            task.status = "under_review"
            task.validation_iteration += 1
            task.completion_notes = request.summary

            # Capture task attributes before async function (to avoid detached instance issues)
            task_validation_iteration = task.validation_iteration
            task_workflow_id = task.workflow_id

            session.commit()

            # Mark original agent as kept alive for validation (do this immediately)
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if agent:
                agent.kept_alive_for_validation = True
                session.commit()

            # Process validation spawning asynchronously (like create_task)
            asyncio.create_task(
                TaskCompletionService.spawn_validation(
                    agent_id=agent_id,
                    task_id=request.task_id,
                    task_workflow_id=task_workflow_id,
                    task_validation_iteration=task_validation_iteration,
                )
            )
            validation_spawned = True

        else:
            # No validation or task failed - proceed normally
            task.status = request.status
            task.completed_at = datetime.utcnow()
            task.completion_notes = request.summary

            if request.status == "failed":
                task.failure_reason = request.failure_reason

            session.commit()

            # Commit in the shared worktree when a task completes successfully,
            # and auto-link the resulting commit to the task's ticket if any.
            if request.status == "done":
                await TaskCompletionService.commit_and_link_ticket(
                    session, agent_id, task, request.summary
                )

            # 4. Schedule agent termination and queue processing (only if no validation)
            async def terminate_and_process_queue():
                await server_state.agent_manager.terminate_agent(agent_id)
                await process_queue()

            asyncio.create_task(terminate_and_process_queue())

        # 3c. Spec gate firing — when a gated phase task completes and the phase
        # is now complete, trigger the gate immediately (don't wait for monitor poll).
        # The orchestrator's _advance_phases only fires when the next phase is
        # pending — if it's already in_progress, the gate is missed. Fix: fire from
        # the completion path itself and actually trigger the evaluation.
        #
        # Must run AFTER the worktree commit above, not before: a goto decision
        # deletes the gate phase's result files (consume_gate_artifacts) so a
        # later re-run can't re-score stale ones. Firing before the commit
        # deleted a report the agent had just written in the same request,
        # before it was ever captured in git history -- the file (and its
        # findings, beyond what's threaded into the corrective task's
        # description) would be lost outright instead of preserved in a commit.
        if request.status == "done" and task.phase_id:
            await TaskCompletionService.fire_spec_gate_if_ready(session, task)

        # 5. Broadcast update
        await server_state.broadcast_update(
            {
                "type": "task_completed",
                "task_id": request.task_id,
                "agent_id": agent_id,
                "status": request.status,
                "summary": request.summary[:200],
            }
        )

        # Return appropriate response based on whether validation was spawned
        if validation_spawned:
            return UpdateTaskStatusResponse(
                success=True,
                message="Task submitted for validation. A validation agent has been spawned - please wait for validation results.",
                termination_scheduled=False,  # Agent kept alive for validation feedback
            )
        else:
            return UpdateTaskStatusResponse(
                success=True,
                message=f"Task {request.status} successfully",
                termination_scheduled=True,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update task status: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()



@app.get("/api/workflows")
async def get_workflows_endpoint(
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Get all workflows."""
    logger.info(f"Agent {agent_id} fetching workflows")

    try:
        session = server_state.db_manager.get_session()
        try:
            workflows = session.query(Workflow).all()

            return [
                {
                    "id": w.id,
                    "name": w.name,
                    "status": w.status,
                    "phases_folder_path": w.phases_folder_path,
                    "created_at": w.created_at.isoformat() if w.created_at else None,
                }
                for w in workflows
            ]
        finally:
            session.close()

    except Exception as e:
        logger.error(f"Failed to fetch workflows: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/{task_id}/pause")
async def pause_task_endpoint(task_id: str):
    """Pause a single task: terminate its agent (if any, WIP is committed by
    terminate_agent) and mark it 'blocked' so it won't be picked up again until
    Resume is pressed. Mirrors /features/{id}/pause's per-task logic, scoped to
    just this one task.
    """
    logger.info(f"Pause request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status not in (
                "pending",
                "queued",
                "assigned",
                "in_progress",
                "under_review",
                "validation_in_progress",
                "needs_work",
            ):
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot pause task in '{task.status}' status",
                )

            agent_id = task.assigned_agent_id
            task.status = "blocked"
            task.assigned_agent_id = None
            session.commit()
        finally:
            session.close()

        if agent_id:
            await server_state.agent_manager.terminate_agent(agent_id)

        await server_state.broadcast_update(
            {"type": "task_paused", "task_id": task_id}
        )

        return {"success": True, "task_id": task_id, "status": "blocked"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to pause task {task_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bump_task_priority")
async def bump_task_priority_endpoint(
    task_id: str = Body(..., embed=True),
):
    """Bump a queued task and start it immediately, bypassing the agent limit.

    This allows urgent tasks to start even when at max capacity (e.g., 2/2 → 3/2).
    When agents complete, the system returns to the configured limit.
    """
    logger.info(f"Priority bump & start request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            # Verify task exists and is queued
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status != "queued":
                raise HTTPException(
                    status_code=400,
                    detail=f"Task {task_id} is not queued (status: {task.status})",
                )

        finally:
            session.close()

        # Boost the task priority first
        success = server_state.queue_service.boost_task_priority(task_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to boost task priority")

        # Dequeue and start immediately (bypassing limit)
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            # Dequeue the task
            server_state.queue_service.dequeue_task(task_id)
        finally:
            session.close()

        from src.services.agent_dispatch_service import AgentDispatchService

        dispatch_context = await AgentDispatchService.build_dispatch_context(
            task_description_for_rag=task.enriched_description or task.raw_description,
            phase_id=task.phase_id,
        )

        # Create agent immediately (bypassing agent limit)
        agent = await AgentDispatchService.dispatch(
            task=task,
            enriched_data={"enriched_description": task.enriched_description},
            dispatch_context=dispatch_context,
        )

        # Update task status
        AgentDispatchService.mark_assigned(task_id, agent.id, status="assigned")

        # Broadcast update
        await server_state.broadcast_update(
            {
                "type": "task_priority_bumped",
                "task_id": task_id,
                "agent_id": agent.id,
            }
        )

        logger.info(
            f"Task {task_id} bumped and agent {agent.id} created (bypassing limit)"
        )

        return {
            "success": True,
            "message": f"Task {task_id[:8]} started immediately (bypassing agent limit)",
            "agent_id": agent.id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to bump and start task: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str):
    """Cancel a task by ID."""
    logger.info(f"Cancel request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        cancelled_task_id = None
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                # Try prefix match with escaped LIKE wildcards
                escaped = task_id.replace("%", "\\%").replace("_", "\\_")
                task = (
                    session.query(Task)
                    .filter(Task.id.like(f"{escaped}%", escape="\\"))
                    .first()
                )
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            # Only allow cancelling pending or queued tasks
            if task.status not in ("pending", "queued"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot cancel task in '{task.status}' status. Terminate the assigned agent first.",
                )

            task.status = "failed"
            task.failure_reason = "Cancelled by user"
            task.completed_at = datetime.utcnow()
            cancelled_task_id = task.id
            session.commit()

        finally:
            session.close()

        if cancelled_task_id:
            await server_state.broadcast_update(
                {
                    "type": "task_cancelled",
                    "task_id": cancelled_task_id,
                }
            )

            logger.info(f"Task {cancelled_task_id} cancelled")
            return {"success": True, "task_id": cancelled_task_id}
        else:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel task: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/cancel_queued_task")
async def cancel_queued_task_endpoint(
    task_id: str = Body(..., embed=True),
):
    """Cancel a queued task and remove it from the queue.

    The task will be marked as failed and removed from the queue.
    """
    logger.info(f"Cancel request for queued task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            # Verify task exists and is queued
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status != "queued":
                raise HTTPException(
                    status_code=400,
                    detail=f"Task {task_id} is not queued (status: {task.status})",
                )

            # Mark task as failed
            task.status = "failed"
            task.failure_reason = "Cancelled by user from queue"
            task.completed_at = datetime.utcnow()
            session.commit()

        finally:
            session.close()

        # Remove from queue
        server_state.queue_service.dequeue_task(task_id)

        # Broadcast update
        await server_state.broadcast_update(
            {
                "type": "task_cancelled",
                "task_id": task_id,
            }
        )

        logger.info(f"Task {task_id} cancelled and removed from queue")

        return {
            "success": True,
            "message": f"Task {task_id[:8]} cancelled and removed from queue",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to cancel queued task: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/restart_task")
async def restart_task_endpoint(
    task_id: str = Body(..., embed=True),
):
    """Restart a completed or failed task.

    This will:
    - Clear completion data (failure_reason, completion_notes, completed_at)
    - Clear trajectory data (guardian analyses, steering interventions)
    - Reset task to pending/queued status
    - Create new agent or queue based on capacity
    """
    logger.info(f"Restart request for task {task_id}")

    try:
        session = server_state.db_manager.get_session()
        try:
            # Verify task exists and is done/failed
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            if task.status not in ["done", "failed", "blocked"]:
                raise HTTPException(
                    status_code=400,
                    detail=f"Can only restart completed, failed, or paused tasks (current status: {task.status})",
                )

            # Get agent ID before clearing (to delete trajectory data)
            old_agent_id = task.assigned_agent_id

            # Clear completion data
            task.status = "pending"
            task.assigned_agent_id = None
            task.started_at = None
            task.completed_at = None
            task.completion_notes = None
            task.failure_reason = None
            # This row is reused (not recreated) on restart -- without
            # clearing these too, a task previously tagged action="goto"/
            # "retry" by _tag_completing_task keeps showing that badge
            # (and a now-meaningless action_target_phase) after being
            # restarted into an unrelated fresh attempt.
            task.action = ""
            task.action_target_phase = None

            # Reopen-point fix (same as _create_corrective_task): resetting
            # the task alone isn't enough if its workflow/phase already
            # thinks it's "completed". Observed live: restarting an
            # already-done task left it stuck pending forever once the
            # agent-creation below got interrupted (e.g. a backend restart
            # mid-request) -- the phase-advancement sweep only ever
            # reconsiders phases that are pending/in_progress, so a task
            # sitting pending under a "completed" phase/workflow is
            # invisible to every self-heal path and nothing ever recreates
            # its agent. Without this, restart_task is only safe when its
            # own inline agent-creation below never fails.
            if task.workflow_id:
                from src.core.database import PhaseExecution, Workflow

                wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
                if wf and wf.status != "active":
                    wf.status = "active"
                if task.phase_id:
                    execution = (
                        session.query(PhaseExecution)
                        .filter_by(phase_id=task.phase_id)
                        .first()
                    )
                    if execution and execution.status != "in_progress":
                        execution.status = "in_progress"
                        execution.task_creation_claimed_at = None

            session.commit()

        finally:
            session.close()

        # Clear trajectory data for old agent
        if old_agent_id:
            session = server_state.db_manager.get_session()
            try:
                from src.core.database import GuardianAnalysis, SteeringIntervention

                # Delete guardian analyses
                session.query(GuardianAnalysis).filter_by(
                    agent_id=old_agent_id
                ).delete()

                # Delete steering interventions
                session.query(SteeringIntervention).filter_by(
                    agent_id=old_agent_id
                ).delete()

                session.commit()
                logger.info(f"Cleared trajectory data for agent {old_agent_id}")

            finally:
                session.close()

        # Check if we should queue or create agent immediately
        should_queue = server_state.queue_service.should_queue_task()

        if should_queue:
            # Queue the task
            server_state.queue_service.enqueue_task(task_id)
            logger.info(f"Task {task_id} restarted and queued")

            # Broadcast update
            await server_state.broadcast_update(
                {
                    "type": "task_restarted",
                    "task_id": task_id,
                    "status": "queued",
                }
            )

            return {
                "success": True,
                "message": f"Task {task_id[:8]} restarted and added to queue",
                "status": "queued",
            }
        else:
            # Create agent immediately
            session = server_state.db_manager.get_session()
            try:
                task = session.query(Task).filter_by(id=task_id).first()
            finally:
                session.close()

            from src.services.agent_dispatch_service import AgentDispatchService

            # NOTE: this now also fetches phase CLI config, which the
            # previous inline version of this endpoint did not (only
            # bump_task_priority_endpoint did) — an inconsistency flagged
            # in docs/SOLID_OO_REVIEW.md finding 1.3 that this shared
            # dispatch-context helper fixes by construction.
            dispatch_context = await AgentDispatchService.build_dispatch_context(
                task_description_for_rag=task.enriched_description
                or task.raw_description,
                phase_id=task.phase_id,
            )

            # Create agent for the task
            agent = await AgentDispatchService.dispatch(
                task=task,
                enriched_data={"enriched_description": task.enriched_description},
                dispatch_context=dispatch_context,
            )

            # Update task status
            AgentDispatchService.mark_assigned(task_id, agent.id, status="assigned")

            logger.info(f"Task {task_id} restarted with new agent {agent.id}")

            # Broadcast update
            await server_state.broadcast_update(
                {
                    "type": "task_restarted",
                    "task_id": task_id,
                    "agent_id": agent.id,
                    "status": "assigned",
                }
            )

            return {
                "success": True,
                "message": f"Task {task_id[:8]} restarted with new agent",
                "agent_id": agent.id,
                "status": "assigned",
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to restart task: {e}")
        import traceback

        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/queue_status")
async def get_queue_status_endpoint():
    """Get current queue status information.

    Returns information about active agents, queued tasks, and available slots.
    """
    try:
        status = server_state.queue_service.get_queue_status()
        return status
    except Exception as e:
        logger.error(f"Failed to get queue status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time updates."""
    await websocket.accept()
    server_state.active_websockets.append(websocket)

    try:
        while True:
            # Keep connection alive and handle any incoming messages
            data = await websocket.receive_text()
            # Echo back or handle commands
            await websocket.send_json({"type": "echo", "data": data})

    except WebSocketDisconnect:
        server_state.active_websockets.remove(websocket)
        logger.info("WebSocket client disconnected")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "1.0.0",
    }


# OAuth endpoints for MCP compatibility with Dynamic Client Registration
@app.get("/.well-known/oauth-authorization-server")
async def oauth_server_metadata():
    """OAuth server metadata with DCR support."""
    config = get_config()
    base_url = f"http://localhost:{config.mcp_port}"
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "registration_endpoint": f"{base_url}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint": f"{base_url}/oauth/revoke",
        "scopes_supported": ["openid", "profile", "email"],
    }


@app.get("/.well-known/openid-configuration")
async def openid_config():
    """OpenID configuration - tells Claude no auth needed."""
    config = get_config()
    base_url = f"http://localhost:{config.mcp_port}"
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/authorize",
        "token_endpoint": f"{base_url}/token",
        "userinfo_endpoint": f"{base_url}/userinfo",
        "response_types_supported": ["none"],
        "grant_types_supported": ["none"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["none"],
    }


# Store registered clients (in production, use a database)
registered_clients = {}


@app.post("/oauth/register")
async def register_client(request: Dict[str, Any]):
    """Dynamic Client Registration endpoint (RFC 7591)."""
    import secrets

    client_id = f"client_{secrets.token_urlsafe(16)}"
    client_secret = secrets.token_urlsafe(32)

    # Store client registration
    registered_clients[client_id] = {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": request.get("client_name", "Claude"),
        "redirect_uris": request.get(
            "redirect_uris", ["https://claude.ai/api/mcp/auth_callback"]
        ),
        "grant_types": request.get("grant_types", ["authorization_code"]),
        "response_types": request.get("response_types", ["code"]),
        "scope": request.get("scope", "openid profile email"),
        "token_endpoint_auth_method": request.get("token_endpoint_auth_method", "none"),
    }

    # Return client registration response
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(datetime.utcnow().timestamp()),
        "client_secret_expires_at": 0,  # Never expires
        "redirect_uris": registered_clients[client_id]["redirect_uris"],
        "grant_types": registered_clients[client_id]["grant_types"],
        "response_types": registered_clients[client_id]["response_types"],
        "client_name": registered_clients[client_id]["client_name"],
        "scope": registered_clients[client_id]["scope"],
        "token_endpoint_auth_method": registered_clients[client_id][
            "token_endpoint_auth_method"
        ],
    }


@app.get("/oauth/authorize")
async def authorize_get(
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    scope: str = "openid profile email",
    state: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
):
    """Authorization endpoint - auto-approves for local use."""
    import secrets

    # Generate authorization code
    auth_code = secrets.token_urlsafe(32)

    # Build redirect URL with code
    redirect_url = f"{redirect_uri}?code={auth_code}"
    if state:
        redirect_url += f"&state={state}"

    # Return HTML that auto-redirects (simulating user approval)
    html_content = f"""
    <html>
    <head>
        <meta http-equiv="refresh" content="0; url={redirect_url}">
    </head>
    <body>
        <p>Authorizing... Redirecting to Claude...</p>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)


@app.post("/oauth/authorize")
async def authorize_post(request: Dict[str, Any]):
    """Authorization endpoint POST - for form submissions."""
    return await authorize_get(
        client_id=request.get("client_id"),
        redirect_uri=request.get("redirect_uri"),
        response_type=request.get("response_type", "code"),
        scope=request.get("scope", "openid profile email"),
        state=request.get("state"),
        code_challenge=request.get("code_challenge"),
        code_challenge_method=request.get("code_challenge_method"),
    )


@app.post("/oauth/token")
async def token(request: Dict[str, Any] = Body(...)):
    """Token endpoint - returns access token."""
    import secrets

    # For simplicity, always return a valid token (no real auth)
    return {
        "access_token": f"access_{secrets.token_urlsafe(32)}",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": f"refresh_{secrets.token_urlsafe(32)}",
        "scope": request.get("scope", "openid profile email"),
    }


@app.post("/oauth/revoke")
async def revoke_token(request: Dict[str, Any]):
    """Token revocation endpoint."""
    # For local use, just return success
    return {"revoked": True}


@app.get("/userinfo")
async def userinfo():
    """Fake userinfo endpoint."""
    return {
        "sub": "local-user",
        "name": "Local User",
        "preferred_username": "local",
    }


@app.get("/")
async def root():
    """Root endpoint with MCP protocol info."""
    return {
        "name": "Hephaestus MCP Server",
        "version": "1.0.0",
        "protocol_version": "1.0",
        "description": "Model Context Protocol server for AI agent orchestration",
        "capabilities": {
            "tools": True,
            "resources": True,
            "prompts": False,
            "auth": {"type": "none", "required": False},
        },
        "endpoints": [
            "/create_task",
            "/update_task_status",
            "/save_memory",
            "/agent_status",
            "/task_progress",
            "/health",
            "/ws",
            "/sse",
            "/tools",
            "/resources",
        ],
    }


# MCP Protocol endpoints
@app.get("/tools")
async def list_tools():
    """List available MCP tools."""
    return {
        "tools": [
            {
                "name": "create_task",
                "description": "Create a new task for an autonomous agent",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task_description": {
                            "type": "string",
                            "description": "Description of the task",
                        },
                        "done_definition": {
                            "type": "string",
                            "description": "What constitutes completion",
                        },
                        "workflow_id": {
                            "type": "string",
                            "description": "ID of the workflow execution this task belongs to (REQUIRED)",
                        },
                        "phase_id": {
                            "type": "string",
                            "description": "Phase ID for workflow-based tasks (REQUIRED)",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "ticket_id": {
                            "type": "string",
                            "description": "Associated ticket ID",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of task IDs that must complete before this one. OMIT or set null for sequential execution (one at a time). Set to [] for immediate parallel execution. Set to [task_id, ...] to wait for specific tasks.",
                        },
                        "parallel_group": {
                            "type": "string",
                            "description": "Tasks in same group can run in parallel. Different groups are sequential.",
                        },
                        "max_concurrent": {
                            "type": "integer",
                            "description": "Max agents working on this task simultaneously (default: 1)",
                        },
                        "context": {
                            "type": "string",
                            "description": "Additional context for the agent (e.g., design document content, requirements summary)",
                        },
                    },
                    "required": [
                        "task_description",
                        "done_definition",
                        "workflow_id",
                        "phase_id",
                    ],
                },
            },
            {
                "name": "save_memory",
                "description": "Save a memory to the knowledge base",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["content", "memory_type"],
                },
            },
            {
                "name": "search_memory",
                "description": "Search the knowledge base for relevant memories using semantic search",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query to find relevant memories",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 10)",
                        },
                        "memory_type": {
                            "type": "string",
                            "description": "Filter by memory type (e.g., decision, discovery, learning)",
                        },
                        "project_id": {
                            "type": "string",
                            "description": "Filter by project ID (auto-detected from agent if not set)",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_task_status",
                "description": "Get status of tasks, optionally filtered by agent_id or workflow_id",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "description": "Filter by task status (pending, assigned, in_progress, done, failed)",
                            "default": "all"
                        },
                        "agent_id": {
                            "type": "string",
                            "description": "Filter tasks assigned to this agent"
                        },
                        "workflow_id": {
                            "type": "string",
                            "description": "Filter tasks belonging to this workflow"
                        }
                    }
                },
            },
            {
                "name": "update_task_status",
                "description": "Update the status of a task (done, failed, etc.)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "ID of the task to update",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["done", "failed", "in_progress", "blocked"],
                            "description": "New status for the task",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Summary of what was done or why it failed",
                            "default": "",
                        },
                        "failure_reason": {
                            "type": "string",
                            "description": "Reason for failure (if status is failed)",
                            "default": "",
                        },
                        "key_learnings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Key learnings to save as memories",
                        },
                        "metadata": {
                            "type": "object",
                            "description": "Optional structured data (e.g. verdict, issue counts) — folded into summary",
                        },
                    },
                    "required": ["task_id", "status"],
                },
            },
            {
                "name": "create_ticket",
                "description": "Create a new ticket in the Kanban board",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Ticket title"},
                        "description": {
                            "type": "string",
                            "description": "Detailed description",
                        },
                        "ticket_type": {
                            "type": "string",
                            "enum": ["bug", "feature", "improvement", "task", "spike"],
                            "description": "Type of ticket",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "critical"],
                            "description": "Priority level",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags for categorization",
                        },
                        "blocked_by_ticket_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "IDs of blocking tickets",
                        },
                        "agent_id": {
                            "type": "string",
                            "description": "Agent ID creating this ticket",
                        },
                        "task_id": {
                            "type": "string",
                            "description": "Task ID this ticket relates to",
                        },
                        "phase_id": {
                            "type": "string",
                            "description": "Phase ID where this ticket was created",
                        },
                    },
                    "required": ["title", "description", "ticket_type", "priority"],
                },
            },
            {
                "name": "search_tickets",
                "description": "Search for existing tickets by title or tags",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query for title",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Filter by tags",
                        },
                        "status": {"type": "string", "description": "Filter by status"},
                    },
                    "required": [],
                },
            },
            {
                "name": "update_ticket_status",
                "description": "Update the status of a ticket",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "string", "description": "Ticket ID"},
                        "new_status": {
                            "type": "string",
                            "description": "New status value",
                        },
                    },
                    "required": ["ticket_id", "new_status"],
                },
            },
            {
                "name": "broadcast_message",
                "description": "Send a message to ALL active agents",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "message": {
                            "type": "string",
                            "description": "Message content to broadcast",
                        },
                        "sender_id": {
                            "type": "string",
                            "description": "Sender agent ID",
                        },
                    },
                    "required": ["message"],
                },
            },
            {
                "name": "send_message",
                "description": "Send a direct message to a specific agent",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Target agent ID",
                        },
                        "message": {
                            "type": "string",
                            "description": "Message content",
                        },
                        "sender_id": {
                            "type": "string",
                            "description": "Sender agent ID",
                        },
                    },
                    "required": ["agent_id", "message"],
                },
            },
            {
                "name": "devtools_connect",
                "description": "Connect to Chrome DevTools Protocol for browser automation",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session identifier for this browser connection",
                        },
                        "debug_url": {
                            "type": "string",
                            "description": "Chrome DevTools debug URL (default: http://localhost:9222)",
                        },
                        "target_url": {
                            "type": "string",
                            "description": "URL to open in a new tab (optional, connects to existing page if omitted)",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_navigate",
                "description": "Navigate the browser to a URL",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "url": {"type": "string", "description": "URL to navigate to"},
                    },
                    "required": ["session_id", "url"],
                },
            },
            {
                "name": "devtools_evaluate",
                "description": "Execute JavaScript in the browser context",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "expression": {
                            "type": "string",
                            "description": "JavaScript expression to evaluate",
                        },
                    },
                    "required": ["session_id", "expression"],
                },
            },
            {
                "name": "devtools_screenshot",
                "description": "Capture a screenshot of the current page",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "path": {
                            "type": "string",
                            "description": "File path to save screenshot (optional, returns base64 if omitted)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["png", "jpeg"],
                            "description": "Image format (default: png)",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_click",
                "description": "Click an element by CSS selector",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the element to click",
                        },
                    },
                    "required": ["session_id", "selector"],
                },
            },
            {
                "name": "devtools_fill",
                "description": "Fill an input field with a value",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the input element",
                        },
                        "value": {"type": "string", "description": "Value to fill in"},
                    },
                    "required": ["session_id", "selector", "value"],
                },
            },
            {
                "name": "devtools_get_console_errors",
                "description": "Get console errors from the browser",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_failed_requests",
                "description": "Get failed network requests from the browser",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "status": {
                            "type": "integer",
                            "description": "Filter by HTTP status code (optional)",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_network_logs",
                "description": "Get all network request logs from the browser",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "method": {
                            "type": "string",
                            "description": "Filter by HTTP method (GET, POST, etc.)",
                        },
                        "status": {
                            "type": "integer",
                            "description": "Filter by HTTP status code",
                        },
                        "failed_only": {
                            "type": "boolean",
                            "description": "Only return failed requests",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_performance",
                "description": "Get page performance metrics",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_page_info",
                "description": "Get current page title, URL, and content summary",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_check_broken_images",
                "description": "Find broken images on the page",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_wait_for_selector",
                "description": "Wait for a CSS selector to appear in the DOM",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector to wait for",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 5000)",
                        },
                    },
                    "required": ["session_id", "selector"],
                },
            },
            {
                "name": "devtools_get_cookies",
                "description": "Get all browser cookies",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_close",
                "description": "Close the browser session",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID to close",
                        }
                    },
                    "required": ["session_id"],
                },
            },
        ]
    }


# ==================== WORKFLOW MANAGEMENT ENDPOINTS ====================


@app.get("/api/workflow-definitions")
async def list_workflow_definitions():
    """List all loaded workflow definitions."""
    try:
        definitions = server_state.phase_manager.list_definitions()
    except Exception as e:
        logger.error(f"Failed to list workflow definitions: {e}")
        return {"definitions": []}

    result = []
    for d in definitions:
        try:
            phases = d.phases_config
            if isinstance(phases, str):
                import json as _json

                phases = _json.loads(phases)
            config = d.workflow_config
            if isinstance(config, str):
                import json as _json

                config = _json.loads(config)
            result.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "description": d.description,
                    "phases_count": len(phases) if phases else 0,
                    "has_result": (config or {}).get("has_result", False),
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "launch_template": (config or {}).get("launch_template"),
                }
            )
        except Exception as e:
            logger.error(f"Error processing definition {d.id}: {e}")
            result.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "description": d.description,
                    "error": str(e),
                }
            )

    return {"definitions": result}


@app.post("/api/workflow-definitions")
async def register_workflow_definition(request: RegisterWorkflowDefinitionRequest):
    """Register a workflow definition."""
    logger.info(f"Registering workflow definition: {request.id}")
    try:
        server_state.phase_manager.register_definition(
            definition_id=request.id,
            name=request.name,
            description=request.description,
            phases_config=request.phases_config,
            workflow_config=request.workflow_config,
        )
        logger.info(f"Successfully registered workflow definition: {request.id}")
        return {
            "id": request.id,
            "name": request.name,
            "status": "registered",
            "message": f"Workflow definition '{request.name}' registered successfully",
        }
    except Exception as e:
        logger.error(f"Failed to register workflow definition {request.id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/workflow-executions")
async def list_workflow_executions(status: str = "all"):
    """List all workflow executions."""
    executions = server_state.phase_manager.list_active_executions(status)
    return {
        "executions": [
            {
                "id": e.id,
                "definition_id": e.definition_id,
                "definition_name": e.definition.name if e.definition else None,
                "description": e.description,
                "status": e.status,
                "status_reason": e.status_reason,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "working_directory": e.working_directory,
                # Add stats
                "stats": server_state.phase_manager.get_execution_stats(e.id),
            }
            for e in executions
        ]
    }


@app.post("/api/workflow-executions")
async def start_workflow_execution(request: StartWorkflowRequest):
    """Start a new workflow execution from a definition."""
    logger.info(
        f"Starting workflow execution: definition={request.definition_id}, desc={request.description}, launch_params={request.launch_params}"
    )
    try:
        # start_execution now returns (workflow_id, initial_task_info)
        result = server_state.phase_manager.start_execution(
            definition_id=request.definition_id,
            description=request.description,
            working_directory=request.working_directory,
            launch_params=request.launch_params,
            design_id=request.design_id,
        )

        # Handle both old (just workflow_id) and new (tuple) return formats
        if isinstance(result, tuple):
            workflow_id, initial_task_info = result
        else:
            workflow_id = result
            initial_task_info = None

        logger.info(f"Successfully started workflow execution: {workflow_id}")

        # If there's an initial task to create, create it through the proper flow
        if initial_task_info:
            # Claim the right to create this phase's first task before doing
            # any of the slower work below. The orchestrator's background
            # self-heal (_case_start_first_phase / _case_in_progress_no_tasks
            # in orchestrator.py) independently creates a task for any
            # in-progress phase it finds with zero tasks -- without this
            # claim, both paths can decide to create phase 1's task and a
            # duplicate agent gets spawned (observed live: burned a full
            # agent run duplicating work the first task had already done).
            phase_uuid = initial_task_info.get("phase_uuid")
            if phase_uuid:
                from src.autopilot.orchestrator import _claim_phase_task_creation
                from src.core.database import get_db as _get_db_for_claim

                with _get_db_for_claim() as _claim_db:
                    won_claim = _claim_phase_task_creation(_claim_db, phase_uuid)
                if not won_claim:
                    logger.info(
                        f"Phase 1 task for workflow {workflow_id} is already "
                        "being created by another path -- skipping"
                    )
                    initial_task_info = None

        if initial_task_info:
            logger.info(f"Creating initial Phase 1 task for workflow {workflow_id}")
            try:
                # Create the task using internal task creation
                # This mimics what /create_task does but internally
                task_request = CreateTaskRequest(
                    task_description=initial_task_info["task_description"],
                    done_definition="Complete the initial phase task as described in the prompt",
                    ai_agent_id="main-session-agent",  # UI-launched task
                    priority=initial_task_info.get("priority", "high"),
                    phase_id=initial_task_info.get("phase_id", "1"),
                    workflow_id=workflow_id,
                )

                # Call the create_task endpoint handler directly
                # Use "main-session-agent" as the creator since this is a UI-launched task
                task_response = await create_task(
                    request=task_request, agent_id="main-session-agent"
                )
                logger.info(
                    f"Created initial task {task_response.task_id} for workflow {workflow_id}"
                )

                # create_task (the generic /create_task handler) knows
                # nothing about PhaseExecution bookkeeping -- see
                # _release_phase_task_creation_claim's own docstring for
                # what silently breaks without this call.
                try:
                    from src.autopilot.orchestrator import (
                        _release_phase_task_creation_claim,
                    )
                    from src.core.database import get_db as _get_db_for_release

                    with _get_db_for_release() as _pdb:
                        _release_phase_task_creation_claim(_pdb, phase_uuid)
                except Exception as claim_error:
                    logger.error(
                        f"Failed to release phase 1 task-creation claim for "
                        f"workflow {workflow_id}: {claim_error}"
                    )
            except Exception as task_error:
                logger.error(
                    f"Failed to create initial task for workflow {workflow_id}: {task_error}"
                )
                # Don't fail the whole workflow creation, just log the error

        return {
            "workflow_id": workflow_id,
            "status": "active",
            "message": f"Started workflow execution: {request.description}",
        }
    except ValueError as e:
        logger.error(f"ValueError starting workflow: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error starting workflow execution: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/workflow-executions/{workflow_id}")
async def get_workflow_execution(workflow_id: str):
    """Get details of a specific workflow execution."""
    workflow = server_state.phase_manager.get_workflow(workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id} not found")

    stats = server_state.phase_manager.get_execution_stats(workflow_id)

    # Get phases for this workflow execution
    phases = server_state.phase_manager.get_phases_for_workflow(workflow_id)

    # Get phase stats
    session = server_state.phase_manager.db_manager.get_session()
    try:
        phases_data = []
        for phase in phases:
            # Count tasks in this phase
            total_tasks = session.query(Task).filter_by(phase_id=phase.id).count()
            completed_tasks = (
                session.query(Task).filter_by(phase_id=phase.id, status="done").count()
            )
            active_tasks = (
                session.query(Task)
                .filter_by(phase_id=phase.id, status="in_progress")
                .count()
            )
            pending_tasks = (
                session.query(Task)
                .filter_by(phase_id=phase.id, status="pending")
                .count()
            )

            # Count active agents working on tasks in this phase
            active_agents = (
                session.query(Agent)
                .join(Task, Agent.current_task_id == Task.id)
                .filter(Task.phase_id == phase.id, Agent.status == "working")
                .count()
            )

            phases_data.append(
                {
                    "id": phase.id,
                    "order": phase.order,
                    "name": phase.name,
                    "description": phase.description,
                    "active_agents": active_agents,
                    "total_tasks": total_tasks,
                    "completed_tasks": completed_tasks,
                    "active_tasks": active_tasks,
                    "pending_tasks": pending_tasks,
                    "cli_config": {
                        "cli_tool": phase.cli_tool,
                        "cli_model": phase.cli_model,
                        "glm_api_token_env": phase.glm_api_token_env,
                    },
                }
            )
    finally:
        session.close()

    return {
        "id": workflow.id,
        "definition_id": workflow.definition_id,
        "definition_name": workflow.definition.name if workflow.definition else None,
        "description": workflow.description,
        "status": workflow.status,
        "status_reason": workflow.status_reason,
        "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
        "working_directory": workflow.working_directory,
        "stats": stats,
        "phases": phases_data,
    }


@app.post("/api/workflow-executions/{workflow_id}/complete")
async def complete_workflow_execution(workflow_id: str, request: Request):
    """Mark a workflow execution as completed (cleanup for orchestrator).
    Only allows localhost access for security."""
    # Security: only allow localhost calls for this destructive operation
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=403, detail="Only localhost can force-complete workflows"
        )

    session = server_state.db_manager.get_session()
    try:
        from src.core.database import Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(
                status_code=404, detail=f"Workflow {workflow_id} not found"
            )
        if workflow.status in ("completed", "failed", "cancelled"):
            return {"status": workflow.status, "message": "Already terminal"}
        workflow.status = "completed"
        session.commit()
        return {"status": "completed", "workflow_id": workflow_id}
    finally:
        session.close()


@app.post("/api/workflow-executions/{workflow_id}/stop")
async def stop_workflow(workflow_id: str, request: Request):
    """Stop a workflow and terminate all its agents."""
    import subprocess

    session = server_state.db_manager.get_session()
    try:
        from src.core.database import Agent, Task, Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(
                status_code=404, detail=f"Workflow {workflow_id} not found"
            )
        if workflow.status in ("completed", "failed", "paused"):
            return {"status": workflow.status, "message": "Already stopped"}

        # Find all tasks in this workflow
        tasks = session.query(Task).filter_by(workflow_id=workflow_id).all()
        task_ids = [t.id for t in tasks]

        # Find and terminate all agents working on these tasks
        terminated_count = 0
        if task_ids:
            agents = (
                session.query(Agent)
                .filter(Agent.current_task_id.in_(task_ids))
                .filter(Agent.status.in_(["working", "starting", "idle"]))
                .all()
            )
            for agent in agents:
                try:
                    subprocess.run(
                        ["tmux", "kill-session", "-t", agent.tmux_session_name],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
                agent.status = "terminated"
                agent.current_task_id = None  # Clear stale reference
                agent.terminated_at = datetime.utcnow()
                terminated_count += 1

        workflow.status = "paused"
        # Marks this as a deliberate user pause so the background sweep's
        # _try_auto_resume_paused_workflow (orchestrator.py) leaves it alone
        # instead of silently reactivating it the moment it next sees a done
        # task sitting in an in-progress phase -- a state pausing itself
        # commonly produces. Without this, a user pause could get reverted
        # within one sweep tick (~20s), repeatedly, until whatever made the
        # phase look "stalled" happened to resolve on its own.
        workflow.paused_by = "user"
        session.commit()

        return {
            "status": "paused",
            "workflow_id": workflow_id,
            "agents_terminated": terminated_count,
        }
    finally:
        session.close()


@app.post("/api/workflow-executions/{workflow_id}/resume")
async def resume_workflow(workflow_id: str, request: Request):
    """Resume a paused workflow."""
    session = server_state.db_manager.get_session()
    try:
        from src.core.database import Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(
                status_code=404, detail=f"Workflow {workflow_id} not found"
            )
        if workflow.status != "paused":
            return {"status": workflow.status, "message": "Not paused"}

        workflow.status = "active"
        workflow.paused_by = None
        workflow.status_reason = None
        session.commit()
        return {"status": "active", "workflow_id": workflow_id}
    finally:
        session.close()


@app.post("/api/autopilot/recover")
async def recover_workflows(workflow_id: Optional[str] = None):
    """Recover interrupted runs on demand (the UI 'Retry' action).

    Re-drives workflows whose in-flight phase agent died (crash / sleep / restart):
    restarts each orphaned agent on its existing worktree branch so the run continues
    from the last committed state. With workflow_id, scopes to that run and flips a
    paused/failed workflow back to 'active' first. Without it, recovers all
    interrupted active/paused workflows.
    """
    try:
        summary = await _resume_interrupted_workflows(
            workflow_id=workflow_id, reactivate=bool(workflow_id)
        )
        return {
            "recovered": True,
            "resumed_agents": summary.get("resumed", 0),
            "workflows": summary.get("workflows", []),
        }
    except Exception as e:
        logger.error(f"[RECOVER] on-demand recovery failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/workflow-executions/{workflow_id}/cancel")
async def cancel_workflow(workflow_id: str, request: Request):
    """Terminate agents and mark workflow as cancelled."""
    import subprocess

    session = server_state.db_manager.get_session()
    try:
        from src.core.database import Agent, Task, Workflow

        workflow = session.query(Workflow).filter_by(id=workflow_id).first()
        if not workflow:
            raise HTTPException(
                status_code=404, detail=f"Workflow {workflow_id} not found"
            )

        # Terminate agents
        tasks = session.query(Task).filter_by(workflow_id=workflow_id).all()
        task_ids = [t.id for t in tasks]
        terminated_count = 0
        if task_ids:
            agents = (
                session.query(Agent)
                .filter(Agent.current_task_id.in_(task_ids))
                .filter(Agent.status.in_(["working", "starting", "idle"]))
                .all()
            )
            for agent in agents:
                try:
                    subprocess.run(
                        ["tmux", "kill-session", "-t", agent.tmux_session_name],
                        capture_output=True,
                        timeout=5,
                    )
                except Exception:
                    pass
                agent.status = "terminated"
                agent.current_task_id = None  # Clear stale reference
                agent.terminated_at = datetime.utcnow()
                terminated_count += 1

        # Mark every non-terminal task failed too -- otherwise a task whose
        # agent was just terminated above is left showing its last live
        # status (e.g. still "in_progress") even though nothing is working
        # on it anymore. Mirrors what pause_feature does for its "blocked" case.
        non_terminal = {
            "pending", "queued", "blocked", "assigned", "in_progress",
            "under_review", "validation_in_progress", "needs_work",
        }
        for task in tasks:
            if task.status in non_terminal:
                task.status = "failed"
                task.failure_reason = "Workflow cancelled by user"
                task.completed_at = datetime.utcnow()

        # Mark as failed (can't delete due to FK constraints, using failed to indicate user cancellation)
        workflow.status = "failed"
        session.commit()
        return {"cancelled": workflow_id, "agents_terminated": terminated_count}
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


async def _tool_create_task(arguments: Dict[str, Any]):
    workflow_id = arguments.get("workflow_id")
    if not workflow_id:
        raise HTTPException(
            status_code=400, detail="workflow_id is required for create_task"
        )

    return await create_task(
        CreateTaskRequest(
            task_description=arguments.get("task_description"),
            done_definition=arguments.get("done_definition"),
            ai_agent_id="mcp-claude",
            workflow_id=workflow_id,
            phase_id=arguments.get("phase_id"),
            priority=arguments.get("priority", "medium"),
            ticket_id=arguments.get("ticket_id"),
            depends_on=arguments.get("depends_on"),
            parallel_group=arguments.get("parallel_group"),
            max_concurrent=arguments.get("max_concurrent", 1),
        ),
        agent_id="mcp-claude",
    )


async def _tool_save_memory(arguments: Dict[str, Any]):
    return await save_memory(
        SaveMemoryRequest(
            ai_agent_id="mcp-claude",
            memory_content=arguments.get("content"),
            memory_type=arguments.get("memory_type", "discovery"),
            tags=arguments.get("tags", []),
            related_files=arguments.get("related_files", []),
        ),
        agent_id="mcp-claude",
    )


async def _tool_search_memory(arguments: Dict[str, Any]):
    return await search_memory(
        SearchMemoryRequest(
            query=arguments.get("query", ""),
            limit=arguments.get("limit", 10),
            memory_type=arguments.get("memory_type"),
            project_id=arguments.get("project_id"),
        ),
        agent_id=arguments.get("_agent_id"),
    )


async def _tool_get_task_status(arguments: Dict[str, Any]):
    agent_id_filter = arguments.get("agent_id")
    workflow_id_filter = arguments.get("workflow_id")
    status_filter = arguments.get("status")

    session = server_state.db_manager.get_session()
    try:
        query = session.query(Task)
        if status_filter and status_filter != "all":
            query = query.filter(Task.status == status_filter)
        else:
            query = query.filter(
                Task.status.in_(["pending", "assigned", "in_progress", "done", "failed"])
            )
        if workflow_id_filter:
            query = query.filter(Task.workflow_id == workflow_id_filter)
        if agent_id_filter:
            query = query.filter(Task.assigned_agent_id == agent_id_filter)
        tasks = query.order_by(Task.created_at.desc()).limit(50).all()

        results = []
        for t in tasks:
            phase_name = None
            if t.phase_id:
                phase = session.query(Phase).filter_by(id=t.phase_id).first()
                phase_name = phase.name if phase else None
            results.append({
                "id": t.id,
                "status": t.status,
                "description": (t.enriched_description or t.raw_description or "")[:200],
                "phase_name": phase_name,
                "workflow_id": t.workflow_id,
                "assigned_agent_id": t.assigned_agent_id,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            })
        return {"tasks": results, "count": len(results)}
    finally:
        session.close()


async def _tool_create_ticket(arguments: Dict[str, Any]):
    from src.services.ticket_service import TicketService

    workflow_id = arguments.get("workflow_id")
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id is required")

    result = await TicketService.create_ticket(
        workflow_id=workflow_id,
        agent_id=arguments.get("agent_id", "mcp-claude"),
        title=arguments.get("title"),
        description=arguments.get("description"),
        ticket_type=arguments.get("ticket_type"),
        priority=arguments.get("priority"),
        tags=arguments.get("tags", []),
        blocked_by_ticket_ids=arguments.get("blocked_by_ticket_ids", []),
    )
    return {"success": True, "ticket": result}


async def _tool_search_tickets(arguments: Dict[str, Any]):

    session = server_state.db_manager.get_session()
    try:
        search_service = TicketSearchService(session)
        results = await search_service.search_tickets(
            query=arguments.get("query"),
            tags=arguments.get("tags"),
            status=arguments.get("status"),
        )
        return {"tickets": results}
    finally:
        session.close()


async def _tool_update_ticket_status(arguments: Dict[str, Any]):
    from src.services.ticket_service import TicketService

    result = await TicketService.change_ticket_status(
        ticket_id=arguments.get("ticket_id"),
        new_status=arguments.get("new_status"),
        agent_id=arguments.get("agent_id", "mcp-claude"),
    )
    return {"success": True, "result": result}


async def _tool_broadcast_message(arguments: Dict[str, Any]):
    message = arguments.get("message", "")
    sender_id = arguments.get("sender_id", "unknown")
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    try:
        await server_state.agent_manager.broadcast_message_to_all_agents(
            message=message,
            sender_agent_id=sender_id,
        )
    except Exception as e:
        logger.warning(f"broadcast_message failed: {e}")
    return {"success": True, "message": "Broadcast sent"}


async def _tool_send_message(arguments: Dict[str, Any]):
    target_agent_id = arguments.get("agent_id")
    message = arguments.get("message", "")
    sender_id = arguments.get("sender_id", "unknown")
    if not target_agent_id or not message:
        raise HTTPException(status_code=400, detail="agent_id and message are required")
    try:
        # FIX #4: Use send_direct_message which accepts sender_id,
        # not send_message_to_agent which doesn't have that parameter.
        await server_state.agent_manager.send_direct_message(
            sender_agent_id=sender_id,
            recipient_agent_id=target_agent_id,
            message=message,
        )
    except Exception as e:
        logger.warning(f"send_message to {target_agent_id[:8]} failed: {e}")
    return {"success": True, "message": f"Message sent to {target_agent_id[:8]}"}


async def _tool_update_task_status(arguments: Dict[str, Any]):
    """Update task status - bridges MCP tool call to HTTP endpoint."""
    task_id = arguments.get("task_id")
    status = arguments.get("status")
    summary = arguments.get("summary", "")
    failure_reason = arguments.get("failure_reason")
    key_learnings = arguments.get("key_learnings", [])
    metadata = arguments.get("metadata")
    agent_id = arguments.get("agent_id")

    if not task_id or not status:
        raise HTTPException(status_code=400, detail="task_id and status are required")

    # Resolve agent_id: use provided, or look up from task
    if not agent_id:
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if task:
                agent_id = task.assigned_agent_id
        finally:
            session.close()

    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required (could not auto-detect from task)")

    # Call the HTTP endpoint handler directly
    request = UpdateTaskStatusRequest(
        task_id=task_id,
        status=status,
        summary=summary or "Task completed",
        key_learnings=key_learnings or [],
        failure_reason=failure_reason,
        metadata=metadata,
    )
    return await update_task_status(request, agent_id=agent_id)


# Registry for non-devtools MCP tools: name -> async handler(arguments).
# Replaces a 9-branch if/elif chain (SOLID review 1.5) — a new tool is added
# by defining one handler and registering it here, instead of editing this
# dispatch chain (devtools_* tools have their own registry in
# _handle_devtools_tool/_DEVTOOLS_TOOLS since they share a different shape:
# a browser-session precondition and per-tool required-args).
_MCP_TOOLS: Dict[str, Any] = {
    "create_task": _tool_create_task,
    "save_memory": _tool_save_memory,
    "search_memory": _tool_search_memory,
    "get_task_status": _tool_get_task_status,
    "update_task_status": _tool_update_task_status,
    "create_ticket": _tool_create_ticket,
    "search_tickets": _tool_search_tickets,
    "update_ticket_status": _tool_update_ticket_status,
    "broadcast_message": _tool_broadcast_message,
    "send_message": _tool_send_message,
}


@app.post("/tools/execute")
async def execute_tool(request: Dict[str, Any]):
    """Execute an MCP tool."""
    tool_name = request.get("tool")
    arguments = request.get("arguments", {})

    if tool_name in _MCP_TOOLS:
        return await _MCP_TOOLS[tool_name](arguments)
    elif tool_name and tool_name.startswith("devtools_"):
        return await _handle_devtools_tool(tool_name, arguments)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown tool: {tool_name}")


# Registry for devtools_* MCP tools: name -> (required_args, handler).
# Replaces a 13-branch if/elif chain (SOLID review 1.5) — adding a devtools
# tool means adding one entry here instead of editing a dispatch chain.
# Handlers receive (browser, arguments) except "devtools_connect", which is
# dispatched separately below since it runs before a browser session exists.
async def _devtools_connect(arguments: Dict[str, Any], session_id: str):
    from src.mcp.devtools import devtools_manager

    debug_url = arguments.get("debug_url", "http://localhost:9222")
    target_url = arguments.get("target_url")
    if target_url:
        browser = await devtools_manager.connect_new_tab(
            session_id, target_url, debug_url
        )
    else:
        browser = await devtools_manager.connect(session_id, debug_url)
    version = await browser.get_version()
    return {
        "success": True,
        "session_id": session_id,
        "browser": version.get("Browser", "unknown"),
    }


async def _devtools_navigate(browser, arguments):
    result = await browser.navigate(arguments["url"])
    return {"success": True, "result": result}


async def _devtools_evaluate(browser, arguments):
    result = await browser.evaluate(arguments["expression"])
    return {"success": True, "result": result}


async def _devtools_screenshot(browser, arguments):
    path = arguments.get("path")
    fmt = arguments.get("format", "png")
    data = await browser.screenshot(path=path, format=fmt)
    return {"success": True, "data_length": len(data) if data else 0, "saved_to": path}


async def _devtools_click(browser, arguments):
    await browser.click(arguments["selector"])
    return {"success": True}


async def _devtools_fill(browser, arguments):
    await browser.fill(arguments["selector"], arguments["value"])
    return {"success": True}


async def _devtools_get_console_errors(browser, arguments):
    errors = await browser.check_console_errors()
    return {"success": True, "errors": errors, "count": len(errors)}


async def _devtools_get_failed_requests(browser, arguments):
    logs = await browser.get_network_logs(
        failed_only=True, status=arguments.get("status")
    )
    return {"success": True, "failed_requests": logs, "count": len(logs)}


async def _devtools_get_network_logs(browser, arguments):
    logs = await browser.get_network_logs(
        method=arguments.get("method"),
        status=arguments.get("status"),
        failed_only=arguments.get("failed_only", False),
    )
    return {"success": True, "logs": logs, "count": len(logs)}


async def _devtools_get_performance(browser, arguments):
    metrics = await browser.get_performance_metrics()
    return {"success": True, "metrics": metrics}


async def _devtools_get_page_info(browser, arguments):
    title = await browser.get_page_title()
    url = await browser.get_page_url()
    return {"success": True, "title": title, "url": url}


async def _devtools_check_broken_images(browser, arguments):
    broken = await browser.check_broken_images()
    return {"success": True, "broken_images": broken, "count": len(broken)}


async def _devtools_wait_for_selector(browser, arguments):
    found = await browser.wait_for_selector(
        arguments["selector"], timeout_ms=arguments.get("timeout_ms", 5000)
    )
    return {"success": True, "found": found}


async def _devtools_get_cookies(browser, arguments):
    cookies = await browser.get_cookies()
    return {"success": True, "cookies": cookies, "count": len(cookies)}


async def _devtools_close(browser, arguments, session_id: str):
    from src.mcp.devtools import devtools_manager

    await devtools_manager.close(session_id)
    return {"success": True, "message": f"Session '{session_id}' closed"}


# name -> (required_args, handler). Handlers whose signature includes
# session_id are called with it explicitly below.
_DEVTOOLS_TOOLS: Dict[str, tuple] = {
    "devtools_connect": ([], _devtools_connect),
    "devtools_navigate": (["url"], _devtools_navigate),
    "devtools_evaluate": (["expression"], _devtools_evaluate),
    "devtools_screenshot": ([], _devtools_screenshot),
    "devtools_click": (["selector"], _devtools_click),
    "devtools_fill": (["selector", "value"], _devtools_fill),
    "devtools_get_console_errors": ([], _devtools_get_console_errors),
    "devtools_get_failed_requests": ([], _devtools_get_failed_requests),
    "devtools_get_network_logs": ([], _devtools_get_network_logs),
    "devtools_get_performance": ([], _devtools_get_performance),
    "devtools_get_page_info": ([], _devtools_get_page_info),
    "devtools_check_broken_images": ([], _devtools_check_broken_images),
    "devtools_wait_for_selector": (["selector"], _devtools_wait_for_selector),
    "devtools_get_cookies": ([], _devtools_get_cookies),
    "devtools_close": ([], _devtools_close),
}


async def _handle_devtools_tool(tool_name: str, arguments: Dict[str, Any]):
    from src.mcp.devtools import devtools_manager, validate_session_id

    entry = _DEVTOOLS_TOOLS.get(tool_name)
    if entry is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown devtools tool: {tool_name}"
        )
    required, handler = entry

    missing = [k for k in required if k not in arguments]
    if missing:
        raise HTTPException(
            status_code=400, detail=f"Missing required arguments: {', '.join(missing)}"
        )

    raw_session = arguments.get("session_id", "default")
    try:
        session_id = validate_session_id(raw_session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        if tool_name == "devtools_connect":
            return await handler(arguments, session_id)

        browser = devtools_manager.get(session_id)
        if not browser:
            raise HTTPException(
                status_code=404,
                detail=f"No browser session '{session_id}'. Call devtools_connect first.",
            )

        if tool_name == "devtools_close":
            return await handler(browser, arguments, session_id)

        return await handler(browser, arguments)

    except HTTPException:
        raise
    except (KeyError, TypeError) as e:
        raise HTTPException(
            status_code=400, detail=f"Invalid arguments for {tool_name}: {e}"
        )
    except Exception as e:
        logger.error(f"DevTools tool error: {tool_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"DevTools error: {str(e)}")


@app.get("/resources")
async def list_resources():
    """List available MCP resources."""
    session = server_state.db_manager.get_session()
    try:
        tasks = session.query(Task).filter(Task.status != "done").all()
        return {
            "resources": [
                {
                    "uri": f"task://{task.id}",
                    "name": f"Task: {task.id[:8]}",
                    "description": (task.enriched_description or task.raw_description)[
                        :100
                    ],
                    "mimeType": "application/json",
                }
                for task in tasks
            ]
        }
    finally:
        session.close()


@app.get("/resources/{resource_uri:path}")
async def get_resource(resource_uri: str):
    """Get a specific MCP resource."""
    if resource_uri.startswith("task://"):
        task_id = resource_uri.replace("task://", "")
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if task:
                return {
                    "uri": resource_uri,
                    "content": {
                        "id": task.id,
                        "description": task.enriched_description
                        or task.raw_description,
                        "status": task.status,
                        "assigned_agent": task.assigned_agent_id,
                        "created_at": task.created_at.isoformat()
                        if task.created_at
                        else None,
                    },
                }
            else:
                raise HTTPException(status_code=404, detail="Task not found")
        finally:
            session.close()
    else:
        raise HTTPException(status_code=404, detail="Resource not found")


@app.get("/sse")
async def sse_endpoint():
    """Server-Sent Events endpoint for Claude MCP integration."""

    async def event_generator():
        """Generate SSE events."""
        # Send initial connection event
        yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to Hephaestus MCP Server', 'timestamp': datetime.utcnow().isoformat()})}\n\n"

        # Create a queue for this SSE connection
        event_queue = asyncio.Queue(maxsize=100)
        server_state.sse_queues.append(event_queue)

        try:
            while True:
                # Wait for events to send
                try:
                    # Check for events with timeout to send keepalive
                    event = await asyncio.wait_for(event_queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive event every 30 seconds
                    yield f"data: {json.dumps({'type': 'keepalive', 'timestamp': datetime.utcnow().isoformat()})}\n\n"
        except asyncio.CancelledError:
            # Clean up when connection is closed
            if event_queue in server_state.sse_queues:
                server_state.sse_queues.remove(event_queue)
            raise
        finally:
            # Ensure cleanup
            if event_queue in server_state.sse_queues:
                server_state.sse_queues.remove(event_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
