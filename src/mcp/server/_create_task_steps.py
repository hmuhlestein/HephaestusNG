"""Named steps for create_task -- extracted from agent_task_routes.py's
create_task god-function (design_docs/phase_1c_server_decomposition.md exit
criteria: create_task must be decomposed into named steps, neither it nor
update_task_status exceeding ~150 lines after the split).

Each function below is a verbatim-logic extraction of one section of the
original create_task body -- behavior-preserving, not a rewrite. See
agent_task_routes.py's create_task for the orchestrator that calls these
in sequence.
"""

import logging
import os
from difflib import SequenceMatcher
from typing import Any, Dict, Optional

from fastapi import HTTPException

from src.core.database import Phase, Task
from src.core.simple_config import get_config
from src.mcp.server._shared import (
    CreateTaskRequest,
    CreateTaskResponse,
    _resolve_agent_current_phase,
    is_sdk_or_root_agent,
    server_state,
)

logger = logging.getLogger("src.mcp.server._create_task_steps")


def _enforce_ticket_tracking_requirement(agent_id: str, request: CreateTaskRequest) -> None:
    """Reject the request if ticket tracking is enabled system-wide and this
    caller isn't exempt (SDK/root agent, or a phase agent seeding its own
    workflow's task) but provided no ticket_id."""
    session = server_state.db_manager.get_session()
    try:
        from src.core.database import BoardConfig

        has_ticket_tracking = session.query(BoardConfig).first() is not None

        if has_ticket_tracking and not request.ticket_id:
            is_sdk_agent = is_sdk_or_root_agent(agent_id)
            is_phase_agent = request.workflow_id is not None and request.phase_id is not None

            if not is_sdk_agent and not is_phase_agent:
                session.close()
                raise HTTPException(
                    status_code=400,
                    detail="Ticket tracking is enabled. MCP agents MUST provide ticket_id. "
                    "Create a ticket first using create_ticket, then use that ticket_id here. "
                    "Only SDK/root agents can create tasks without tickets.",
                )
    finally:
        if session.is_active:
            session.close()


def _resolve_dedup_phase_id(agent_id: str, request: CreateTaskRequest) -> Optional[str]:
    """Validate/auto-resolve request.phase_id in place for workflow tasks,
    then resolve the dedup-check phase id (which may come from phase_order,
    or from phase_id itself when it's a digit string -- the MCP create_task
    tool sends phase order numbers through the phase_id field). Returns the
    resolved dedup_phase_id, or None if this isn't a workflow task."""
    if not request.workflow_id:
        return None

    logger.info(f"[CREATE_TASK] phase_id={repr(request.phase_id)}, phase_order={repr(request.phase_order)}")

    if not request.phase_id and not request.phase_order:
        resolved_phase = _resolve_agent_current_phase(agent_id, request.workflow_id)
        if resolved_phase:
            logger.info(f"[CREATE_TASK] Auto-resolved phase_id for agent {agent_id[:8]}: {resolved_phase}")
            request.phase_id = resolved_phase
        else:
            logger.error(f"[CREATE_TASK] REJECTED: no phase_id for workflow {request.workflow_id}")
            raise HTTPException(
                status_code=400,
                detail=f"phase_id or phase_order is REQUIRED for workflow tasks. Agent {agent_id} must provide phase_id when workflow_id is set.",
            )
    if request.phase_id in ("None", "none", "null", ""):
        resolved_phase = _resolve_agent_current_phase(agent_id, request.workflow_id)
        if resolved_phase:
            logger.info(f"[CREATE_TASK] Auto-resolved invalid phase_id for agent {agent_id[:8]}: {resolved_phase}")
            request.phase_id = resolved_phase
        else:
            logger.error(f"[CREATE_TASK] REJECTED: invalid phase_id={repr(request.phase_id)}")
            raise HTTPException(
                status_code=400,
                detail=f"phase_id cannot be None/null/empty string. Agent {agent_id} must provide a valid phase_id.",
            )

    dedup_phase_id = request.phase_id
    phase_order_to_resolve = request.phase_order
    if not phase_order_to_resolve and dedup_phase_id and str(dedup_phase_id).isdigit():
        phase_order_to_resolve = int(dedup_phase_id)
    if phase_order_to_resolve and (not dedup_phase_id or str(dedup_phase_id).isdigit()):
        from src.core.database import Phase as PhaseModel

        _s = server_state.db_manager.get_session()
        try:
            _phase = _s.query(PhaseModel).filter_by(workflow_id=request.workflow_id, order=phase_order_to_resolve).first()
            if _phase:
                dedup_phase_id = _phase.id
        finally:
            _s.close()
    return dedup_phase_id


