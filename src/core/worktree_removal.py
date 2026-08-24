"""Safe git-worktree removal with a hard main-repo safety guard.

Extracted from WorktreeManager (SOLID review 4.5) -- pure git/filesystem
logic, no DB coupling, so it splits out cleanly ahead of the harder
git+DB-fused methods still living on WorktreeManager (create_agent_worktree,
merge_to_main, cleanup_all_stale_branches).

Stateless like ConflictResolver: main_repo is passed in per call rather than
held as constructor state, since WorktreeManager.reload() can repoint it to
a different repo at runtime.
"""

import logging
import shutil
from pathlib import Path
from typing import Optional

from git import GitCommandError, Repo

logger = logging.getLogger(__name__)


class WorktreeRemover:
    def remove(
        self, main_repo: Repo, worktree_path: str, require_clean: bool = True
    ) -> None:
        """Remove a git worktree and its directory.

        Hard safety guard: refuses to touch the main repo, regardless of
        what any caller's own path-matching logic decided. This is the sole
        choke point every removal (git worktree remove, and its shutil.rmtree
        fallback) goes through -- putting the check here means a future bug
        in a caller's "is this the main repo" comparison (like the resolved-
        vs-unresolved path mismatch found and fixed in
        cleanup_all_stale_branches, which let this exact function attempt to
        delete the main repository) can't reach shutil.rmtree on it again.

        require_clean: refuses to remove a worktree carrying uncommitted
        changes (modified, staged, or untracked) unless the caller
        explicitly opts out. Workflow.status is not a reliable enough
        signal to gate a destructive delete on: a workflow can be marked
        "failed" by an unrelated self-heal (e.g. "abandoned: no activity"
        firing because the *backend itself* crashed and stopped recording
        activity, not because the agent actually stopped working) while an
        agent is still genuinely mid-task with real, uncommitted fixes
        sitting in this exact worktree. Every phase already commits its
        own work here as a matter of course (see this module's own
        docstring), so a worktree that's truly done has nothing uncommitted
        left to lose -- this check only ever blocks the exact case it's
        meant to. Observed live: a security_review agent's uncommitted
        fixes (C-1/H-1/H-2, a written report) were permanently destroyed
        this way when a crash-induced false "abandoned" marking let the
        generic stale-worktree sweep (cleanup_all_stale_branches) delete
        the worktree out from under it.
        """
        try:
            target = Path(worktree_path).resolve()
        except OSError:
            target = Path(worktree_path)
        try:
            main_repo_path = Path(main_repo.working_dir).resolve()
        except OSError:
            main_repo_path = Path(main_repo.working_dir)
        if target == main_repo_path:
            logger.error(
                f"[WORKTREE] Refusing to remove {worktree_path} -- resolves to "
                "the main repository, not a linked worktree. This should never "
                "be reached; a caller's path-matching logic has a bug."
            )
            return

        if require_clean and target.is_dir():
            try:
                wt_repo = Repo(target)
                dirty = wt_repo.is_dirty(untracked_files=True)
            except Exception as e:
                # Can't prove it's clean -- and "assume clean" is exactly the
                # failure mode this guard exists to close. Refuse rather
                # than silently fall through to a force-delete.
                logger.error(
                    f"[WORKTREE] Refusing to remove {worktree_path} -- could "
                    f"not verify it has no uncommitted changes ({e}). Pass "
                    "require_clean=False to force removal if this worktree "
                    "is genuinely being discarded."
                )
                return
            if dirty:
                logger.error(
                    f"[WORKTREE] Refusing to remove {worktree_path} -- has "
                    "uncommitted changes (modified, staged, or untracked "
                    "files). A worktree that's genuinely done has already "
                    "committed everything; this one hasn't, so treating it "
                    "as stale would destroy real, unrecovered work. Pass "
                    "require_clean=False to force removal if this worktree "
                    "is genuinely being discarded."
                )
                return

        # Every refusal branch above logs why -- but until now, a
        # SUCCESSFUL removal (the actually destructive outcome) logged
        # nothing at all. A caller hitting "worktree is missing or not a
        # valid git worktree" later has no way to answer its own logged
        # advice ("find out what deleted it") -- there was nothing to
        # find. logger.stack_info=True: the caller (cleanup sweep? discard-
        # agent? orphan reclaim?) is exactly the missing piece of this
        # picture, and every caller of this method goes through several
        # layers of indirection (WorktreeManager._remove_worktree, etc.)
        # that a bare log message can't disambiguate on its own.
        try:
            main_repo.git.worktree("remove", worktree_path, "--force")
            logger.info(f"[WORKTREE] Removed {worktree_path}", stack_info=True)
        except GitCommandError:
            # Fall back to manual removal + prune
            try:
                if Path(worktree_path).exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)
                main_repo.git.worktree("prune")
                logger.info(
                    f"[WORKTREE] Removed {worktree_path} (fallback rmtree+prune)",
                    stack_info=True,
                )
            except Exception as e:
                logger.warning(
                    f"[WORKTREE] Could not remove worktree {worktree_path}: {e}"
                )

    def orphan_blocker(
        self, main_repo: Repo, base_branch: str, path: Path
    ) -> Optional[str]:
        """Why this orphaned worktree must not be reclaimed, or None."""
        try:
            repo = Repo(str(path))
        except Exception as e:
            return f"not a readable git worktree: {e}"
        try:
            if repo.is_dirty(untracked_files=True):
                return "uncommitted or untracked changes"
            branch = repo.active_branch.name
        except Exception as e:
            return f"could not determine state: {e}"
        try:
            unmerged = main_repo.git.rev_list(
                "--count", f"{base_branch}..{branch}"
            ).strip()
            if unmerged not in ("", "0"):
                return f"{unmerged} commit(s) not in {base_branch}"
        except Exception as e:
            return f"could not compare against base branch: {e}"
        return None
