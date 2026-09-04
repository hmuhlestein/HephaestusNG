"""Task listing, detail, and blocking-status queries.

Split out of FrontendAPI (src/mcp/frontend/_shared.py) -- SOLID review 1.7:
routing was already split into per-domain routers, but the class underneath
stayed one 2673-line, 41-method god object. This is the task_routes.py
domain's share of that split.
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import desc

from src.agents.manager import AgentManager
from src.core.database import Agent, AgentLog, CostEntry, DatabaseManager, Task, Workflow, utc_now
from src.core.phase_lookup import resolve_task_phase
from src.phases import PhaseManager

logger = logging.getLogger(__name__)


def _task_summary_dict(task: Task, fields: tuple, **overrides: Any) -> Dict[str, Any]:
    """Project a Task row into a small dict for embedding inside another
    task's full-details response (parent/child/duplicate/related lists).

    Each embedding site wants a different field subset -- child_tasks adds
    priority, duplicated_tasks adds created_by_agent_id, related_tasks_details
    needs a computed similarity score rather than the row's own column
    (hence **overrides) -- so this builds the superset once and lets the
    caller select which keys it actually wants instead of four near-identical
    dict literals.
    """
    values = {
        "id": task.id,
        "description": (task.enriched_description or task.raw_description)[:100],
        "status": task.status,
        "priority": task.priority,
        "similarity_score": task.similarity_score,
        "created_at": task.created_at.isoformat() + "Z" if task.created_at else None,
        "created_by_agent_id": task.created_by_agent_id,
    }
    values.update(overrides)
    return {field: values[field] for field in fields}


class TaskService:
    """API handlers for task listing, detail, and blocking status."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        phase_manager: PhaseManager = None,
    ):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.phase_manager = phase_manager

    async def get_tasks(
        self,
        skip: int = 0,
        limit: int = 50,
        status: Optional[str] = None,
        workflow_id: Optional[str] = None,
        project_id: Optional[str] = None,
        phase_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get all tasks with pagination."""

        def _fetch_sync():
            session = self.db_manager.get_session()
            try:
                query = session.query(Task)

                if status:
                    query = query.filter(Task.status == status)
                if workflow_id:
                    query = query.filter(Task.workflow_id == workflow_id)
                if phase_id:
                    query = query.filter(Task.phase_id == phase_id)
                if project_id:
                    # Filter through workflow -> project_id
                    query = query.join(
                        Workflow, Task.workflow_id == Workflow.id
                    ).filter(Workflow.project_id == project_id)

                tasks = (
                    query.order_by(desc(Task.created_at))
                    .offset(skip)
                    .limit(limit)
                    .all()
                )

                result = []
                for task in tasks:
                    task_data = {
                        "id": task.id,
                        "description": task.enriched_description or task.raw_description,
                        "done_definition": task.done_definition,
                        "status": task.status,
                        "failure_reason": task.failure_reason,
                        "priority": task.priority,
                        "assigned_agent_id": task.assigned_agent_id,
                        "created_by_agent_id": task.created_by_agent_id,
                        "parent_task_id": task.parent_task_id,
                        "created_at": task.created_at.isoformat()
                        + "Z",  # Add UTC timezone indicator
                        "started_at": task.started_at.isoformat() + "Z"
                        if task.started_at
                        else None,
                        "completed_at": task.completed_at.isoformat() + "Z"
                        if task.completed_at
                        else None,
                        "estimated_complexity": task.estimated_complexity,
                        "phase_id": task.phase_id,
                        "workflow_id": task.workflow_id,
                        "action": task.action or "",
                        "action_target_phase": task.action_target_phase or None,
                        "depends_on": task.depends_on,
                        "parallel_group": task.parallel_group,
                        "max_concurrent": task.max_concurrent,
                    }

                    # Add phase information if available
                    if task.phase_id:
                        # Handle numeric phase_id (order) vs UUID phase_id
                        phase = resolve_task_phase(session, task)

                        if phase:
                            task_data["phase_name"] = phase.name
                            task_data["phase_order"] = phase.order

                    result.append(task_data)

                return result
            finally:
                session.close()

        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _fetch_sync)

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """Get a single task by ID with basic information."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            task_data = {
                "id": task.id,
                "description": task.enriched_description or task.raw_description,
                "done_definition": task.done_definition,
                "status": task.status,
                "failure_reason": task.failure_reason,
                "priority": task.priority,
                "assigned_agent_id": task.assigned_agent_id,
                "created_by_agent_id": task.created_by_agent_id,
                "parent_task_id": task.parent_task_id,
                "created_at": task.created_at.isoformat() + "Z" if task.created_at else None,
                "started_at": task.started_at.isoformat() + "Z" if task.started_at else None,
                "completed_at": task.completed_at.isoformat() + "Z"
                if task.completed_at
                else None,
                "estimated_complexity": task.estimated_complexity,
                "phase_id": task.phase_id,
                "phase_name": None,
                "phase_order": None,
                "workflow_id": task.workflow_id,
                # Engine action (continue, retry, goto)
                "action": task.action or "",
                "action_target_phase": task.action_target_phase or None,
                # Deduplication fields
                "duplicate_of_task_id": task.duplicate_of_task_id,
                "similarity_score": task.similarity_score,
                "related_task_ids": task.related_task_ids
                if task.related_task_ids
                else [],
            }

            # SOLID review 1.10: this site never resolved phase_id -> phase
            # name/order at all, unlike get_tasks()'s identical field pair --
            # every caller of get_task() always saw phase_name/phase_order
            # as null even when the task had a real phase_id.
            if task.phase_id:
                phase = resolve_task_phase(session, task)
                if phase:
                    task_data["phase_name"] = phase.name
                    task_data["phase_order"] = phase.order

            return task_data
        finally:
            session.close()

    async def get_task_full_details(self, task_id: str) -> Dict[str, Any]:
        """Get comprehensive task details including prompts and relationships."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            # Get every agent ever created for this task, not just the
            # current assignee -- task.assigned_agent_id is overwritten on
            # each retry/restart, so it alone can't reconstruct history.
            # AgentLog's "created" entries (details.task_id) survive
            # reassignment and agent termination, same lookup
            # _authorize_agent_for_task uses to recognize a terminated
            # agent's own task. Falls back to a Python-level filter since
            # SQLite JSON extraction via as_string() can miss rows.
            creation_logs = (
                session.query(AgentLog)
                .filter(
                    AgentLog.log_type == "created",
                    AgentLog.details["task_id"].as_string() == task.id,
                )
                .all()
            )
            matched_agent_ids = {log.agent_id for log in creation_logs if log.agent_id}
            if not matched_agent_ids:
                for log in session.query(AgentLog).filter(AgentLog.log_type == "created").all():
                    if log.details and log.details.get("task_id") == task.id and log.agent_id:
                        matched_agent_ids.add(log.agent_id)

            # "Assigned" agent details shown as the task's current/latest
            # agent -- task.assigned_agent_id is NOT durable (cleared on
            # termination/failure, see the invariant above), so a
            # completed/failed task would otherwise show no agent at all.
            # Prefer the latest AgentLog-tracked agent (history_agents[-1]
            # below, ordered by created_at); fall back to assigned_agent_id
            # only for tasks predating this AgentLog "created" logging.
            agent_info = None
            system_prompt = None

            agent_history = []
            if matched_agent_ids:
                history_agents = (
                    session.query(Agent)
                    .filter(Agent.id.in_(matched_agent_ids))
                    .order_by(Agent.created_at)
                    .all()
                )
                if history_agents:
                    latest_agent = history_agents[-1]
                    agent_info = {
                        "id": latest_agent.id,
                        "status": latest_agent.status,
                        "cli_type": latest_agent.cli_type,
                        "cli_model": latest_agent.cli_model,
                        "created_at": latest_agent.created_at.isoformat() + "Z"
                        if latest_agent.created_at
                        else None,
                        "last_activity": latest_agent.last_activity.isoformat() + "Z"
                        if latest_agent.last_activity
                        else None,
                    }
                    system_prompt = latest_agent.system_prompt
                # Each agent's own attempt outcome isn't stored anywhere as
                # a single field -- only the task's CURRENT status is, and
                # every retry/restart overwrites it. mechanical_recovery's
                # session/spend-limit path DOES leave a durable record
                # (AgentLog "session_limit_terminated", written before the
                # task gets reset for the next attempt) -- use it when
                # present. Other termination paths (e.g. exceeding the
                # restart cap in launch_pipeline.restart_agent) write
                # nothing durable, so an earlier agent falls back to
                # "superseded": true (a later agent replaced it) without
                # fabricating a reason we don't actually have.
                session_limit_logs = (
                    session.query(AgentLog)
                    .filter(
                        AgentLog.log_type == "session_limit_terminated",
                        AgentLog.agent_id.in_(matched_agent_ids),
                    )
                    .order_by(AgentLog.timestamp)
                    .all()
                )
                session_limit_reason_by_agent = {
                    log.agent_id: log.message for log in session_limit_logs if log.agent_id
                }

                last_agent_id = history_agents[-1].id
                agent_history = [
                    {
                        "id": a.id,
                        "status": a.status,
                        "cli_type": a.cli_type,
                        "cli_model": a.cli_model,
                        "created_at": a.created_at.isoformat() + "Z" if a.created_at else None,
                        "last_activity": a.last_activity.isoformat() + "Z" if a.last_activity else None,
                        "terminated_at": a.terminated_at.isoformat() + "Z" if a.terminated_at else None,
                        "outcome": (
                            task.status if a.id == last_agent_id
                            else "session_limit" if a.id in session_limit_reason_by_agent
                            else "superseded"
                        ),
                        "outcome_detail": session_limit_reason_by_agent.get(a.id),
                    }
                    for a in history_agents
                ]
            elif task.assigned_agent_id:
                # No AgentLog "created" record at all (task predates that
                # logging) -- assigned_agent_id is the only signal we have.
                agent = session.query(Agent).filter_by(id=task.assigned_agent_id).first()
                if agent:
                    agent_info = {
                        "id": agent.id,
                        "status": agent.status,
                        "cli_type": agent.cli_type,
                        "cli_model": agent.cli_model,
                        "created_at": agent.created_at.isoformat() + "Z"
                        if agent.created_at
                        else None,
                        "last_activity": agent.last_activity.isoformat() + "Z"
                        if agent.last_activity
                        else None,
                    }
                    system_prompt = agent.system_prompt

            # Get phase information
            phase_info = None
            if task.phase_id:
                phase = resolve_task_phase(session, task)

                if phase:
                    phase_info = {
                        "id": phase.id,
                        "name": phase.name,
                        "order": phase.order,
                        "description": phase.description,
                        "done_definitions": phase.done_definitions,
                        "additional_notes": phase.additional_notes,
                    }

            # Get child tasks (tasks created by this task's agent)
            child_tasks = []
            if task.assigned_agent_id:
                children = (
                    session.query(Task)
                    .filter(
                        Task.created_by_agent_id == task.assigned_agent_id,
                        Task.id != task.id,
                    )
                    .all()
                )

                child_tasks = [
                    _task_summary_dict(
                        child, ("id", "description", "status", "priority", "created_at")
                    )
                    for child in children
                ]

            # Get parent task
            parent_task = None
            if task.parent_task_id:
                # Explicit parent_task_id is set
                parent = session.query(Task).filter_by(id=task.parent_task_id).first()
                if parent:
                    parent_task = _task_summary_dict(
                        parent, ("id", "description", "status", "created_at")
                    )
            elif task.created_by_agent_id:
                # No explicit parent_task_id, but we can infer it from the agent that created this task
                # Find the task that was assigned to the agent that created this task
                parent = (
                    session.query(Task)
                    .filter_by(assigned_agent_id=task.created_by_agent_id)
                    .first()
                )
                if parent and parent.id != task.id:  # Make sure it's not the same task
                    parent_task = _task_summary_dict(
                        parent, ("id", "description", "status", "created_at")
                    )

            # Get tasks that are duplicates of this task
            duplicated_tasks = []
            duplicates = (
                session.query(Task)
                .filter_by(duplicate_of_task_id=task.id, status="duplicated")
                .all()
            )
            for dup in duplicates:
                duplicated_tasks.append(
                    _task_summary_dict(
                        dup,
                        (
                            "id",
                            "description",
                            "similarity_score",
                            "created_at",
                            "created_by_agent_id",
                        ),
                    )
                )

            # Get related tasks with details
            related_tasks_details = []
            if task.related_task_ids:
                import json

                try:
                    # Parse the related_task_ids if it's a JSON string
                    related_data = (
                        task.related_task_ids
                        if isinstance(task.related_task_ids, list)
                        else json.loads(task.related_task_ids)
                    )

                    # Backfill similarity scores for old-format related_data (plain ids,
                    # no scores) by cosine over the already-stored task embeddings. Use the
                    # embedding class's shared (static) cosine — no hardcoded math, no
                    # OpenAI dependency, no model load.
                    from src.memory.embedding_factory import EmbeddingProvider

                    task_embedding = None

                    # Check if we need to calculate similarities (old format without scores)
                    needs_similarity_calculation = bool(
                        related_data
                    ) and not isinstance(related_data[0], dict)
                    if needs_similarity_calculation and task.embedding:
                        try:
                            task_embedding = (
                                task.embedding
                                if isinstance(task.embedding, list)
                                else json.loads(task.embedding)
                            )
                        except Exception as e:
                            logger.warning(
                                f"Could not parse task embedding for similarity calculation: {e}"
                            )

                    for item in related_data:
                        # Handle both new format (dict with id and similarity) and old format (just string id)
                        if isinstance(item, dict):
                            task_id = item.get("id")
                            similarity = item.get("similarity", 0.0)
                        else:
                            task_id = item
                            similarity = 0.0  # Will calculate if possible

                        # Fetch the related task
                        related_task = session.query(Task).filter_by(id=task_id).first()

                        # Try to calculate similarity for old format
                        if (
                            isinstance(item, str)
                            and task_embedding
                            and related_task
                            and related_task.embedding
                        ):
                            try:
                                related_embedding = (
                                    related_task.embedding
                                    if isinstance(related_task.embedding, list)
                                    else json.loads(related_task.embedding)
                                )
                                similarity = (
                                    EmbeddingProvider.calculate_cosine_similarity(
                                        task_embedding, related_embedding
                                    )
                                )
                            except Exception as e:
                                logger.debug(
                                    f"Could not calculate similarity for task {task_id}: {e}"
                                )
                                similarity = 0.0

                        if related_task:
                            related_tasks_details.append(
                                _task_summary_dict(
                                    related_task,
                                    (
                                        "id",
                                        "description",
                                        "status",
                                        "similarity_score",
                                        "created_at",
                                    ),
                                    similarity_score=similarity,
                                )
                            )
                except (json.JSONDecodeError, TypeError) as e:
                    logger.error(f"Error parsing related tasks: {e}")
                    pass

            # Calculate runtime
            runtime_seconds = 0
            if task.started_at:
                end_time = task.completed_at or utc_now()
                runtime_seconds = int((end_time - task.started_at).total_seconds())

            result = {
                "id": task.id,
                "raw_description": task.raw_description,
                "enriched_description": task.enriched_description,
                "done_definition": task.done_definition,
                "status": task.status,
                "priority": task.priority,
                "created_at": task.created_at.isoformat() + "Z"
                if task.created_at
                else None,
                "started_at": task.started_at.isoformat() + "Z"
                if task.started_at
                else None,
                "completed_at": task.completed_at.isoformat() + "Z"
                if task.completed_at
                else None,
                "completion_notes": task.completion_notes,
                "failure_reason": task.failure_reason,
                "estimated_complexity": task.estimated_complexity,
                "runtime_seconds": runtime_seconds,
                "system_prompt": system_prompt,
                "user_prompt": task.enriched_description or task.raw_description,
                "workflow_id": task.workflow_id,
                "action": task.action or "",
                "action_target_phase": task.action_target_phase or None,
                "phase_info": phase_info,
                "agent_info": agent_info,
                "agent_history": agent_history,
                "parent_task": parent_task,
                "child_tasks": child_tasks,
                "has_results": task.has_results,
                "validation_enabled": task.validation_enabled,
                # Task deduplication fields
                "duplicate_of_task_id": task.duplicate_of_task_id,
                "similarity_score": task.similarity_score,
                "related_task_ids": task.related_task_ids
                if task.related_task_ids
                else None,
                "duplicated_tasks": duplicated_tasks,
                "related_tasks_details": related_tasks_details,
                # Ticket tracking integration
                "ticket_id": task.ticket_id,
                "related_ticket_ids": task.related_ticket_ids
                if task.related_ticket_ids
                else None,
            }

            # Get cost data for this task
            from sqlalchemy import func
            task_cost = session.query(func.sum(CostEntry.cost_usd)).filter(
                CostEntry.task_id == task.id
            ).scalar() or 0.0
            result["cost_total_usd"] = round(task_cost, 4)

            return result
        finally:
            session.close()

    async def get_blocked_tasks(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all blocked tasks with blocker information."""
        from src.services.task_blocking_service import TaskBlockingService

        try:
            blocked_tasks = TaskBlockingService.get_all_blocked_tasks(project_id)
            return blocked_tasks
        except Exception as e:
            logger.error(f"Failed to get blocked tasks: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    async def get_task_blocker_details(self, task_id: str) -> Dict[str, Any]:
        """Get detailed blocker information for a specific task."""
        from src.services.task_blocking_service import TaskBlockingService

        try:
            blocker_info = TaskBlockingService.get_blocking_ticket_info(task_id)

            if not blocker_info:
                return {
                    "task_id": task_id,
                    "is_blocked": False,
                    "blocker_count": 0,
                    "blockers": [],
                }

            return blocker_info
        except Exception as e:
            logger.error(f"Failed to get blocker details for task {task_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    async def sync_blocking_status(self) -> Dict[str, Any]:
        """Manually trigger sync of task blocking status."""
        import asyncio

        from src.services.task_blocking_service import TaskBlockingService

        try:
            # Offloaded -- sync_task_blocking_status does N+1 blocking DB
            # round trips (one query for all tasks, then a get_db() session
            # per task), which would otherwise stall the event loop for the
            # full duration of the sync.
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, TaskBlockingService.sync_task_blocking_status
            )
            return result
        except Exception as e:
            logger.error(f"Failed to sync blocking status: {e}")
            raise HTTPException(status_code=500, detail=str(e))