def _check_duplicate_active_task_for_phase(
    request: CreateTaskRequest, dedup_phase_id: Optional[str]
) -> Optional[CreateTaskResponse]:
    """Content-aware dedup: if the phase already has a near-identical active
    task, return the existing task's response so the caller can short-circuit
    instead of creating a duplicate. Returns None if no dedup match (or no
    dedup_phase_id to check)."""
    if not dedup_phase_id:
        return None

    _s = server_state.db_manager.get_session()
    try:
        from src.core.database import Task as TaskModel

        existing = (
            _s.query(TaskModel)
            .filter(
                TaskModel.phase_id == dedup_phase_id,
                TaskModel.workflow_id == request.workflow_id,
                TaskModel.status.in_(["pending", "assigned", "in_progress", "queued"]),
            )
            .first()
        )
        if existing:
            # Matching on phase_id alone would silently swallow every
            # genuinely-different task submitted while one was already
            # active -- only treat it as a real duplicate if the
            # description is actually the same.
            similarity = SequenceMatcher(
                None,
                (existing.raw_description or "")[:500],
                request.task_description[:500],
            ).ratio()
            if similarity >= 0.85:
                logger.info(f"[CREATE_TASK] Dedup: phase already has near-identical active task {existing.id[:8]} (similarity={similarity:.2f}), returning it")
                _s.close()
                return CreateTaskResponse(
                    task_id=existing.id,
                    enriched_description=existing.enriched_description or existing.raw_description,
                    assigned_agent_id=existing.assigned_agent_id or "unassigned",
                    estimated_completion_time=30,
                    status="queued",
                )
            logger.info(f"[CREATE_TASK] Phase has active task {existing.id[:8]} but new description differs (similarity={similarity:.2f}) — creating a new task rather than deduping")
    finally:
        if _s.is_active:
            _s.close()
    return None


def _guard_phase_ownership(agent_id: str, request: CreateTaskRequest, dedup_phase_id: Optional[str]) -> None:
    """Reject a phase agent seeding the FIRST task for a phase other than its
    own -- agents have no reliable way to know a workflow's real phase
    order/names, and guessing wrong here has created tasks with content for
    the wrong phase. Agents with no currently-assigned task (SDK/root/system
    agents bootstrapping a workflow) are exempt."""
    if not dedup_phase_id:
        return

    _s = server_state.db_manager.get_session()
    try:
        from src.core.database import Phase as PhaseModel
        from src.core.database import Task as TaskModel

        own_task = (
            _s.query(TaskModel)
            .filter(
                TaskModel.assigned_agent_id == agent_id,
                TaskModel.workflow_id == request.workflow_id,
            )
            .order_by(TaskModel.created_at.desc())
            .first()
        )
        if own_task and own_task.phase_id:
            own_phase = _s.query(PhaseModel).filter_by(id=own_task.phase_id).first()
            target_phase = _s.query(PhaseModel).filter_by(id=dedup_phase_id).first()
            if own_phase and target_phase and own_phase.order != target_phase.order:
                logger.error(
                    f"[CREATE_TASK] REJECTED: agent {agent_id[:8]} (own phase "
                    f"'{own_phase.name}', order {own_phase.order}) tried to seed "
                    f"the first task for phase '{target_phase.name}' "
                    f"(order {target_phase.order})"
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Refusing to create a task for phase '{target_phase.name}' "
                        f"(order {target_phase.order}) — you are working phase "
                        f"'{own_phase.name}' (order {own_phase.order}). Only create "
                        "subtasks within your OWN current phase. The orchestrator "
                        "automatically creates the next phase's task, with the "
                        "correct name and required output, once you mark your own "
                        "task done — do not try to create it yourself."
                    ),
                )
    finally:
        if _s.is_active:
            _s.close()


