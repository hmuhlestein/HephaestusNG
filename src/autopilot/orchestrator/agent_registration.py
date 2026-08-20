"""Registration of the orchestrator's own Agent row. Extracted from
orchestrator/__init__.py (SOLID review: that module mixed this DB-touching
registration helper into the actual pipeline-execution flow -- see
docs/SOLID_OO_REVIEW_UPDATE_2026-08-19.md).
"""

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from src.autopilot.orchestrator.engine_client import terminate_agent

if TYPE_CHECKING:
    from src.autopilot.orchestrator import OrchestratorLogger


def _register_orchestrator_agent(
    log_dir: Path, cli_tool: str, logger: "OrchestratorLogger"
) -> Optional[str]:
    """Register (or re-register, after a restart) the orchestrator's own
    Agent row, whose id becomes Task.created_by_agent_id for every task the
    orchestrator itself creates (_create_phase_task, _create_corrective_task).

    Returns the new agent's id, or None if registration failed -- in which
    case those task-creation call sites fall back to created_by_agent_id=
    None (the column is nullable).
    """
    try:
        import uuid

        from src.core.database import Agent, DatabaseManager

        db_manager = DatabaseManager(None)
        session = db_manager.get_session()
        try:
            new_agent_id = f"orchestrator-{uuid.uuid4().hex[:8]}"
            orchestrator_agent = session.query(Agent).filter_by(id=new_agent_id).first()
            if orchestrator_agent:
                orchestrator_agent.status = "working"
                orchestrator_agent.last_activity = datetime.utcnow()
            else:
                # Check if tmux_session_name is already taken
                existing = session.query(Agent).filter_by(tmux_session_name="orchestrator").first()
                if existing:
                    terminate_agent(existing.id, session=session)
                    # tmux_session_name has a UNIQUE constraint -- marking
                    # the old row "terminated" alone doesn't free the value
                    # "orchestrator" up, so the commit below still collides
                    # with it. Without this, registration silently failed
                    # on every restart after the first (logged as just a
                    # warning), leaving the caller's _orchestrator_agent_id
                    # pointing at an Agent row that was never actually
                    # persisted -- so any task creation using it as
                    # created_by_agent_id (_create_phase_task) hit a
                    # FOREIGN KEY failure the moment FK enforcement was
                    # turned on. Uses the FULL id, not a slice: every
                    # orchestrator agent id shares the literal prefix
                    # "orchestrator-", so id[:8] is always "orchestr" for
                    # every one of them -- not unique at all, and the very
                    # first fix attempt using it collided with itself
                    # across restarts the same way the original bug did.
                    existing.tmux_session_name = f"orchestrator-terminated-{existing.id}"
                orchestrator_agent = Agent(
                    id=new_agent_id,
                    system_prompt=f"LOG_DIR:{log_dir}",
                    status="working",
                    cli_type=cli_tool,
                    agent_type="orchestrator",
                    tmux_session_name="orchestrator",
                )
                session.add(orchestrator_agent)
            session.commit()
            logger.info(f"Registered orchestrator agent: {orchestrator_agent.id[:8]}")
            return new_agent_id
        except Exception as e:
            logger.warning(f"Warning: Could not register orchestrator agent: {e}")
            return None
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Warning: Could not register orchestrator agent: {e}")
        return None
