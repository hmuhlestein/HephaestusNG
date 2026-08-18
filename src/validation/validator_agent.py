"""Validator agent spawning and management."""

import asyncio
import logging
import uuid
from typing import Any, Dict

from sqlalchemy.orm import Session

from src.agents.manager import AgentManager
from src.core.database import DatabaseManager, Phase, Task
from src.core.worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


async def spawn_validator_agent(
    validation_type: str,
    target_id: str,
    workflow_id: str,
    commit_sha: str,
    db_manager: DatabaseManager,
    branch_manager: WorktreeManager,
    agent_manager: AgentManager,
    original_agent_id: str,
    criteria: str = None,
) -> str:
    """Spawn a validator agent for either task or result validation.

    Args:
        validation_type: "task" or "result"
        target_id: ID of task or result to validate
        workflow_id: ID of the workflow
        commit_sha: Commit SHA to validate
        db_manager: Database manager
        branch_manager: Worktree manager
        agent_manager: Agent manager
        original_agent_id: ID of the agent that created the task/result
        criteria: Validation criteria (for result validation)

    Returns:
        ID of spawned validator agent
    """
    logger.info(f"Spawning {validation_type} validator agent for {target_id}")

    with db_manager.session_scope() as session:
        # Create validator agent ID first (needed for prompt)
        validator_agent_id = f"{validation_type}-validator-{uuid.uuid4().hex[:8]}"

        # Build validator prompt based on type
        if validation_type == "task":
            # Get task and phase for task validation
            task = session.query(Task).filter_by(id=target_id).first()
            if not task:
                raise ValueError(f"Task {target_id} not found")

            if task.phase_id:
                session.query(Phase).filter_by(id=task.phase_id).first()

            # Get workspace changes
            branch_manager.get_workspace_changes(
                agent_id=original_agent_id,
                since_commit=None,  # Get all changes
            )

            # Get agent claims/results
            get_agent_results(target_id, session)

            # Build task validation prompt using the new prompt loader
            from src.monitoring.prompt_loader import prompt_loader

            # Get previous feedback if any
            previous_feedback = getattr(task, "last_validation_feedback", None)

            validator_prompt = prompt_loader.format_task_validation_prompt(
                validator_agent_id=validator_agent_id,
                task_id=target_id,
                task_description=task.raw_description,
                done_definition=task.done_definition,
                enriched_description=task.enriched_description or task.raw_description,
                original_agent_id=original_agent_id,
                iteration=task.validation_iteration,
                working_directory=branch_manager.get_agent_branch_path(
                    original_agent_id
                )
                or "/tmp",
                commit_sha=commit_sha,
                previous_feedback=previous_feedback,
            )

            # Create validation task for task validator
            validation_task_id = str(uuid.uuid4())
            validation_task = Task(
                id=validation_task_id,
                raw_description=f"Validate task completion: {task.raw_description}",
                enriched_description=f"Validate the work completed by agent {original_agent_id} for task {target_id}",
                done_definition="Review task completion and provide validation feedback using heph_give_validation_review",
                status="assigned",
                priority="high",
                assigned_agent_id=validator_agent_id,
                parent_task_id=target_id,
                phase_id=task.phase_id,
                workflow_id=workflow_id,
                validation_enabled=False,
            )
            session.add(validation_task)

        elif validation_type == "result":
            # Get result and workflow for result validation
            from src.core.database import Workflow, WorkflowResult

            result = session.query(WorkflowResult).filter_by(id=target_id).first()
            if not result:
                raise ValueError(f"Result {target_id} not found")

            workflow = session.query(Workflow).filter_by(id=workflow_id).first()
            if not workflow:
                raise ValueError(f"Workflow {workflow_id} not found")

            # Build result validation prompt using the new prompt loader
            from src.monitoring.prompt_loader import prompt_loader

            validator_prompt = prompt_loader.format_result_validation_prompt(
                validator_agent_id=validator_agent_id,
                result_id=result.id,
                result_file_path=result.result_file_path,
                workflow_name=workflow.name,
                workflow_id=workflow_id,
                validation_criteria=criteria,
                submitted_by_agent=original_agent_id,
                submitted_at=result.created_at.isoformat(),
            )

            # Create validation task for result validator
            validation_task_id = str(uuid.uuid4())
            validation_task = Task(
                id=validation_task_id,
                raw_description=f"Validate result submission for workflow: {workflow.name}",
                enriched_description=f"Validate the result submitted by agent {original_agent_id} for workflow {workflow_id}",
                done_definition="Review and validate the submitted result against workflow criteria using heph_submit_result_validation",
                status="assigned",
                priority="high",
                assigned_agent_id=validator_agent_id,
                workflow_id=workflow_id,
                validation_enabled=False,
            )
            session.add(validation_task)

        else:
            raise ValueError(f"Invalid validation_type: {validation_type}")

        # For result validators, we need the commit SHA to create worktree from
        # The commit_sha parameter should have been passed from submit_result

        # Guard: don't dispatch if the phase already has another active task.
        # Prevents duplicate agents when concurrent validation runs target
        # the same phase. created_by_filter=False because validators are
        # subtasks (created_by_agent_id = original_agent_id), not orchestrator
        # tasks, so the orchestrator-scoped guard wouldn't catch them.
        from src.autopilot.orchestrator.engine_client import check_phase_sibling_active
        if validation_task.phase_id:
            _sibling = check_phase_sibling_active(
                session, validation_task.id, validation_task.phase_id,
                created_by_filter=False,
            )
            if _sibling is not None:
                logger.warning(
                    f"[spawn_validator_agent] Skipping: phase {validation_task.phase_id[:8]} "
                    f"already has active task {_sibling.id[:8]} ({_sibling.status})"
                )
                return _sibling.assigned_agent_id or _sibling.id

        # Use AgentManager to create agent properly (like normal agents)
        # Pass commit_sha to create worktree from the specific commit
        await agent_manager.create_agent_for_task(
            task=validation_task,
            enriched_data={
                "type": f"{validation_type}_validation",
                "target_id": target_id,
                "validation_prompt": validator_prompt,  # Pass the formatted prompt
            },
            memories=[],  # Validators don't need memories
            project_context="",  # Validators have read-only access
            cli_type="claude",
            working_directory=None,  # Will be created from commit
            agent_type="result_validator"
            if validation_type == "result"
            else "validator",
            use_existing_worktree=False,  # Create new worktree from commit
            commit_sha=commit_sha,  # Create worktree from this commit
        )

        logger.info(
            f"Spawned {validation_type} validator agent {validator_agent_id} for {target_id}"
        )
        return validator_agent_id