def _resolve_task_repo_id(session, request: CreateTaskRequest) -> Optional[str]:
    """Resolve + validate this task's repo_id (REQ-19/WARNING-1/BLOCKER --
    see architecture.md's "Feature.repo_id + create_task repo/feature
    validation" component). Never guesses across projects: an explicit
    repo_id is validated to belong to the task's own project BEFORE the
    Task row is persisted, and a Feature/task repo_id mismatch is rejected
    outright. Returns the resolved repo_id (possibly None -- single-repo
    projects/no workflow are unaffected, byte-identical to before this
    change). Raises HTTPException(400) on any validation failure.
    """
    from src.core.database import Feature, Workflow
    from src.core.repo_resolution import RepoNotFoundError, repo_id_for_path, resolve_repo_path

    if not request.workflow_id:
        return request.repo_id

    wf = session.query(Workflow).filter_by(id=request.workflow_id).first()
    if not wf or not wf.project_id:
        return request.repo_id
    project_id = wf.project_id

    resolved_repo_id = request.repo_id
    if resolved_repo_id is None and request.cwd:
        resolved_repo_id = repo_id_for_path(session, project_id, request.cwd)

    if resolved_repo_id is not None:
        try:
            resolve_repo_path(session, project_id, resolved_repo_id)
        except RepoNotFoundError:
            raise HTTPException(400, "repo_id does not belong to this project")
        except ValueError as e:
            raise HTTPException(400, str(e))

    feature = None
    if wf.feature_id:
        feature = session.query(Feature).filter_by(id=wf.feature_id).first()

    if feature is not None:
        if feature.repo_id and resolved_repo_id and feature.repo_id != resolved_repo_id:
            raise HTTPException(
                400,
                f"task repo_id {resolved_repo_id} conflicts with this feature's assigned "
                f"repo {feature.repo_id} -- every task under one Feature must share its repo (REQ-19)",
            )
        if feature.repo_id and not resolved_repo_id:
            resolved_repo_id = feature.repo_id
        elif not feature.repo_id and resolved_repo_id:
            feature.repo_id = resolved_repo_id

    return resolved_repo_id


