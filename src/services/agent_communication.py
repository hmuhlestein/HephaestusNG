"""
Agent Communication Service

Generic parent-child agent communication system.
Allows any agent to:
- Track its child agents
- Read child logs
- Send messages to children
- Monitor child progress
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from src.core.database import Agent, DatabaseManager, Task
from src.prompts.loader import get_prompt

logger = logging.getLogger(__name__)


class AgentCommunicationService:
    """Manages parent-child agent communication."""

    def __init__(self, db_manager: DatabaseManager, agent_manager=None):
        self.db_manager = db_manager
        self._agent_manager = agent_manager
        self._messenger = None
        if agent_manager is not None:
            from src.agents.messenger import AgentMessenger
            self._messenger = AgentMessenger(db_manager, agent_manager)

    def get_children(self, parent_agent_id: str) -> List[Dict[str, Any]]:
        """
        Get all child agents for a parent agent.

        A child is any agent working on a task created by the parent.
        """
        with self.db_manager.session_scope() as session:
            # Find tasks created by this agent
            parent_tasks = (
                session.query(Task).filter_by(created_by_agent_id=parent_agent_id).all()
            )
            parent_task_ids = [t.id for t in parent_tasks]

            if not parent_task_ids:
                return []

            # Find agents assigned to those tasks
            children = (
                session.query(Agent)
                .filter(Agent.current_task_id.in_(parent_task_ids))
                .all()
            )

            result = []
            for child in children:
                task = session.query(Task).filter_by(id=child.current_task_id).first()
                result.append(
                    {
                        "agent_id": child.id,
                        "status": child.status,
                        "task_id": child.current_task_id,
                        "task_description": (
                            task.enriched_description or task.raw_description or ""
                        )[:200]
                        if task
                        else None,
                        "task_status": task.status if task else None,
                        "last_activity": child.last_activity.isoformat() + "Z"
                        if child.last_activity
                        else None,
                        "health_check_failures": child.health_check_failures,
                        "tmux_session": child.tmux_session_name,
                    }
                )

            return result

    def get_child_logs(
        self, parent_agent_id: str, child_agent_id: str, lines: int = 50
    ) -> Optional[str]:
        """
        Read logs from a child agent.

        Returns the last N lines of the child's tmux output.
        """
        # Verify the child belongs to this parent
        children = self.get_children(parent_agent_id)
        child_ids = [c["agent_id"] for c in children]

        if child_agent_id not in child_ids:
            logger.warning(
                f"Agent {parent_agent_id} tried to access non-child {child_agent_id}"
            )
            return None

        try:
            with self.db_manager.session_scope() as session:
                agent = session.query(Agent).filter_by(id=child_agent_id).first()
                if not agent or not agent.tmux_session_name:
                    return None

                if self._agent_manager is None:
                    logger.error("No agent_manager available for tmux access")
                    return None

                tmux_server = self._agent_manager.tmux_server
                tmux_session = None
                for sess in tmux_server.sessions:
                    if sess.name == agent.tmux_session_name:
                        tmux_session = sess
                        break

                if not tmux_session:
                    return None

                pane = tmux_session.attached_window.attached_pane
                output_lines = pane.cmd(
                    "capture-pane", "-p", "-S", "-2000"
                ).stdout
                if output_lines:
                    return "\n".join(output_lines[-lines:])
                return None
        except Exception as e:
            logger.error(f"Failed to get child logs: {e}")
            return None

    async def send_message_to_child(
        self, parent_agent_id: str, child_agent_id: str, message: str
    ) -> bool:
        """
        Send a message from parent to child agent.

        Routes through AgentMessenger for consistent escaping and
        stuck-shell (_pane_is_wedged) detection.
        """
        # Verify the child belongs to this parent. to_thread: get_children
        # does blocking DB I/O -- inline here stalls the whole event loop,
        # matching the fix already applied at the route layer for its
        # sibling (sync) methods (agents_api.py).
        children = await asyncio.to_thread(self.get_children, parent_agent_id)
        child_ids = [c["agent_id"] for c in children]

        if child_agent_id not in child_ids:
            logger.warning(
                f"Agent {parent_agent_id} tried to message non-child {child_agent_id}"
            )
            return False

        if self._messenger is None:
            logger.error("No agent_manager available for message delivery")
            return False

        try:
            await self._messenger.send_message_to_agent(child_agent_id, message)
            logger.info(
                f"Parent {parent_agent_id[:8]} messaged child {child_agent_id[:8]}"
            )
            return True
        except Exception as e:
            logger.error(f"Failed to send message to child: {e}")
            return False

    async def nudge_child(
        self,
        parent_agent_id: str,
        child_agent_id: str,
        reason: str = "No progress detected",
    ) -> bool:
        """
        Nudge a child agent that appears stuck.
        """
        nudge_msg = get_prompt("parent_nudge_child", {"reason": reason})
        return await self.send_message_to_child(parent_agent_id, child_agent_id, nudge_msg)

    def get_children_status_summary(self, parent_agent_id: str) -> Dict[str, Any]:
        """
        Get a summary of all children's status.
        """
        children = self.get_children(parent_agent_id)

        summary = {
            "total": len(children),
            "working": 0,
            "idle": 0,
            "stuck": 0,
            "completed": 0,
            "failed": 0,
            "children": children,
        }

        now = datetime.utcnow()
        for child in children:
            status = child.get("status", "unknown")
            if status == "working":
                # Check if stuck (no activity for 5 minutes)
                last_activity = child.get("last_activity")
                if last_activity:
                    try:
                        last_dt = datetime.fromisoformat(
                            last_activity.replace("Z", "+00:00")
                        )
                        if (now - last_dt.replace(tzinfo=None)) > timedelta(minutes=5):
                            summary["stuck"] += 1
                        else:
                            summary["working"] += 1
                    except (ValueError, TypeError):
                        summary["working"] += 1
                else:
                    summary["working"] += 1
            elif status == "idle":
                summary["idle"] += 1
            elif status in ("completed", "done"):
                summary["completed"] += 1
            elif status in ("failed", "terminated"):
                summary["failed"] += 1

        return summary

    async def monitor_and_nudge_stuck_children(
        self, parent_agent_id: str, stuck_threshold_seconds: int = 300
    ) -> List[str]:
        """
        Monitor children and nudge any that appear stuck.

        Returns list of nudged child agent IDs.
        """
        # to_thread: blocking DB reads plus tmux pane inspection per child --
        # same reasoning as send_message_to_child above.
        summary = await asyncio.to_thread(self.get_children_status_summary, parent_agent_id)
        nudged = []

        for child in summary["children"]:
            if child["status"] != "working":
                continue

            last_activity = child.get("last_activity")
            if not last_activity:
                continue

            try:
                last_dt = datetime.fromisoformat(last_activity.replace("Z", "+00:00"))
                elapsed = (
                    datetime.utcnow() - last_dt.replace(tzinfo=None)
                ).total_seconds()

                if elapsed > stuck_threshold_seconds:
                    reason = f"No activity for {int(elapsed)}s"
                    if await self.nudge_child(parent_agent_id, child["agent_id"], reason):
                        nudged.append(child["agent_id"])
                        logger.info(
                            f"Nudged stuck child {child['agent_id'][:8]}: {reason}"
                        )
            except Exception as e:
                logger.error(f"Error checking child activity: {e}")

        return nudged

    def create_child_task(
        self,
        parent_agent_id: str,
        description: str,
        priority: str = "medium",
        phase_id: str = None,
    ) -> Optional[str]:
        """
        Create a task that will spawn a child agent.
        Returns the task ID.
        """
        try:
            with self.db_manager.session_scope() as session:
                import uuid

                task_id = str(uuid.uuid4())

                task = Task(
                    id=task_id,
                    raw_description=description,
                    enriched_description=description,
                    done_definition="Task completed - agent reports done",
                    status="pending",
                    priority=priority,
                    created_by_agent_id=parent_agent_id,
                    phase_id=phase_id,
                )
                session.add(task)

                logger.info(
                    f"Parent {parent_agent_id[:8]} created child task {task_id[:8]}"
                )
                return task_id
        except Exception as e:
            logger.error(f"Failed to create child task: {e}")
            return None
