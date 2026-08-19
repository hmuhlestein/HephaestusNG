"""Shared FrontendAPI class and mutable global for the frontend package.

Extracted from src/mcp/api.py (phase_1b_decomposition.md §4.1).
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import joinedload

from src.agents.manager import AgentManager
from src.autopilot.orchestrator.engine_client import terminate_agent
from src.core.database import (
    Agent,
    AgentLog,
    AgentResult,
    CostEntry,
    DatabaseManager,
    Memory,
    Phase,
    PhasePromptVersion,
    Task,
    TaskPromptOverride,
    Workflow,
    WorkflowResult,
)
from src.phases import PhaseManager

logger = logging.getLogger(__name__)

class FrontendAPI:
    """API handlers for frontend."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        phase_manager: PhaseManager = None,
    ):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.phase_manager = phase_manager

    def _format_timestamp(self, value: Optional[datetime]) -> Optional[str]:
        if not value:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    def _parse_datetime(self, raw: Optional[str]) -> Optional[datetime]:
        if not raw:
            return None
        try:
            normalized = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _deduplicate_results(
        self, results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Deduplicate results by preferring workflow-level results over task-level results
        when both exist from the same agent for the same workflow within a close timeframe.
        Preserves task_id from task result in the workflow result entry.

        Args:
            results: List of result dictionaries

        Returns:
            Deduplicated list of results
        """

        # Group results by agent_id and workflow_id
        grouped = {}
        for result in results:
            key = (result["agent_id"], result["workflow_id"])
            if key not in grouped:
                grouped[key] = []
            grouped[key].append(result)

        deduplicated = []

        for (agent_id, workflow_id), group in grouped.items():
            # Skip if only one result in group
            if len(group) == 1:
                deduplicated.extend(group)
                continue

            # Separate workflow and task results
            workflow_results = [r for r in group if r["scope"] == "workflow"]
            task_results = [r for r in group if r["scope"] == "task"]

            # If we have both types from the same agent/workflow
            if workflow_results and task_results:
                # Check if they were created within 5 minutes of each other
                for wf_result in workflow_results:
                    wf_time = self._parse_datetime(wf_result["created_at"])
                    if not wf_time:
                        continue

                    # Find task results created within 5 minutes
                    related_task_results = []
                    for task_result in task_results:
                        task_time = self._parse_datetime(task_result["created_at"])
                        if not task_time:
                            continue

                        time_diff = abs((wf_time - task_time).total_seconds())
                        if time_diff <= 300:  # 5 minutes
                            related_task_results.append(task_result)

                    # Enhance workflow result with task_id from related task result
                    if related_task_results:
                        # Use the first related task result's task_id
                        wf_result["task_id"] = related_task_results[0]["task_id"]
                        wf_result["task_description"] = related_task_results[0][
                            "task_description"
                        ]

                    # Add workflow result (preferred)
                    deduplicated.append(wf_result)

                    # Remove related task results from the task_results list
                    for related in related_task_results:
                        if related in task_results:
                            task_results.remove(related)

                # Add any remaining task results that weren't duplicates
                deduplicated.extend(task_results)
            else:
                # No duplication, add all results
                deduplicated.extend(group)

        return deduplicated

    async def get_dashboard_stats(self, project_id: Optional[str] = None) -> Dict[str, Any]:
        """Get dashboard statistics."""
        session = self.db_manager.get_session()
        try:
            # Base query filters
            agent_query = session.query(Agent)
            task_query = session.query(Task)

            if project_id:
                # Filter by project through workflow
                project_workflow_ids = session.query(Workflow.id).filter(
                    Workflow.project_id == project_id
                ).subquery()
                project_agent_ids = session.query(Task.assigned_agent_id).filter(
                    Task.workflow_id.in_(project_workflow_ids),
                    Task.assigned_agent_id.isnot(None)
                ).distinct().subquery()
                agent_query = agent_query.filter(
                    Agent.id.in_(project_agent_ids)
                )
                task_query = task_query.filter(
                    Task.workflow_id.in_(project_workflow_ids)
                )

            active_agents = (
                agent_query
                .filter(Agent.status != "terminated")
                .count()
            )

            running_tasks = (
                task_query
                .filter(Task.status.in_(["assigned", "in_progress"]))
                .count()
            )

            queued_tasks = (
                task_query
                .filter(Task.status == "queued")
                .count()
            )

            total_memories = session.query(func.count(Memory.id)).scalar()

            # Get recent activity
            recent_logs = (
                session.query(AgentLog)
                .order_by(desc(AgentLog.timestamp))
                .limit(10)
                .all()
            )

            recent_activity = [
                {
                    "id": log.id,
                    "type": log.log_type,
                    "message": log.message,
                    "agent_id": log.agent_id,
                    "timestamp": log.timestamp.isoformat(),
                }
                for log in recent_logs
            ]

            # Get system health
            stuck_agents = (
                session.query(func.count(Agent.id))
                .filter(Agent.status == "stuck")
                .scalar()
            )

            failed_tasks_today = (
                session.query(func.count(Task.id))
                .filter(
                    Task.status == "failed",
                    Task.completed_at >= datetime.utcnow() - timedelta(days=1),
                )
                .scalar()
            )

            return {
                "active_agents": active_agents,
                "running_tasks": running_tasks,
                "queued_tasks": queued_tasks,
                "total_memories": total_memories,
                "recent_activity": recent_activity,
                "stuck_agents": stuck_agents,
                "failed_tasks_today": failed_tasks_today,
                "timestamp": datetime.utcnow().isoformat(),
            }
        finally:
            session.close()

    async def get_tasks(
        self,
        skip: int = 0,
        limit: int = 50,
        status: Optional[str] = None,
        workflow_id: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get all tasks with pagination."""
        session = self.db_manager.get_session()
        try:
            query = session.query(Task)

            if status:
                query = query.filter(Task.status == status)
            if workflow_id:
                query = query.filter(Task.workflow_id == workflow_id)
            if project_id:
                # Filter through workflow -> project_id
                query = query.join(Workflow, Task.workflow_id == Workflow.id).filter(
                    Workflow.project_id == project_id
                )

            tasks = (
                query.order_by(desc(Task.created_at)).offset(skip).limit(limit).all()
            )

            result = []
            for task in tasks:
                task_data = {
                    "id": task.id,
                    "description": task.enriched_description or task.raw_description,
                    "done_definition": task.done_definition,
                    "status": task.status,
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
                    if task.phase_id.isdigit():
                        # Look up by phase order
                        phase = (
                            session.query(Phase)
                            .filter_by(order=int(task.phase_id))
                            .first()
                        )
                    else:
                        # Look up by phase UUID
                        phase = session.query(Phase).filter_by(id=task.phase_id).first()

                    if phase:
                        task_data["phase_name"] = phase.name
                        task_data["phase_order"] = phase.order

                result.append(task_data)

            return result
        finally:
            session.close()

    async def get_memories(
        self,
        skip: int = 0,
        limit: int = 50,
        memory_type: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get memories with pagination and search."""
        session = self.db_manager.get_session()
        try:
            query = session.query(Memory)

            if memory_type:
                query = query.filter(Memory.memory_type == memory_type)

            if search:
                query = query.filter(Memory.content.ilike(f"%{search}%"))

            # Get total count for this query
            total = query.count()

            # Get counts by type for all memories (not filtered by search)
            type_counts = {}
            base_query = session.query(Memory)
            for mem_type in [
                "error_fix",
                "discovery",
                "decision",
                "learning",
                "warning",
                "codebase_knowledge",
            ]:
                count = base_query.filter(Memory.memory_type == mem_type).count()
                type_counts[mem_type] = count

            memories = (
                query.order_by(desc(Memory.created_at)).offset(skip).limit(limit).all()
            )

            return {
                "memories": [
                    {
                        "id": memory.id,
                        "content": memory.content,
                        "memory_type": memory.memory_type,
                        "agent_id": memory.agent_id,
                        "related_task_id": memory.related_task_id,
                        "tags": memory.tags,
                        "related_files": memory.related_files,
                        "created_at": memory.created_at.isoformat(),
                    }
                    for memory in memories
                ],
                "total": total,
                "type_counts": type_counts,
            }
        finally:
            session.close()

    async def get_graph_data(self, workflow_id: Optional[str] = None) -> Dict[str, Any]:
        """Get graph data for visualization."""
        session = self.db_manager.get_session()
        try:
            # Get tasks filtered by workflow_id if provided
            if workflow_id:
                tasks = (
                    session.query(Task).filter(Task.workflow_id == workflow_id).all()
                )
                phases = (
                    session.query(Phase).filter(Phase.workflow_id == workflow_id).all()
                )
                # Get agents that are assigned to tasks in this workflow
                agent_ids = set(
                    t.assigned_agent_id for t in tasks if t.assigned_agent_id
                )
                agent_ids.update(
                    t.created_by_agent_id for t in tasks if t.created_by_agent_id
                )
                agents = (
                    session.query(Agent).filter(Agent.id.in_(agent_ids)).all()
                    if agent_ids
                    else []
                )
            else:
                tasks = session.query(Task).all()
                agents = session.query(Agent).all()
                phases = session.query(Phase).all()

            # Build nodes
            nodes = []

            # Track which agents we've already added as nodes
            agent_ids_added = set()

            # Add agent nodes from agents table
            for agent in agents:
                agent_ids_added.add(agent.id)
                nodes.append(
                    {
                        "id": f"agent_{agent.id}",
                        "type": "agent",
                        "label": f"Agent {agent.id[:8]}",
                        "data": {
                            "id": agent.id,
                            "status": agent.status,
                            "cli_type": agent.cli_type,
                            "current_task_id": agent.current_task_id,
                            "created_at": agent.created_at.isoformat()
                            if agent.created_at
                            else None,
                        },
                    }
                )

            # Add external agent nodes (agents that created tasks but aren't in agents table)
            for task in tasks:
                if (
                    task.created_by_agent_id
                    and task.created_by_agent_id not in agent_ids_added
                ):
                    agent_ids_added.add(task.created_by_agent_id)
                    nodes.append(
                        {
                            "id": f"agent_{task.created_by_agent_id}",
                            "type": "agent",
                            "label": f"Agent {task.created_by_agent_id[:8] if len(task.created_by_agent_id) > 8 else task.created_by_agent_id}",
                            "data": {
                                "id": task.created_by_agent_id,
                                "status": "external",  # Mark as external agent
                                "cli_type": "mcp",  # These are typically MCP agents
                                "current_task_id": None,
                            },
                        }
                    )

            # Add task nodes
            for task in tasks:
                # Resolve phase information using conditional lookup
                phase = None
                phase_name = None
                phase_order = None
                if task.phase_id:
                    if task.phase_id.isdigit():
                        # Numeric phase_id - lookup by order
                        phase = (
                            session.query(Phase)
                            .filter_by(order=int(task.phase_id))
                            .first()
                        )
                    else:
                        # UUID phase_id - lookup by id
                        phase = session.query(Phase).filter_by(id=task.phase_id).first()

                    if phase:
                        phase_name = phase.name
                        phase_order = phase.order

                nodes.append(
                    {
                        "id": f"task_{task.id}",
                        "type": "task",
                        "label": (task.enriched_description or task.raw_description)[
                            :50
                        ],
                        "data": {
                            "id": task.id,
                            "status": task.status,
                            "priority": task.priority,
                            "description": task.enriched_description
                            or task.raw_description,
                            "created_at": task.created_at.isoformat()
                            if task.created_at
                            else None,
                            "phase_id": task.phase_id,
                            "phase_name": phase_name,
                            "phase_order": phase_order,
                        },
                    }
                )

            # Build edges
            edges = []

            # Agent created task edges
            for task in tasks:
                if task.created_by_agent_id:
                    edges.append(
                        {
                            "id": f"edge_{task.created_by_agent_id}_{task.id}",
                            "source": f"agent_{task.created_by_agent_id}",
                            "target": f"task_{task.id}",
                            "label": "created",
                            "type": "created",
                        }
                    )

            # Task assigned to agent edges
            for task in tasks:
                if task.assigned_agent_id:
                    edges.append(
                        {
                            "id": f"edge_{task.id}_{task.assigned_agent_id}",
                            "source": f"task_{task.id}",
                            "target": f"agent_{task.assigned_agent_id}",
                            "label": "assigned",
                            "type": "assigned",
                        }
                    )

            # Parent-child task edges (based on parent_task_id)
            for task in tasks:
                if task.parent_task_id:
                    edges.append(
                        {
                            "id": f"edge_parent_{task.parent_task_id}_{task.id}",
                            "source": f"task_{task.parent_task_id}",
                            "target": f"task_{task.id}",
                            "label": "subtask",
                            "type": "subtask",
                        }
                    )

            # Task spawning edges (tasks created by the agent assigned to execute another task)
            # This captures the actual task hierarchy: if Task A is assigned to Agent X,
            # and Agent X creates Task B, then A -> B (A spawned B)
            task_ids = {task.id for task in tasks}
            for task in tasks:
                if task.assigned_agent_id:
                    # Find tasks created by this task's assigned agent
                    for other_task in tasks:
                        if (
                            other_task.created_by_agent_id == task.assigned_agent_id
                            and other_task.id != task.id
                            and other_task.id in task_ids
                        ):
                            edges.append(
                                {
                                    "id": f"edge_spawned_{task.id}_{other_task.id}",
                                    "source": f"task_{task.id}",
                                    "target": f"task_{other_task.id}",
                                    "label": "spawned",
                                    "type": "subtask",
                                }
                            )

            # Create phase mapping - include both UUID and numeric keys
            phase_info = {}
            for phase in phases:
                phase_data = {
                    "id": phase.id,
                    "name": phase.name,
                    "order": phase.order,
                    "description": phase.description,
                }
                # Add phase by UUID key
                phase_info[phase.id] = phase_data
                # Add phase by numeric order key
                phase_info[str(phase.order)] = phase_data

            return {
                "nodes": nodes,
                "edges": edges,
                "phases": phase_info,
                "timestamp": datetime.utcnow().isoformat(),
            }
        finally:
            session.close()

    async def get_workflow_info(self, workflow_id: Optional[str] = None) -> Dict[str, Any]:
        """Get current workflow information.

        Args:
            workflow_id: If given, return this specific workflow (e.g. the
                one the frontend has selected). If omitted, fall back to the
                most recently created active workflow -- NOT session.query
                (Workflow).first(), which had no ORDER BY and returned
                whatever row SQLite's B-tree happened to store first for the
                UUID-string primary key: an arbitrary workflow unrelated to
                "current" or "selected", so this view showed a effectively
                random workflow's phases (sometimes a just-started Phase 0
                run with a single "Feature Architect" phase, sometimes an
                unrelated older pipeline) with no way to pick which.
        """
        session = self.db_manager.get_session()
        try:
            if workflow_id:
                workflow = session.query(Workflow).filter_by(id=workflow_id).first()
            else:
                workflow = (
                    session.query(Workflow)
                    .filter_by(status="active")
                    .order_by(Workflow.created_at.desc())
                    .first()
                )
                if not workflow:
                    # No active workflow -- fall back to the most recent of
                    # any status rather than showing nothing.
                    workflow = (
                        session.query(Workflow)
                        .order_by(Workflow.created_at.desc())
                        .first()
                    )
            if not workflow:
                return {
                    "id": None,
                    "name": "No Workflow",
                    "status": "inactive",
                    "total_phases": 0,
                    "phases": [],
                }

            # Get phases for this workflow
            phases = (
                session.query(Phase)
                .filter(Phase.workflow_id == workflow.id)
                .order_by(Phase.order)
                .all()
            )

            phase_data = []
            for phase in phases:
                # Count active agents for this phase
                # Handle both numeric phase_id (order) and UUID phase_id
                active_agents = (
                    session.query(func.count(Agent.id))
                    .join(Task, Agent.id == Task.assigned_agent_id)
                    .filter(
                        or_(
                            Task.phase_id == phase.id,  # UUID match
                            Task.phase_id == str(phase.order),  # Numeric order match
                        ),
                        Agent.status.in_(["active", "working"]),
                    )
                    .scalar()
                    or 0
                )

                # Count tasks by status for this phase
                total_tasks = (
                    session.query(func.count(Task.id))
                    .filter(
                        or_(
                            Task.phase_id == phase.id,  # UUID match
                            Task.phase_id == str(phase.order),  # Numeric order match
                        )
                    )
                    .scalar()
                    or 0
                )

                completed_tasks = (
                    session.query(func.count(Task.id))
                    .filter(
                        or_(
                            Task.phase_id == phase.id,  # UUID match
                            Task.phase_id == str(phase.order),  # Numeric order match
                        ),
                        Task.status == "done",
                    )
                    .scalar()
                    or 0
                )

                active_tasks = (
                    session.query(func.count(Task.id))
                    .filter(
                        or_(
                            Task.phase_id == phase.id,  # UUID match
                            Task.phase_id == str(phase.order),  # Numeric order match
                        ),
                        Task.status.in_(["assigned", "in_progress"]),
                    )
                    .scalar()
                    or 0
                )

                pending_tasks = (
                    session.query(func.count(Task.id))
                    .filter(
                        or_(
                            Task.phase_id == phase.id,  # UUID match
                            Task.phase_id == str(phase.order),  # Numeric order match
                        ),
                        Task.status == "pending",
                    )
                    .scalar()
                    or 0
                )

                phase_data.append(
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

            return {
                "id": workflow.id,
                "name": workflow.name,
                "status": workflow.status or "active",
                "total_phases": len(phases),
                "phases": phase_data,
            }
        finally:
            session.close()

    async def get_phases(self, workflow_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all phases with their metrics."""
        workflow_info = await self.get_workflow_info(workflow_id)
        return workflow_info.get("phases", [])

    async def get_phase_details(self, phase_id: str) -> Dict[str, Any]:
        """Get detailed phase information from database."""
        session = self.db_manager.get_session()
        try:
            # Get the phase from database
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            # Return phase details directly from database
            return {
                "description": phase.description or "",
                "done_definitions": phase.done_definitions or [],
                "additional_notes": phase.additional_notes or "",
                "outputs": phase.outputs or "",
                "next_steps": phase.next_steps or "",
            }
        finally:
            session.close()

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        """Get a single task by ID with basic information."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

            return {
                "id": task.id,
                "description": task.enriched_description or task.raw_description,
                "done_definition": task.done_definition,
                "status": task.status,
                "priority": task.priority,
                "assigned_agent_id": task.assigned_agent_id,
                "created_by_agent_id": task.created_by_agent_id,
                "parent_task_id": task.parent_task_id,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "started_at": task.started_at.isoformat() if task.started_at else None,
                "completed_at": task.completed_at.isoformat()
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
        finally:
            session.close()

    async def get_task_full_details(self, task_id: str) -> Dict[str, Any]:
        """Get comprehensive task details including prompts and relationships."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            # Get assigned agent details
            agent_info = None
            system_prompt = None
            if task.assigned_agent_id:
                agent = (
                    session.query(Agent).filter_by(id=task.assigned_agent_id).first()
                )
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
                if task.phase_id.isdigit():
                    phase = (
                        session.query(Phase).filter_by(order=int(task.phase_id)).first()
                    )
                else:
                    phase = session.query(Phase).filter_by(id=task.phase_id).first()

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
                    {
                        "id": child.id,
                        "description": (
                            child.enriched_description or child.raw_description
                        )[:100],
                        "status": child.status,
                        "priority": child.priority,
                        "created_at": child.created_at.isoformat() + "Z"
                        if child.created_at
                        else None,
                    }
                    for child in children
                ]

            # Get parent task
            parent_task = None
            if task.parent_task_id:
                # Explicit parent_task_id is set
                parent = session.query(Task).filter_by(id=task.parent_task_id).first()
                if parent:
                    parent_task = {
                        "id": parent.id,
                        "description": (
                            parent.enriched_description or parent.raw_description
                        )[:100],
                        "status": parent.status,
                        "created_at": parent.created_at.isoformat() + "Z"
                        if parent.created_at
                        else None,
                    }
            elif task.created_by_agent_id:
                # No explicit parent_task_id, but we can infer it from the agent that created this task
                # Find the task that was assigned to the agent that created this task
                parent = (
                    session.query(Task)
                    .filter_by(assigned_agent_id=task.created_by_agent_id)
                    .first()
                )
                if parent and parent.id != task.id:  # Make sure it's not the same task
                    parent_task = {
                        "id": parent.id,
                        "description": (
                            parent.enriched_description or parent.raw_description
                        )[:100],
                        "status": parent.status,
                        "created_at": parent.created_at.isoformat() + "Z"
                        if parent.created_at
                        else None,
                    }

            # Get tasks that are duplicates of this task
            duplicated_tasks = []
            duplicates = (
                session.query(Task)
                .filter_by(duplicate_of_task_id=task.id, status="duplicated")
                .all()
            )
            for dup in duplicates:
                duplicated_tasks.append(
                    {
                        "id": dup.id,
                        "description": (
                            dup.enriched_description or dup.raw_description
                        )[:100],
                        "similarity_score": dup.similarity_score,
                        "created_at": dup.created_at.isoformat() + "Z"
                        if dup.created_at
                        else None,
                        "created_by_agent_id": dup.created_by_agent_id,
                    }
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
                                {
                                    "id": related_task.id,
                                    "description": (
                                        related_task.enriched_description
                                        or related_task.raw_description
                                    )[:100],
                                    "status": related_task.status,
                                    "similarity_score": similarity,
                                    "created_at": related_task.created_at.isoformat()
                                    + "Z"
                                    if related_task.created_at
                                    else None,
                                }
                            )
                except (json.JSONDecodeError, TypeError) as e:
                    logger.error(f"Error parsing related tasks: {e}")
                    pass

            # Calculate runtime
            runtime_seconds = 0
            if task.started_at:
                end_time = task.completed_at or datetime.utcnow()
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

    async def get_guardian_analyses(
        self, agent_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get guardian analyses for a specific agent."""
        from src.core.database import GuardianAnalysis

        session = self.db_manager.get_session()
        try:
            analyses = (
                session.query(GuardianAnalysis)
                .filter_by(agent_id=agent_id)
                .order_by(desc(GuardianAnalysis.timestamp))
                .limit(limit)
                .all()
            )

            # Process analyses and detect phase changes
            result = []
            prev_phase = None

            for i, analysis in enumerate(analyses):
                # Check if this is a phase change
                phase_changed = False
                if prev_phase is not None and analysis.current_phase != prev_phase:
                    phase_changed = True
                prev_phase = analysis.current_phase

                result.append(
                    {
                        "id": analysis.id,
                        "agent_id": analysis.agent_id,
                        "timestamp": analysis.timestamp.isoformat() + "Z",
                        "current_phase": analysis.current_phase,
                        "phase_changed": phase_changed,
                        "trajectory_aligned": analysis.trajectory_aligned,
                        "alignment_score": analysis.alignment_score,
                        "progress_assessment": analysis.details.get(
                            "progress_assessment"
                        )
                        if analysis.details
                        else None,
                        "needs_steering": analysis.needs_steering,
                        "steering_type": analysis.steering_type,
                        "steering_recommendation": analysis.steering_recommendation,
                        "trajectory_summary": analysis.trajectory_summary,
                        "accumulated_goal": analysis.accumulated_goal,
                        "current_focus": analysis.current_focus,
                        "session_duration": analysis.session_duration,
                        "conversation_length": analysis.conversation_length,
                    }
                )

            return result
        finally:
            session.close()

    async def get_conductor_analyses(self, limit: int = 20) -> List[Dict[str, Any]]:
        """Get conductor analyses for system overview."""
        from src.core.database import ConductorAnalysis, DetectedDuplicate

        session = self.db_manager.get_session()
        try:
            analyses = (
                session.query(ConductorAnalysis)
                .order_by(desc(ConductorAnalysis.timestamp))
                .limit(limit)
                .all()
            )

            result = []
            for analysis in analyses:
                # Get duplicates for this analysis
                duplicates = (
                    session.query(DetectedDuplicate)
                    .filter_by(conductor_analysis_id=analysis.id)
                    .all()
                )

                duplicate_list = [
                    {
                        "agent1_id": dup.agent1_id,
                        "agent2_id": dup.agent2_id,
                        "similarity_score": dup.similarity_score,
                        "work_description": dup.work_description,
                    }
                    for dup in duplicates
                ]

                result.append(
                    {
                        "id": analysis.id,
                        "timestamp": analysis.timestamp.isoformat() + "Z",
                        "coherence_score": analysis.coherence_score,
                        "num_agents": analysis.num_agents,
                        "system_status": analysis.system_status,
                        "detected_duplicates": duplicate_list,
                        "recommendations": analysis.details.get("recommendations")
                        if analysis.details
                        else None,
                    }
                )

            return result
        finally:
            session.close()

    async def get_latest_conductor_analysis(self) -> Optional[Dict[str, Any]]:
        """Get the most recent conductor analysis."""
        analyses = await self.get_conductor_analyses(limit=1)
        return analyses[0] if analyses else None

    async def get_steering_interventions(
        self, agent_id: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get steering interventions, optionally filtered by agent."""
        from src.core.database import SteeringIntervention

        session = self.db_manager.get_session()
        try:
            query = session.query(SteeringIntervention)

            if agent_id:
                query = query.filter_by(agent_id=agent_id)

            interventions = (
                query.order_by(desc(SteeringIntervention.timestamp)).limit(limit).all()
            )

            return [
                {
                    "id": intervention.id,
                    "agent_id": intervention.agent_id,
                    "guardian_analysis_id": intervention.guardian_analysis_id,
                    "timestamp": intervention.timestamp.isoformat() + "Z",
                    "steering_type": intervention.steering_type,
                    "message": intervention.message,
                    "was_successful": intervention.was_successful,
                }
                for intervention in interventions
            ]
        finally:
            session.close()

    async def get_system_overview(self, workflow_id: Optional[str] = None) -> Dict[str, Any]:
        """Get comprehensive system overview data."""
        from datetime import datetime, timedelta

        from src.core.database import ConductorAnalysis, GuardianAnalysis

        session = self.db_manager.get_session()
        try:
            # Get basic stats
            active_agents = (
                session.query(func.count(Agent.id))
                .filter(Agent.status != "terminated")
                .scalar()
            )

            running_tasks = (
                session.query(func.count(Task.id))
                .filter(Task.status.in_(["assigned", "in_progress"]))
                .scalar()
            )

            # Get latest conductor analysis
            latest_conductor = await self.get_latest_conductor_analysis()

            # Get recent steering events
            recent_steerings = await self.get_steering_interventions(limit=10)

            # Get agent alignment scores (most recent for each active agent)
            active_agent_ids = (
                session.query(Agent.id).filter(Agent.status != "terminated").all()
            )

            agent_alignments = []
            for (agent_id,) in active_agent_ids:
                latest_guardian = (
                    session.query(GuardianAnalysis)
                    .filter_by(agent_id=agent_id)
                    .order_by(desc(GuardianAnalysis.timestamp))
                    .first()
                )

                if latest_guardian:
                    agent_alignments.append(
                        {
                            "agent_id": agent_id,
                            "alignment_score": latest_guardian.alignment_score,
                            "current_phase": latest_guardian.current_phase,
                            "needs_steering": latest_guardian.needs_steering,
                            "last_update": latest_guardian.timestamp.isoformat() + "Z",
                        }
                    )

            # Get workflow info with phases
            workflow_info = await self.get_workflow_info(workflow_id)

            # Calculate system health (average alignment score)
            avg_alignment = 0
            if agent_alignments:
                avg_alignment = sum(
                    a["alignment_score"] or 0 for a in agent_alignments
                ) / len(agent_alignments)

            # Get metrics history (last 6 hours)
            metrics_history = []

            # Get conductor analyses over time
            conductor_analyses = (
                session.query(ConductorAnalysis)
                .filter(
                    ConductorAnalysis.timestamp > datetime.utcnow() - timedelta(hours=6)
                )
                .order_by(ConductorAnalysis.timestamp)
                .all()
            )

            for analysis in conductor_analyses:
                # Get average alignment at this time
                time_guardian_analyses = (
                    session.query(GuardianAnalysis)
                    .filter(
                        GuardianAnalysis.timestamp
                        >= analysis.timestamp - timedelta(minutes=5),
                        GuardianAnalysis.timestamp
                        <= analysis.timestamp + timedelta(minutes=5),
                    )
                    .all()
                )

                time_avg_alignment = 0
                if time_guardian_analyses:
                    time_avg_alignment = sum(
                        g.alignment_score or 0 for g in time_guardian_analyses
                    ) / len(time_guardian_analyses)

                metrics_history.append(
                    {
                        "timestamp": analysis.timestamp.isoformat() + "Z",
                        "coherence_score": analysis.coherence_score,
                        "avg_alignment": time_avg_alignment,
                        "active_agents": analysis.num_agents,
                        "phase": analysis.details.get("primary_phase")
                        if analysis.details
                        else None,
                    }
                )

            return {
                "system_health": {
                    "coherence_score": latest_conductor["coherence_score"]
                    if latest_conductor
                    else 0,
                    "average_alignment": avg_alignment,
                    "active_agents": active_agents,
                    "running_tasks": running_tasks,
                    "status": latest_conductor["system_status"]
                    if latest_conductor
                    else "No analysis available",
                },
                "phase_distribution": workflow_info["phases"] if workflow_info else [],
                "latest_conductor_analysis": latest_conductor,
                "recent_steering_events": recent_steerings,
                "agent_alignments": agent_alignments,
                "metrics_history": metrics_history,
                "timestamp": datetime.utcnow().isoformat() + "Z",
            }
        finally:
            session.close()

    async def get_results(
        self,
        scope: str = "all",
        status: Optional[str] = None,
        workflow_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        search: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        session = self.db_manager.get_session()
        try:
            logger.info(f"get_results called with scope={scope}, status={status}")
            results: List[Dict[str, Any]] = []

            search_term = search.lower() if search else None
            created_after = self._parse_datetime(date_from)
            created_before = self._parse_datetime(date_to)

            include_workflow = scope in ("all", "workflow")
            include_task = scope in ("all", "task")

            if include_workflow:
                wf_query = session.query(WorkflowResult).options(
                    joinedload(WorkflowResult.workflow),
                    joinedload(WorkflowResult.agent),
                    joinedload(WorkflowResult.validator_agent),
                )

                if workflow_id:
                    wf_query = wf_query.filter(
                        WorkflowResult.workflow_id == workflow_id
                    )
                if agent_id:
                    wf_query = wf_query.filter(WorkflowResult.agent_id == agent_id)
                if status:
                    wf_query = wf_query.filter(WorkflowResult.status == status)
                if created_after:
                    wf_query = wf_query.filter(
                        WorkflowResult.created_at >= created_after.replace(tzinfo=None)
                    )
                if created_before:
                    wf_query = wf_query.filter(
                        WorkflowResult.created_at <= created_before.replace(tzinfo=None)
                    )

                for wf_result in wf_query.all():
                    try:
                        workflow = wf_result.workflow
                        agent = wf_result.agent
                        validator = wf_result.validator_agent

                        summary_source = wf_result.validation_feedback or (
                            wf_result.result_content[:200]
                            if wf_result.result_content
                            else ""
                        )

                        # Safely handle extra_files - ensure it's a list
                        extra_files = []
                        if wf_result.extra_files:
                            if isinstance(wf_result.extra_files, list):
                                extra_files = wf_result.extra_files
                            else:
                                logger.warning(
                                    f"extra_files is not a list for result {wf_result.id}: {type(wf_result.extra_files)}"
                                )
                                extra_files = []

                        entry = {
                            "result_id": wf_result.id,
                            "scope": "workflow",
                            "workflow_id": wf_result.workflow_id,
                            "workflow_name": workflow.name if workflow else None,
                            "task_id": None,
                            "task_description": None,
                            "agent_id": wf_result.agent_id,
                            "agent_label": (
                                agent.id[:8] if agent else wf_result.agent_id[:8]
                            )
                            if wf_result.agent_id
                            else None,
                            "status": wf_result.status,
                            "validation_feedback": wf_result.validation_feedback,
                            "validation_evidence": wf_result.validation_evidence or [],
                            "result_type": None,
                            "summary": summary_source,
                            "created_at": self._format_timestamp(wf_result.created_at),
                            "validated_at": self._format_timestamp(
                                wf_result.validated_at
                            ),
                            "result_file_path": wf_result.result_file_path,
                            "validation_report_path": None,
                            "validator_agent_id": validator.id
                            if validator
                            else wf_result.validated_by_agent_id,
                            "extra_files": extra_files,
                        }
                    except Exception as e:
                        logger.error(
                            f"Error processing workflow result {wf_result.id}: {e}",
                            exc_info=True,
                        )
                        continue

                    if status and status != "all" and entry["status"] != status:
                        continue

                    if search_term:
                        haystack = " ".join(
                            filter(
                                None,
                                [
                                    entry["result_id"],
                                    entry["workflow_id"],
                                    entry["workflow_name"],
                                    entry["summary"],
                                    entry["validation_feedback"],
                                    entry["agent_id"],
                                ],
                            )
                        ).lower()
                        if search_term not in haystack:
                            continue

                    results.append(entry)

            if include_task:
                task_query = session.query(AgentResult).options(
                    joinedload(AgentResult.task).joinedload(Task.workflow),
                    joinedload(AgentResult.agent),
                    joinedload(AgentResult.validation_review),
                )

                if workflow_id:
                    task_query = task_query.join(Task).filter(
                        Task.workflow_id == workflow_id
                    )
                if agent_id:
                    task_query = task_query.filter(AgentResult.agent_id == agent_id)
                if status and status in {"unverified", "verified", "disputed"}:
                    task_query = task_query.filter(
                        AgentResult.verification_status == status
                    )
                if created_after:
                    task_query = task_query.filter(
                        AgentResult.created_at >= created_after.replace(tzinfo=None)
                    )
                if created_before:
                    task_query = task_query.filter(
                        AgentResult.created_at <= created_before.replace(tzinfo=None)
                    )

                for task_result in task_query.all():
                    task = task_result.task
                    workflow = task.workflow if task else None
                    agent = task_result.agent
                    validation = task_result.validation_review

                    entry = {
                        "result_id": task_result.id,
                        "scope": "task",
                        "workflow_id": task.workflow_id if task else None,
                        "workflow_name": workflow.name if workflow else None,
                        "task_id": task_result.task_id,
                        "task_description": (
                            task.enriched_description or task.raw_description
                        )
                        if task
                        else None,
                        "agent_id": task_result.agent_id,
                        "agent_label": (
                            agent.id[:8] if agent else task_result.agent_id[:8]
                        )
                        if task_result.agent_id
                        else None,
                        "status": task_result.verification_status,
                        "validation_feedback": validation.feedback
                        if validation
                        else (task.last_validation_feedback if task else None),
                        "validation_evidence": validation.evidence
                        if validation and validation.evidence
                        else [],
                        "result_type": task_result.result_type,
                        "summary": task_result.summary,
                        "created_at": self._format_timestamp(task_result.created_at),
                        "validated_at": self._format_timestamp(
                            task_result.verified_at
                        ),  # AgentResult uses verified_at not validated_at
                        "result_file_path": task_result.markdown_file_path,
                        "validation_report_path": None,
                        "validator_agent_id": validation.validator_agent_id
                        if validation
                        else None,
                        "extra_files": [],  # Task results don't have extra_files yet, but include for consistency
                    }

                    if status and status != "all" and entry["status"] != status:
                        continue

                    if search_term:
                        haystack = " ".join(
                            filter(
                                None,
                                [
                                    entry["result_id"],
                                    entry["workflow_id"],
                                    entry["workflow_name"],
                                    entry["summary"],
                                    entry["task_description"],
                                    entry["agent_id"],
                                ],
                            )
                        ).lower()
                        if search_term not in haystack:
                            continue

                    results.append(entry)

            # Deduplicate: When both workflow and task results exist from the same agent,
            # prefer the workflow result (as it's the final validated answer)
            if scope == "all":
                results = self._deduplicate_results(results)

            # Sort newest first
            results.sort(key=lambda item: item["created_at"] or "", reverse=True)
            logger.info(f"get_results returning {len(results)} results")
            return results
        except Exception as e:
            logger.error(f"Error in get_results: {e}", exc_info=True)
            raise
        finally:
            session.close()

    async def get_result_content(self, result_id: str) -> Dict[str, Any]:
        session = self.db_manager.get_session()
        try:
            workflow_result = (
                session.query(WorkflowResult).filter_by(id=result_id).first()
            )
            if workflow_result:
                return {
                    "result_id": workflow_result.id,
                    "content": workflow_result.result_content,
                    "content_type": "markdown",
                }

            task_result = session.query(AgentResult).filter_by(id=result_id).first()
            if task_result:
                return {
                    "result_id": task_result.id,
                    "content": task_result.markdown_content,
                    "content_type": "markdown",
                }

            raise HTTPException(status_code=404, detail="Result not found")
        finally:
            session.close()

    async def get_result_validation(self, result_id: str) -> Dict[str, Any]:
        session = self.db_manager.get_session()
        try:
            workflow_result = (
                session.query(WorkflowResult).filter_by(id=result_id).first()
            )
            if workflow_result:
                # Transform evidence to expected format if needed
                evidence = []
                if workflow_result.validation_evidence:
                    # Handle different possible evidence formats
                    if isinstance(workflow_result.validation_evidence, list):
                        # Already a list - ensure each item has the required structure
                        for item in workflow_result.validation_evidence:
                            if isinstance(item, dict):
                                evidence.append(
                                    {
                                        "criterion": item.get(
                                            "criterion",
                                            item.get(
                                                "description", "Unknown criterion"
                                            ),
                                        ),
                                        "passed": item.get(
                                            "passed", item.get("met", True)
                                        ),
                                        "notes": item.get(
                                            "notes", item.get("details", None)
                                        ),
                                        "artifact_path": item.get(
                                            "artifact_path", None
                                        ),
                                    }
                                )
                    elif isinstance(workflow_result.validation_evidence, dict):
                        # If it's a single dict, convert to list with one item
                        evidence = [
                            {
                                "criterion": workflow_result.validation_evidence.get(
                                    "criterion", "Validation criteria"
                                ),
                                "passed": workflow_result.validation_evidence.get(
                                    "passed", True
                                ),
                                "notes": workflow_result.validation_evidence.get(
                                    "notes", workflow_result.validation_feedback
                                ),
                                "artifact_path": workflow_result.validation_evidence.get(
                                    "artifact_path", None
                                ),
                            }
                        ]

                # If no evidence but validation was done, create a summary item from feedback
                if (
                    not evidence
                    and workflow_result.validation_feedback
                    and workflow_result.status == "validated"
                ):
                    evidence = [
                        {
                            "criterion": "Overall validation assessment",
                            "passed": True,
                            "notes": workflow_result.validation_feedback,
                            "artifact_path": None,
                        }
                    ]

                return {
                    "result_id": workflow_result.id,
                    "status": workflow_result.status,
                    "validator_agent_id": workflow_result.validated_by_agent_id,
                    "feedback": workflow_result.validation_feedback,
                    "evidence": evidence,
                    "started_at": None,
                    "completed_at": self._format_timestamp(
                        workflow_result.validated_at
                    ),
                    "report_path": None,
                }

            task_result = (
                session.query(AgentResult)
                .options(joinedload(AgentResult.validation_review))
                .filter_by(id=result_id)
                .first()
            )
            if task_result:
                validation = task_result.validation_review
                return {
                    "result_id": task_result.id,
                    "status": task_result.verification_status,
                    "validator_agent_id": validation.validator_agent_id
                    if validation
                    else None,
                    "feedback": validation.feedback if validation else None,
                    "evidence": validation.evidence
                    if validation and validation.evidence
                    else [],
                    "started_at": None,
                    "completed_at": self._format_timestamp(task_result.verified_at),
                    "report_path": None,
                }

            raise HTTPException(status_code=404, detail="Result not found")
        finally:
            session.close()

    async def get_extra_file_content(
        self, result_id: str, file_index: int
    ) -> Dict[str, Any]:
        """Get content of a specific extra file for a result."""
        session = self.db_manager.get_session()
        try:
            # Only workflow results have extra_files currently
            workflow_result = (
                session.query(WorkflowResult).filter_by(id=result_id).first()
            )
            if not workflow_result:
                raise HTTPException(status_code=404, detail="Result not found")

            if not workflow_result.extra_files or len(workflow_result.extra_files) == 0:
                raise HTTPException(
                    status_code=404, detail="No extra files found for this result"
                )

            if file_index < 0 or file_index >= len(workflow_result.extra_files):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid file index. Must be between 0 and {len(workflow_result.extra_files) - 1}",
                )

            file_path = workflow_result.extra_files[file_index]

            # Security check: ensure file exists
            if not os.path.exists(file_path):
                raise HTTPException(
                    status_code=404,
                    detail=f"Extra file not found on disk: {os.path.basename(file_path)}",
                )

            # Read file content
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except UnicodeDecodeError:
                # If it's a binary file, read as binary and encode as base64
                with open(file_path, "rb") as f:
                    import base64

                    content = base64.b64encode(f.read()).decode("utf-8")
                return {
                    "result_id": result_id,
                    "file_index": file_index,
                    "file_path": file_path,
                    "filename": os.path.basename(file_path),
                    "content": content,
                    "content_type": "binary",
                    "encoding": "base64",
                }

            return {
                "result_id": result_id,
                "file_index": file_index,
                "file_path": file_path,
                "filename": os.path.basename(file_path),
                "content": content,
                "content_type": "text",
                "encoding": "utf-8",
            }
        finally:
            session.close()

    async def download_result_markdown(self, result_id: str) -> str:
        """Get the file path for result markdown to download."""
        session = self.db_manager.get_session()
        try:
            workflow_result = (
                session.query(WorkflowResult).filter_by(id=result_id).first()
            )
            if workflow_result and workflow_result.result_file_path:
                if os.path.exists(workflow_result.result_file_path):
                    return workflow_result.result_file_path
                raise HTTPException(
                    status_code=404, detail="Result file not found on disk"
                )

            task_result = session.query(AgentResult).filter_by(id=result_id).first()
            if task_result and task_result.markdown_file_path:
                if os.path.exists(task_result.markdown_file_path):
                    return task_result.markdown_file_path
                raise HTTPException(
                    status_code=404, detail="Result file not found on disk"
                )

            raise HTTPException(
                status_code=404, detail="Result not found or no file path available"
            )
        finally:
            session.close()

    async def download_validation_report(self, result_id: str) -> str:
        """Get the file path for validation report markdown to download."""
        session = self.db_manager.get_session()
        try:
            # For workflow results, check if there's a validation report path
            workflow_result = (
                session.query(WorkflowResult).filter_by(id=result_id).first()
            )
            if workflow_result:
                # Currently workflow results don't have a separate validation report path
                # but we can check for validation_evidence or generate from validation_feedback
                raise HTTPException(
                    status_code=404,
                    detail="Validation report not available for this result type",
                )

            # For task results, check validation review
            task_result = (
                session.query(AgentResult)
                .options(joinedload(AgentResult.validation_review))
                .filter_by(id=result_id)
                .first()
            )

            if task_result and task_result.validation_review:
                # Check if there's a report_path (if your ValidationReview model has this field)
                # For now, return 404 as validation reports might not be stored as separate files
                raise HTTPException(
                    status_code=404, detail="Validation report file not available"
                )

            raise HTTPException(status_code=404, detail="Validation report not found")
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
        from src.services.task_blocking_service import TaskBlockingService

        try:
            result = TaskBlockingService.sync_task_blocking_status()
            return result
        except Exception as e:
            logger.error(f"Failed to sync blocking status: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Phase Prompt Editor ──────────────────────────────────────────────

    async def update_phase(
        self, phase_id: str, updates: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Partial update of phase definition fields."""
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            # Only allow mutable fields with type validation
            mutable_fields = {
                "description",
                "done_definitions",
                "additional_notes",
                "outputs",
                "next_steps",
                "working_directory",
                "cli_tool",
                "cli_model",
                "glm_api_token_env",
            }
            str_fields = {
                "description",
                "additional_notes",
                "outputs",
                "next_steps",
                "working_directory",
                "cli_tool",
                "cli_model",
                "glm_api_token_env",
            }
            list_fields = {"done_definitions"}

            for key, value in updates.items():
                if key not in mutable_fields:
                    raise HTTPException(
                        status_code=400, detail=f"Field '{key}' is not mutable"
                    )
                if (
                    key in str_fields
                    and value is not None
                    and not isinstance(value, str)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Field '{key}' must be a string or null",
                    )
                if (
                    key in list_fields
                    and value is not None
                    and not isinstance(value, list)
                ):
                    raise HTTPException(
                        status_code=400, detail=f"Field '{key}' must be a list or null"
                    )
                setattr(phase, key, value)

            session.commit()
            return {
                "success": True,
                "phase": {
                    "id": phase.id,
                    "order": phase.order,
                    "name": phase.name,
                    "description": phase.description,
                    "done_definitions": phase.done_definitions,
                    "additional_notes": phase.additional_notes,
                    "outputs": phase.outputs,
                    "next_steps": phase.next_steps,
                    "working_directory": phase.working_directory,
                    "cli_tool": phase.cli_tool,
                    "cli_model": phase.cli_model,
                },
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()


    async def stop_workflow(self, workflow_id: str) -> Dict[str, Any]:
        """Stop a running workflow and terminate its agents."""
        from datetime import datetime

        from src.core.database import Agent, Task, Workflow

        session = self.db_manager.get_session()
        try:
            workflow = session.query(Workflow).filter_by(id=workflow_id).first()
            if not workflow:
                raise HTTPException(
                    status_code=404, detail=f"Workflow {workflow_id} not found"
                )

            if workflow.status != "active":
                raise HTTPException(
                    status_code=400, detail=f"Workflow is {workflow.status}, not active"
                )

            # Terminate all active agents for this workflow (agents link via tasks)
            agents = (
                session.query(Agent)
                .join(Task, Agent.current_task_id == Task.id)
                .filter(Task.workflow_id == workflow_id, Agent.status == "working")
                .all()
            )

            terminated_count = 0
            import asyncio

            loop = asyncio.get_event_loop()
            for agent in agents:
                _tmux_name = agent.tmux_session_name
                if terminate_agent(agent.id, session=session):
                    terminated_count += 1
                # Kill tmux session -- offloaded to the executor, matching
                # reset_phase's identical operation a few lines below.
                # Un-offloaded, this blocks the whole event loop (every
                # other request this process is serving) for as long as
                # the tmux CLI takes to respond, once per agent.
                if _tmux_name:
                    try:
                        import functools
                        import subprocess

                        await loop.run_in_executor(
                            None,
                            functools.partial(
                                subprocess.run,
                                ["tmux", "kill-session", "-t", _tmux_name],
                                capture_output=True,
                                timeout=5,
                            ),
                        )
                    except Exception:
                        pass

            # Mark assigned tasks as failed
            tasks = (
                session.query(Task)
                .filter_by(workflow_id=workflow_id)
                .filter(
                    Task.status.in_(["assigned", "in_progress", "queued", "pending"])
                )
                .all()
            )

            for task in tasks:
                task.status = "failed"
                task.failure_reason = "Workflow stopped by user"
                task.completed_at = datetime.utcnow()

            # Update workflow status
            workflow.status = "failed"

            session.commit()

            return {
                "success": True,
                "message": f"Workflow stopped. Terminated {terminated_count} agents, failed {len(tasks)} tasks.",
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def reset_phase(
        self, phase_id: str, target_status: str, force: bool = False
    ) -> Dict[str, Any]:
        """Reset phase execution status, handling active agents."""
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            valid_statuses = {
                "pending",
                "in_progress",
                "completed",
                "failed",
                "skipped",
            }
            if target_status not in valid_statuses:
                raise HTTPException(
                    status_code=400,
                    detail=f"target_status must be one of: {valid_statuses}",
                )

            # Find active agents in this phase
            active_agents = (
                session.query(Agent)
                .join(Task, Agent.current_task_id == Task.id)
                .filter(Task.phase_id == phase.id)
                .filter(Agent.status == "working")
                .all()
            )

            if active_agents and not force:
                return {
                    "success": False,
                    "active_agents": len(active_agents),
                    "message": f"{len(active_agents)} agents are active. Use force=true to terminate them.",
                    "requires_confirmation": True,
                }

            # Terminate active agents if force
            terminated_count = 0
            if active_agents and force:
                import asyncio
                import functools

                loop = asyncio.get_event_loop()
                for agent in active_agents:
                    try:
                        # Terminate via tmux kill-session (non-blocking)
                        import subprocess

                        _tmux_name = agent.tmux_session_name
                        if _tmux_name:
                            await loop.run_in_executor(
                                None,
                                functools.partial(
                                    subprocess.run,
                                    [
                                        "tmux",
                                        "kill-session",
                                        "-t",
                                        _tmux_name,
                                    ],
                                    timeout=5,
                                    capture_output=True,
                                ),
                            )
                        if terminate_agent(agent.id, session=session):
                            terminated_count += 1
                    except Exception as e:
                        logger.warning(f"Failed to terminate agent {agent.id}: {e}")

            # Fail assigned tasks
            tasks = (
                session.query(Task)
                .filter(Task.phase_id == phase.id)
                .filter(Task.status.in_(["assigned", "in_progress", "pending"]))
                .all()
            )
            for task in tasks:
                task.status = "failed"
                task.failure_reason = f"Phase reset to {target_status}"
                task.completed_at = datetime.utcnow()

            # Update phase execution status
            from src.core.database import PhaseExecution

            pe = (
                session.query(PhaseExecution)
                .filter_by(phase_id=phase.id)
                .order_by(PhaseExecution.started_at.desc())
                .first()
            )
            if pe:
                pe.status = target_status
                if target_status in ("completed", "failed"):
                    pe.completed_at = datetime.utcnow()

            session.commit()
            return {
                "success": True,
                "terminated_agents": terminated_count,
                "reset_tasks": len(tasks),
                "message": f"Phase reset to {target_status}",
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def get_phase_agents(self, phase_id: str) -> Dict[str, Any]:
        """List agents currently working in a phase."""
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            agents = (
                session.query(Agent)
                .join(Task, Agent.current_task_id == Task.id)
                .filter(Task.phase_id == phase.id)
                .all()
            )

            return {
                "agents": [
                    {
                        "id": agent.id,
                        "status": agent.status,
                        "cli_type": agent.cli_type,
                        "current_task_id": agent.current_task_id,
                        "started_at": agent.created_at.isoformat()
                        if agent.created_at
                        else None,
                        "health_check_failures": agent.health_check_failures,
                    }
                    for agent in agents
                ]
            }
        finally:
            session.close()

    async def get_phase_prompt_versions(self, phase_id: str) -> Dict[str, Any]:
        """List prompt versions for a phase (newest first)."""
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                raise HTTPException(status_code=404, detail="Phase not found")

            versions = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id)
                .order_by(PhasePromptVersion.version.desc())
                .all()
            )

            return {
                "versions": [
                    {
                        "version": v.version,
                        "status": v.status,
                        "created_by": v.created_by,
                        "created_at": v.created_at.isoformat()
                        if v.created_at
                        else None,
                        "change_summary": v.change_summary,
                        "parent_version": v.parent_version,
                        "changed_fields": list(
                            {
                                f
                                for f, val in [
                                    ("description", v.description),
                                    ("done_definitions", v.done_definitions),
                                    ("additional_notes", v.additional_notes),
                                    ("outputs", v.outputs),
                                    ("next_steps", v.next_steps),
                                ]
                                if val is not None and val != "" and val != []
                            }
                        ),
                    }
                    for v in versions
                ]
            }
        finally:
            session.close()

    async def get_phase_prompt_version(
        self, phase_id: str, version: int
    ) -> Dict[str, Any]:
        """Get a specific prompt version's content."""
        session = self.db_manager.get_session()
        try:
            pv = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=version)
                .first()
            )
            if not pv:
                raise HTTPException(
                    status_code=404,
                    detail=f"Version {version} not found for phase {phase_id}",
                )

            return {
                "version": pv.version,
                "status": pv.status,
                "description": pv.description,
                "done_definitions": pv.done_definitions or [],
                "additional_notes": pv.additional_notes,
                "outputs": pv.outputs,
                "next_steps": pv.next_steps,
                "change_summary": pv.change_summary,
                "created_by": pv.created_by,
                "created_at": pv.created_at.isoformat() if pv.created_at else None,
                "parent_version": pv.parent_version,
            }
        finally:
            session.close()

    async def create_phase_prompt_version(
        self, phase_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Create a new prompt version for a phase."""
        import time

        from sqlalchemy.exc import IntegrityError

        last_error = None
        for attempt in range(3):
            session = self.db_manager.get_session()
            try:
                phase = session.query(Phase).filter_by(id=phase_id).first()
                if not phase:
                    raise HTTPException(status_code=404, detail="Phase not found")

                max_version = (
                    session.query(func.max(PhasePromptVersion.version))
                    .filter_by(phase_id=phase_id)
                    .scalar()
                    or 0
                )
                new_version = max_version + 1
                publish = data.get("publish", False)

                new_pv = PhasePromptVersion(
                    id=f"{phase_id}_v{new_version}",
                    phase_id=phase_id,
                    version=new_version,
                    status="active" if publish else "draft",
                    description=data.get("description", phase.description or ""),
                    done_definitions=data.get(
                        "done_definitions", phase.done_definitions or []
                    ),
                    additional_notes=data.get(
                        "additional_notes", phase.additional_notes
                    ),
                    outputs=data.get("outputs", phase.outputs),
                    next_steps=data.get("next_steps", phase.next_steps),
                    change_summary=data.get("change_summary", ""),
                    created_by=data.get("created_by", "ui-user"),
                    parent_version=max_version if max_version > 0 else None,
                )
                session.add(new_pv)

                if publish:
                    existing = (
                        session.query(PhasePromptVersion)
                        .filter_by(phase_id=phase_id, status="active")
                        .all()
                    )
                    for pv in existing:
                        pv.status = "archived"
                    phase.description = data.get("description", phase.description)
                    phase.done_definitions = data.get(
                        "done_definitions", phase.done_definitions
                    )
                    phase.additional_notes = data.get(
                        "additional_notes", phase.additional_notes
                    )
                    phase.outputs = data.get("outputs", phase.outputs)
                    phase.next_steps = data.get("next_steps", phase.next_steps)

                session.commit()

                diff_result = {}
                if max_version > 0 and new_pv.parent_version:
                    parent_pv = (
                        session.query(PhasePromptVersion)
                        .filter_by(phase_id=phase_id, version=max_version)
                        .first()
                    )
                    if parent_pv:
                        from src.prompts.assembler import PromptAssembler

                        old_asm = PromptAssembler(
                            phase_description=parent_pv.description,
                            done_definitions=parent_pv.done_definitions or [],
                            additional_notes=parent_pv.additional_notes,
                            outputs=parent_pv.outputs,
                            next_steps=parent_pv.next_steps,
                        )
                        new_asm = PromptAssembler(
                            phase_description=new_pv.description,
                            done_definitions=new_pv.done_definitions or [],
                            additional_notes=new_pv.additional_notes,
                            outputs=new_pv.outputs,
                            next_steps=new_pv.next_steps,
                        )
                        diff_result = old_asm.diff(new_asm)

                return {
                    "success": True,
                    "version": new_version,
                    "status": new_pv.status,
                    "created_at": new_pv.created_at.isoformat()
                    if new_pv.created_at
                    else None,
                    "created_by": new_pv.created_by,
                    "diff": diff_result,
                }
            except IntegrityError:
                session.rollback()
                last_error = "Version conflict"
                time.sleep(0.05 * (attempt + 1))
                continue
            except HTTPException:
                session.close()
                raise
            except Exception as e:
                session.rollback()
                session.close()
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                try:
                    session.close()
                except Exception:
                    pass

        raise HTTPException(
            status_code=500,
            detail=f"Failed to create version after retries: {last_error}",
        )

    async def publish_phase_prompt_version(
        self, phase_id: str, version: int
    ) -> Dict[str, Any]:
        """Publish a draft version as active."""
        session = self.db_manager.get_session()
        try:
            pv = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=version)
                .first()
            )
            if not pv:
                raise HTTPException(
                    status_code=404, detail=f"Version {version} not found"
                )

            # Demote existing active
            existing_active = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, status="active")
                .all()
            )
            for v in existing_active:
                v.status = "archived"

            pv.status = "active"

            # Update phase definition
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if phase:
                phase.description = pv.description
                phase.done_definitions = pv.done_definitions
                phase.additional_notes = pv.additional_notes
                phase.outputs = pv.outputs
                phase.next_steps = pv.next_steps

            session.commit()
            return {"success": True, "version": version, "status": "active"}
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def restore_phase_prompt_version(
        self, phase_id: str, version: int
    ) -> Dict[str, Any]:
        """Restore an older version as a new active version."""
        import time

        from sqlalchemy.exc import IntegrityError

        last_error = None
        for attempt in range(3):
            session = self.db_manager.get_session()
            try:
                pv = (
                    session.query(PhasePromptVersion)
                    .filter_by(phase_id=phase_id, version=version)
                    .first()
                )
                if not pv:
                    raise HTTPException(
                        status_code=404, detail=f"Version {version} not found"
                    )

                max_version = (
                    session.query(func.max(PhasePromptVersion.version))
                    .filter_by(phase_id=phase_id)
                    .scalar()
                    or 0
                )
                new_version = max_version + 1

                new_pv = PhasePromptVersion(
                    id=f"{phase_id}_v{new_version}",
                    phase_id=phase_id,
                    version=new_version,
                    status="active",
                    description=pv.description,
                    done_definitions=pv.done_definitions,
                    additional_notes=pv.additional_notes,
                    outputs=pv.outputs,
                    next_steps=pv.next_steps,
                    change_summary=f"Restored from version {version}",
                    created_by="ui-user",
                    parent_version=version,
                )
                session.add(new_pv)

                existing_active = (
                    session.query(PhasePromptVersion)
                    .filter_by(phase_id=phase_id, status="active")
                    .all()
                )
                for v in existing_active:
                    if v.id != new_pv.id:
                        v.status = "archived"

                phase = session.query(Phase).filter_by(id=phase_id).first()
                if phase:
                    phase.description = pv.description
                    phase.done_definitions = pv.done_definitions
                    phase.additional_notes = pv.additional_notes
                    phase.outputs = pv.outputs
                    phase.next_steps = pv.next_steps

                session.commit()
                return {
                    "success": True,
                    "version": new_version,
                    "restored_from": version,
                    "status": "active",
                }
            except IntegrityError:
                session.rollback()
                last_error = "Version conflict"
                time.sleep(0.05 * (attempt + 1))
                continue
            except HTTPException:
                session.close()
                raise
            except Exception as e:
                session.rollback()
                session.close()
                raise HTTPException(status_code=500, detail=str(e))
            finally:
                try:
                    session.close()
                except Exception:
                    pass

        raise HTTPException(
            status_code=500,
            detail=f"Failed to restore version after retries: {last_error}",
        )

    async def get_phase_prompt_preview(
        self, phase_id: str, variables: Optional[Dict[str, str]] = None
    ) -> Dict[str, Any]:
        """Render a preview of the assembled prompt."""
        try:
            from src.prompts.assembler import assemble_phase_prompt

            result = assemble_phase_prompt(phase_id, variables=variables)
            return {
                "system_prompt": result.system_prompt,
                "user_prompt": result.user_prompt,
                "variables_used": result.variables_used,
                "variables_missing": result.variables_missing,
                "warnings": result.warnings,
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    async def get_phase_prompt_diff(
        self, phase_id: str, v1: int, v2: int
    ) -> Dict[str, Any]:
        """Get diff between two versions."""
        session = self.db_manager.get_session()
        try:
            pv1 = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=v1)
                .first()
            )
            pv2 = (
                session.query(PhasePromptVersion)
                .filter_by(phase_id=phase_id, version=v2)
                .first()
            )
            if not pv1:
                raise HTTPException(status_code=404, detail=f"Version {v1} not found")
            if not pv2:
                raise HTTPException(status_code=404, detail=f"Version {v2} not found")

            from src.prompts.assembler import PromptAssembler

            assembler1 = PromptAssembler(
                phase_description=pv1.description,
                done_definitions=pv1.done_definitions or [],
                additional_notes=pv1.additional_notes,
                outputs=pv1.outputs,
                next_steps=pv1.next_steps,
            )
            assembler2 = PromptAssembler(
                phase_description=pv2.description,
                done_definitions=pv2.done_definitions or [],
                additional_notes=pv2.additional_notes,
                outputs=pv2.outputs,
                next_steps=pv2.next_steps,
            )
            diff = assembler1.diff(assembler2)
            diff["from_version"] = v1
            diff["to_version"] = v2
            return diff
        finally:
            session.close()

    async def get_task_prompt_overrides(self, task_id: str) -> Dict[str, Any]:
        """Get prompt overrides for a task."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            override = (
                session.query(TaskPromptOverride).filter_by(task_id=task_id).first()
            )
            if not override:
                return {"system_prompt": None, "user_prompt": None}

            return {
                "system_prompt": override.system_prompt,
                "user_prompt": override.user_prompt,
                "updated_at": override.updated_at.isoformat()
                if override.updated_at
                else None,
                "updated_by": override.updated_by,
            }
        finally:
            session.close()

    async def set_task_prompt_overrides(
        self, task_id: str, data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Set prompt overrides for a task."""
        session = self.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if not task:
                raise HTTPException(status_code=404, detail="Task not found")

            if task.status in ("done", "failed", "duplicated"):
                raise HTTPException(
                    status_code=400, detail="Cannot edit prompts for completed tasks"
                )

            override = (
                session.query(TaskPromptOverride).filter_by(task_id=task_id).first()
            )
            if override:
                if data.get("system_prompt") is not None:
                    override.system_prompt = data["system_prompt"]
                if data.get("user_prompt") is not None:
                    override.user_prompt = data["user_prompt"]
                override.updated_by = data.get("updated_by", "ui-user")
            else:
                override = TaskPromptOverride(
                    task_id=task_id,
                    system_prompt=data.get("system_prompt"),
                    user_prompt=data.get("user_prompt"),
                    updated_by=data.get("updated_by", "ui-user"),
                )
                session.add(override)

            session.commit()

            # Build effective prompt using already-loaded data (no N+1 query)
            from src.prompts.assembler import PromptAssembler

            phase = None
            if task.phase_id:
                if task.phase_id.isdigit():
                    phase = (
                        session.query(Phase).filter_by(order=int(task.phase_id)).first()
                    )
                else:
                    phase = session.query(Phase).filter_by(id=task.phase_id).first()

            assembler = PromptAssembler(
                phase_description=phase.description if phase else "",
                done_definitions=phase.done_definitions if phase else [],
                additional_notes=phase.additional_notes if phase else None,
                outputs=phase.outputs if phase else None,
                next_steps=phase.next_steps if phase else None,
                phase_order=phase.order if phase else None,
                phase_name=phase.name if phase else None,
            )
            effective = assembler.render(
                task_description=task.enriched_description or task.raw_description,
                task_done_definition=task.done_definition,
                agent_id=task.assigned_agent_id,
                task_id=task.id,
                task_system_prompt=override.system_prompt,
                task_user_prompt=override.user_prompt,
            )

            return {
                "success": True,
                "overrides": {
                    "system_prompt": override.system_prompt,
                    "user_prompt": override.user_prompt,
                },
                "effective_prompt": {
                    "system_prompt": effective.system_prompt[:500] + "..."
                    if len(effective.system_prompt) > 500
                    else effective.system_prompt,
                    "user_prompt": effective.user_prompt[:500] + "..."
                    if len(effective.user_prompt) > 500
                    else effective.user_prompt,
                },
            }
        except HTTPException:
            raise
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()

    async def clear_task_prompt_overrides(self, task_id: str) -> Dict[str, Any]:
        """Clear prompt overrides for a task."""
        session = self.db_manager.get_session()
        try:
            override = (
                session.query(TaskPromptOverride).filter_by(task_id=task_id).first()
            )
            if override:
                session.delete(override)
                session.commit()
            return {"success": True, "message": "Overrides cleared"}
        except Exception as e:
            session.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        finally:
            session.close()


# Create the API instance (will be initialized in server.py)

frontend_api = None

