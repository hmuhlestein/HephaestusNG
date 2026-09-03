"""Cost collection on task completion.

Extracted from src.services.task_completion_service.TaskCompletionService
per design_docs/phase_1b_decomposition.md section 4.4.
"""

import logging

logger = logging.getLogger(__name__)


def collect_cost_on_completion(task_id: str) -> None:
    """Collect cost data from CLI session when a task completes.

    Called from update_task_status handler when task status is set to 'done'.
    Reads the CLI session transcript (pi JSONL, Claude Code, etc.) and
    writes CostEntry rows for any new usage since the last checkpoint.

    Args:
        task_id: The completed task's ID
    """
    try:
        from src.services.cost_collection_service import collect_task_cost

        collect_task_cost(task_id)
    except Exception as e:
        logger.warning(f"Cost collection failed for task {task_id[:8]}: {e}")


def collect_cost_on_termination(task_id: str, agent_id: str) -> None:
    """Checkpoint cost data for an agent's in-flight task when it is terminated.

    Called from engine_client.terminate_agent -- the single shared primitive
    for agent termination -- so every termination path is covered, not just
    the ones that go through normal task completion (orphan reaper,
    mechanical recovery's auto-restart, manual API termination, a
    crash-restart, etc.). Without this, an agent that is killed, times out,
    or gets restarted mid-task never gets its already-consumed tokens
    recorded anywhere: collect_task_cost otherwise only ever fires from
    collect_cost_on_completion, which requires the task to actually reach
    'done'/'failed' first.

    Reuses collect_task_cost -- the same extraction/checkpoint logic
    collect_cost_on_completion uses -- so this is never a second, divergent
    way of deriving cost from a transcript. Safe to call even when a task
    completed normally moments before and collect_cost_on_completion already
    ran for it: collect_task_cost is checkpointed per session_id
    (SessionCostCheckpoint.lines_processed), so a second call for the same
    task/session simply finds no new transcript lines and records nothing
    further -- it does not double-count.

    Args:
        task_id: The terminated agent's in-flight task ID (its
            current_task_id at the moment termination started)
        agent_id: The terminated agent's own ID, passed through to
            collect_task_cost's agent_id param -- REQUIRED here, not
            optional, and must be resolved by the caller before dispatch.
            engine_client.terminate_agent's own subsequent steps clear
            Task.assigned_agent_id and Agent.current_task_id -- the two
            fields collect_task_cost's default derivation depends on --
            and when this call is dispatched fire-and-forget (a caller
            with a running event loop), those steps routinely finish on
            the calling thread before this even starts on its own worker
            thread. Passing the already-known agent_id directly sidesteps
            that race instead of re-deriving it from fields about to be
            cleared underneath it.
    """
    try:
        from src.services.cost_collection_service import collect_task_cost

        collect_task_cost(task_id, agent_id=agent_id)
    except Exception as e:
        logger.warning(f"Cost checkpoint on termination failed for task {task_id[:8]}: {e}")
