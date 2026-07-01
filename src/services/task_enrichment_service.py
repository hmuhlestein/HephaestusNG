"""Task enrichment: resolving a task's phase and running it through the
RAG + LLM enrichment pipeline.

Extracted from src/mcp/server.py, where this exact sequence (resolve
phase_id, fetch phase context, retrieve RAG memories, get project context,
call llm_provider.enrich_task, normalize the result) was independently
duplicated in create_task's process_task_async closure and in
process_queue — see docs/SOLID_OO_REVIEW.md findings 1.2/1.3.
"""

import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class TaskEnrichmentService:
    """Resolves a task's phase and enriches its description via LLM."""

    @staticmethod
    def resolve_phase_id(
        phase_id_raw: Optional[str],
        phase_order: Optional[int],
        workflow_id: Optional[str],
        requesting_agent_id: str,
    ) -> Optional[str]:
        """Resolve a phase_id that may be a UUID, a digit-string order
        number, or absent (in which case phase_order/current phase is used).

        This is the canonical "order vs UUID" resolution — previously
        reimplemented independently at every call site (SOLID review 1.4).
        """
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        if phase_id_raw and str(phase_id_raw).isdigit():
            return server_state.phase_manager.get_phase_for_task(
                phase_id=None,
                order=int(phase_id_raw),
                requesting_agent_id=requesting_agent_id,
                workflow_id=workflow_id,
            )
        elif phase_id_raw:
            return phase_id_raw
        else:
            return server_state.phase_manager.get_phase_for_task(
                phase_id=None,
                order=phase_order,
                requesting_agent_id=requesting_agent_id,
                workflow_id=workflow_id,
            )

    @staticmethod
    def get_phase_context_str(phase_id: Optional[str]) -> Tuple[str, Optional[str]]:
        """Returns (phase_context_str, workflow_id_from_phase_context)."""
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        if not phase_id:
            return "", None
        phase_context = server_state.phase_manager.get_phase_context(phase_id)
        if not phase_context:
            return "", None
        return phase_context.to_prompt_context(), phase_context.workflow_id

    @staticmethod
    def _normalize_enriched_description(enriched_task: Dict[str, Any], fallback: str) -> None:
        """Ensure enriched_task['enriched_description'] is always a string, in place."""
        raw_desc = enriched_task.get("enriched_description")
        if raw_desc is None:
            raw_desc = fallback
        elif isinstance(raw_desc, dict):
            raw_desc = json.dumps(raw_desc, indent=2)
        enriched_task["enriched_description"] = str(raw_desc)

    @staticmethod
    async def enrich(
        raw_description: str,
        done_definition: Optional[str],
        phase_context_str: str,
        requesting_agent_id: str,
    ) -> Dict[str, Any]:
        """Run the RAG + LLM enrichment pipeline for a task description.

        Returns a dict with:
            enriched_task: dict from llm_provider.enrich_task, normalized
            context_memories: list of RAG memory dicts
            project_context: project context string (with phase context appended)
        """
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        context_memories = await server_state.rag_system.retrieve_for_task(
            task_description=raw_description,
            requesting_agent_id=requesting_agent_id,
        )

        project_context = await server_state.agent_manager.get_project_context()
        if phase_context_str:
            project_context = f"{project_context}\n\n{phase_context_str}"

        context_strings = [mem.get("content", "") for mem in context_memories]
        enriched_task = await server_state.llm_provider.enrich_task(
            task_description=raw_description,
            done_definition=done_definition or "Task completed successfully",
            context=context_strings,
            phase_context=phase_context_str if phase_context_str else None,
        )
        TaskEnrichmentService._normalize_enriched_description(
            enriched_task, raw_description
        )

        return {
            "enriched_task": enriched_task,
            "context_memories": context_memories,
            "project_context": project_context,
        }