def _persist_new_task(agent_id: str, request: CreateTaskRequest, task_id: str) -> None:
    """Create the initial task row (pending status), auto-creating the
    created_by_agent_id Agent FK row if it doesn't exist yet."""
    # try/finally around the whole body: a failure partway through (e.g. an
    # IntegrityError on session.add/flush/commit) previously propagated with
    # the session never closed or rolled back, leaking a connection holding
    # a failed, uncommitted transaction -- the same leak class documented on
    # _apply_enrichment_to_task below.
    session = server_state.db_manager.get_session()
    try:
        resolved_phase_id = request.phase_id
        if request.phase_id:
            if not session.query(Phase).filter_by(id=request.phase_id).first():
                resolved_phase_id = None
        from src.core.database import Agent

        if not session.query(Agent).filter_by(id=agent_id).first():
            session.add(
                Agent(
                    id=agent_id,
                    system_prompt="auto-created by create_task",
                    status="idle",
                    cli_type="system",
                )
            )
            session.flush()
        resolved_repo_id = _resolve_task_repo_id(session, request)
        task = Task(
            id=task_id,
            raw_description=request.task_description,
            enriched_description=f"[Processing] {request.task_description}",
            done_definition=request.done_definition,
            status="pending",
            priority=request.priority,
            parent_task_id=request.parent_task_id,
            created_by_agent_id=agent_id,
            phase_id=resolved_phase_id,
            workflow_id=request.workflow_id,
            estimated_complexity=5,
            ticket_id=request.ticket_id,
            depends_on=request.depends_on,
            parallel_group=request.parallel_group,
            max_concurrent=request.max_concurrent or 1,
            repo_id=resolved_repo_id,
        )
        session.add(task)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _apply_ticket_blocking_if_needed(request: CreateTaskRequest, task_id: str) -> Optional[dict]:
    """If the task's ticket is blocked, mark the task blocked immediately and
    broadcast. Returns the early-return response dict if blocked, else None."""
    if not request.ticket_id:
        return None

    from src.services.task_blocking_service import TaskBlockingService

    blocking_info = TaskBlockingService.check_task_blocked(task_id)

    if not blocking_info["is_blocked"]:
        return None

    logger.info(f"Task {task_id} associated with blocked ticket {request.ticket_id}. Marking task as 'blocked'. Blocked by: {blocking_info['blocking_ticket_ids']}")

    session = server_state.db_manager.get_session()
    try:
        task_obj = session.query(Task).filter_by(id=task_id).first()
        if task_obj:
            task_obj.status = "blocked"
            blocker_titles = [t["title"] for t in blocking_info["blocking_tickets"]]
            task_obj.completion_notes = f"Blocked by tickets: {', '.join(blocker_titles)}"
            session.commit()
    finally:
        session.close()

    from src.core.database import resolve_project_for_workflow

    bcast_project_id, bcast_project_name = resolve_project_for_workflow(request.workflow_id)
    await server_state.broadcast_update(
        {
            "type": "task_blocked",
            "task_id": task_id,
            "description": request.task_description[:200],
            "blocking_tickets": blocking_info["blocking_ticket_ids"],
        },
        project_id=bcast_project_id,
        project_name=bcast_project_name,
    )

    return {
        "task_id": task_id,
        "enriched_description": request.task_description,
        "assigned_agent_id": "none",
        "estimated_completion_time": 0,
        "status": "blocked",
    }


async def _resolve_phase_and_enrich(request: CreateTaskRequest, agent_id: str) -> Dict[str, Any]:
    """Determine phase (if a workflow is active), working directory, and run
    LLM enrichment (shared with process_queue -- see TaskEnrichmentService).
    Returns a dict of the values later steps need."""
    from src.services.task_enrichment_service import TaskEnrichmentService

    phase_id = request.phase_id
    workflow_id = None
    phase_context_str = ""

    target_workflow_id = request.workflow_id or server_state.phase_manager.workflow_id
    if target_workflow_id:
        phase_id = TaskEnrichmentService.resolve_phase_id(
            phase_id_raw=request.phase_id,
            phase_order=request.phase_order,
            workflow_id=request.workflow_id,
            requesting_agent_id=agent_id,
        )
        if phase_id:
            phase_context_str, ctx_workflow_id = TaskEnrichmentService.get_phase_context_str(phase_id)
            if ctx_workflow_id:
                workflow_id = ctx_workflow_id
        else:
            logger.warning("No phase_id determined for task")
    else:
        logger.warning("No active workflow in phase_manager")

    working_directory = request.cwd
    if not working_directory and phase_id:
        session = server_state.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if phase and phase.working_directory:
                working_directory = phase.working_directory
        finally:
            session.close()
    if not working_directory:
        working_directory = os.getcwd()

    enrichment_result = await TaskEnrichmentService.enrich(
        raw_description=request.task_description,
        done_definition=request.done_definition,
        phase_context_str=phase_context_str,
        requesting_agent_id=agent_id,
        phase_id=phase_id,
    )

    return {
        "phase_id": phase_id,
        "workflow_id": workflow_id,
        "working_directory": working_directory,
        "enriched_task": enrichment_result["enriched_task"],
        "context_memories": enrichment_result["context_memories"],
        "project_context": enrichment_result["project_context"],
    }


