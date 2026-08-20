"""Memory API routes — save, search, report results, validation review.

Extracted from server.py for better modularity.
"""

import asyncio
import functools
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from src.core.app_context import get_app_state
from src.core.database import (
    Agent,
    AgentResult,
    Memory,
    Task,
    ValidationReview,
    Workflow,
    WorkflowResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["memory", "results"])


def _commit_validated_worktree(worktree_path: str, agent_id: str) -> bool:
    """`git add -A` + commit any validated work in an agent's worktree --
    real subprocess work, called via run_in_executor by
    give_validation_review below. Returns True if a commit was made."""
    from git import Repo

    wt_repo = Repo(worktree_path)
    wt_repo.git.add("-A")
    if not (wt_repo.is_dirty() or wt_repo.untracked_files):
        return False
    wt_repo.git.commit(
        "-m", f"[Agent {agent_id[:8]}] Validated work completed", "--no-verify",
    )
    return True


def _get_server_state():
    """Get server state (lazy import to avoid circular deps)."""
    return get_app_state()


async def _get_project_id_for_agent(agent_id: str) -> Optional[str]:
    """Resolve the project_id for an agent via their current task's workflow."""
    if not agent_id:
        return None
    try:
        server_state = _get_server_state()
        session = server_state.db_manager.get_session()
        try:
            from src.core.database import Task as TaskModel

            task = (
                session.query(TaskModel)
                .filter(TaskModel.assigned_agent_id == agent_id)
                .order_by(TaskModel.created_at.desc())
                .first()
            )
            if task and task.workflow_id:
                wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
                if wf:
                    return wf.project_id
        finally:
            session.close()
    except Exception as e:
        logger.warning(f"Failed to resolve project_id for agent {agent_id}: {e}")
    return None


# ── Request / Response Models ─────────────────────────────────────────


class SaveMemoryRequest(BaseModel):
    """Request model for saving memory."""

    ai_agent_id: str
    memory_content: str
    memory_type: str = Field(
        ...,
        pattern="^(error_fix|discovery|decision|learning|warning|codebase_knowledge)$",
    )
    related_files: Optional[List[str]] = Field(default=None)
    tags: Optional[List[str]] = Field(default=None)


class SaveMemoryResponse(BaseModel):
    """Response model for memory saving."""

    memory_id: str
    indexed: bool
    similar_memories: Optional[List[str]] = Field(default=None)


class SearchMemoryRequest(BaseModel):
    """Request model for searching memories."""

    query: str
    limit: int = 10
    memory_type: Optional[str] = None
    project_id: Optional[str] = (
        None  # Filter by project (auto-detected from agent if not set)
    )


class SearchMemoryResponse(BaseModel):
    """Response model for memory search."""

    results: List[Dict[str, Any]]
    total: int


class ReportResultsRequest(BaseModel):
    """Request model for reporting task results."""

    task_id: str = Field(..., description="ID of the task")
    markdown_file_path: str = Field(
        ..., description="Path to markdown file with results"
    )
    result_type: str = Field(
        ...,
        pattern="^(implementation|analysis|fix|design|test|documentation)$",
        description="Type of result",
    )
    summary: str = Field(..., description="Brief summary of the result")


class ReportResultsResponse(BaseModel):
    """Response model for result reporting."""

    status: str = Field(..., description="stored or error")
    result_id: str = Field(..., description="ID of the stored result")
    task_id: str = Field(..., description="ID of the task")
    agent_id: str = Field(..., description="ID of the agent")
    verification_status: str = Field(..., description="Verification status")
    created_at: str = Field(..., description="ISO timestamp of creation")


class GiveValidationReviewRequest(BaseModel):
    """Request model for validation review submission."""

    task_id: str = Field(..., description="ID of task being validated")
    validator_agent_id: str = Field(..., description="ID of validator agent")
    validation_passed: bool = Field(..., description="Whether validation passed")
    feedback: str = Field(..., description="Detailed feedback")
    evidence: List[Dict[str, Any]] = Field(
        default_factory=list, description="Evidence supporting decision"
    )
    recommendations: List[str] = Field(
        default_factory=list, description="Follow-up task recommendations"
    )


