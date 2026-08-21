"""Agent management API routes.

Extracted from server.py for better modularity.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Body, Header, HTTPException, Query, Request

from src.core.agent_identity import is_root_agent
from src.core.app_context import get_app_state
from src.core.database import Agent, Task
from src.core.phase_lookup import resolve_task_phase

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


def _get_server_state():
    """Get server state (lazy import to avoid circular deps)."""
    return get_app_state()


def _serialize_agent(session, a) -> dict:
    """Build the API representation of a single agent, including its current
    (or, if terminated, most recent) task and workflow."""
    from src.core.database import Workflow

    agent_data = {
        "id": a.id,
        "status": a.status,
        "agent_type": getattr(a, "agent_type", "phase"),
        "current_task_id": a.current_task_id,
        "health_check_failures": a.health_check_failures,
        "last_activity": a.last_activity.isoformat() + "Z"
        if a.last_activity
        else None,
        "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
        "terminated_at": getattr(a, 'terminated_at', None).isoformat() + "Z" if getattr(a, 'terminated_at', None) else None,
        "tmux_session_name": a.tmux_session_name,
        "cli_type": getattr(a, "cli_type", None),
        "cli_model": getattr(a, "cli_model", None),
        "current_task": None,
        "workflow": None,
    }
    if a.current_task_id:
        task = session.query(Task).filter_by(id=a.current_task_id).first()
        if task:
            task_data = {
                "id": task.id,
                "description": (
                    task.enriched_description or task.raw_description or ""
                )[:200],
                "status": task.status,
                "priority": task.priority,
                "started_at": task.started_at.isoformat() + "Z" if task.started_at else None,
                "completed_at": task.completed_at.isoformat() + "Z" if task.completed_at else None,
                "runtime_seconds": int(
                    (datetime.utcnow() - task.started_at).total_seconds()
                )
                if task.started_at
                else 0,
                "phase_info": None,
            }
            if task.phase_id:
                phase = resolve_task_phase(session, task)
                if phase:
                    task_data["phase_info"] = {
                        "id": phase.id,
                        "name": phase.name,
                        "order": phase.order,
                    }
            elif task.workflow_id:
                logger.warning(
                    f"Task {task.id} has workflow_id={task.workflow_id} but no phase_id — agent failed to provide it"
                )
            agent_data["current_task"] = task_data

            if task.workflow_id:
                wf = (
                    session.query(Workflow)
                    .filter_by(id=task.workflow_id)
                    .first()
                )
                if wf:
                    agent_data["workflow"] = {
                        "id": wf.id,
                        "name": wf.name,
                        "status": wf.status,
                        "description": (wf.description or "")[:100],
                    }

    # Fallback for terminated agents: look up their last task
    if not agent_data["current_task"] and a.status == "terminated":
        last_task = (
            session.query(Task)
            .filter_by(assigned_agent_id=a.id)
            .order_by(Task.completed_at.desc().nullslast(), Task.created_at.desc())
            .first()
        )
        if last_task:
            task_data = {
                "id": last_task.id,
                "description": (
                    last_task.enriched_description or last_task.raw_description or ""
                )[:200],
                "status": last_task.status,
                "priority": last_task.priority,
                "started_at": last_task.started_at.isoformat() + "Z" if last_task.started_at else None,
                "completed_at": last_task.completed_at.isoformat() + "Z" if last_task.completed_at else None,
                "runtime_seconds": int(
                    (last_task.completed_at - last_task.started_at).total_seconds()
                )
                if last_task.started_at and last_task.completed_at
                else 0,
                "phase_info": None,
            }
            if last_task.phase_id:
                phase = resolve_task_phase(session, last_task)
                if phase:
                    task_data["phase_info"] = {
                        "id": phase.id,
                        "name": phase.name,
                        "order": phase.order,
                    }
            agent_data["current_task"] = task_data

            if last_task.workflow_id:
                wf = session.query(Workflow).filter_by(id=last_task.workflow_id).first()
                if wf:
                    agent_data["workflow"] = {
                        "id": wf.id,
                        "name": wf.name,
                        "status": wf.status,
                        "description": (wf.description or "")[:100],
                    }

    return agent_data


def _require_localhost(request: Request):
    """Guard that only localhost can access the endpoint."""
    client_host = request.client.host if request.client else None
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Localhost only")


@router.get("/api/agents")
async def list_agents(
    request: Request,
    status: str = Query("active", pattern="^(active|all)$"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    project_id: Optional[str] = None,
):
    """List agents with pagination. status='active' excludes terminated, 'all' includes everything."""
    server_state = _get_server_state()
    _require_localhost(request)
    from src.core.database import Task, Workflow

    with server_state.db_manager.session_scope() as session:
        query = session.query(Agent)
        if status == "active":
            query = query.filter(Agent.status.notin_(["terminated", "idle"]))

        if project_id:
            project_workflow_ids = session.query(Workflow.id).filter(
                Workflow.project_id == project_id
            ).subquery()
            project_agent_ids = session.query(Task.assigned_agent_id).filter(
                Task.workflow_id.in_(project_workflow_ids),
                Task.assigned_agent_id.isnot(None)
            ).distinct().subquery()
            query = query.filter(
                Agent.id.in_(project_agent_ids)
            )

        total = query.count()
        agents = (
            query.order_by(Agent.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        result = [_serialize_agent(session, a) for a in agents]

        return {
            "agents": result,
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page,
        }


@router.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str, request: Request):
    """Fetch a single agent by id."""
    server_state = _get_server_state()
    _require_localhost(request)

    with server_state.db_manager.session_scope() as session:
        a = session.query(Agent).filter_by(id=agent_id).first()
        if not a:
            raise HTTPException(status_code=404, detail="Agent not found")
        return _serialize_agent(session, a)


@router.post("/api/agents/{agent_id}/message")
async def send_agent_message(agent_id: str, request: Request):
    """Send a message to an agent's tmux session (parent nudge)."""
    server_state = _get_server_state()
    _require_localhost(request)
    body = await request.json()
    message = body.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="Message required")

    session = server_state.db_manager.get_session()
    try:
        agent = session.query(Agent).filter_by(id=agent_id).first()
        if not agent or not agent.tmux_session_name:
            raise HTTPException(status_code=404, detail="Agent not found")

        await server_state.agent_manager.send_message_to_agent(agent_id, message)
        return {"sent": True, "agent_id": agent_id}
    finally:
        session.close()