def _apply_enrichment_to_task(
    task_id: str, request: CreateTaskRequest, phase_id: Optional[str], workflow_id: Optional[str], enriched_task: dict
) -> Optional[dict]:
    """Write enriched fields back to the task row, inheriting phase
    validation if enabled. Returns the task_data dict later steps need, or
    None if the task row is gone (log + let caller stop)."""
    # try/finally around the whole body, not just the early "task not
    # found" return: a commit() failure (e.g. the FK violation a
    # not-yet-resolved phase_id used to cause) previously propagated out
    # of this function with the session never closed or rolled back --
    # leaking a connection holding a failed, uncommitted transaction. That
    # leaked connection is a strong candidate for why the caller's own
    # failure-recovery write (_handle_task_processing_failure, marking
    # the task "failed") then silently failed too, leaving the task
    # stuck at "pending" forever with no error visible anywhere.
    session = server_state.db_manager.get_session()
    try:
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            logger.error(f"Task {task_id} not found after creation")
            return None

        enriched_desc = enriched_task["enriched_description"]
        if isinstance(enriched_desc, dict):
            import json

            enriched_desc = json.dumps(enriched_desc, indent=2)
        task.enriched_description = enriched_desc
        task.phase_id = phase_id
        # Prioritize request.workflow_id for multi-workflow support, fallback to phase context
        task.workflow_id = request.workflow_id or workflow_id
        task.estimated_complexity = enriched_task.get("estimated_complexity", 5)

        if phase_id:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if phase and phase.validation:
                if phase.validation.get("enabled", True):
                    task.validation_enabled = True
                    logger.info(f"Task {task_id} inheriting validation from phase {phase.name}")
                else:
                    logger.info(f"Task {task_id} validation explicitly disabled in phase {phase.name}")

        session.commit()

        return {
            "id": task_id,
            "raw_description": request.task_description,
            "enriched_description": enriched_task["enriched_description"],
            "done_definition": request.done_definition,
            "phase_id": phase_id,
            "workflow_id": request.workflow_id,
        }
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def _check_for_duplicate_task(task_id: str, phase_id: Optional[str], enriched_task: dict) -> bool:
    """Embedding-based dedup (only if enabled and services are available).
    Returns True if the task was marked duplicated (caller should stop)."""
    if not (server_state.embedding_service and server_state.task_similarity_service and get_config().task_dedup.task_dedup_enabled):
        return False

    try:
        task_embedding = await server_state.embedding_service.generate_embedding(enriched_task["enriched_description"])

        duplicate_info = await server_state.task_similarity_service.check_for_duplicates(
            enriched_task["enriched_description"],
            task_embedding,
            phase_id=phase_id,
        )

        if duplicate_info["is_duplicate"]:
            session = server_state.db_manager.get_session()
            try:
                task = session.query(Task).filter_by(id=task_id).first()
                if task:
                    task.status = "duplicated"
                    task.duplicate_of_task_id = duplicate_info["duplicate_of"]
                    task.similarity_score = duplicate_info["max_similarity"]
                    session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

            logger.warning(f"Task {task_id} detected as duplicate of {duplicate_info['duplicate_of']} with similarity {duplicate_info['max_similarity']:.3f}")
            return True

        await server_state.task_similarity_service.store_task_embedding(
            task_id,
            task_embedding,
            related_tasks_details=duplicate_info.get("related_tasks_details", []),
        )

        if duplicate_info.get("related_tasks"):
            logger.info(f"Task {task_id} has {len(duplicate_info['related_tasks'])} related tasks")

    except Exception as e:
        logger.error(f"Failed to check for duplicates: {e}")
        # Continue without deduplication on error

    return False


async def _maybe_queue_task_at_capacity(task_id: str, workflow_id: Optional[str], enriched_task: dict) -> bool:
    """If the server is at global capacity, enqueue the task and broadcast.
    Returns True if queued (caller should stop, don't dispatch an agent)."""
    if not server_state.queue_service.should_queue_task():
        return False

    server_state.queue_service.enqueue_task(task_id)
    queue_status = server_state.queue_service.get_queue_status()

    from src.core.database import resolve_project_for_workflow

    bcast_project_id, bcast_project_name = resolve_project_for_workflow(workflow_id)
    await server_state.broadcast_update(
        {
            "type": "task_queued",
            "task_id": task_id,
            "description": enriched_task["enriched_description"][:200],
            "queue_position": queue_status.get("queued_tasks_count", 0),
            "slots_available": queue_status.get("slots_available", 0),
        },
        project_id=bcast_project_id,
        project_name=bcast_project_name,
    )

    logger.info(f"Task {task_id} queued (at capacity: {queue_status['active_agents']}/{queue_status['max_concurrent_agents']} agents)")
    return True