class GiveValidationReviewResponse(BaseModel):
    """Response model for validation review."""

    status: str = Field(..., description="completed, needs_work, or error")
    message: str = Field(..., description="Status message")
    iteration: Optional[int] = Field(
        default=None, description="Current iteration number"
    )


class SubmitResultRequest(BaseModel):
    """Request model for submitting workflow results."""

    markdown_file_path: str = Field(
        ..., description="Path to markdown file with result evidence"
    )
    explanation: str = Field(
        ..., description="Brief explanation of what was accomplished"
    )
    evidence: Optional[List[str]] = Field(
        default=None, description="List of evidence supporting completion"
    )
    extra_files: Optional[List[str]] = Field(
        default=None,
        description="List of additional file paths (e.g., patches, reproduction scripts) for validators",
    )


class SubmitResultResponse(BaseModel):
    """Response model for result submission."""

    status: str = Field(..., description="submitted, rejected, or error")
    result_id: Optional[str] = Field(
        default=None, description="ID of the submitted result"
    )
    workflow_id: str = Field(..., description="ID of the workflow")
    agent_id: str = Field(..., description="ID of the agent")
    validation_triggered: bool = Field(
        ..., description="Whether validation was triggered"
    )
    message: str = Field(..., description="Status message")
    created_at: Optional[str] = Field(
        default=None, description="ISO timestamp of creation"
    )


class SubmitResultValidationRequest(BaseModel):
    """Request model for result validation submission."""

    result_id: str = Field(..., description="ID of result being validated")
    validation_passed: bool = Field(..., description="Whether validation passed")
    feedback: str = Field(..., description="Detailed validation feedback")
    evidence: List[Dict[str, Any]] = Field(
        default_factory=list, description="Evidence supporting decision"
    )


class SubmitResultValidationResponse(BaseModel):
    """Response model for result validation."""

    status: str = Field(..., description="completed, workflow_terminated, or error")
    message: str = Field(..., description="Status message")
    workflow_action_taken: Optional[str] = Field(
        default=None, description="Action taken on workflow"
    )
    result_id: str = Field(..., description="ID of the validated result")


# ── Routes ───────────────────────────────────────────────────────────


