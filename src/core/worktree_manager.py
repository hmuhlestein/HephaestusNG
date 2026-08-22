"""Git worktree manager for agent isolation.

Each agent works in its own git worktree under ``<repo>/.worktrees/wt_<agent_id>``
on its own branch. The main repository stays on the base branch and is never used
for agent working-tree edits, so concurrent agents are fully isolated:

- A failed agent's worktree is discarded (``git worktree remove --force``) and its
  branch dropped — ``main`` never sees half-baked files (no Repair flow needed).
- A successful agent's branch is merged into ``main``; the next phase branches from
  the updated ``main`` and sees the committed prior work.

Agents never read out-of-tree paths. Curated inbound context (design doc, qa_spec,
task framing) is copied into a git-excluded ``<worktree>/.hephaestus/`` directory.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import git
from git import GitCommandError, Repo
from sqlalchemy.orm import Session

from src.core.constants import CONTEXT_DIR_NAME, WORKTREES_SUBDIR
from src.core.database import (
    AgentBranch,
    DatabaseManager,
    Workflow,
    WorktreeCommit,
)
from src.core.simple_config import get_config
from src.core.worktree_conflict_resolution import ConflictResolver
from src.core.worktree_merge_lock import MergeLockManager
from src.core.worktree_removal import WorktreeRemover

logger = logging.getLogger(__name__)

# Directories that must never be tracked or merged into main. Written to
# .git/info/exclude (shared across all linked worktrees) so the user's tracked
# .gitignore is left untouched.
EXCLUDE_ENTRIES = (f"{WORKTREES_SUBDIR}/", f"{CONTEXT_DIR_NAME}/")


class MergeStatus(Enum):
    ACTIVE = "active"
    MERGED = "merged"
    ABANDONED = "abandoned"
    CLEANED = "cleaned"


class CommitType(Enum):
    PARENT_CHECKPOINT = "parent_checkpoint"
    VALIDATION_READY = "validation_ready"
    FINAL = "final"
    AUTO_SAVE = "auto_save"
    CONFLICT_RESOLUTION = "conflict_resolution"


@dataclass
class BranchInfo:
    agent_id: str
    branch_name: str
    parent_agent_id: Optional[str]
    parent_commit_sha: str
    merge_status: MergeStatus
    created_at: str


@dataclass
class ConflictResolution:
    agent_id: str
    file_path: str
    parent_timestamp: str
    child_timestamp: str
    resolution_choice: str
    resolved_at: str
    commit_sha: str


@dataclass
class MergeResult:
    status: str
    merged_to: str
    commit_sha: str
    conflicts_resolved: List[ConflictResolution]
    resolution_strategy: str
    total_conflicts: int
    resolution_time_ms: int


class WorktreeManager:
    """Manages per-agent git worktrees for isolation.

    Each agent gets its own worktree + branch; the main repo stays on the base
    branch. Merge-on-success, discard-on-failure.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self.config = get_config()
        self._project_root = Path(self.config.git.main_repo_path)

        try:
            self.main_repo = Repo(self._project_root)
        except (git.InvalidGitRepositoryError, git.NoSuchPathError) as e:
            logger.error(
                f"Cannot open git repository at {self._project_root}: {e}\n"
                f"Set paths.project_root and git.main_repo_path in hephaestus_config.yaml, "
                f"or activate a project with: heph project activate <name>"
            )
            raise ValueError(f"Not a valid git repository: {self._project_root}") from e

        self.merge_lock_path = (
            Path(self.main_repo.working_dir) / ".git" / ".hephaestus_merge_lock"
        )
        self._merge_lock = MergeLockManager(self.merge_lock_path)
        self._conflict_resolver = ConflictResolver()
        self._worktree_remover = WorktreeRemover()
        self._ensure_excludes()
        logger.info(f"WorktreeManager initialized for repo: {self._project_root}")

    def reload(self, new_path):
        """Reinitialize with a new repository path.

        Instance-local only -- does NOT write through to the process-wide
        config singleton (get_config()). Two WorktreeManager instances each
        reload()ed to a different project must never interfere with each
        other; writing to the shared config here would let whichever
        instance reloaded last silently redirect every OTHER instance's
        `worktree_base` (which read the config fresh on every access) to
        the wrong project's repo.
        """
        new_path = Path(new_path) if not isinstance(new_path, Path) else new_path
        try:
            self.main_repo = Repo(new_path)
        except git.InvalidGitRepositoryError:
            raise ValueError(f"Not a git repository: {new_path}")
        self._project_root = new_path
        self.merge_lock_path = Path(new_path) / ".git" / ".hephaestus_merge_lock"
        self._merge_lock = MergeLockManager(self.merge_lock_path)
        self._ensure_excludes()
        logger.info(f"WorktreeManager reloaded with repo: {new_path}")

    # ── Worktree layout ──────────────────────────────────────────

    @property
    def worktree_base(self) -> Path:
        """Base directory for agent worktrees (``<repo>/.worktrees``)."""
        override = getattr(self.config.paths, "worktree_base_path", None)
        if override:
            return Path(override)
        return self._project_root / WORKTREES_SUBDIR

    def _worktree_path_for(self, agent_id: str) -> Path:
        return self.worktree_base / f"wt_{agent_id}"

    def _ensure_excludes(self) -> None:
        """Idempotently add worktree/context dirs to .git/info/exclude.

        Uses info/exclude (per-repo, untracked, shared across linked worktrees)
        so the user's committed .gitignore stays pristine.
        """
        try:
            git_common = Path(self.main_repo.git_dir)  # main repo's .git
            exclude_file = git_common / "info" / "exclude"
            exclude_file.parent.mkdir(parents=True, exist_ok=True)
            existing = exclude_file.read_text() if exclude_file.exists() else ""
            lines = {ln.strip() for ln in existing.splitlines()}
            missing = [e for e in EXCLUDE_ENTRIES if e not in lines]
            if missing:
                header = "" if existing.endswith("\n") or not existing else "\n"
                with open(exclude_file, "a") as f:
                    if not existing:
                        f.write("# Hephaestus worktree isolation (auto-managed)\n")
                    else:
                        f.write(header)
                    for e in missing:
                        f.write(f"{e}\n")
                logger.info(f"[WORKTREE] Added to info/exclude: {missing}")
        except Exception as e:
            logger.warning(f"[WORKTREE] Could not update info/exclude: {e}")

    def _agent_record(self, session: Session, agent_id: str) -> Optional[AgentBranch]:
        return session.query(AgentBranch).filter_by(agent_id=agent_id).first()

    def _agent_repo(self, agent_id: str) -> Repo:
        """Open the Repo handle for an agent's worktree."""
        with self.db_manager.session_scope() as session:
            record = self._agent_record(session, agent_id)
            if not record or not record.worktree_path:
                raise ValueError(f"No worktree record for agent {agent_id}")
            return Repo(record.worktree_path)

    # ── Worktree creation ────────────────────────────────────────

    def create_agent_worktree(
        self,
        agent_id: str,
        parent_agent_id: Optional[str] = None,
        base_commit_sha: Optional[str] = None,
        context_files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Create an isolated git worktree + branch for an agent.

        Args:
            agent_id: Unique agent identifier
            parent_agent_id: Optional parent agent for inheritance
            base_commit_sha: Optional specific commit to branch from
            context_files: Optional {relative_path: content} written into the
                worktree's git-excluded .hephaestus/ inbound-context dir.

        Returns:
            Dict with branch_name, parent_commit, and working_directory (the
            worktree path — the only directory the agent should ever see).
        """
        logger.info(
            f"[WORKTREE] Creating worktree for agent {agent_id} (parent={parent_agent_id})"
        )

        with self.db_manager.session_scope() as session:
            try:
                # Determine parent commit
                if base_commit_sha:
                    parent_commit_sha = base_commit_sha
                elif parent_agent_id:
                    parent_commit_sha = self._get_parent_commit(parent_agent_id, session)
                    if not parent_commit_sha:
                        parent_commit_sha = self.main_repo.head.commit.hexsha
                        logger.info(
                            f"[WORKTREE] Parent has no commits, using main HEAD: {parent_commit_sha[:8]}"
                        )
                else:
                    parent_commit_sha = self.main_repo.head.commit.hexsha
                    logger.info(f"[WORKTREE] Using main HEAD: {parent_commit_sha[:8]}")

                branch_name = f"{self.config.git.branch_prefix}{agent_id}"

                # Create branch from parent commit
                try:
                    self.main_repo.git.branch(branch_name, parent_commit_sha)
                    logger.info(
                        f"[WORKTREE] Created branch {branch_name} from {parent_commit_sha[:8]}"
                    )
                except GitCommandError as e:
                    if "already exists" in str(e):
                        logger.info(
                            f"[WORKTREE] Branch exists, recreating from {parent_commit_sha[:8]}"
                        )
                        self.main_repo.git.branch("-D", branch_name)
                        self.main_repo.git.branch(branch_name, parent_commit_sha)
                    elif "not a valid branch point" in str(
                        e
                    ) or "not a valid object name" in str(e):
                        logger.warning(
                            f"[WORKTREE] Commit {parent_commit_sha[:8]} not found, falling back to main HEAD"
                        )
                        parent_commit_sha = self.main_repo.head.commit.hexsha
                        self.main_repo.git.branch(branch_name, parent_commit_sha)
                    else:
                        raise

                # Create the worktree checkout
                self._ensure_excludes()
                self.worktree_base.mkdir(parents=True, exist_ok=True)
                worktree_path = self._worktree_path_for(agent_id)
                if worktree_path.exists():
                    # This path is about to be overwritten by the fresh worktree
                    # `git worktree add` creates right below -- whatever's here
                    # is being intentionally replaced, not swept up as "stale".
                    self._remove_worktree(str(worktree_path), require_clean=False)
                try:
                    self.main_repo.git.worktree("add", str(worktree_path), branch_name)
                except GitCommandError:
                    # Stale admin entry — prune and retry once
                    self.main_repo.git.worktree("prune")
                    self.main_repo.git.worktree("add", str(worktree_path), branch_name)
                logger.info(f"[WORKTREE] Worktree ready at {worktree_path}")

                # Populate inbound context (git-excluded inside the worktree)
                context_dir = worktree_path / CONTEXT_DIR_NAME
                context_dir.mkdir(parents=True, exist_ok=True)
                if context_files:
                    for rel_path, content in context_files.items():
                        dest = context_dir / rel_path
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        dest.write_text(content)
                    logger.info(
                        f"[WORKTREE] Wrote {len(context_files)} context file(s) to {context_dir}"
                    )

                # Record in database
                record = AgentBranch(
                    agent_id=agent_id,
                    worktree_path=str(worktree_path),
                    branch_name=branch_name,
                    parent_agent_id=parent_agent_id,
                    parent_commit_sha=parent_commit_sha,
                    base_commit_sha=parent_commit_sha,
                    merge_status="active",
                )
                session.add(record)

                return {
                    "branch_name": branch_name,
                    "parent_commit": parent_commit_sha,
                    "working_directory": str(worktree_path),
                    "context_dir": str(context_dir),
                }

            except Exception as e:
                logger.error(f"[WORKTREE] Failed to create worktree for {agent_id}: {e}")
                raise

    # Upstream-compatible name for the same operation.
    def create_agent_branch(
        self,
        agent_id: str,
        parent_agent_id: Optional[str] = None,
        base_commit_sha: Optional[str] = None,
        context_files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Deprecated alias for create_agent_worktree."""
        return self.create_agent_worktree(
            agent_id, parent_agent_id, base_commit_sha, context_files
        )

    def switch_to_branch(self, branch_name: str) -> None:
        """No-op: each agent has its own worktree, so the main repo never switches.

        Kept for call-site compatibility.
        """
        logger.debug(
            f"[WORKTREE] switch_to_branch({branch_name}) is a no-op (worktree isolation)"
        )

    def switch_to_main(self) -> None:
        """No-op under worktree isolation (main repo stays on the base branch)."""
        logger.debug("[WORKTREE] switch_to_main() is a no-op (worktree isolation)")

    # ── Commit operations ────────────────────────────────────────

    def _commit_in_worktree(
        self, agent_id: str, message: str, commit_type: str
    ) -> Dict[str, Any]:
        """Stage and commit all changes in the agent's worktree."""
        repo = self._agent_repo(agent_id)
        repo.git.add("-A")

        if not repo.is_dirty() and not repo.untracked_files:
            return {
                "commit_sha": repo.head.commit.hexsha,
                "files_changed": 0,
                "message": "No changes",
            }

        repo.git.commit("-m", f"[Agent {agent_id}] {message}", "--no-verify")
        commit = repo.head.commit
        stats = commit.stats.total

        with self.db_manager.session_scope() as session:
            session.add(
                WorktreeCommit(
                    id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    commit_sha=commit.hexsha,
                    commit_type=commit_type,
                    commit_message=message,
                    files_changed=stats.get("files", 0),
                    insertions=stats.get("insertions", 0),
                    deletions=stats.get("deletions", 0),
                )
            )

        return {
            "commit_sha": commit.hexsha,
            "files_changed": stats.get("files", 0),
            "message": message,
        }

    def commit_changes(
        self, agent_id: str, message: str, branch_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Stage and commit all changes in the agent's worktree."""
        return self._commit_in_worktree(agent_id, message, "auto_save")

    def commit_for_validation(
        self, agent_id: str, iteration: int, message: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a checkpoint commit in the agent's worktree for validation."""
        msg = message or f"Iteration {iteration} - Ready for validation"
        return self._commit_in_worktree(agent_id, msg, "validation_ready")

    # ── Merge operations ─────────────────────────────────────────

    def merge_to_main(self, agent_id: str) -> Dict[str, Any]:
        """Merge an agent's worktree branch into the base branch.

        The agent's work is committed in its own worktree first; the main repo
        (clean, on the base branch) then merges the branch. Conflicts resolve
        with newest-file-wins.
        """
        logger.info(f"[WORKTREE:{agent_id}] ========== MERGE TO MAIN START ==========")
        start_time = datetime.utcnow()
        lock_file = None
        stashed = False

        session = self.db_manager.get_session()
        try:
            lock_file = self._merge_lock.acquire(agent_id)

            record = self._agent_record(session, agent_id)
            if not record:
                raise ValueError(f"No worktree record found for agent {agent_id}")
            branch_name = record.branch_name
            target_branch = self.config.git.base_branch
            logger.info(
                f"[WORKTREE:{agent_id}] Merging branch {branch_name} -> {target_branch}"
            )

            # Commit any uncommitted work in the agent's worktree first
            try:
                wt_repo = Repo(record.worktree_path)
                wt_repo.git.add("-A")
                if wt_repo.is_dirty() or wt_repo.untracked_files:
                    wt_repo.git.commit(
                        "-m",
                        f"[Agent {agent_id}] Final - Task completed",
                        "--no-verify",
                    )
                    final = wt_repo.head.commit
                    session.add(
                        WorktreeCommit(
                            id=str(uuid.uuid4()),
                            agent_id=agent_id,
                            commit_sha=final.hexsha,
                            commit_type="final",
                            commit_message=f"[Agent {agent_id}] Final - Task completed",
                            files_changed=final.stats.total.get("files", 0),
                        )
                    )
            except Exception as e:
                logger.warning(
                    f"[WORKTREE:{agent_id}] Could not finalize worktree commit: {e}"
                )

            # Ensure main repo is on the base branch and clean
            if self.main_repo.active_branch.name != target_branch:
                self.main_repo.heads[target_branch].checkout()

            # Abort any in-progress merge from a previous failed attempt
            try:
                self.main_repo.git.merge("--abort")
            except GitCommandError:
                pass  # No merge in progress

            # Hard reset to clean state — previous failed merges leave conflicts
            try:
                self.main_repo.git.reset("--hard", "HEAD")
                self.main_repo.git.clean("-fd")
            except GitCommandError:
                pass

            if self.main_repo.is_dirty() or self.main_repo.untracked_files:
                try:
                    self.main_repo.git.stash(
                        "push", "-u", "-m", f"Auto-stash before merge for {agent_id}"
                    )
                    stashed = True
                except GitCommandError:
                    pass

            conflicts_resolved = []
            try:
                self.main_repo.git.merge(
                    branch_name,
                    no_ff=True,
                    m=f"Merge agent {agent_id} into {target_branch}",
                )
                merge_commit_sha = self.main_repo.head.commit.hexsha
                status = "success"
                logger.info(f"[WORKTREE:{agent_id}] Merge completed (no conflicts)")
            except GitCommandError as e:
                err_str = str(e)
                if "CONFLICT" in err_str or "unresolved conflict" in err_str:
                    logger.info(
                        f"[WORKTREE:{agent_id}] Conflicts detected, resolving (newest-file-wins)"
                    )
                    conflicts_resolved = self._conflict_resolver.resolve(
                        agent_id, session, self.main_repo
                    )
                    self.main_repo.git.commit(
                        "-m",
                        f"[Auto-Merge] Resolved conflicts for agent {agent_id}",
                        "--no-verify",
                    )
                    merge_commit_sha = self.main_repo.head.commit.hexsha
                    status = "conflict_resolved"
                else:
                    logger.error(f"[WORKTREE:{agent_id}] Merge failed: {e}")
                    raise

            record.merge_status = "merged"
            record.merged_at = datetime.utcnow()
            record.merge_commit_sha = merge_commit_sha
            session.commit()

            if stashed:
                try:
                    self.main_repo.git.stash("pop")
                except GitCommandError as e:
                    logger.warning(f"[WORKTREE:{agent_id}] Stash pop conflict: {e}")

            elapsed_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            logger.info(
                f"[WORKTREE:{agent_id}] ========== MERGE COMPLETE ({elapsed_ms}ms) =========="
            )

            return {
                "status": status,
                "merged_to": target_branch,
                "commit_sha": merge_commit_sha,
                "conflicts_resolved": conflicts_resolved,
                # ConflictResolver always runs newest-file-wins,
                # unconditionally -- this used to echo
                # self.config.conflict_resolution_strategy, a config field
                # that was never actually branched on anywhere (removed).
                "resolution_strategy": "newest_file_wins",
                "total_conflicts": len(conflicts_resolved),
                "resolution_time_ms": elapsed_ms,
            }

        except Exception as e:
            logger.error(f"[WORKTREE:{agent_id}] Merge failed: {e}", exc_info=True)
            session.rollback()
            if stashed:
                try:
                    self.main_repo.git.stash("pop")
                except GitCommandError as stash_err:
                    logger.error(
                        f"[WORKTREE:{agent_id}] Failed to restore stashed changes "
                        f"after the merge failure above -- they remain in the "
                        f"stash list, not the working tree: {stash_err}"
                    )
            raise
        finally:
            if lock_file:
                self._merge_lock.release(lock_file, agent_id)
            session.close()

    # ── Helpers ──────────────────────────────────────────────────

    def _get_parent_commit(self, parent_id: str, session: Session) -> Optional[str]:
        """Get the commit SHA a child agent should branch from.

        Commits any uncommitted work in the parent's worktree first so the child
        inherits it. Falls back to main HEAD if the parent has been merged/cleaned.
        """
        parent = self._agent_record(session, parent_id)
        if not parent:
            return None

        try:
            if parent.worktree_path and Path(parent.worktree_path).exists():
                prepo = Repo(parent.worktree_path)
                prepo.git.add("-A")
                if prepo.is_dirty() or prepo.untracked_files:
                    prepo.git.commit(
                        "-m",
                        f"[Agent {parent_id}] Checkpoint before spawning child",
                        "--no-verify",
                    )
                    commit = prepo.head.commit
                    session.add(
                        WorktreeCommit(
                            id=str(uuid.uuid4()),
                            agent_id=parent_id,
                            commit_sha=commit.hexsha,
                            commit_type="parent_checkpoint",
                            commit_message="Checkpoint before spawning child",
                            files_changed=commit.stats.total.get("files", 0),
                        )
                    )
                    session.flush()
                    return commit.hexsha
                return prepo.head.commit.hexsha
        except Exception as e:
            logger.warning(
                f"[WORKTREE] Could not read parent {parent_id} worktree: {e}"
            )

        # Parent merged/cleaned — branch from its recorded commit or main HEAD
        return parent.parent_commit_sha or self.main_repo.head.commit.hexsha

    def get_workspace_changes(
        self, agent_id: str, since_commit: Optional[str] = None
    ) -> Dict[str, Any]:
        """Get the diff for an agent's changes within its worktree."""
        with self.db_manager.session_scope() as session:
            record = self._agent_record(session, agent_id)
            if not record:
                raise ValueError(f"No worktree record for agent {agent_id}")

            repo = Repo(record.worktree_path)
            base = since_commit or record.parent_commit_sha
            current = repo.head.commit

            diff_index = repo.commit(base).diff(current)
            created, modified, deleted = [], [], []

            for d in diff_index:
                if d.new_file:
                    created.append(d.b_path)
                elif d.deleted_file:
                    deleted.append(d.a_path)
                elif d.renamed_file:
                    deleted.append(d.a_path)
                    created.append(d.b_path)
                else:
                    modified.append(d.b_path or d.a_path)

            diff_stats = repo.git.diff(base, current.hexsha, "--stat")
            insertions = deletions = 0
            for line in diff_stats.split("\n"):
                for part in line.split(","):
                    if "insertion" in part:
                        insertions = int(part.strip().split()[0])
                    elif "deletion" in part:
                        deletions = int(part.strip().split()[0])

            return {
                "files_created": created,
                "files_modified": modified,
                "files_deleted": deleted,
                "total_changes": len(created) + len(modified) + len(deleted),
                "stats": {"insertions": insertions, "deletions": deletions},
                "detailed_diff": repo.git.diff(base, current.hexsha),
            }

    def get_agent_branch_path(self, agent_id: str) -> Optional[str]:
        """Get the working directory (worktree path) for an agent.

        Returns None, not the main repo path, when no AgentBranch record
        exists -- fail loudly, don't silently redirect a caller into the
        main project repository (matching create_agent_for_task's own
        convention). Every caller of this method already treats a falsy
        return as "no path found" (a truthy main-repo path used to satisfy
        those checks by accident, letting e.g. restart_agent silently
        relaunch an agent into the main repo instead of failing).
        """
        with self.db_manager.session_scope() as session:
            record = self._agent_record(session, agent_id)
            if record and record.worktree_path:
                return record.worktree_path
            return None

    def merge_to_parent(self, agent_id: str) -> Dict[str, Any]:
        """Alias for merge_to_main — merges the agent's branch into the base branch."""
        return self.merge_to_main(agent_id)

    # ── Cleanup ──────────────────────────────────────────────────

    def _remove_worktree(self, worktree_path: str, require_clean: bool = True) -> None:
        """Remove a git worktree and its directory.

        Delegates to WorktreeRemover (SOLID review 4.5) -- kept as a thin
        method here, not inlined at call sites, since
        tests/test_cleanup_stale_branches_race.py patches/calls this method
        by name directly.
        """
        self._worktree_remover.remove(self.main_repo, worktree_path, require_clean)

    def cleanup_worktree(
        self, agent_id: str, delete_branch: bool = False
    ) -> Dict[str, Any]:
        """Remove an agent's worktree directory.

        Args:
            delete_branch: If True, also delete the branch (discard semantics —
                use for failed agents or after a successful merge). If False
                (default), the branch is preserved for history.
        """
        with self.db_manager.session_scope() as session:
            record = self._agent_record(session, agent_id)
            if not record:
                return {"status": "not_found"}

            if record.worktree_path:
                # Both live callers (cleanup_branch, discard_agent) pass
                # delete_branch=True -- explicit discard semantics ("failed
                # work never touches main"), so any uncommitted changes here
                # are meant to be thrown away, not treated as at-risk work.
                self._remove_worktree(record.worktree_path, require_clean=False)
                logger.info(f"[WORKTREE] Removed worktree {record.worktree_path}")

            if delete_branch:
                try:
                    self.main_repo.git.branch("-D", record.branch_name)
                    logger.info(f"[WORKTREE] Deleted branch {record.branch_name}")
                except GitCommandError as e:
                    logger.warning(f"[WORKTREE] Could not delete branch: {e}")

            record.merge_status = "cleaned"
            return {
                "status": "cleaned",
                "branch": record.branch_name,
                "branch_preserved": not delete_branch,
            }

    def discard_agent(self, agent_id: str) -> Dict[str, Any]:
        """Discard a failed agent: remove worktree + branch, nothing merged.

        This is what replaces the Repair flow — failed work never touches main.
        """
        return self.cleanup_worktree(agent_id, delete_branch=True)

    def merge_shared_branch(
        self,
        branch_name: str,
        *,
        message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Merge a branch into the base branch, abort-and-preserve on conflict.

        This is the single merge primitive for all worktree cleanup paths.
        On conflict: abort the merge and preserve the branch for manual
        resolution — never auto-resolve (newest-file-wins is too risky for
        design-level merges) and never force-delete.

        Returns {"action": "merged", "branch": branch_name} on success,
        {"action": "preserved", "branch": branch_name} on conflict,
        or {"action": "skipped", "branch": branch_name} if the branch
        doesn't exist.
        """
        try:
            self.main_repo.git.rev_parse("--verify", branch_name)
        except GitCommandError:
            return {"action": "skipped", "branch": branch_name}

        msg = message or f"[Cleanup] Merged {branch_name}"
        try:
            self.main_repo.git.merge(
                branch_name, no_ff=True, m=msg
            )
            return {"action": "merged", "branch": branch_name}
        except GitCommandError as e:
            if "CONFLICT" in str(e):
                logger.warning(
                    f"[WORKTREE] Merge conflict on {branch_name} -> "
                    f"{self.config.git.base_branch}, aborting -- branch preserved "
                    f"for manual merge/PR"
                )
                try:
                    self.main_repo.git.merge("--abort")
                except GitCommandError:
                    pass
                return {"action": "preserved", "branch": branch_name}
            raise

    def cleanup_all_stale_branches(self) -> Dict[str, Any]:
        """Clean up worktrees and branches from terminated/stale agents.

        1. Prune and remove stale worktrees.
        2. Merge active branches into main (newest-file-wins on conflict).
        3. Delete branches (force-delete unmergeable ones).
        """
        with self.db_manager.session_scope() as session:
            cleaned: List[str] = []
            merged: List[str] = []
            failed: List[str] = []
            worktrees_cleaned = 0
            target_branch = self.config.git.base_branch
            # Deliberately unguarded, matching merge_to_main's identical
            # checkout above -- a swallowed failure here previously let
            # execution continue with main_repo still checked out on
            # whatever branch it actually had, and merge_shared_branch's
            # `git merge <branch>` merges into whichever branch is
            # currently checked out, not target_branch by name. Every
            # branch this function then "successfully" merges gets
            # force-deleted afterward (_merge_and_delete), so a silent
            # checkout failure here meant agent work silently merged into
            # the wrong branch and its real branch was deleted -- a
            # real, silent corruption/data-loss bug. Failing loudly here
            # aborts the whole cleanup pass instead, which both call
            # sites already handle correctly (control_routes.py's own
            # try/except turns it into a proper 500; the background
            # thread in queue_routes.py just logs to stderr).
            if self.main_repo.active_branch.name != target_branch:
                self.main_repo.heads[target_branch].checkout()

            # Step 1: remove all linked worktrees (except the main one and any
            # currently claimed by an active workflow).
            #
            # This used to remove every linked worktree unconditionally, with
            # no check for whether a workflow was still using it -- despite
            # the "stale" naming, "stale" was never actually verified. This
            # runs from a background thread fired by /autopilot/queue/rerun
            # at the same moment a brand-new orchestrator process starts and
            # creates a fresh Phase 0 worktree at a deterministic,
            # design-derived path (same path reused across every retry of the
            # same design) -- so this could, and reliably did, delete the
            # brand-new worktree the new run had just created or was about to
            # use, moments after Rerun was clicked. Observed live: a Feature
            # Architect run completed successfully, but its worktree vanished
            # ~16s later and the whole design got marked "failed" even though
            # nothing had actually gone wrong.
            # Resolved (not raw) paths: `git worktree list` reports canonical
            # paths, but Workflow.working_directory may be stored unresolved
            # (e.g. macOS's /var -> /private/var symlink) -- comparing raw
            # strings would silently never match and defeat this guard.
            #
            # "paused" is protected here too, not just "active": pause_feature
            # (autopilot_api.py) sets a workflow to "paused" while deliberately
            # keeping working_directory intact so _resume_interrupted_workflows
            # can restart the agent on its "existing worktree branch (prior
            # commits + context intact)" later. Only excluding "active" left
            # every paused workflow exposed to the identical worktree-deletion
            # race this guard exists to fix -- found on review.
            active_working_directories = {
                str(Path(wf.working_directory).resolve())
                for wf in session.query(Workflow)
                .filter(Workflow.status.in_(["active", "paused"]))
                .all()
                if wf.working_directory
            }
            # Resolved, not raw: git worktree list --porcelain reports the
            # canonical path for EVERY entry, including the main repo's own.
            # self.main_repo.working_dir is whatever unresolved form the Repo
            # object was opened with. On any system where the repo lives under
            # a symlink (guaranteed on macOS: /var -> /private/var), a raw
            # string comparison would NEVER match -- meaning a "skip the main
            # repo" guard built that way silently never works, and this
            # function would try to `git worktree remove` the main project
            # repository itself, falling back to a raw shutil.rmtree on
            # failure. Confirmed live via direct reproduction. (_remove_worktree
            # itself also hard-refuses the main repo path as defense-in-depth,
            # independent of this check.)
            main_repo_path = str(Path(self.main_repo.working_dir).resolve())
            # Branch checked out at each active workflow's worktree, so Step 2
            # below can skip it too -- protecting the worktree directory alone
            # isn't enough: `git merge <branch>` doesn't require the branch to
            # be un-checked-out anywhere, so an active workflow's in-progress
            # branch could still get merged into main (with whatever partial
            # commits exist so far) and force-deleted out from under it, even
            # though its worktree directory now survives.
            active_branch_names = set()
            try:
                # prune first (removes admin entries for worktrees whose
                # directories are already gone -- never touches ones that
                # still exist on disk), then a single porcelain listing
                # serves both the removal loop and active-branch collection
                # instead of parsing the same output twice.
                self.main_repo.git.worktree("prune")
                blocks = self.main_repo.git.worktree("list", "--porcelain").split(
                    "\n\n"
                )
                for wt in blocks:
                    lines = wt.strip().split("\n")
                    if not lines or not lines[0].startswith("worktree "):
                        continue
                    wt_path = lines[0].split(" ", 1)[1]
                    resolved = str(Path(wt_path).resolve())
                    if resolved == main_repo_path:
                        continue
                    if resolved in active_working_directories:
                        for line in lines[1:]:
                            if line.startswith("branch "):
                                ref = line.split(" ", 1)[1]
                                active_branch_names.add(ref.removeprefix("refs/heads/"))
                        continue
                    # Default require_clean=True: this is a generic "is this
                    # actually stale" sweep, not an intentional discard --
                    # Workflow.status alone (all that active_working_
                    # directories above is built from) isn't proof this
                    # worktree has no real, uncommitted work left in it.
                    self._remove_worktree(wt_path)
                    worktrees_cleaned += 1
            except GitCommandError:
                pass

            # Step 1b: null out Workflow.working_directory for any terminal
            # (non-active/paused) workflow whose directory isn't a real git
            # worktree anymore -- not just the ones removed by Step 1 just
            # above. A worktree can also disappear via `git worktree remove`
            # called directly (_cleanup_worktree in orchestrator.py, run at
            # each feature pipeline's own completion) or by anything else
            # outside this function entirely; once gone, it drops out of
            # `git worktree list` and Step 1 never sees it again. Checking
            # bare directory existence isn't enough: observed live, some
            # code keeps appending tmux transcripts to a removed worktree's
            # .hephaestus/tmux/ path after `git worktree remove` deletes
            # everything, which resurrects an empty parent directory on
            # disk -- .exists() then reports True for a worktree that's
            # genuinely gone. `.git` is what `git worktree remove` actually
            # controls (it's the one thing that distinguishes a real
            # worktree from a plain directory), so its absence is the
            # correct signal. Left stale, a terminal workflow's
            # working_directory keeps pointing at a dead path forever --
            # silently breaking any later lookup of that workflow's docs
            # (_resolve_feature_docs_base trusted the stale path instead of
            # falling back to project_path).
            for wf in (
                session.query(Workflow)
                .filter(
                    Workflow.working_directory.isnot(None),
                    Workflow.status.notin_(["active", "paused"]),
                )
                .all()
            ):
                if not (Path(wf.working_directory) / ".git").exists():
                    wf.working_directory = None

            # Step 2: merge + delete tracked active branches (skipping any
            # branch still checked out at an active workflow's worktree --
            # see active_branch_names above. Also skips AgentBranch rows:
            # merge_status="active" only means "not yet merged/cleaned", the
            # state for every currently-in-progress agent's branch, not "the
            # agent finished" -- without this, any agent still genuinely
            # working when this runs would have its branch merged into main
            # and deleted mid-task.)
            records = [
                r
                for r in session.query(AgentBranch)
                .filter(AgentBranch.merge_status.in_(["active", None]))
                .all()
                if r.branch_name not in active_branch_names
            ]
            tracked_branches = {r.branch_name for r in records} | active_branch_names
            all_branches = [b.name for b in self.main_repo.branches]
            # self.config.branch_prefix, not a hardcoded "agent-": the
            # config default is "agent-" but it's overridable (git.
            # branch_prefix / BRANCH_PREFIX), and create_agent_worktree
            # (above, line 281) already builds real branch names from this
            # same attribute -- a hardcoded literal here would silently
            # stop matching the moment someone overrides the prefix.
            # "autopilot-" dropped: no code anywhere constructs a branch
            # with that prefix (confirmed via full-repo grep), it never
            # matched anything. "feature/" added: covers both
            # f"feature/{design}" (Phase 0 -> feature_architect handoff,
            # orchestrator/__init__.py) and f"feature/{design}/{feature}"
            # (per-feature branches, worktree_integration.py) -- neither
            # was covered before, so branches from every feature pipeline
            # run were permanently exempt from this cleanup.
            untracked_branches = [
                b
                for b in all_branches
                # "autopilot-" is the pre-rename legacy prefix and must stay:
                # dropping it silently stops reclaiming every branch created
                # before the rename (test_legacy_autopilot_prefixed_branch_
                # still_cleaned_up covers exactly that). config.branch_prefix
                # is "agent-" by default but is configurable, so list both.
                if b.startswith(
                    (
                        self.config.git.branch_prefix,
                        "agent-",
                        "autopilot-",
                        "feature_architect/",
                        "feature/",
                    )
                )
                and b not in tracked_branches
            ]

            def _merge_and_delete(branch_name: str, agent_id: Optional[str]) -> None:
                result = self.merge_shared_branch(branch_name)
                action = result["action"]
                if action == "merged":
                    merged.append(branch_name)
                    # Delete the branch after successful merge.
                    try:
                        self.main_repo.git.branch("-D", branch_name)
                        cleaned.append(branch_name)
                    except GitCommandError:
                        failed.append(branch_name)
                elif action == "preserved":
                    # Conflict — branch preserved for manual resolution.
                    # Do NOT delete it.
                    failed.append(branch_name)
                else:
                    # Branch doesn't exist.
                    cleaned.append(branch_name)

            for record in records:
                _merge_and_delete(record.branch_name, record.agent_id)
                record.merge_status = "cleaned"
            for branch_name in untracked_branches:
                _merge_and_delete(branch_name, None)

            # Step 4: reconcile disk against the database, both directions.
            #
            # Everything above is DB-driven: it walks AgentBranch rows and
            # git branches. Neither reaches a worktree DIRECTORY that has no
            # row. create_agent_worktree does its git work (worktree add +
            # branch) and only then writes the record and commits, so a
            # failure in that window leaks a directory nothing can ever
            # reclaim -- observed live: seven orphans accumulated under
            # .worktrees/, invisible to every sweep, and had to be removed by
            # hand. `git worktree prune` does not help: it only drops admin
            # entries for directories that are already gone.
            reconciled_dirs, preserved_dirs, reconciled_rows = (
                self._reconcile_worktrees_with_db(session)
            )

            return {
                "cleaned": len(cleaned),
                "merged": len(merged),
                "failed": len(failed),
                "worktrees_cleaned": worktrees_cleaned,
                "branches": cleaned,
                "orphan_worktrees_reclaimed": reconciled_dirs,
                "orphan_worktrees_preserved": preserved_dirs,
                "stale_rows_reconciled": reconciled_rows,
            }

    def _reconcile_worktrees_with_db(self, session) -> Tuple[int, int, int]:
        """Reconcile worktree directories against AgentBranch rows.

        Returns (reclaimed_dirs, preserved_dirs, reconciled_rows).

        Direction 1 -- a directory under this repo's worktree base with no
        AgentBranch row. Reclaimed only if it is provably disposable: clean
        working tree, and its branch holds no commits the base branch lacks.
        Anything else is preserved and logged, matching the abort-and-preserve
        strategy merge_shared_branch settled on -- an orphan we cannot explain
        is exactly the case where destroying work would be unrecoverable.

        Direction 2 -- a row marked "active" whose directory is gone. Marked
        "cleaned" so the table stops claiming a live worktree. Scoped to rows
        under THIS repo's worktree base: agent_worktrees is shared across every
        project the installation has run, cleanup_all_stale_branches only ever
        sees one repo, and a path being absent from here says nothing about
        whether another project's worktree exists. Measured on this database:
        56 of 176 rows were "active" with a missing directory, all belonging to
        a different project, none reconcilable from here.
        """
        reclaimed = preserved = rows_fixed = 0
        base = self.worktree_base
        main_repo_path = str(Path(self.main_repo.working_dir).resolve())

        tracked_paths = set()
        for r in session.query(AgentBranch).all():
            if r.worktree_path:
                try:
                    tracked_paths.add(str(Path(r.worktree_path).resolve()))
                except OSError:
                    tracked_paths.add(r.worktree_path)

        # A worktree can be legitimately live with NO AgentBranch row -- the
        # shared feature_architect/phase-0 worktrees are tracked by a Workflow's
        # working_directory instead. Without this, "has no AgentBranch row" reads
        # as "orphaned" and reconciliation deletes a worktree an active workflow
        # is using. That is the exact race
        # test_cleanup_stale_branches_race.py::test_active_workflows_worktree_
        # survives_cleanup exists for, and it caught this on the first run.
        # Paused counts as live for the same reason the sweep above says so:
        # _resume_interrupted_workflows restarts from that worktree later.
        tracked_paths |= {
            str(Path(wf.working_directory).resolve())
            for wf in session.query(Workflow)
            .filter(Workflow.status.in_(["active", "paused"]))
            .all()
            if wf.working_directory
        }

        # Direction 1: directories with no row.
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if not d.is_dir() or not d.name.startswith("wt_"):
                    continue
                resolved = str(d.resolve())
                if resolved in tracked_paths or resolved == main_repo_path:
                    continue
                why = self._worktree_remover.orphan_blocker(
                    self.main_repo, self.config.git.base_branch, d
                )
                if why:
                    preserved += 1
                    logger.warning(
                        f"[RECONCILE] Orphaned worktree {d.name} has no "
                        f"AgentBranch row but is not safe to reclaim ({why}) "
                        "-- preserving for manual review"
                    )
                    continue
                try:
                    self._remove_worktree(str(d), require_clean=True)
                    reclaimed += 1
                    logger.info(
                        f"[RECONCILE] Reclaimed orphaned worktree {d.name} "
                        "(no AgentBranch row, clean, nothing unmerged)"
                    )
                except Exception as e:
                    preserved += 1
                    logger.warning(
                        f"[RECONCILE] Failed to reclaim orphan {d.name}: {e}"
                    )

        # Direction 2: rows under this repo whose directory is gone.
        base_str = str(base.resolve()) if base.exists() else str(base)
        for r in (
            session.query(AgentBranch)
            .filter(AgentBranch.merge_status == "active")
            .all()
        ):
            path = r.worktree_path or ""
            if not path.startswith(base_str):
                continue  # another project's row -- not this sweep's to judge
            if Path(path).is_dir():
                continue
            r.merge_status = "cleaned"
            rows_fixed += 1
            logger.info(
                f"[RECONCILE] agent_worktrees row for {(r.agent_id or '?')[:8]} "
                f"claimed active but {path} does not exist -- marked cleaned"
            )

        return reclaimed, preserved, rows_fixed


# Backward-compatible alias for call sites that still import WorktreeManager.
