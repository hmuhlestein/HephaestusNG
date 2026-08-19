"""Git commit and ticket-linking for task-completion.

Extracted from src.services.task_completion_service.TaskCompletionService
per design_docs/phase_1b_decomposition.md section 4.4.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _do_git_commit(wt_path: str, phase_label: str, agent_id: str, task_id: str, summary: Optional[str]) -> Optional[str]:
    """`git add -A` + `git commit` in the worktree -- real subprocess work,
    called via run_in_executor by commit_and_link_ticket below. Returns
    the commit SHA if a commit was made, else None. Any exception (e.g.
    Repo() on an invalid path) propagates through the awaited executor
    future to commit_and_link_ticket's own try/except, same as when this
    body ran inline there."""
    from git import Repo

    wt_repo = Repo(wt_path)
    wt_repo.git.add("-A")
    if not (wt_repo.is_dirty(index=True) or wt_repo.untracked_files):
        logger.debug(f"[COMMIT] phase agent {agent_id[:8]}: nothing to commit")
        return None

    summary_str = (summary or "").strip()
    subject = f"phase({phase_label}): " + (summary_str[:60] if summary_str else f"task {task_id[:8]} done")
    msg = subject if not summary_str or len(summary_str) <= 60 else f"{subject}\n\n{summary_str}"
    wt_repo.git.commit("-m", msg, "--no-verify")
    merge_commit_sha = wt_repo.head.commit.hexsha
    logger.info(f"[COMMIT] phase({phase_label}) agent {agent_id[:8]}: {merge_commit_sha[:8]}")
    return merge_commit_sha


async def commit_and_link_ticket(session, agent_id: str, task, summary: str) -> Optional[str]:
    """Commit the agent's changes in the shared worktree, and if the
    task has a ticket_id, auto-link the resulting commit to it.

    Returns the commit SHA if a commit was made, else None.
    """
    import asyncio
    import functools

    from src.core.app_context import get_app_state
    from src.core.database import Phase

    server_state = get_app_state()
    from src.services.ticket_service import TicketService

    merge_commit_sha = None
    try:
        from pathlib import Path

        wt_path = None

        # Shared-worktree path (normal autopilot): use the workflow's directory.
        if task.workflow_id:
            from src.core.database import Workflow

            wf_row = session.query(Workflow).filter_by(id=task.workflow_id).first()
            if wf_row and wf_row.working_directory:
                wt_path = wf_row.working_directory

        # Legacy per-agent worktree fallback.
        if not wt_path and hasattr(server_state, "branch_manager"):
            record = server_state.branch_manager._agent_record(session, agent_id)
            if record and record.worktree_path:
                wt_path = record.worktree_path

        if wt_path and Path(wt_path).is_dir():
            phase_obj = session.query(Phase).filter_by(id=task.phase_id).first() if task.phase_id else None
            phase_label = phase_obj.name if phase_obj else (task.phase_id[:8] if task.phase_id else "unknown")

            # The actual git subprocess work (_do_git_commit) doesn't need
            # `session` -- only the two fast, single-row lookups above do,
            # which stay on this thread. Offloaded because `git add -A`
            # over a worktree after an agent's edits routinely takes
            # 0.5-3s, called directly on the event loop with no executor
            # -- confirmed live 2026-08-19 investigating intermittent
            # multi-second /health stalls, one of three call sites found
            # via a systematic audit.
            loop = asyncio.get_event_loop()
            merge_commit_sha = await loop.run_in_executor(
                None,
                functools.partial(
                    _do_git_commit, wt_path, phase_label, agent_id, task.id, summary,
                ),
            )
    except Exception as e:
        logger.warning(f"Failed to commit after task done for agent {agent_id[:8]}: {e}")

    if task.ticket_id and merge_commit_sha:
        try:
            logger.info(f"Auto-linking commit {merge_commit_sha} to ticket {task.ticket_id}")
            await TicketService.link_commit(
                ticket_id=task.ticket_id,
                agent_id=agent_id,
                commit_sha=merge_commit_sha,
                commit_message=f"Task {task.id} completed and merged",
                link_method="auto_task_completion",
            )
            logger.info(f"Commit {merge_commit_sha} linked to ticket {task.ticket_id}")

            from src.core.database import resolve_project_for_workflow

            bcast_project_id, bcast_project_name = resolve_project_for_workflow(task.workflow_id)
            await server_state.broadcast_update(
                {
                    "type": "ticket_commit_linked",
                    "ticket_id": task.ticket_id,
                    "task_id": task.id,
                    "agent_id": agent_id,
                    "commit_sha": merge_commit_sha,
                },
                project_id=bcast_project_id,
                project_name=bcast_project_name,
            )
        except Exception as e:
            logger.error(f"Failed to auto-link commit to ticket: {e}")
            # Don't fail the task if ticket operations fail

    return merge_commit_sha
