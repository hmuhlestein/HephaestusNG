"""Agent-in-phase queries.

Split out of FrontendAPI (src/mcp/frontend/_shared.py) -- SOLID review 1.7:
routing was already split into per-domain routers, but the class underneath
stayed one 2673-line, 41-method god object. This is the agent_routes.py
domain's share of that split.
"""

import logging
from typing import Any, Dict

from fastapi import HTTPException

from src.agents.manager import AgentManager
from src.core.database import Agent, DatabaseManager, Phase, Task
from src.phases import PhaseManager

logger = logging.getLogger(__name__)

class AgentService:
    """API handlers for querying agents within a phase."""

    def __init__(
        self,
        db_manager: DatabaseManager,
        agent_manager: AgentManager,
        phase_manager: PhaseManager = None,
    ):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.phase_manager = phase_manager

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
                        # SOLID review 1.10: this read agent.created_at (row
                        # creation, at registration) under the key
                        # "started_at" -- PhaseAgentList.tsx renders it
                        # labeled "Started:", and Agent.launched_at is the
                        # field that actually means "when this agent's CLI
                        # session launched" (set in launch_pipeline.py).
                        # Prefers launched_at, falling back to created_at
                        # for older/never-launched agents rather than
                        # regressing to no value at all.
                        "started_at": (agent.launched_at or agent.created_at).isoformat() + "Z"
                        if (agent.launched_at or agent.created_at)
                        else None,
                        "health_check_failures": agent.health_check_failures,
                    }
                    for agent in agents
                ]
            }
        finally:
            session.close()

