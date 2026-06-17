"""Git branch manager for agent isolation.

Replaces worktree_manager.py — agents work on branches in the main repo
instead of isolated worktrees. Simpler, fewer edge cases, and compatible
with tools like opencode that need the full project directory.
"""

import os
import uuid
import logging
import fcntl
import time
from pathlib import Path
from typing import Optional, Dict, List, Any
from datetime import datetime
from dataclasses import dataclass
from enum import Enum

import git
from git import Repo, GitCommandError
from sqlalchemy.orm import Session

from src.core.database import (
    DatabaseManager,
    AgentWorktree,
    WorktreeCommit,
    MergeConflictResolution,
)
from src.core.simple_config import get_config

logger = logging.getLogger(__name__)


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


class BranchManager:
    """Manages git branches for agent isolation.

    Agents work directly in the main repo on their own branches.
    No worktrees — simpler and compatible with all tools.
    """

    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self.config = get_config()

        try:
            self.main_repo = Repo(self.config.main_repo_path)
        except git.InvalidGitRepositoryError:
            logger.error(f"Invalid git repository at {self.config.main_repo_path}")
            raise ValueError(f"Not a git repository: {self.config.main_repo_path}")

        self.merge_lock_path = Path(self.config.main_repo_path) / ".git" / ".hephaestus_merge_lock"
        logger.info(f"BranchManager initialized for repo: {self.config.main_repo_path}")

    def reload(self, new_path):
        """Reinitialize with a new repository path."""
        from pathlib import Path as PathlibPath
        new_path = PathlibPath(new_path) if not isinstance(new_path, PathlibPath) else new_path
        try:
            self.main_repo = Repo(new_path)
        except git.InvalidGitRepositoryError:
            raise ValueError(f"Not a git repository: {new_path}")
        self.config.main_repo_path = new_path
        self.config.project_root = new_path
        logger.info(f"BranchManager reloaded with repo: {new_path}")

    # ── Lock management ──────────────────────────────────────────

    def _acquire_merge_lock(self, agent_id: str, timeout: int = 300):
        """Acquire exclusive lock for merge operations."""
        logger.info(f"[BRANCH:{agent_id}] Acquiring merge lock (timeout={timeout}s)")
        self.merge_lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.merge_lock_path.touch(exist_ok=True)

        lock_file = open(self.merge_lock_path, 'w')
        start_time = time.time()

        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elapsed = time.time() - start_time
                logger.info(f"[BRANCH:{agent_id}] Merge lock acquired after {elapsed:.2f}s")
                return lock_file
            except IOError:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    lock_file.close()
                    raise TimeoutError(f"[BRANCH:{agent_id}] Failed to acquire merge lock after {timeout}s")
                if int(elapsed) % 10 == 0:
                    logger.info(f"[BRANCH:{agent_id}] Waiting for merge lock... ({elapsed:.0f}s)")
                time.sleep(0.5)

    def _release_merge_lock(self, lock_file, agent_id: str):
        """Release merge lock."""
        logger.info(f"[BRANCH:{agent_id}] Releasing merge lock")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception as e:
            logger.error(f"[BRANCH:{agent_id}] Error releasing lock: {e}")

    # ── Branch operations ────────────────────────────────────────

    def create_agent_branch(
        self,
        agent_id: str,
        parent_agent_id: Optional[str] = None,
        base_commit_sha: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a git branch for an agent.

        The agent works on this branch in the main working directory.

        Args:
            agent_id: Unique agent identifier
            parent_agent_id: Optional parent agent for inheritance
            base_commit_sha: Optional specific commit to branch from

        Returns:
            Dict with branch_name and parent_commit
        """
        logger.info(f"[BRANCH] Creating branch for agent {agent_id} (parent={parent_agent_id})")

        session = self.db_manager.get_session()
        try:
            # Determine parent commit
            if base_commit_sha:
                parent_commit_sha = base_commit_sha
            elif parent_agent_id:
                parent_commit_sha = self._get_parent_commit(parent_agent_id, session)
                if not parent_commit_sha:
                    parent_commit_sha = self.main_repo.head.commit.hexsha
                    logger.info(f"[BRANCH] Parent has no commits, using main HEAD: {parent_commit_sha[:8]}")
            else:
                parent_commit_sha = self.main_repo.head.commit.hexsha
                logger.info(f"[BRANCH] Using main HEAD: {parent_commit_sha[:8]}")

            branch_name = f"{self.config.worktree_branch_prefix}{agent_id}"

            # Create branch from parent commit
            try:
                self.main_repo.git.branch(branch_name, parent_commit_sha)
                logger.info(f"[BRANCH] Created branch {branch_name} from {parent_commit_sha[:8]}")
            except GitCommandError as e:
                if "already exists" in str(e):
                    logger.info(f"[BRANCH] Branch exists, recreating from {parent_commit_sha[:8]}")
                    self.main_repo.git.branch("-D", branch_name)
                    self.main_repo.git.branch(branch_name, parent_commit_sha)
                else:
                    raise

            # Record in database (reusing AgentWorktree table for compatibility)
            record = AgentWorktree(
                agent_id=agent_id,
                worktree_path=str(self.config.project_root),  # Main repo, not a worktree
                branch_name=branch_name,
                parent_agent_id=parent_agent_id,
                parent_commit_sha=parent_commit_sha,
                base_commit_sha=parent_commit_sha,
                merge_status="active",
            )
            session.add(record)
            session.commit()

            logger.info(f"[BRANCH] Branch {branch_name} ready for agent {agent_id}")

            return {
                "branch_name": branch_name,
                "parent_commit": parent_commit_sha,
                "working_directory": str(self.config.project_root),
            }

        except Exception as e:
            logger.error(f"[BRANCH] Failed to create branch for {agent_id}: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    def switch_to_branch(self, branch_name: str) -> None:
        """Switch the main repo to a branch.

        Args:
            branch_name: Branch to checkout
        """
        self.main_repo.heads[branch_name].checkout()
        logger.info(f"[BRANCH] Switched to branch {branch_name}")

    def switch_to_main(self) -> None:
        """Switch back to the main branch."""
        target = self.config.base_branch
        self.main_repo.heads[target].checkout()
        logger.info(f"[BRANCH] Switched to {target}")

    # ── Commit operations ────────────────────────────────────────

    def commit_changes(self, agent_id: str, message: str, branch_name: Optional[str] = None) -> Dict[str, Any]:
        """Stage and commit all changes on the current branch.

        Args:
            agent_id: Agent identifier
            message: Commit message
            branch_name: Optional branch name for DB record

        Returns:
            Dict with commit_sha and stats
        """
        self.main_repo.git.add("-A")

        if not self.main_repo.is_dirty() and not self.main_repo.untracked_files:
            return {"commit_sha": self.main_repo.head.commit.hexsha, "files_changed": 0, "message": "No changes"}

        self.main_repo.git.commit("-m", f"[Agent {agent_id}] {message}", "--no-verify")
        commit = self.main_repo.head.commit
        stats = commit.stats.total

        session = self.db_manager.get_session()
        try:
            record = WorktreeCommit(
                id=str(uuid.uuid4()),
                agent_id=agent_id,
                commit_sha=commit.hexsha,
                commit_type="auto_save",
                commit_message=message,
                files_changed=stats.get("files", 0),
                insertions=stats.get("insertions", 0),
                deletions=stats.get("deletions", 0),
            )
            session.add(record)
            session.commit()
        finally:
            session.close()

        logger.info(f"[BRANCH] Committed {commit.hexsha[:8]} for {agent_id}: {stats}")
        return {"commit_sha": commit.hexsha, "files_changed": stats.get("files", 0), "message": message}

    def commit_for_validation(self, agent_id: str, iteration: int, message: Optional[str] = None) -> Dict[str, Any]:
        """Create checkpoint commit for validation."""
        msg = message or f"Iteration {iteration} - Ready for validation"
        return self.commit_changes(agent_id, msg)

    # ── Merge operations ─────────────────────────────────────────

    def merge_to_main(self, agent_id: str) -> Dict[str, Any]:
        """Merge agent's branch into main with automatic conflict resolution.

        Args:
            agent_id: Agent identifier

        Returns:
            Merge result dict
        """
        logger.info(f"[BRANCH:{agent_id}] ========== MERGE TO MAIN START ==========")
        start_time = datetime.utcnow()
        lock_file = None
        stashed = False

        session = self.db_manager.get_session()
        try:
            # Step 1: Acquire lock
            lock_file = self._acquire_merge_lock(agent_id)

            # Step 2: Get branch info
            worktree = session.query(AgentWorktree).filter_by(agent_id=agent_id).first()
            if not worktree:
                raise ValueError(f"No branch record found for agent {agent_id}")

            branch_name = worktree.branch_name
            logger.info(f"[BRANCH:{agent_id}] Merging branch {branch_name}")

            # Step 3: Ensure we're on main
            target_branch = self.config.base_branch
            self.main_repo.heads[target_branch].checkout()

            # Step 4: Stash any uncommitted changes in main
            if self.main_repo.is_dirty() or self.main_repo.untracked_files:
                logger.info(f"[BRANCH:{agent_id}] Stashing main repo changes")
                try:
                    self.main_repo.git.stash("push", "-u", "-m", f"Auto-stash before merge for {agent_id}")
                    stashed = True
                except GitCommandError:
                    pass

            # Step 5: Commit uncommitted changes on agent branch
            # Temporarily switch to agent branch to commit
            self.main_repo.heads[branch_name].checkout()
            if self.main_repo.is_dirty() or self.main_repo.untracked_files:
                self.main_repo.git.add("-A")
                self.main_repo.git.commit("-m", f"[Agent {agent_id}] Final - Task completed", "--no-verify")
                final = self.main_repo.head.commit
                session.add(WorktreeCommit(
                    id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    commit_sha=final.hexsha,
                    commit_type="final",
                    commit_message=f"[Agent {agent_id}] Final - Task completed",
                    files_changed=final.stats.total.get("files", 0),
                ))

            # Step 6: Switch back to main and merge
            self.main_repo.heads[target_branch].checkout()
            conflicts_resolved = []
            merge_commit_sha = None

            try:
                self.main_repo.git.merge(branch_name, no_ff=True, m=f"Merge agent {agent_id} into {target_branch}")
                merge_commit_sha = self.main_repo.head.commit.hexsha
                status = "success"
                logger.info(f"[BRANCH:{agent_id}] Merge completed (no conflicts)")
            except GitCommandError as e:
                if "CONFLICT" in str(e):
                    logger.info(f"[BRANCH:{agent_id}] Conflicts detected, resolving")
                    conflicts_resolved = self._resolve_conflicts(agent_id, session)
                    self.main_repo.git.commit(
                        "-m", f"[Auto-Merge] Resolved conflicts for agent {agent_id}",
                        "--no-verify",
                    )
                    merge_commit_sha = self.main_repo.head.commit.hexsha
                    status = "conflict_resolved"
                else:
                    raise

            # Step 7: Update DB
            worktree.merge_status = "merged"
            worktree.merged_at = datetime.utcnow()
            worktree.merge_commit_sha = merge_commit_sha
            session.commit()

            # Step 8: Restore stash
            if stashed:
                try:
                    self.main_repo.git.stash("pop")
                except GitCommandError as e:
                    logger.warning(f"[BRANCH:{agent_id}] Stash pop conflict: {e}")

            elapsed_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
            logger.info(f"[BRANCH:{agent_id}] ========== MERGE COMPLETE ({elapsed_ms}ms) ==========")

            return {
                "status": status,
                "merged_to": target_branch,
                "commit_sha": merge_commit_sha,
                "conflicts_resolved": conflicts_resolved,
                "resolution_strategy": self.config.conflict_resolution_strategy,
                "total_conflicts": len(conflicts_resolved),
                "resolution_time_ms": elapsed_ms,
            }

        except Exception as e:
            logger.error(f"[BRANCH:{agent_id}] Merge failed: {e}", exc_info=True)
            session.rollback()
            if stashed:
                try:
                    self.main_repo.git.stash("pop")
                except GitCommandError:
                    pass
            raise
        finally:
            if lock_file:
                self._release_merge_lock(lock_file, agent_id)
            session.close()

    def merge_main_into_branch(self, agent_id: str, branch_name: str) -> Dict[str, Any]:
        """Merge main branch into agent's branch to keep it up-to-date."""
        logger.info(f"[BRANCH:{agent_id}] Merging main into {branch_name}")

        session = self.db_manager.get_session()
        try:
            base_ref = self.config.base_branch
            base_commit = self.main_repo.heads[base_ref].commit.hexsha

            # Switch to agent branch
            self.main_repo.heads[branch_name].checkout()

            # Check if already up-to-date
            if self.main_repo.head.commit.hexsha == base_commit:
                return {"status": "up_to_date", "total_conflicts": 0}

            conflicts_resolved = []
            try:
                self.main_repo.git.merge(
                    base_commit, no_ff=True,
                    m=f"[Auto-Merge] Merged {base_ref} into {branch_name}",
                )
                status = "success"
            except GitCommandError as e:
                if "CONFLICT" in str(e):
                    conflicts_resolved = self._resolve_conflicts(agent_id, session)
                    self.main_repo.git.commit(
                        "-m", f"[Auto-Merge] Resolved conflicts merging main into {branch_name}",
                        "--no-verify",
                    )
                    status = "conflict_resolved"
                else:
                    raise

            # Switch back to main
            self.main_repo.heads[base_ref].checkout()

            return {
                "status": status,
                "total_conflicts": len(conflicts_resolved),
                "conflicts_resolved": conflicts_resolved,
            }
        finally:
            session.close()

    # ── Conflict resolution ──────────────────────────────────────

    def _resolve_conflicts(self, agent_id: str, session: Session) -> List[Dict]:
        """Resolve merge conflicts using newest-file-wins strategy."""
        conflicted = self.main_repo.git.diff("--name-only", "--diff-filter=U").splitlines()
        logger.info(f"[BRANCH:{agent_id}] Resolving {len(conflicted)} conflicts")

        resolved = []
        for file_path in conflicted:
            parent_ts = self._get_file_timestamp(self.main_repo, file_path, "HEAD")
            child_ts = self._get_file_timestamp(self.main_repo, file_path, "MERGE_HEAD")

            if parent_ts is None:
                parent_ts = datetime.utcnow()
            if child_ts is None:
                child_ts = datetime.utcnow()

            if child_ts > parent_ts:
                choice = "child"
                content = self._get_file_content(self.main_repo, file_path, "MERGE_HEAD")
            elif parent_ts > child_ts:
                choice = "parent"
                content = self._get_file_content(self.main_repo, file_path, "HEAD")
            else:
                choice = "tie_child"
                content = self._get_file_content(self.main_repo, file_path, "MERGE_HEAD")

            # Nuclear resolution: remove from index, write, re-add
            try:
                self.main_repo.git.rm("--cached", "-f", file_path)
            except GitCommandError:
                pass
            self._write_file_content(self.main_repo.working_dir, file_path, content)
            self.main_repo.git.add(file_path)

            session.add(MergeConflictResolution(
                id=str(uuid.uuid4()),
                agent_id=agent_id,
                file_path=file_path,
                parent_modified_at=parent_ts,
                child_modified_at=child_ts,
                resolution_choice=choice,
            ))

            resolved.append({"file": file_path, "resolution": choice})

        session.flush()
        return resolved

    # ── Helpers ──────────────────────────────────────────────────

    def _get_parent_commit(self, parent_id: str, session: Session) -> Optional[str]:
        """Get commit SHA to branch from for a child agent."""
        parent = session.query(AgentWorktree).filter_by(agent_id=parent_id).first()
        if not parent:
            return None

        # Commit any uncommitted changes on main before branching
        if self.main_repo.is_dirty() or self.main_repo.untracked_files:
            self.main_repo.git.add("-A")
            self.main_repo.git.commit("-m", f"[Agent {parent_id}] Checkpoint before spawning child", "--no-verify")
            commit = self.main_repo.head.commit
            session.add(WorktreeCommit(
                id=str(uuid.uuid4()),
                agent_id=parent_id,
                commit_sha=commit.hexsha,
                commit_type="parent_checkpoint",
                commit_message="Checkpoint before spawning child",
                files_changed=commit.stats.total.get("files", 0),
            ))
            session.flush()
            return commit.hexsha

        return self.main_repo.head.commit.hexsha

    def _get_file_timestamp(self, repo: Repo, file_path: str, ref: str = "HEAD") -> Optional[datetime]:
        try:
            commits = list(repo.iter_commits(ref, paths=file_path, max_count=1))
            if commits:
                return datetime.fromtimestamp(commits[0].committed_date)
        except Exception:
            pass
        return None

    def _get_file_content(self, repo: Repo, file_path: str, ref: str = "HEAD") -> str:
        try:
            full_path = Path(repo.working_dir) / file_path
            if full_path.exists():
                return full_path.read_text()
            return repo.git.show(f"{ref}:{file_path}")
        except Exception:
            return ""

    def _write_file_content(self, repo_dir: str, file_path: str, content: str):
        full_path = Path(repo_dir) / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    def get_workspace_changes(self, agent_id: str, since_commit: Optional[str] = None) -> Dict[str, Any]:
        """Get diff for agent's changes."""
        session = self.db_manager.get_session()
        try:
            record = session.query(AgentWorktree).filter_by(agent_id=agent_id).first()
            if not record:
                raise ValueError(f"No branch record for agent {agent_id}")

            base = since_commit or record.parent_commit_sha
            current = self.main_repo.head.commit

            diff_index = self.main_repo.commit(base).diff(current)
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

            diff_stats = self.main_repo.git.diff(base, current.hexsha, "--stat")
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
                "detailed_diff": self.main_repo.git.diff(base, current.hexsha),
            }
        finally:
            session.close()

    def cleanup_branch(self, agent_id: str) -> Dict[str, Any]:
        """Delete agent branch after merge.

        Branch is preserved for history — this just marks it as cleaned.
        """
        session = self.db_manager.get_session()
        try:
            record = session.query(AgentWorktree).filter_by(agent_id=agent_id).first()
            if not record:
                return {"status": "not_found"}

            # Try to delete the branch
            try:
                self.main_repo.git.branch("-D", record.branch_name)
                logger.info(f"[BRANCH] Deleted branch {record.branch_name}")
            except GitCommandError as e:
                logger.warning(f"[BRANCH] Could not delete branch: {e}")

            record.merge_status = "cleaned"
            session.commit()
            return {"status": "cleaned", "branch": record.branch_name}
        finally:
            session.close()


# ── Alias for backward compatibility ────────────────────────────
WorktreeManager = BranchManager
