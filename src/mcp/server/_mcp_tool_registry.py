"""MCP tool registry -- the 14 core _tool_* handlers, MCPToolSpec, and
MCP_TOOL_REGISTRY. Split out of mcp_protocol.py to keep it under the
~800-line budget (design_docs/phase_1c_server_decomposition.md).

Must stay together: MCP_TOOL_REGISTRY references the _tool_* functions as
literal Python objects (handler=_tool_give_validation_review, etc.), not by
string name, so splitting the registry table alone from its handlers would
create a circular import. mcp_protocol.py imports MCP_TOOL_REGISTRY,
_MCP_TOOLS, and MCP_TOOL_NAMES back from here.
"""

import logging
from typing import Any, Dict, List, NamedTuple

from fastapi import HTTPException

from src.core.database import (
    Agent,
    Task,
)
from src.core.phase_lookup import resolve_task_phase
from src.mcp.memory_api import (
    GiveValidationReviewRequest,
    SaveMemoryRequest,
    SearchMemoryRequest,
    SubmitResultRequest,
    SubmitResultValidationRequest,
    give_validation_review,
    save_memory,
    search_memory,
    submit_result,
    submit_result_validation,
)
from src.mcp.server._shared import CreateTaskRequest, UpdateTaskStatusRequest, server_state
from src.mcp.server.agent_task_routes import create_task, update_task_status
from src.mcp.server.devtools_tools import _DEVTOOLS_TOOLS
from src.services.ticket_search_service import TicketSearchService
from src.services.ticket_service import TicketService

logger = logging.getLogger("src.mcp.server._mcp_tool_registry")

async def _tool_create_task(arguments: Dict[str, Any]):
    workflow_id = arguments.get("workflow_id")
    if not workflow_id:
        raise HTTPException(status_code=400, detail="workflow_id is required for create_task")

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
            query = query.filter(Task.status.in_(["pending", "assigned", "in_progress", "done", "failed"]))
        if workflow_id_filter:
            query = query.filter(Task.workflow_id == workflow_id_filter)
        if agent_id_filter:
            query = query.filter(Task.assigned_agent_id == agent_id_filter)
        tasks = query.order_by(Task.created_at.desc()).limit(50).all()

        results = []
        for t in tasks:
            phase_name = None
            if t.phase_id:
                # SOLID review 1.10: a raw Phase.filter_by(id=...) doesn't
                # handle phase_id given as a numeric order vs. a real UUID,
                # or scope to the task's own workflow, unlike
                # resolve_task_phase (used everywhere else this lookup
                # happens) -- could silently resolve the wrong phase.
                phase = resolve_task_phase(session, t)
                phase_name = phase.name if phase else None
            results.append(
                {
                    "id": t.id,
                    "status": t.status,
                    "description": (t.enriched_description or t.raw_description or "")[:200],
                    "phase_name": phase_name,
                    "workflow_id": t.workflow_id,
                    "assigned_agent_id": t.assigned_agent_id,
                    # SOLID review 1.10: missing the "Z" UTC suffix every
                    # other timestamp in this codebase's API responses uses.
                    "created_at": t.created_at.isoformat() + "Z" if t.created_at else None,
                    "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
                }
            )
        return {"tasks": results, "count": len(results)}
    finally:
        session.close()

async def _tool_create_ticket(arguments: Dict[str, Any]):

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

async def _tool_propose_prompt_change(arguments: Dict[str, Any]):
    """File a prompt-rewrite proposal for human review.

    Deliberately does NOT edit anything. The proposal lands in the autopilot
    Improvements tab, where a person approves or rejects it; only then is the
    YAML written and committed. Guards (which fields are reachable, and the
    self-edit block) live in prompt_proposal_service and are applied here via
    create_proposal, the same entry point the HTTP route uses -- a tool path
    that reimplemented them would be a second, unguarded door into the same
    edit engine.
    """
    from src.services.prompt_proposal_service import create_proposal

    try:
        result = create_proposal(
            phase_name=arguments.get("phase_name"),
            field=arguments.get("field"),
            proposed_value=arguments.get("proposed_value"),
            rationale=arguments.get("rationale"),
            evidence=arguments.get("evidence"),
            quoted_current_value=arguments.get("current_value"),
            workflow_definition=arguments.get("workflow_definition", "autopilot"),
            workflow_id=arguments.get("workflow_id"),
            proposing_phase=arguments.get("proposing_phase"),
            created_by_agent_id=arguments.get("agent_id"),
        )
    except ValueError as e:
        # Returned rather than raised: the proposing agent should record that
        # this particular proposal was refused and carry on with the rest of
        # its report, not treat it as a tool failure worth retrying.
        return {"success": False, "rejected": True, "reason": str(e)}
    return {
        "success": True,
        "proposal": result,
        "note": "Filed for human review. Nothing has been changed yet.",
    }