def get_agent_results(task_id: str, session: Session) -> str:
    """Get results/claims from the agent working on the task.

    Args:
        task_id: Task ID
        session: Database session

    Returns:
        Agent results as string
    """
    task = session.query(Task).filter_by(id=task_id).first()
    if not task:
        return "No task found"

    # Get completion notes or other results
    results = []

    if task.completion_notes:
        results.append(f"Completion Notes: {task.completion_notes}")

    if task.enriched_description:
        results.append(f"Task Description: {task.enriched_description}")

    if task.done_definition:
        results.append(f"Done Definition: {task.done_definition}")

    # Could also fetch from agent logs or other sources
    # For now, return what we have
    if results:
        return "\n\n".join(results)
    else:
        return "Agent has not provided specific results yet"


def send_feedback_to_agent(agent_id: str, feedback: str, iteration: int) -> bool:
    """Send validation feedback to a running agent via tmux.

    Args:
        agent_id: Agent ID
        feedback: Feedback message
        iteration: Validation iteration number

    Returns:
        True if feedback sent successfully
    """
    import subprocess
    import tempfile

    session_name = f"agent_{agent_id}"

    try:
        # Create feedback file
        feedback_content = f"""
VALIDATION FEEDBACK (Iteration {iteration}):
=====================================
{feedback}
=====================================

Please address the issues above and try again.
When ready, you can claim completion again.
"""

        # Write to temporary file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(feedback_content)
            feedback_file = f.name

        # Send to tmux pane
        cmd = f"cat {feedback_file}"
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, cmd, "Enter"], check=True
        )

        logger.info(f"Sent feedback to agent {agent_id}")
        return True

    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to send feedback to agent: {e}")
        return False
    except Exception as e:
        logger.error(f"Error sending feedback: {e}")
        return False