def _has_unmet_dependencies(depends_on) -> bool:
    """True if `depends_on` names at least one task that is not yet "done".

    depends_on entries referencing an unknown task id (deleted, typo,
    cross-workflow id that never existed) count as unmet -- fail closed. A
    vanished dependency is not the same as a satisfied one, and dispatching
    anyway would silently defeat the ordering the caller asked for.
    """
    if not depends_on:
        return False
    session = server_state.db_manager.get_session()
    try:
        for dep_id in depends_on:
            dep_status = (
                session.query(Task.status).filter_by(id=dep_id).first()
            )
            if dep_status is None or dep_status[0] != "done":
                return True
        return False
    finally:
        session.close()


async def _dispatch_ready_dependents(completed_task_id: str, workflow_id: Optional[str]) -> None:
    """When a task finishes, dispatch any sibling task whose `depends_on`
    named it -- the promotion half of the gate _has_unmet_dependencies
    enforces at creation.

    Regression this closes: create_task wrote depends_on/parallel_group to
    the row and then dispatched (or capacity-queued) the new task almost
    immediately regardless of what they said -- nothing ever read
    depends_on again after the write. architecture_design.yaml's prompt
    teaches agents to build dependency chains with these fields
    (`parallel_group: "types"` finishing before `parallel_group:
    "handlers"` starts), but the chain was purely advisory: a "handlers"
    task dispatched the instant it was created, whether or not "types" had
    finished.

    Scoped to same-workflow siblings still in "pending" -- a task that
    already dispatched, failed, or belongs to a different workflow is not a
    candidate. A FAILED dependency is deliberately never treated as
    satisfying anything downstream: its dependents stay pending rather than
    proceeding on top of a known-broken prerequisite. That is a real
    limitation (a permanently stuck chain needs a human or a retry to
    unstick), not an oversight -- proceeding anyway is the less safe
    default.

    Fired as a background asyncio task from update_task_status on
    status=="done", the same fire-and-forget shape
    terminate_agents_and_process_queue already uses there.
    """
    if not workflow_id:
        return

    session = server_state.db_manager.get_session()
    try:
        candidates = (
            session.query(Task)
            .filter(Task.workflow_id == workflow_id, Task.status == "pending")
            .all()
        )
        # Snapshot the fields each candidate needs before the session that
        # produced them closes -- dispatch below does its own session work
        # per candidate and must not hold this one open across it.
        ready = []
        for t in candidates:
            depends_on = t.depends_on or []
            if completed_task_id not in depends_on:
                continue
            if _has_unmet_dependencies(depends_on):
                continue  # this dependency cleared, but another sibling hasn't
            ready.append(
                {
                    "id": t.id,
                    "raw_description": t.raw_description,
                    "enriched_description": t.enriched_description,
                    "done_definition": t.done_definition,
                    "phase_id": t.phase_id,
                    "workflow_id": t.workflow_id,
                    "created_by_agent_id": t.created_by_agent_id,
                }
            )
    finally:
        session.close()

    for task_data in ready:
        try:
            await _dispatch_or_queue_promoted_task(task_data)
        except Exception as e:
            # One sibling failing to dispatch must not stop the others --
            # each is an independent task that will surface its own error
            # (or sit pending for the next promotion event / manual retry)
            # rather than silently blocking unrelated dependents.
            logger.error(
                f"[DEPENDENCY-PROMOTE] Failed to dispatch {task_data['id']} "
                f"after its dependencies cleared: {e}"
            )


