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
