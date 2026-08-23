"""Task enrichment: resolving a task's phase and running it through the
RAG + LLM enrichment pipeline.

Extracted from src/mcp/server.py, where this exact sequence (resolve
phase_id, fetch phase context, retrieve RAG memories, get project context,
call llm_provider.enrich_task, normalize the result) was independently
duplicated in create_task's process_task_async closure and in
process_queue — see docs/SOLID_OO_REVIEW.md findings 1.2/1.3.
"""

import asyncio
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
        """Resolve a phase_id that may be a UUID, a phase NAME (e.g.
        "adversarial_review" -- the natural, common way an agent names a
        phase when creating a follow-up task), a digit-string order
        number, or absent (in which case phase_order/current phase is
        used).

        This is the canonical "order vs UUID" resolution — previously
        reimplemented independently at every call site (SOLID review 1.4).

        The name-lookup branch was added 2026-08-19 after a live FK
        violation: this function's non-digit branch used to return
        phase_id_raw completely unvalidated, silently assuming any
        non-digit string was already a real Phase.id UUID. An agent
        passing a phase NAME (very much the common case -- "adversarial_
        review" is far more natural to write than its UUID) sailed
        through unresolved, and the later `UPDATE tasks SET phase_id=...`
        failed with `FOREIGN KEY constraint failed` -- silently, since
        that write happens inside a fire-and-forget background task whose
        own failure handler (_handle_task_processing_failure) then failed
        too (a separate, also-fixed session-leak bug), leaving the task
        permanently stuck at status="pending" with no phase_id and no
        error visible anywhere the agent or a human would see it.
        """
        from src.core.app_context import get_app_state
        from src.core.database import Phase

        server_state = get_app_state()

        if phase_id_raw and str(phase_id_raw).isdigit():
            return server_state.phase_manager.get_phase_for_task(
                phase_id=None,
                order=int(phase_id_raw),
                requesting_agent_id=requesting_agent_id,
                workflow_id=workflow_id,
            )
        elif phase_id_raw:
            with server_state.db_manager.session_scope() as session:
                if session.query(Phase).filter_by(id=phase_id_raw).first():
                    return phase_id_raw

                # Not a real Phase.id -- try it as a phase NAME instead.
                # Scope to the resolved workflow when we have one: phase
                # names are not unique across different workflow
                # definitions (e.g. "development" appears in more than
                # one), so an unscoped lookup could silently match the
                # wrong workflow's phase.
                resolved_workflow_id = workflow_id or server_state.phase_manager.workflow_id
                query = session.query(Phase).filter_by(name=phase_id_raw)
                if resolved_workflow_id:
                    query = query.filter_by(workflow_id=resolved_workflow_id)
                matches = query.all()
                if len(matches) == 1:
                    return matches[0].id
                if len(matches) > 1:
                    logger.warning(
                        f"resolve_phase_id: phase name {phase_id_raw!r} matched "
                        f"{len(matches)} phases (workflow_id={resolved_workflow_id!r} "
                        "did not narrow it to one) -- refusing to guess"
                    )
                    return None

            logger.warning(
                f"resolve_phase_id: {phase_id_raw!r} is neither a known Phase.id "
                "nor a phase name resolvable in this workflow"
            )
            return None
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
    async def gather_dispatch_context(
        raw_description: str,
        requesting_agent_id: str,
        phase_context_str: str = "",
        phase_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """The RAG-memory + project-context half of enrich(), split out so a
        task that was already enriched once (its enriched_description is
        already persisted) can get fresh dispatch context WITHOUT re-running
        the LLM description rewrite below -- used when a dependency-gated
        task is promoted and dispatched later than its original creation
        (see _create_task_steps._dispatch_ready_dependents). Re-running
        enrich_task there would rewrite an already-good description a second
        time for no benefit and a real LLM-call cost.

        phase_id: resolved to project_id/phase_name (Phase.workflow_id ->
        Workflow.project_id) so get_project_context() can inject the
        multi-repo section (REQ-17/18/21). None (the default) -- callers
        with no phase_id in scope -- behaves exactly as before this param
        existed.

        Returns a dict with `context_memories` (list of RAG memory dicts)
        and `project_context` (str, with phase context appended).
        """
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        project_id = None
        phase_name = None
        if phase_id:
            from src.core.database import Phase, Workflow

            with server_state.db_manager.session_scope() as _session:
                phase = _session.query(Phase).filter_by(id=phase_id).first()
                if phase:
                    phase_name = phase.name
                    if phase.workflow_id:
                        wf = _session.query(Workflow).filter_by(id=phase.workflow_id).first()
                        if wf:
                            project_id = wf.project_id

        # retrieve_for_task() (embedding + vector search) and
        # get_project_context() (DB reads) don't read each other's output --
        # the phase-context append below only needs project_context.
        context_memories, project_context = await asyncio.gather(
            server_state.rag_system.retrieve_for_task(
                task_description=raw_description,
                requesting_agent_id=requesting_agent_id,
            ),
            server_state.agent_manager.get_project_context(project_id=project_id, phase_name=phase_name),
        )
        if phase_context_str:
            project_context = f"{project_context}\n\n{phase_context_str}"

        return {"context_memories": context_memories, "project_context": project_context}

    @staticmethod
    async def enrich(
        raw_description: str,
        done_definition: Optional[str],
        phase_context_str: str,
        requesting_agent_id: str,
        phase_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the RAG + LLM enrichment pipeline for a task description.

        Returns a dict with:
            enriched_task: dict from llm_provider.enrich_task, normalized
            context_memories: list of RAG memory dicts
            project_context: project context string (with phase context appended)
        """
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        dispatch_context = await TaskEnrichmentService.gather_dispatch_context(
            raw_description, requesting_agent_id, phase_context_str, phase_id=phase_id
        )
        context_memories = dispatch_context["context_memories"]
        project_context = dispatch_context["project_context"]

        context_strings = [mem.get("content", "") for mem in context_memories]
        enriched_task = await server_state.llm_provider.enrich_task(
            task_description=raw_description,
            done_definition=done_definition or "Task completed successfully",
            context=context_strings,
            phase_context=phase_context_str if phase_context_str else None,
        )
        # Guard against None return from LLM parser
        if not enriched_task:
            logger.warning("enrich_task returned None, using fallback")
            enriched_task = {
                "enriched_description": raw_description,
                "completion_criteria": [done_definition or "Task completed successfully"],
                "agent_prompt": f"Complete this task: {raw_description}",
                "required_capabilities": ["general"],
                "estimated_complexity": 5,
            }
        TaskEnrichmentService._normalize_enriched_description(
            enriched_task, raw_description
        )

        return {
            "enriched_task": enriched_task,
            "context_memories": context_memories,
            "project_context": project_context,
        }
