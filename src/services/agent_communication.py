"""
Agent Communication Service

Generic parent-child agent communication system.
Allows any agent to:
- Track its child agents
- Read child logs
- Send messages to children
- Monitor child progress
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from src.core.database import Agent, DatabaseManager, Task

logger = logging.getLogger(__name__)


class AgentCommunicationService:
    """Manages parent-child agent communication."""

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager

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
                        "last_activity": child.last_activity.isoformat()
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

        # Get child's tmux session output using agent manager
        try:
            with self.db_manager.session_scope() as session:
                agent = session.query(Agent).filter_by(id=child_agent_id).first()
                if not agent or not agent.tmux_session_name:
                    return None

                import subprocess

                cmd = [
                    "tmux",
                    "capture-pane",
                    "-t",
                    agent.tmux_session_name,
                    "-p",
                    "-S",
                    "-2000",
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    output_lines = result.stdout.strip().split("\n")
                    return "\n".join(output_lines[-lines:])
                return None
        except Exception as e:
            logger.error(f"Failed to get child logs: {e}")
            return None

    def send_message_to_child(
        self, parent_agent_id: str, child_agent_id: str, message: str
    ) -> bool:
        """
        Send a message from parent to child agent.
        """
        # Verify the child belongs to this parent
        children = self.get_children(parent_agent_id)
        child_ids = [c["agent_id"] for c in children]

        if child_agent_id not in child_ids:
            logger.warning(
                f"Agent {parent_agent_id} tried to message non-child {child_agent_id}"
            )
            return False

        # Get child's tmux session name from database
        try:
            with self.db_manager.session_scope() as session:
                agent = session.query(Agent).filter_by(id=child_agent_id).first()
                if not agent or not agent.tmux_session_name:
                    logger.error(f"Child agent {child_agent_id[:8]} has no tmux session")
                    return False

                # Send message via tmux using argument list (no shell=True)
                import subprocess

                # Split message into individual keystrokes to avoid injection
                cmd = ["tmux", "send-keys", "-t", agent.tmux_session_name]
                # Send each character separately to avoid shell interpretation
                for char in message:
                    if char == "\n":
                        cmd.extend(["Enter"])
                    else:
                        cmd.append(char)
                cmd.append("Enter")  # Final enter

                result = subprocess.run(cmd, capture_output=True, timeout=5)
                if result.returncode == 0:
                    logger.info(
                        f"Parent {parent_agent_id[:8]} messaged child {child_agent_id[:8]}"
                    )
                    return True
                else:
                    logger.error(f"Failed to send message: {result.stderr}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send message to child: {e}")
            return False

    def nudge_child(
        self,
        parent_agent_id: str,
        child_agent_id: str,
        reason: str = "No progress detected",
    ) -> bool:
        """
        Nudge a child agent that appears stuck.
        """
        nudge_msg = (
            f"[PARENT NUDGE] {reason}. "
            f"If you're done writing files, call hephaestus_update_task_status NOW. "
            f"Do NOT exit to the command line."
        )
        return self.send_message_to_child(parent_agent_id, child_agent_id, nudge_msg)

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

    def monitor_and_nudge_stuck_children(
        self, parent_agent_id: str, stuck_threshold_seconds: int = 300
    ) -> List[str]:
        """
        Monitor children and nudge any that appear stuck.

        Returns list of nudged child agent IDs.
        """
        summary = self.get_children_status_summary(parent_agent_id)
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
                    if self.nudge_child(parent_agent_id, child["agent_id"], reason):
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