async def _tool_search_tickets(arguments: Dict[str, Any]):
    # TicketSearchService has no __init__ and only static methods, so the
    # previous `TicketSearchService(session)` raised TypeError before it ever
    # reached the (also non-existent) search_tickets method. Mirrors
    # search_tickets_endpoint's default hybrid mode.
    workflow_id = arguments.get("workflow_id")
    if not workflow_id:
        raise HTTPException(
            status_code=400, detail="workflow_id is required for search_tickets"
        )

    filters: Dict[str, Any] = {}
    if arguments.get("status"):
        filters["status"] = arguments["status"]

    # Tags are not a supported filter key (only status/priority/ticket_type
    # are), but _ticket_text indexes them into the searchable document, so
    # folding them into the query is what actually makes them match.
    query = arguments.get("query") or ""
    tags = arguments.get("tags") or []
    if tags:
        query = f"{query} {' '.join(tags)}".strip()

    results = await TicketSearchService.hybrid_search(
        query=query,
        workflow_id=workflow_id,
        limit=arguments.get("limit", 10),
        filters=filters or None,
    )
    return {"tickets": results}

async def _tool_update_ticket_status(arguments: Dict[str, Any]):
    # The method is change_status, not change_ticket_status, and `comment` is
    # required -- change_ticket_status has never existed, so this tool raised
    # AttributeError for every agent that called it.
    comment = arguments.get("comment")
    if not comment:
        raise HTTPException(
            status_code=400,
            detail="comment is required for update_ticket_status",
        )

    result = await TicketService.change_status(
        ticket_id=arguments.get("ticket_id"),
        agent_id=arguments.get("agent_id", "mcp-claude"),
        new_status=arguments.get("new_status"),
        comment=comment,
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
        # Unlike a best-effort broadcast, this is a targeted delivery the
        # calling agent may depend on for coordination -- unconditionally
        # reporting success regardless of whether send_direct_message
        # actually raised gave the caller no way to detect a failed
        # delivery. Raises the same way this function's own agent_id/
        # message validation above does, letting FastAPI convert it to a
        # proper error response instead of a fictitious 200.
        logger.warning(f"send_message to {target_agent_id[:8]} failed: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to send message to {target_agent_id[:8]}: {e}"
        ) from e
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

async def _tool_complete_my_task(arguments: Dict[str, Any]):
    """Mark the calling agent's own current task done/failed -- no task_id
    needed. Resolves it from Agent.current_task_id (the server already
    knows this; it's set when the task's agent was created) and delegates
    to the same update_task_status handler update_task_status's own tool
    bridge uses, so both paths share identical validation/commit/gate
    logic.

    Exists because agents kept passing a mangled task_id to
    update_task_status -- observed live, repeatedly, an agent using the
    8-char short form this codebase's own logs display everywhere
    (task.id[:8]) instead of the full UUID, hard-failing every retry on a
    task that had actually already finished its real work.
    """
    status = arguments.get("status")
    summary = arguments.get("summary", "")
    failure_reason = arguments.get("failure_reason")
    key_learnings = arguments.get("key_learnings", [])
    metadata = arguments.get("metadata")
    agent_id = arguments.get("agent_id")

    if not agent_id or not status:
        raise HTTPException(status_code=400, detail="agent_id and status are required")

    session = server_state.db_manager.get_session()
    try:
        agent = session.query(Agent).filter_by(id=agent_id).first()
        task_id = agent.current_task_id if agent else None
    finally:
        session.close()

    if not task_id:
        raise HTTPException(
            status_code=400,
            detail=f"Agent {agent_id} has no current task on record -- "
            "nothing to mark complete.",
        )

    request = UpdateTaskStatusRequest(
        task_id=task_id,
        status=status,
        summary=summary or "Task completed",
        key_learnings=key_learnings or [],
        failure_reason=failure_reason,
        metadata=metadata,
    )
    return await update_task_status(request, agent_id=agent_id)

async def _tool_submit_result(arguments: Dict[str, Any]):
    """Bridges the MCP tool call to POST /submit_result -- registered so
    heph_submit_result(agent_id=..., ...) (system_prompts.yaml's own
    documented call shape) actually resolves instead of 400ing with
    "Unknown tool: submit_result" (Phase 2 §4.10). agent_id is a real
    argument here, not a header, since MCP tool calls have no HTTP
    header to extract it from -- same reasoning as _tool_complete_my_task
    reading agent_id out of `arguments` before calling update_task_status.
    """
    agent_id = arguments.get("agent_id")
    markdown_file_path = arguments.get("markdown_file_path")
    explanation = arguments.get("explanation")
    if not agent_id or not markdown_file_path or not explanation:
        raise HTTPException(
            status_code=400,
            detail="agent_id, markdown_file_path, and explanation are required",
        )
    return await submit_result(
        SubmitResultRequest(
            markdown_file_path=markdown_file_path,
            explanation=explanation,
            evidence=arguments.get("evidence"),
            extra_files=arguments.get("extra_files"),
        ),
        agent_id=agent_id,
    )

async def _tool_submit_result_validation(arguments: Dict[str, Any]):
    """Bridges the MCP tool call to POST /submit_result_validation --
    registered so heph_submit_result_validation (validator_agent.py's own
    documented call shape) actually resolves instead of 400ing with
    "Unknown tool: submit_result_validation" (Phase 2 §4.10). No agent_id
    argument needed: the route derives the validator agent internally
    from the WorkflowResult's own linked validator record.
    """
    result_id = arguments.get("result_id")
    feedback = arguments.get("feedback")
    validation_passed = arguments.get("validation_passed")
    if not result_id or feedback is None or validation_passed is None:
        raise HTTPException(
            status_code=400,
            detail="result_id, validation_passed, and feedback are required",
        )
    return await submit_result_validation(
        SubmitResultValidationRequest(
            result_id=result_id,
            validation_passed=validation_passed,
            feedback=feedback,
            evidence=arguments.get("evidence", []),
        )
    )

async def _tool_give_validation_review(arguments: Dict[str, Any]):
    """Bridges the MCP tool call to POST /give_validation_review --
    registered so heph_give_validation_review (validator_agent.py's own
    documented call shape, for a TASK validator, distinct from
    heph_submit_result_validation's RESULT validator) actually resolves
    instead of 400ing with "Unknown tool: give_validation_review" (Phase 2
    §4.10 -- found alongside the other two while auditing every heph_
    reference across the codebase, not just config/prompts/**/*.yaml).
    agent_id is a real argument here, same reasoning as the other two
    submit_* wrappers above.
    """
    agent_id = arguments.get("agent_id")
    task_id = arguments.get("task_id")
    feedback = arguments.get("feedback")
    validation_passed = arguments.get("validation_passed")
    if not agent_id or not task_id or feedback is None or validation_passed is None:
        raise HTTPException(
            status_code=400,
            detail="agent_id, task_id, validation_passed, and feedback are required",
        )
    return await give_validation_review(
        GiveValidationReviewRequest(
            task_id=task_id,
            validator_agent_id=arguments.get("validator_agent_id", agent_id),
            validation_passed=validation_passed,
            feedback=feedback,
            evidence=arguments.get("evidence", []),
            recommendations=arguments.get("recommendations", []),
        ),
        agent_id=agent_id,
    )

class MCPToolSpec(NamedTuple):
    """One non-devtools MCP tool's complete declaration: name, the schema
    /tools advertises to agents, and the handler /tools/execute dispatches
    to. Single source of truth for both (Phase 2 §4.10) -- before this,
    the name/description/input_schema lived in list_tools()'s hand-written
    JSON while the name/handler pairing lived separately in _MCP_TOOLS,
    with nothing enforcing the two ever agreed. Six historical commits
    (bede479, ef438e8, 8e4105d, d50ebd8, e44689c, 137f12b) each patched
    exactly one of these surfaces (plus the third, agent-facing-prompt
    surface -- see MCP_TOOL_NAMES below) after it was caught drifting from
    the others.
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Any

MCP_TOOL_REGISTRY: List[MCPToolSpec] = [
    MCPToolSpec(
        name="create_task",
        description="Create a new task for an autonomous agent",
        input_schema={
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
        handler=_tool_create_task,
    ),
    MCPToolSpec(
        name="save_memory",
        description="Save a memory to the knowledge base",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "memory_type": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["content", "memory_type"],
        },
        handler=_tool_save_memory,
    ),
    MCPToolSpec(
        name="search_memory",
        description="Search the knowledge base for relevant memories using semantic search",
        input_schema={
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
        handler=_tool_search_memory,
    ),
    MCPToolSpec(
        name="get_task_status",
        description="Get status of tasks, optionally filtered by agent_id or workflow_id",
        input_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by task status (pending, assigned, in_progress, done, failed)", "default": "all"},
                "agent_id": {"type": "string", "description": "Filter tasks assigned to this agent"},
                "workflow_id": {"type": "string", "description": "Filter tasks belonging to this workflow"},
            },
        },
        handler=_tool_get_task_status,
    ),
    MCPToolSpec(
        name="update_task_status",
        description="Update the status of a task (done, failed, etc.)",
        input_schema={
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
        handler=_tool_update_task_status,
    ),
    MCPToolSpec(
        name="complete_my_task",
        description=(
            "Mark YOUR OWN currently-assigned task done or failed -- "
            "no task_id needed, the server already knows which task "
            "you're working on. Use this instead of "
            "heph_update_task_status for the normal case of finishing "
            "your own work; heph_update_task_status still exists for the "
            "rare case of updating a task that isn't your current one."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Your agent ID",
                },
                "status": {
                    "type": "string",
                    "enum": ["done", "failed", "in_progress", "blocked"],
                    "description": "New status for your current task",
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
            "required": ["agent_id", "status"],
        },
        handler=_tool_complete_my_task,
    ),
    MCPToolSpec(
        name="create_ticket",
        description="Create a new ticket in the Kanban board",
        input_schema={
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
        handler=_tool_create_ticket,
    ),
    MCPToolSpec(
        name="propose_prompt_change",
        description=(
            "Propose a rewrite of one phase-prompt field for human review. Does NOT "
            "change anything: the proposal appears in the autopilot Improvements tab "
            "where a person approves or rejects it. Only prose fields are editable "
            "(description, done_definitions, additional_notes) -- the orchestration "
            "wiring (spec_gate, outputs, thresholds) is deliberately out of reach."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "phase_name": {
                    "type": "string",
                    "description": "Phase whose prompt should change, e.g. 'development'",
                },
                "field": {
                    "type": "string",
                    "enum": ["description", "done_definitions", "additional_notes"],
                    "description": "Which field to rewrite",
                },
                "proposed_value": {
                    "description": (
                        "The COMPLETE new value for that field, not a diff or a "
                        "description of the change. A string, except for "
                        "done_definitions, which is a list of strings -- pass it "
                        "either as a real JSON array or as a JSON/YAML-encoded "
                        "string (e.g. '[\"item one\", \"item two\"]'); both are "
                        "parsed the same way."
                    ),
                },
                "current_value": {
                    "description": (
                        "The value you are proposing to replace, as you read it. Used "
                        "to flag the proposal as stale if the file changed before a "
                        "human reviewed it."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "Why this change, citing what actually went wrong in the run. "
                        "Required -- a change with no recorded reason cannot be "
                        "reviewed, only guessed at."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": "Optional: quoted log lines or artifact text supporting it",
                },
                "proposing_phase": {
                    "type": "string",
                    "description": "Your own phase name (a phase may not rewrite its own prompt)",
                },
                "workflow_id": {"type": "string", "description": "Your workflow ID"},
                "agent_id": {"type": "string", "description": "Your agent ID"},
            },
            "required": ["phase_name", "field", "proposed_value", "rationale"],
        },
        handler=_tool_propose_prompt_change,
    ),
    MCPToolSpec(
        name="search_tickets",
        description="Search for existing tickets by title or tags",
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for title",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags to bias the search toward (folded into the query text)",
                },
                "status": {"type": "string", "description": "Filter by status"},
                "workflow_id": {
                    "type": "string",
                    "description": "Workflow whose tickets to search",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 10)",
                },
            },
            "required": ["workflow_id"],
        },
        handler=_tool_search_tickets,
    ),
    MCPToolSpec(
        name="update_ticket_status",
        description="Update the status of a ticket",
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string", "description": "Ticket ID"},
                "new_status": {
                    "type": "string",
                    "description": "New status value",
                },
                "comment": {
                    "type": "string",
                    "description": "Why the status is changing (recorded on the ticket)",
                },
            },
            "required": ["ticket_id", "new_status", "comment"],
        },
        handler=_tool_update_ticket_status,
    ),
    MCPToolSpec(
        name="broadcast_message",
        description="Send a message to ALL active agents",
        input_schema={
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
        handler=_tool_broadcast_message,
    ),
    MCPToolSpec(
        name="send_message",
        description="Send a direct message to a specific agent",
        input_schema={
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
        handler=_tool_send_message,
    ),
    MCPToolSpec(
        name="submit_result",
        description=(
            "Submit your finished workflow result for validation. Bridges "
            "to the same /submit_result endpoint the SDK's direct-HTTP path "
            "uses; agent_id is passed explicitly here since MCP tool calls "
            "carry no HTTP header."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Your agent ID",
                },
                "markdown_file_path": {
                    "type": "string",
                    "description": "Path to markdown file with result evidence",
                },
                "explanation": {
                    "type": "string",
                    "description": "Brief explanation of what was accomplished",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of evidence supporting completion",
                },
                "extra_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of additional file paths (e.g., patches, reproduction scripts) for validators",
                },
            },
            "required": ["agent_id", "markdown_file_path", "explanation"],
        },
        handler=_tool_submit_result,
    ),
    MCPToolSpec(
        name="submit_result_validation",
        description="Submit a validator agent's pass/fail review of a submitted workflow result",
        input_schema={
            "type": "object",
            "properties": {
                "result_id": {
                    "type": "string",
                    "description": "ID of result being validated",
                },
                "validation_passed": {
                    "type": "boolean",
                    "description": "Whether validation passed",
                },
                "feedback": {
                    "type": "string",
                    "description": "Detailed validation feedback",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Evidence supporting decision",
                },
            },
            "required": ["result_id", "validation_passed", "feedback"],
        },
        handler=_tool_submit_result_validation,
    ),
    MCPToolSpec(
        name="give_validation_review",
        description=(
            "Submit a TASK validator's pass/fail review (distinct from "
            "submit_result_validation, which is for a workflow RESULT "
            "validator). Only callable by an agent whose agent_type is "
            "'validator'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "agent_id": {
                    "type": "string",
                    "description": "Your (the validator's) agent ID",
                },
                "task_id": {
                    "type": "string",
                    "description": "ID of task being validated",
                },
                "validator_agent_id": {
                    "type": "string",
                    "description": "ID of validator agent (defaults to agent_id if omitted)",
                },
                "validation_passed": {
                    "type": "boolean",
                    "description": "Whether validation passed",
                },
                "feedback": {
                    "type": "string",
                    "description": "Detailed feedback",
                },
                "evidence": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Evidence supporting decision",
                },
                "recommendations": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Follow-up task recommendations",
                },
            },
            "required": ["agent_id", "task_id", "validation_passed", "feedback"],
        },
        handler=_tool_give_validation_review,
    ),
]

_MCP_TOOLS: Dict[str, Any] = {t.name: t.handler for t in MCP_TOOL_REGISTRY}

MCP_TOOL_NAMES: frozenset = frozenset(
    {t.name for t in MCP_TOOL_REGISTRY} | set(_DEVTOOLS_TOOLS.keys())
)