@router.get("/api/agents/{agent_id}/logs")
async def get_agent_logs(agent_id: str, limit: int = 50, request: Request = None):
    """Get logs for a specific agent."""
    server_state = _get_server_state()
    if request:
        _require_localhost(request)
    session = server_state.db_manager.get_session()
    try:
        from src.core.database import AgentLog

        logs = (
            session.query(AgentLog)
            .filter_by(agent_id=agent_id)
            .order_by(AgentLog.created_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": log.id,
                "log_type": log.log_type,
                "message": log.message,
                "details": log.details,
                "created_at": log.created_at.isoformat() + "Z" if log.created_at else None,
            }
            for log in logs
        ]
    finally:
        session.close()


@router.get("/api/agents/{agent_id}/output")
async def get_agent_output(agent_id: str, lines: int = 200, request: Request = None):
    """Get agent output from transcript log (full history) or tmux (fallback)."""
    server_state = _get_server_state()
    if request:
        _require_localhost(request)
    session = server_state.db_manager.get_session()
    try:
        agent = session.query(Agent).filter_by(id=agent_id).first()
        if not agent or not agent.tmux_session_name:
            return {"output": ""}

        if agent.status == 'terminated':
            lines = 0
        else:
            # _read_transcript_log reads and filters the whole transcript
            # file once per change (cached by file mtime/size) and only
            # tails it to `lines` afterward -- raising this cap doesn't add
            # backend read/filter cost, only more text over the wire.
            lines = min(lines, 30000)

        try:
            # to_thread: reads/filters the whole transcript file (or shells
            # out to tmux capture-pane) -- documented up to ~4s for a large
            # transcript, and this is a hot path the dashboard polls
            # repeatedly per active agent. Same treatment as this file's
            # sibling children/logs routes.
            output = await asyncio.to_thread(
                server_state.agent_manager.get_agent_output, agent_id, lines=lines
            )
            return {"output": output or ""}
        except Exception:
            return {"output": ""}
    finally:
        session.close()