@router.post("/save_memory", response_model=SaveMemoryResponse)
async def save_memory(
    request: SaveMemoryRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Store important discoveries and learnings."""
    server_state = _get_server_state()
    # Touch agent activity timestamp
    from src.mcp.server._shared import _touch_agent_activity
    _touch_agent_activity(agent_id)
    logger.info(
        f"Saving memory from agent {agent_id}: {request.memory_content[:100]}..."
    )

    try:
        memory_id = str(uuid.uuid4())

        # Create initial memory record in database
        session = server_state.db_manager.get_session()
        memory = Memory(
            id=memory_id,
            agent_id=agent_id,
            content=request.memory_content,
            memory_type=request.memory_type,
            embedding_id=None,
            tags=request.tags,
            related_files=request.related_files,
        )
        session.add(memory)
        session.commit()
        session.close()

        # Process the embedding and deduplication asynchronously
        async def process_memory_async():
            try:
                embedding = await server_state.llm_provider.generate_embedding(
                    request.memory_content
                )

                similar = await server_state.vector_store.search(
                    collection="agent_memories",
                    query_vector=embedding,
                    limit=5,
                    score_threshold=0.95,
                )

                if not similar or similar[0]["score"] < 0.95:
                    # Resolve project_id from agent's workflow
                    project_id = await _get_project_id_for_agent(agent_id)

                    success = await server_state.vector_store.store_memory(
                        collection="agent_memories",
                        memory_id=memory_id,
                        embedding=embedding,
                        content=request.memory_content,
                        metadata={
                            "agent_id": agent_id,
                            "memory_type": request.memory_type,
                            "related_files": request.related_files,
                            "tags": request.tags,
                            "project_id": project_id,
                        },
                    )

                    session = server_state.db_manager.get_session()
                    memory = session.query(Memory).filter_by(id=memory_id).first()
                    if memory:
                        memory.embedding_id = memory_id if success else None
                        session.commit()
                    session.close()

                    logger.info(
                        f"Memory {memory_id} indexed successfully in background"
                    )
                else:
                    session = server_state.db_manager.get_session()
                    memory = session.query(Memory).filter_by(id=memory_id).first()
                    if memory:
                        memory.tags = (memory.tags or []) + [
                            f"duplicate_of:{similar[0]['id']}"
                        ]
                        session.commit()
                    session.close()
                    logger.info(
                        f"Memory {memory_id} marked as duplicate of {similar[0]['id']}"
                    )

            except Exception as e:
                logger.error(f"Failed to process memory {memory_id} in background: {e}")
                session = server_state.db_manager.get_session()
                memory = session.query(Memory).filter_by(id=memory_id).first()
                if memory:
                    memory.tags = (memory.tags or []) + [
                        f"indexing_error:{str(e)[:50]}"
                    ]
                    session.commit()
                session.close()

        asyncio.create_task(process_memory_async())

        return SaveMemoryResponse(
            memory_id=memory_id,
            indexed=True,
            similar_memories=None,
        )

    except Exception as e:
        logger.error(f"Failed to save memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/search_memory", response_model=SearchMemoryResponse)
async def search_memory(
    request: SearchMemoryRequest,
    agent_id: str = Header(None, alias="X-Agent-ID"),
):
    """Search the knowledge base for relevant memories using semantic search."""
    server_state = _get_server_state()
    logger.info(f"Searching memory: '{request.query[:100]}' (limit={request.limit})")

    try:
        query_embedding = await server_state.llm_provider.generate_embedding(
            request.query
        )

        project_id = request.project_id
        if not project_id and agent_id:
            project_id = await _get_project_id_for_agent(agent_id)

        filters: Optional[Dict[str, Any]] = {}
        if request.memory_type:
            filters["memory_type"] = request.memory_type
        if project_id:
            filters["project_id"] = project_id
        if not filters:
            filters = None

        results = await server_state.vector_store.search(
            collection="agent_memories",
            query_vector=query_embedding,
            limit=request.limit,
            filters=filters,
        )

        formatted_results = []
        for r in results:
            # Both vector store backends (turbovec_store.py, vector_store.py's
            # Qdrant wrapper) already normalize their results to
            # {"id", "score", "content", "metadata": {...}} -- neither has a
            # "payload" key. Reading r.get("payload", {}) here always
            # returned an empty dict, silently dropping content/memory_type
            # from every single result regardless of backend. Observed live:
            # hephaestus_search_memory correctly reported "Found 10
            # memories" but rendered every one as "- []" (empty
            # memory_type, empty content).
            metadata = r.get("metadata", {})
            formatted_results.append(
                {
                    "id": r.get("id", ""),
                    "content": r.get("content", ""),
                    "memory_type": metadata.get("memory_type", ""),
                    "score": r.get("score", 0),
                    "metadata": {
                        k: v
                        for k, v in metadata.items()
                        if k not in ("content", "memory_id", "timestamp")
                    },
                }
            )

        return SearchMemoryResponse(
            results=formatted_results,
            total=len(formatted_results),
        )

    except Exception as e:
        logger.error(f"Failed to search memory: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/report_results", response_model=ReportResultsResponse)
async def report_results(
    request: ReportResultsRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Submit formal results for a completed task."""
    server_state = _get_server_state()
    logger.info(f"Agent {agent_id} reporting results for task {request.task_id}")

    try:
        # Import the result service
        from src.services.result_service import ResultService

        # Create the result
        result = ResultService.create_result(
            agent_id=agent_id,
            task_id=request.task_id,
            markdown_file_path=request.markdown_file_path,
            result_type=request.result_type,
            summary=request.summary,
        )

        # Broadcast update
        from src.core.database import get_db, resolve_project_for_workflow

        with get_db() as db:
            report_task = db.query(Task).filter_by(id=request.task_id).first()
            report_workflow_id = report_task.workflow_id if report_task else None
        bcast_project_id, bcast_project_name = resolve_project_for_workflow(
            report_workflow_id
        )
        await server_state.broadcast_update(
            {
                "type": "results_reported",
                "task_id": request.task_id,
                "agent_id": agent_id,
                "result_id": result["result_id"],
                "summary": request.summary[:200],
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        return ReportResultsResponse(
            status=result["status"],
            result_id=result["result_id"],
            task_id=result["task_id"],
            agent_id=result["agent_id"],
            verification_status=result["verification_status"],
            created_at=result["created_at"],
        )

    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to report results: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/give_validation_review", response_model=GiveValidationReviewResponse)
async def give_validation_review(
    request: GiveValidationReviewRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Submit validation review for a task."""
    server_state = _get_server_state()
    logger.info(
        f"Validation review from {agent_id}: task={request.task_id}, passed={request.validation_passed}"
    )

    try:
        session = server_state.db_manager.get_session()

        # 1. Verify caller is a validator agent
        agent = session.query(Agent).filter_by(id=agent_id).first()
        if not agent or agent.agent_type != "validator":
            raise HTTPException(
                status_code=403, detail="Only validator agents can submit reviews"
            )

        # 2. Get task
        task = session.query(Task).filter_by(id=request.task_id).first()
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        original_agent_id = task.assigned_agent_id

        # 3. Create validation review record
        review = ValidationReview(
            id=str(uuid.uuid4()),
            task_id=request.task_id,
            validator_agent_id=agent_id,
            iteration_number=task.validation_iteration,
            validation_passed=request.validation_passed,
            feedback=request.feedback,
            evidence=request.evidence,
            recommendations=request.recommendations,
        )
        session.add(review)

        if request.validation_passed:
            # 4a. Validation successful
            task.status = "done"
            task.failure_reason = None
            task.review_done = True
            task.completed_at = datetime.utcnow()

            # Update verification status of results if they exist
            if task.has_results:
                from src.services.result_service import ResultService

                results = ResultService.get_results_for_task(request.task_id)
                for result_info in results:
                    try:
                        ResultService.verify_result(
                            result_id=result_info["result_id"],
                            validation_review_id=review.id,
                            verified=True,
                        )
                    except Exception as e:
                        logger.warning(
                            f"Failed to verify result {result_info['result_id']}: {e}"
                        )

            # Create recommended follow-up tasks
            if request.recommendations:
                for rec in request.recommendations:
                    follow_up_task = Task(
                        id=str(uuid.uuid4()),
                        raw_description=rec,
                        done_definition="Complete as described",
                        parent_task_id=request.task_id,
                        created_by_agent_id=agent_id,
                        priority="medium",
                        status="pending",
                    )
                    session.add(follow_up_task)

            session.commit()

            # Commit validated work in the agent's worktree (don't merge to main yet)
            if hasattr(server_state, "branch_manager") and original_agent_id:
                try:
                    record = server_state.branch_manager._agent_record(
                        session, original_agent_id
                    )
                    if record and record.worktree_path:
                        # GitPython does real subprocess work (git add/status/
                        # commit) -- offloaded so it doesn't block the event
                        # loop, same class of issue fixed elsewhere today.
                        loop = asyncio.get_event_loop()
                        committed = await loop.run_in_executor(
                            None, _commit_validated_worktree, record.worktree_path, original_agent_id
                        )
                        if committed:
                            logger.info(
                                f"Committed validated work in worktree for {original_agent_id[:8]}"
                            )

                    # Track for final merge
                    if not hasattr(server_state, "_completed_agent_branches"):
                        server_state._completed_agent_branches = []
                    if record:
                        server_state._completed_agent_branches.append(
                            {
                                "agent_id": original_agent_id,
                                "branch": record.branch_name,
                                "phase": "validated",
                            }
                        )
                except Exception as e:
                    logger.warning(f"Failed to commit validated work: {e}")

            # Terminate both original and validator agents, then process queue
            from src.mcp.server.background_loops import terminate_agents_and_process_queue

            asyncio.create_task(
                terminate_agents_and_process_queue(
                    server_state.agent_manager, [original_agent_id, agent_id]
                )
            )

            # Broadcast success
            from src.core.database import resolve_project_for_workflow

            bcast_project_id, bcast_project_name = resolve_project_for_workflow(
                task.workflow_id
            )
            await server_state.broadcast_update(
                {
                    "type": "validation_passed",
                    "task_id": request.task_id,
                    "agent_id": original_agent_id,
                    "validator_id": agent_id,
                    "iteration": task.validation_iteration,
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )

            return GiveValidationReviewResponse(
                status="completed",
                message="Validation passed, task completed",
                iteration=task.validation_iteration,
            )

        else:
            # 4b. Validation failed - send feedback to original agent
            task.status = "needs_work"
            task.last_validation_feedback = request.feedback
            session.commit()

            # Send feedback to the still-running agent. Offloaded --
            # shells out to `tmux send-keys`, blocking.
            from src.validation.validator_agent import send_feedback_to_agent

            loop = asyncio.get_event_loop()
            feedback_sent = await loop.run_in_executor(
                None,
                functools.partial(
                    send_feedback_to_agent,
                    agent_id=original_agent_id,
                    feedback=request.feedback,
                    iteration=task.validation_iteration,
                ),
            )

            if not feedback_sent:
                logger.error(f"Failed to send feedback to agent {original_agent_id}")

            # Terminate validator (its job is done) and process queue
            from src.mcp.server.background_loops import terminate_agents_and_process_queue

            asyncio.create_task(
                terminate_agents_and_process_queue(server_state.agent_manager, [agent_id])
            )

            # Broadcast validation failure
            from src.core.database import resolve_project_for_workflow

            bcast_project_id, bcast_project_name = resolve_project_for_workflow(
                task.workflow_id
            )
            await server_state.broadcast_update(
                {
                    "type": "validation_failed",
                    "task_id": request.task_id,
                    "agent_id": original_agent_id,
                    "validator_id": agent_id,
                    "iteration": task.validation_iteration,
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )

            return GiveValidationReviewResponse(
                status="needs_work",
                message="Validation failed, feedback sent to agent",
                iteration=task.validation_iteration,
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process validation review: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        session.close()


# ── Workflow Result Submission ───────────────────────────────────────


@router.post("/submit_result", response_model=SubmitResultResponse)
async def submit_result(
    request: SubmitResultRequest,
    agent_id: str = Header(..., alias="X-Agent-ID"),
):
    """Submit a workflow result for validation."""
    server_state = _get_server_state()
    try:
        logger.info(f"Agent {agent_id} submitting result: {request.explanation}")

        session = server_state.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                raise HTTPException(
                    status_code=404, detail=f"Agent not found: {agent_id}"
                )

            task = session.query(Task).filter_by(assigned_agent_id=agent_id).first()
            if not task:
                raise HTTPException(
                    status_code=404, detail=f"No task found for agent: {agent_id}"
                )

            workflow_id = task.workflow_id
            if not workflow_id:
                raise HTTPException(
                    status_code=400, detail=f"Task {task.id} has no workflow_id"
                )

            logger.info(
                f"Derived workflow_id {workflow_id} from agent {agent_id}'s task {task.id}"
            )
        finally:
            session.close()

        # Submit the result
        from src.services.workflow_result_service import WorkflowResultService

        result = WorkflowResultService.submit_result(
            agent_id=agent_id,
            workflow_id=workflow_id,
            markdown_file_path=request.markdown_file_path,
            explanation=request.explanation,
            evidence=request.evidence,
            extra_files=request.extra_files,
        )

        # Create AgentResult entry
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(assigned_agent_id=agent_id).first()
            if task:
                with open(request.markdown_file_path, "r") as f:
                    markdown_content = f.read()

                agent_result = AgentResult(
                    id=f"agent-result-{uuid.uuid4()}",
                    agent_id=agent_id,
                    task_id=task.id,
                    markdown_content=markdown_content,
                    markdown_file_path=request.markdown_file_path,
                    result_type="implementation",
                    summary=request.explanation or "Workflow result submitted",
                    created_at=datetime.utcnow(),
                )
                session.add(agent_result)
                session.commit()
                logger.info(
                    f"Created AgentResult {agent_result.id} for workflow result {result['result_id']}"
                )
        except Exception as e:
            logger.warning(f"Failed to create AgentResult entry: {e}")
            session.rollback()
        finally:
            session.close()

        # Create commit for result submission
        commit_sha = None
        if hasattr(server_state, "branch_manager"):
            try:
                loop = asyncio.get_event_loop()
                commit_result = await loop.run_in_executor(
                    None,
                    functools.partial(
                        server_state.branch_manager.commit_for_validation,
                        agent_id=agent_id,
                        iteration=1,
                        message="Result submitted for workflow validation",
                    ),
                )
                commit_sha = commit_result.get("commit_sha")
                logger.info(
                    f"Created commit {commit_sha} for result submission by agent {agent_id}"
                )
            except Exception as e:
                logger.warning(f"Failed to create result submission commit: {e}")

        # Check if validation should be triggered
        should_validate, criteria = (
            server_state.result_validator_service.should_spawn_validator(workflow_id)
        )

        validation_triggered = False
        if should_validate and criteria:
            async def spawn_validator_async():
                try:
                    from src.validation.validator_agent import spawn_validator_agent

                    validator_id = await spawn_validator_agent(
                        validation_type="result",
                        target_id=result["result_id"],
                        workflow_id=workflow_id,
                        commit_sha=commit_sha or "HEAD",
                        db_manager=server_state.db_manager,
                        branch_manager=getattr(server_state, "branch_manager", None),
                        agent_manager=server_state.agent_manager,
                        criteria=criteria,
                        original_agent_id=agent_id,
                    )
                    logger.info(
                        f"Spawned result validator {validator_id} for result {result['result_id']}"
                    )
                except Exception as e:
                    logger.error(f"Failed to spawn result validator: {e}")

            asyncio.create_task(spawn_validator_async())
            validation_triggered = True

        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(
            workflow_id
        )
        await server_state.broadcast_update(
            {
                "type": "result_submitted",
                "result_id": result["result_id"],
                "workflow_id": workflow_id,
                "agent_id": agent_id,
                "validation_triggered": validation_triggered,
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        return SubmitResultResponse(
            status=result["status"],
            result_id=result["result_id"],
            workflow_id=workflow_id,
            agent_id=agent_id,
            validation_triggered=validation_triggered,
            message="Result submitted successfully"
            + (" and validation triggered" if validation_triggered else ""),
            created_at=result["created_at"],
        )

    except FileNotFoundError as e:
        logger.error(f"File not found: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        logger.error(f"Validation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to submit result: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/submit_result_validation", response_model=SubmitResultValidationResponse)
async def submit_result_validation(
    request: SubmitResultValidationRequest,
):
    """Submit validation review for a workflow result (validator agents only)."""
    server_state = _get_server_state()
    try:
        logger.info(f"Processing validation for result {request.result_id}")

        # Get the workflow result to find the validator agent
        session = server_state.db_manager.get_session()
        try:
            result = (
                session.query(WorkflowResult).filter_by(id=request.result_id).first()
            )
            if not result:
                raise HTTPException(
                    status_code=404, detail=f"Result {request.result_id} not found"
                )

            # The validator agent should be the one currently assigned
            # to a task in this workflow. Scoped by workflow_id to
            # prevent cross-wiring outcomes from concurrent validation
            # runs across different workflows.
            validator_agent = (
                session.query(Agent)
                .join(Task, Agent.current_task_id == Task.id)
                .filter(
                    Agent.agent_type == "result_validator",
                    Task.workflow_id == result.workflow_id,
                )
                .order_by(Agent.created_at.desc())
                .first()
            )

            if not validator_agent:
                raise HTTPException(
                    status_code=500,
                    detail="No validator agent found for this validation",
                )

            agent_id = validator_agent.id
            logger.info(
                f"Using validator agent {agent_id} for result {request.result_id}"
            )
        finally:
            session.close()

        # Process validation outcome
        outcome = server_state.result_validator_service.process_validation_outcome(
            result_id=request.result_id,
            passed=request.validation_passed,
            feedback=request.feedback,
            evidence=request.evidence,
            validator_agent_id=agent_id,
        )

        # Handle workflow actions
        workflow_action_taken = None
        if "terminate_workflow" in outcome["next_actions"]:
            from src.workflow.termination_handler import WorkflowTerminationHandler

            termination_handler = WorkflowTerminationHandler(
                db_manager=server_state.db_manager,
                agent_manager=server_state.agent_manager,
            )

            await termination_handler.terminate_workflow(outcome["workflow_id"])
            workflow_action_taken = "workflow_terminated"
            logger.info(
                f"Terminated workflow {outcome['workflow_id']} due to validated result"
            )

        elif "continue_workflow" in outcome["next_actions"]:
            workflow_action_taken = "workflow_continues"
            logger.info(
                f"Workflow {outcome['workflow_id']} continues after validated result"
            )

        # Terminate validator agent and process queue
        async def terminate_result_validator_and_process_queue():
            await server_state.agent_manager.terminate_agent(agent_id)
            from src.mcp.server.background_loops import process_queue
            await process_queue()

        asyncio.create_task(terminate_result_validator_and_process_queue())

        # Broadcast validation result
        from src.core.database import resolve_project_for_workflow

        bcast_project_id, bcast_project_name = resolve_project_for_workflow(
            outcome["workflow_id"]
        )
        await server_state.broadcast_update(
            {
                "type": "result_validation_completed",
                "result_id": request.result_id,
                "workflow_id": outcome["workflow_id"],
                "validation_passed": request.validation_passed,
                "validator_agent_id": agent_id,
                "workflow_action": workflow_action_taken,
            },
            project_id=bcast_project_id,
            project_name=bcast_project_name,
        )

        status = (
            "workflow_terminated"
            if workflow_action_taken == "workflow_terminated"
            else "completed"
        )
        message = f"Validation {'passed' if request.validation_passed else 'failed'}"
        if workflow_action_taken == "workflow_terminated":
            message += " - workflow terminated"
        elif workflow_action_taken == "workflow_continues":
            message += " - workflow continues"

        return SubmitResultValidationResponse(
            status=status,
            message=message,
            workflow_action_taken=workflow_action_taken,
            result_id=request.result_id,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to process result validation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/workflows/{workflow_id}/results")
async def get_workflow_results(
    workflow_id: str,
    requesting_agent_id: str = Header(None, alias="X-Agent-ID"),
):
    """Get all results for a specific workflow."""
    try:
        from src.services.workflow_result_service import WorkflowResultService
        results = WorkflowResultService.get_workflow_results(workflow_id)
        return results
    except Exception as e:
        logger.error(f"Failed to get workflow results: {e}")
        raise HTTPException(status_code=500, detail=str(e))