async def _dispatch_or_queue_promoted_task(task_data: dict) -> None:
    """Dispatch one dependency-cleared task, or capacity-queue it if the
    server has no free slot right now -- the same decision
    process_task_async makes for a freshly-created, already-ready task,
    reused here so a promoted task is subject to the identical capacity
    rules rather than a bespoke path that could disagree with them.

    Does NOT re-run TaskEnrichmentService.enrich(): task_data's
    enriched_description was already produced (and is already persisted)
    at creation time. Re-enriching here would rewrite an already-good
    description a second time for no benefit and a real LLM-call cost.
    Only the dispatch-time context (RAG memories, project context) is
    gathered fresh, via TaskEnrichmentService.gather_dispatch_context.
    """
    from src.services.task_enrichment_service import TaskEnrichmentService

    task_id = task_data["id"]
    phase_id = task_data["phase_id"]
    agent_id = task_data["created_by_agent_id"]

    working_directory = None
    if phase_id:
        session = server_state.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if phase and phase.working_directory:
                working_directory = phase.working_directory
        finally:
            session.close()
    if not working_directory:
        working_directory = os.getcwd()

    dispatch_context = await TaskEnrichmentService.gather_dispatch_context(
        raw_description=task_data["raw_description"],
        requesting_agent_id=agent_id,
    )

    enriched_task = {"enriched_description": task_data["enriched_description"]}

    if await _maybe_queue_task_at_capacity(task_id, task_data["workflow_id"], enriched_task):
        return

    agent = await _dispatch_agent_for_task(
        task_id,
        task_data,
        agent_id,
        task_data["workflow_id"],
        enriched_task,
        dispatch_context["context_memories"],
        dispatch_context["project_context"],
        working_directory,
    )
    if agent is None:
        return  # queued by the per-cli/model concurrency gate instead

    await _finalize_task_dispatch(task_id, task_data, agent, enriched_task)
    logger.info(
        f"[DEPENDENCY-PROMOTE] Dispatched {task_id} -- its dependencies just cleared"
    )