@router.get("/api/tasks/{task_id}/instructions")
async def get_task_instructions(task_id: str, request: Request = None):
    """Read the markdown instructions file an agent was launched with.

    Agents now receive their task as a short tmux pointer to
    .hephaestus/tasks/{task_id}.md in their worktree, not a live-pasted
    message -- this is the only remaining way to see what an agent was
    actually told without shelling into the worktree.
    """
    server_state = _get_server_state()
    if request:
        _require_localhost(request)
    session = server_state.db_manager.get_session()
    try:
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        worktree_path = None
        if task.workflow_id:
            from src.core.database import Workflow

            wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
            if wf and wf.working_directory:
                worktree_path = wf.working_directory

        if not worktree_path and task.assigned_agent_id:
            try:
                worktree_path = (
                    server_state.agent_manager.branch_manager.get_agent_branch_path(
                        task.assigned_agent_id
                    )
                )
            except Exception:
                worktree_path = None

        if not worktree_path:
            raise HTTPException(
                status_code=404,
                detail=f"Could not resolve a worktree for task {task_id}",
            )

        from pathlib import Path

        instructions_path = (
            Path(worktree_path) / ".hephaestus" / "tasks" / f"{task_id}.md"
        )
        if not instructions_path.exists():
            raise HTTPException(
                status_code=404,
                detail=f"No instructions file found for task {task_id}",
            )

        return {
            "content": instructions_path.read_text(),
            "path": str(instructions_path),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to read task instructions for {task_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ── Parent-Child Agent Communication ──────────────────────────────────


@router.get("/api/agents/{agent_id}/children")
async def get_agent_children(
    agent_id: str, requesting_agent_id: str = Header(..., alias="X-Agent-ID")
):
    """Get all child agents for a parent agent."""
    server_state = _get_server_state()
    if requesting_agent_id != agent_id and not is_root_agent(requesting_agent_id):
        raise HTTPException(403, "Can only view your own children")
    from src.services.agent_communication import AgentCommunicationService

    comm = AgentCommunicationService(server_state.db_manager, server_state.agent_manager)
    # to_thread: get_children does blocking DB I/O, and this is an async
    # route -- calling it inline stalls the whole event loop.
    children = await asyncio.to_thread(comm.get_children, agent_id)
    return {"children": children, "count": len(children)}


@router.get("/api/agents/{agent_id}/children/status")
async def get_children_status(
    agent_id: str, requesting_agent_id: str = Header(..., alias="X-Agent-ID")
):
    """Get summary of all children's status."""
    server_state = _get_server_state()
    if requesting_agent_id != agent_id and not is_root_agent(requesting_agent_id):
        raise HTTPException(403, "Can only view your own children")
    from src.services.agent_communication import AgentCommunicationService

    comm = AgentCommunicationService(server_state.db_manager, server_state.agent_manager)
    # to_thread: blocking DB reads plus tmux pane inspection per child.
    summary = await asyncio.to_thread(comm.get_children_status_summary, agent_id)
    return summary


@router.get("/api/agents/{agent_id}/children/{child_id}/logs")
async def get_child_logs(
    agent_id: str,
    child_id: str,
    lines: int = Query(default=50, ge=1, le=2000),
    requesting_agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Read logs from a child agent."""
    server_state = _get_server_state()
    if requesting_agent_id != agent_id and not is_root_agent(requesting_agent_id):
        raise HTTPException(403, "Can only view your own children's logs")
    from src.services.agent_communication import AgentCommunicationService

    comm = AgentCommunicationService(server_state.db_manager, server_state.agent_manager)
    # to_thread: this shells out to `tmux capture-pane` over up to 2000
    # lines of scrollback -- by far the worst of the three to run inline.
    logs = await asyncio.to_thread(
        comm.get_child_logs, agent_id, child_id, lines
    )
    if logs is None:
        raise HTTPException(404, "Child not found or access denied")
    return {"logs": logs}


@router.post("/api/agents/{agent_id}/children/{child_id}/message")
async def send_message_to_child(
    agent_id: str,
    child_id: str,
    request: Request,
    requesting_agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Send a message from parent to child agent."""
    server_state = _get_server_state()
    if requesting_agent_id != agent_id and not is_root_agent(requesting_agent_id):
        raise HTTPException(403, "Can only message your own children")
    from src.services.agent_communication import AgentCommunicationService

    comm = AgentCommunicationService(server_state.db_manager, server_state.agent_manager)

    body = await request.json()
    message = body.get("message", "")
    if not message:
        raise HTTPException(400, "Message is required")

    success = await comm.send_message_to_child(agent_id, child_id, message)
    if not success:
        raise HTTPException(
            400, "Failed to send message - child not found or access denied"
        )
    return {"sent": True}


@router.post("/api/agents/{agent_id}/children/{child_id}/nudge")
async def nudge_child_agent(
    agent_id: str,
    child_id: str,
    request: Request,
    requesting_agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Nudge a child agent that appears stuck."""
    server_state = _get_server_state()
    if requesting_agent_id != agent_id and not is_root_agent(requesting_agent_id):
        raise HTTPException(403, "Can only nudge your own children")
    from src.services.agent_communication import AgentCommunicationService

    comm = AgentCommunicationService(server_state.db_manager, server_state.agent_manager)

    body = (
        await request.json()
        if request.headers.get("content-type") == "application/json"
        else {}
    )
    reason = body.get("reason", "No progress detected")

    success = await comm.nudge_child(agent_id, child_id, reason)
    if not success:
        raise HTTPException(400, "Failed to nudge - child not found or access denied")
    return {"nudged": True}


@router.post("/api/agents/{agent_id}/children/monitor")
async def monitor_and_nudge_stuck_children(
    agent_id: str,
    request: Request,
    requesting_agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Monitor all children and nudge any that appear stuck."""
    server_state = _get_server_state()
    if requesting_agent_id != agent_id and not is_root_agent(requesting_agent_id):
        raise HTTPException(403, "Can only monitor your own children")
    from src.services.agent_communication import AgentCommunicationService

    comm = AgentCommunicationService(server_state.db_manager, server_state.agent_manager)

    body = (
        await request.json()
        if request.headers.get("content-type") == "application/json"
        else {}
    )
    threshold = body.get("stuck_threshold_seconds", 300)

    nudged = await comm.monitor_and_nudge_stuck_children(agent_id, threshold)
    return {"nudged_count": len(nudged), "nudged_agents": nudged}


# ── Agent Lifecycle ──────────────────────────────────────────────────


@router.post("/api/create_agent_for_task")
async def create_agent_for_task_endpoint(
    task_id: str = Body(..., embed=True),
    workflow_id: str = Body(..., embed=True),
    phase_id: str = Body(default=None, embed=True),
):
    """Create an agent for a pending task."""
    server_state = _get_server_state()
    logger.info(f"Creating agent for task {task_id}")

    if not workflow_id:
        raise HTTPException(
            status_code=400, detail="workflow_id is REQUIRED for create_agent_for_task"
        )

    try:
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            enriched_data = {}
            if task.enriched_description:
                enriched_data["enriched_description"] = task.enriched_description
            if hasattr(task, "completion_criteria") and task.completion_criteria:
                enriched_data["completion_criteria"] = task.completion_criteria

            # REQ-17..21: repo-aware context (mirrors create_agent_for_task_direct).
            from src.services.agent_dispatch_service import AgentDispatchService

            project_context = await AgentDispatchService.resolve_task_project_context(
                task, session=session
            )

            agent = await server_state.agent_manager.create_agent_for_task(
                task=task,
                enriched_data=enriched_data,
                memories=[],
                project_context=project_context,
                agent_type="phase",
                use_existing_worktree=True,
            )

            logger.info(f"Created agent {agent.id[:8]} for task {task_id}")
            return {"agent_id": agent.id, "status": "created"}
        finally:
            session.close()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating agent for task: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/terminate_agent")
async def terminate_agent_endpoint(
    agent_id: str = Body(..., embed=True),
    reason: str = Body(default="Manual termination", embed=True),
):
    """Manually terminate an agent from the UI."""
    server_state = _get_server_state()
    logger.info(f"Manual termination request for agent {agent_id}: {reason}")

    try:
        session = server_state.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                raise HTTPException(
                    status_code=404, detail=f"Agent {agent_id} not found"
                )

            if agent.status == "terminated":
                raise HTTPException(
                    status_code=400, detail=f"Agent {agent_id} is already terminated"
                )

            task = None
            if agent.current_task_id:
                task = session.query(Task).filter_by(id=agent.current_task_id).first()

            await server_state.agent_manager.terminate_agent(agent_id)

            if task:
                task.status = "failed"
                task.failure_reason = f"Manually terminated: {reason}"
                task.completed_at = datetime.utcnow()
                session.commit()

            task_workflow_id = task.workflow_id if task else None

        finally:
            session.close()

        # Process queue after termination (don't block the response)
        from src.mcp.server._shared import spawn_background_task
        from src.mcp.server.background_loops import process_queue

        spawn_background_task(process_queue())

        # Broadcast update
        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(
            task_workflow_id
        )
        await server_state.broadcast_update(
            {
                "type": "agent_terminated_manually",
                "agent_id": agent_id,
                "reason": reason,
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        return {
            "success": True,
            "message": f"Agent {agent_id[:8]} terminated successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to terminate agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agent_status")
async def get_agent_status(
    agent_id: Optional[str] = None,
    requesting_agent_id: str = Header(None, alias="X-Agent-ID"),
):
    """Get status of specific agent or all agents."""
    server_state = _get_server_state()
    try:
        with server_state.db_manager.session_scope() as session:
            if agent_id:
                agent = session.query(Agent).filter_by(id=agent_id).first()
                if not agent:
                    raise HTTPException(status_code=404, detail="Agent not found")

                result = {
                    "id": agent.id,
                    "status": agent.status,
                    "current_task_id": agent.current_task_id,
                    "last_activity": agent.last_activity.isoformat() + "Z"
                    if agent.last_activity
                    else None,
                    "health_check_failures": agent.health_check_failures,
                }
            else:
                agents = session.query(Agent).filter(Agent.status != "terminated").all()

                result = [
                    {
                        "id": agent.id,
                        "status": agent.status,
                        "current_task_id": agent.current_task_id,
                        "last_activity": agent.last_activity.isoformat() + "Z"
                        if agent.last_activity
                        else None,
                    }
                    for agent in agents
                ]

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get agent status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/task_progress")
async def get_task_progress(
    task_id: Optional[str] = None,
    requesting_agent_id: str = Header(None, alias="X-Agent-ID"),
):
    """Get progress of specific task or all active tasks."""
    server_state = _get_server_state()
    try:
        with server_state.db_manager.session_scope() as session:
            if task_id:
                task = session.query(Task).filter_by(id=task_id).first()
                if not task:
                    raise HTTPException(status_code=404, detail="Task not found")

                result = {
                    "id": task.id,
                    "status": task.status,
                    "description": task.enriched_description or task.raw_description,
                    "assigned_agent_id": task.assigned_agent_id,
                    "started_at": task.started_at.isoformat() + "Z" if task.started_at else None,
                    "completed_at": task.completed_at.isoformat() + "Z"
                    if task.completed_at
                    else None,
                    "phase_id": task.phase_id,
                    "workflow_id": task.workflow_id,
                }
                if task.phase_id:
                    # SOLID review 1.10: this branch bypassed resolve_task_phase,
                    # unlike the multi-task branch just below it in this same
                    # function -- a raw Phase.filter_by(id=...) doesn't handle
                    # phase_id given as a numeric order vs. a real UUID, or scope
                    # to the task's own workflow, so this could silently resolve
                    # the wrong phase (or none) depending on which form task_id
                    # was passed as, inconsistent with every other endpoint.
                    phase = resolve_task_phase(session, task)
                    if phase:
                        result["phase_name"] = phase.name
                        result["phase_order"] = phase.order
            else:
                tasks = (
                    session.query(Task)
                    .filter(Task.status.in_(["pending", "assigned", "in_progress"]))
                    .all()
                )

                result = []
                for t in tasks:
                    phase_name = None
                    phase_order = None
                    if t.phase_id:
                        from src.core.database import Phase
                        phase = resolve_task_phase(session, t)
                        if phase:
                            phase_name = phase.name
                            phase_order = phase.order
                    result.append({
                        "id": t.id,
                        "status": t.status,
                        "description": (t.enriched_description or t.raw_description)[:200],
                        "assigned_agent_id": t.assigned_agent_id,
                        "started_at": t.started_at.isoformat() + "Z" if t.started_at else None,
                        "completed_at": t.completed_at.isoformat() + "Z" if t.completed_at else None,
                        "phase_id": t.phase_id,
                        "workflow_id": t.workflow_id,
                        "phase_name": phase_name,
                        "phase_order": phase_order,
                    })

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to get task progress: {e}")
        raise HTTPException(status_code=500, detail=str(e))
