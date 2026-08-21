"""Gathering dispatch context (project/phase context, RAG memories, working
directory, phase CLI config) and creating an agent for a task.

Extracted from src/mcp/server.py, where this exact sequence was
independently duplicated across create_task's process_task_async closure,
process_queue, bump_task_priority_endpoint, and restart_task_endpoint —
see docs/SOLID_OO_REVIEW.md findings 1.2/1.3. Two of those call sites
(bump/restart) had drifted out of sync on whether they fetched phase CLI
config before this extraction; unifying them here also fixes that
inconsistency (bump did fetch it, restart didn't — restart now does too).
"""

import asyncio
import functools
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AgentDispatchService:
    """Gathers dispatch context and creates/tracks an agent for a task."""

    @staticmethod
    def get_phase_cli_config(session, phase_id: Optional[str]) -> Dict[str, Any]:
        """Returns phase CLI config + working_directory for a phase, or all-None defaults."""
        from src.core.database import Phase

        result: Dict[str, Any] = {
            "cli_tool": None,
            "cli_model": None,
            "glm_token_env": None,
            "thinking_level": None,
            "working_directory": None,
        }
        if not phase_id:
            return result
        phase = session.query(Phase).filter_by(id=phase_id).first()
        if phase:
            result["cli_tool"] = phase.cli_tool
            result["cli_model"] = phase.cli_model
            result["glm_token_env"] = phase.glm_api_token_env
            result["thinking_level"] = phase.thinking_level
            result["working_directory"] = phase.working_directory
        return result

    @staticmethod
    async def build_dispatch_context(
        task_description_for_rag: str,
        phase_id: Optional[str],
        requesting_agent_id: str = "system",
        explicit_working_directory: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Gather everything needed to create an agent for a task.

        explicit_working_directory takes precedence over the phase's
        working_directory (used by create_task, which honors an
        explicit request.cwd before falling back to the phase).
        """
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        # get_project_context() (DB reads) and retrieve_for_task() (embedding
        # + vector search) don't read each other's output -- the phase-context
        # merge below only needs project_context, and is itself synchronous.
        project_context, context_memories = await asyncio.gather(
            server_state.agent_manager.get_project_context(),
            server_state.rag_system.retrieve_for_task(
                task_description=task_description_for_rag,
                requesting_agent_id=requesting_agent_id,
            ),
        )
        # get_phase_context (a Phase query) and _assemble_dispatch_dict (its
        # own Phase query, for cli config) are both plain synchronous
        # SQLAlchemy calls -- offloaded together since neither is async I/O
        # and nothing after this point needs the event loop until the
        # returned dict is used.
        def _finalize_dispatch_dict_sync():
            final_project_context = project_context
            if phase_id and server_state.phase_manager:
                phase_context = server_state.phase_manager.get_phase_context(phase_id)
                if phase_context:
                    final_project_context = (
                        f"{project_context}\n\n{phase_context.to_prompt_context()}"
                    )

            # FIX #6: Pass explicit_working_directory (may be None) to the assembler
            # so the phase's configured working_directory can be used as fallback.
            # Do NOT resolve to os.getcwd() here — that shadows the phase config.
            return AgentDispatchService._assemble_dispatch_dict(
                project_context=final_project_context,
                context_memories=context_memories,
                working_directory=explicit_working_directory,
                phase_id=phase_id,
            )

        return await asyncio.get_event_loop().run_in_executor(None, _finalize_dispatch_dict_sync)

    @staticmethod
    def _assemble_dispatch_dict(
        project_context: str,
        context_memories: list,
        working_directory: str,
        phase_id: Optional[str],
    ) -> Dict[str, Any]:
        """Shared assembly for dispatch context dicts.

        FIX #6: Extracted to eliminate duplication between
        build_dispatch_context and build_dispatch_context_from_existing.
        """
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        with server_state.db_manager.session_scope() as session:
            cli_config = AgentDispatchService.get_phase_cli_config(session, phase_id)

        # Phase working_directory is a fallback if caller didn't provide one
        effective_working_directory = (
            working_directory
            or cli_config["working_directory"]
            or os.getcwd()
        )

        return {
            "project_context": project_context,
            "context_memories": context_memories,
            "working_directory": effective_working_directory,
            "phase_cli_tool": cli_config["cli_tool"],
            "phase_cli_model": cli_config["cli_model"],
            "phase_glm_token_env": cli_config["glm_token_env"],
            "phase_thinking_level": cli_config["thinking_level"],
        }

    @staticmethod
    async def build_dispatch_context_from_existing(
        memories: list,
        project_context: str,
        working_directory: str,
        phase_id: Optional[str],
    ) -> Dict[str, Any]:
        """Like build_dispatch_context, but reuses already-fetched RAG
        memories and project context instead of re-fetching them.

        create_task dispatches using the same context it enriched with
        (unlike process_queue, which re-fetches after enrichment) — this
        variant only adds the phase CLI config lookup on top of what the
        caller already has.
        """
        # FIX #6: Delegate to _assemble_dispatch_dict. Offloaded: it does its
        # own synchronous Phase query for cli config, which would otherwise
        # block the event loop directly inside this async function.
        return await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(
                AgentDispatchService._assemble_dispatch_dict,
                project_context=project_context,
                context_memories=memories,
                working_directory=working_directory,
                phase_id=phase_id,
            ),
        )

    @staticmethod
    async def dispatch(task, enriched_data: Dict[str, Any], dispatch_context: Dict[str, Any]):
        """Create an agent for task using the gathered dispatch context."""
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        return await server_state.agent_manager.create_agent_for_task(
            task=task,
            enriched_data=enriched_data,
            memories=dispatch_context["context_memories"],
            project_context=dispatch_context["project_context"],
            working_directory=dispatch_context["working_directory"],
            phase_cli_tool=dispatch_context["phase_cli_tool"],
            phase_cli_model=dispatch_context["phase_cli_model"],
            phase_glm_token_env=dispatch_context["phase_glm_token_env"],
            phase_thinking_level=dispatch_context["phase_thinking_level"],
        )

    @staticmethod
    def mark_assigned(
        task_id: str,
        agent_id: str,
        status: str = "assigned",
        session=None,
    ) -> None:
        """Update Task.assigned_agent_id/status/started_at after a successful dispatch.

        FIX #12: Added rollback on commit failure.
        FIX #16: Accept optional session to avoid double-session issue.
        """
        from src.core.app_context import get_app_state
        from src.core.database import Task

        server_state = get_app_state()

        owns_session = session is None
        if owns_session:
            session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if task:
                task.assigned_agent_id = agent_id
                task.status = status
                task.started_at = datetime.utcnow()
                session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if owns_session:
                session.close()