async def _dispatch_agent_for_task(
    task_id: str,
    task_data: dict,
    agent_id: str,
    workflow_id: Optional[str],
    enriched_task: dict,
    context_memories,
    project_context,
    working_directory: str,
):
    """Build dispatch context, apply the per-cli/model concurrency gate
    (queueing on the fallback path instead of dispatching into a saturated
    slot), and dispatch. Returns the created Agent, or None if queued
    instead (caller should stop)."""
    from src.services.agent_dispatch_service import AgentDispatchService

    logger.info(f"[CREATE_TASK] Creating agent for task {task_id}")
    logger.info(f"[CREATE_TASK] Task was created by agent: {agent_id}")

    temp_task = Task(
        id=task_id,
        raw_description=task_data["raw_description"],
        enriched_description=task_data["enriched_description"],
        done_definition=task_data["done_definition"],
        phase_id=task_data["phase_id"],
        workflow_id=task_data["workflow_id"],
        created_by_agent_id=agent_id,
    )

    # REQ-18: for a task with repo_id already resolved (see
    # _resolve_task_repo_id/_persist_new_task), state plainly which repo is
    # writable for THIS task -- task-specific, so it belongs here rather
    # than in the task-agnostic get_project_context().
    with server_state.db_manager.session_scope() as _repo_session:
        db_task = _repo_session.query(Task).filter_by(id=task_id).first()
        if db_task and db_task.repo_id:
            from src.core.database import ProjectRepo

            repo = _repo_session.query(ProjectRepo).filter_by(id=db_task.repo_id).first()
            if repo:
                project_context = (
                    f"{project_context}\n\nYour assigned repo for this task: "
                    f"{repo.label} ({repo.path}) -- write here. Other listed repos are "
                    "read-only reference."
                )

    # Dispatch reuses the RAG memories/project context already fetched
    # during enrichment above (unlike process_queue, which re-fetches
    # post-enrichment) -- only the phase CLI config lookup is added here.
    dispatch_context = await AgentDispatchService.build_dispatch_context_from_existing(
        memories=context_memories,
        project_context=project_context,
        working_directory=working_directory,
        phase_id=temp_task.phase_id,
    )

    # Per-cli/model concurrency gate (e.g. a local model's single inference
    # slot) -- the global capacity check upstream only covers the global
    # max_concurrent_agents cap, which says nothing about whether THIS
    # specific combo has a free slot. Dispatch on the fallback model instead
    # if it's saturated; if no fallback is usable, queue the task the same
    # way the global-cap check does, rather than dispatch into a slot that
    # isn't actually free.
    qs = server_state.queue_service
    _reservation = None
    if qs.cli_model_concurrency_limits:
        with qs.db_manager.session_scope() as _qsession:
            _cli_override, _model_override, _reservation, _saturated = qs.resolve_cli_model_dispatch(
                _qsession, temp_task
            )
        if _saturated:
            logger.info(
                f"Task {task_id}'s combo is already at its concurrency limit with no "
                "usable fallback -- queueing instead of dispatching"
            )
            qs.enqueue_task(task_id)
            queue_status = qs.get_queue_status()
            from src.core.database import resolve_project_for_workflow

            bcast_project_id, bcast_project_name = resolve_project_for_workflow(workflow_id)
            await server_state.broadcast_update(
                {
                    "type": "task_queued",
                    "task_id": task_id,
                    "description": task_data["enriched_description"][:200],
                    "queue_position": queue_status.get("queued_tasks_count", 0),
                    "slots_available": queue_status.get("slots_available", 0),
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )
            return None
        if _cli_override:
            logger.info(
                f"Task {task_id}'s primary combo at its concurrency limit -- "
                f"dispatching on fallback model {_model_override} instead"
            )
            dispatch_context["phase_cli_tool"] = _cli_override
            dispatch_context["phase_cli_model"] = _model_override

    # _reservation (if any) must be released once this dispatch attempt
    # finishes, success or not.
    try:
        agent = await AgentDispatchService.dispatch(
            task=temp_task,
            enriched_data=enriched_task,
            dispatch_context=dispatch_context,
        )
    finally:
        if _reservation:
            qs.release_cli_model_slot(*_reservation)

    return agent


async def _finalize_task_dispatch(task_id: str, task_data: dict, agent, enriched_task: dict) -> None:
    """Mark the task assigned and broadcast task_created."""
    from src.core.database import resolve_project_for_workflow
    from src.services.agent_dispatch_service import AgentDispatchService

    agent_id_str = str(agent.id) if agent else None

    AgentDispatchService.mark_assigned(task_id, agent_id_str, status="assigned")

    bcast_project_id, bcast_project_name = resolve_project_for_workflow(task_data["workflow_id"])
    await server_state.broadcast_update(
        {
            "type": "task_created",
            "task_id": task_id,
            "agent_id": agent_id_str,
            "description": enriched_task["enriched_description"][:200],
        },
        project_id=bcast_project_id,
        project_name=bcast_project_name,
    )


async def _handle_task_processing_failure(task_id: str, error: Exception) -> None:
    """Mark the task failed after an unhandled error in background
    processing."""
    logger.error(f"Failed to process task {task_id} in background: {error}")
    # try/except/finally around the whole body: this is the last line of
    # defense in a fire-and-forget background coroutine -- nothing above it
    # catches a failure here. Without cleanup, a commit() failure (e.g. a
    # transient lock) previously leaked a connection holding a failed,
    # uncommitted transaction and left the task stuck at "pending" forever
    # with no error visible anywhere -- exactly the leak
    # _apply_enrichment_to_task's docstring points at this function as the
    # likely victim of.
    session = server_state.db_manager.get_session()
    try:
        task = session.query(Task).filter_by(id=task_id).first()
        if task:
            task.status = "failed"
            task.failure_reason = str(error)
            session.commit()
    except Exception as recovery_error:
        session.rollback()
        logger.error(
            f"Failed to mark task {task_id} as failed during recovery: {recovery_error}"
        )
    finally:
        session.close()


