"""Dashboard, memory, graph, workflow/phase-summary, guardian/conductor, and
results (view + download) queries.

Split out of FrontendAPI (src/mcp/frontend/_shared.py) -- SOLID review 1.7:
routing was already split into per-domain routers, but the class underneath
stayed one 2673-line, 41-method god object. This is the dashboard_routes.py
domain's share of that split.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import desc, func, or_
from sqlalchemy.orm import joinedload

from src.agents.manager import AgentManager
from src.core.database import (
    Agent,
    AgentLog,
    AgentResult,
    DatabaseManager,
    Memory,
    Phase,
    Task,
    Workflow,
    WorkflowResult,
    utc_now,
)
from src.core.phase_lookup import resolve_task_phase
from src.phases import PhaseManager

logger = logging.getLogger(__name__)

class DashboardService:
    """API handlers for dashboard stats, memories, graph data, workflow/phase
    summaries, guardian/conductor analyses, and results."""

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
                    "timestamp": log.timestamp.isoformat() + "Z",
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
                    Task.completed_at >= utc_now() - timedelta(days=1),
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
                "timestamp": utc_now().isoformat() + "Z",
            }
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
                        "created_at": memory.created_at.isoformat() + "Z",
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
                            "created_at": agent.created_at.isoformat() + "Z"
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
                    phase = resolve_task_phase(session, task)

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
                            "created_at": task.created_at.isoformat() + "Z"
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
                "timestamp": utc_now().isoformat() + "Z",
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
        from datetime import timedelta

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
                    ConductorAnalysis.timestamp > utc_now() - timedelta(hours=6)
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
                "timestamp": utc_now().isoformat() + "Z",
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

