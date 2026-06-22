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

import os
import uuid
import shutil
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
    AgentBranch,
    WorktreeCommit,
    MergeConflictResolution,
)
from src.core.simple_config import get_config

logger = logging.getLogger(__name__)

# Directories that must never be tracked or merged into main. Written to
# .git/info/exclude (shared across all linked worktrees) so the user's tracked
# .gitignore is left untouched.
EXCLUDE_ENTRIES = (".worktrees/", ".hephaestus/")

# Per-worktree inbound context directory (inside each worktree, git-excluded).
CONTEXT_DIR_NAME = ".hephaestus"


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

        try:
            self.main_repo = Repo(self.config.main_repo_path)
        except git.InvalidGitRepositoryError:
            logger.error(f"Invalid git repository at {self.config.main_repo_path}")
            raise ValueError(f"Not a git repository: {self.config.main_repo_path}")

        self.merge_lock_path = Path(self.config.main_repo_path) / ".git" / ".hephaestus_merge_lock"
        self._ensure_excludes()
        logger.info(f"WorktreeManager initialized for repo: {self.config.main_repo_path}")

    def reload(self, new_path):
        """Reinitialize with a new repository path."""
        new_path = Path(new_path) if not isinstance(new_path, Path) else new_path
        try:
            self.main_repo = Repo(new_path)
        except git.InvalidGitRepositoryError:
            raise ValueError(f"Not a git repository: {new_path}")
        self.config.main_repo_path = new_path
        self.config.project_root = new_path
        self.merge_lock_path = Path(new_path) / ".git" / ".hephaestus_merge_lock"
        self._ensure_excludes()
        logger.info(f"WorktreeManager reloaded with repo: {new_path}")

    # ── Worktree layout ──────────────────────────────────────────

    @property
    def worktree_base(self) -> Path:
        """Base directory for agent worktrees (``<repo>/.worktrees``)."""
        override = getattr(self.config, "worktree_base_path", None)
        if override:
            return Path(override)
        return Path(self.config.main_repo_path) / ".worktrees"

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
        session = self.db_manager.get_session()
        try:
            record = self._agent_record(session, agent_id)
            if not record or not record.worktree_path:
                raise ValueError(f"No worktree record for agent {agent_id}")
            return Repo(record.worktree_path)
        finally:
            session.close()

    # ── Lock management ──────────────────────────────────────────

    def _acquire_merge_lock(self, agent_id: str, timeout: int = 300):
        """Acquire exclusive lock for merge operations."""
        logger.info(f"[WORKTREE:{agent_id}] Acquiring merge lock (timeout={timeout}s)")
        self.merge_lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.merge_lock_path.touch(exist_ok=True)

        lock_file = open(self.merge_lock_path, 'w')
        start_time = time.time()

        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elapsed = time.time() - start_time
                logger.info(f"[WORKTREE:{agent_id}] Merge lock acquired after {elapsed:.2f}s")
                return lock_file
            except IOError:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    lock_file.close()
                    raise TimeoutError(f"[WORKTREE:{agent_id}] Failed to acquire merge lock after {timeout}s")
                if int(elapsed) % 10 == 0:
                    logger.info(f"[WORKTREE:{agent_id}] Waiting for merge lock... ({elapsed:.0f}s)")
                time.sleep(0.5)

    def _release_merge_lock(self, lock_file, agent_id: str):
        """Release merge lock."""
        logger.info(f"[WORKTREE:{agent_id}] Releasing merge lock")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception as e:
            logger.error(f"[WORKTREE:{agent_id}] Error releasing lock: {e}")

    # ── Worktree creation ────────────────────────────────────────

    def create_agent_branch(
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
        logger.info(f"[WORKTREE] Creating worktree for agent {agent_id} (parent={parent_agent_id})")

        session = self.db_manager.get_session()
        try:
            # Determine parent commit
            if base_commit_sha:
                parent_commit_sha = base_commit_sha
            elif parent_agent_id:
                parent_commit_sha = self._get_parent_commit(parent_agent_id, session)
                if not parent_commit_sha:
                    parent_commit_sha = self.main_repo.head.commit.hexsha
                    logger.info(f"[WORKTREE] Parent has no commits, using main HEAD: {parent_commit_sha[:8]}")
            else:
                parent_commit_sha = self.main_repo.head.commit.hexsha
                logger.info(f"[WORKTREE] Using main HEAD: {parent_commit_sha[:8]}")

            branch_name = f"{self.config.branch_prefix}{agent_id}"

            # Create branch from parent commit
            try:
                self.main_repo.git.branch(branch_name, parent_commit_sha)
                logger.info(f"[WORKTREE] Created branch {branch_name} from {parent_commit_sha[:8]}")
            except GitCommandError as e:
                if "already exists" in str(e):
                    logger.info(f"[WORKTREE] Branch exists, recreating from {parent_commit_sha[:8]}")
                    self.main_repo.git.branch("-D", branch_name)
                    self.main_repo.git.branch(branch_name, parent_commit_sha)
                elif "not a valid branch point" in str(e) or "not a valid object name" in str(e):
                    logger.warning(f"[WORKTREE] Commit {parent_commit_sha[:8]} not found, falling back to main HEAD")
                    parent_commit_sha = self.main_repo.head.commit.hexsha
                    self.main_repo.git.branch(branch_name, parent_commit_sha)
                else:
                    raise

            # Create the worktree checkout
            self._ensure_excludes()
            self.worktree_base.mkdir(parents=True, exist_ok=True)
            worktree_path = self._worktree_path_for(agent_id)
            if worktree_path.exists():
                self._remove_worktree(str(worktree_path))
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
                logger.info(f"[WORKTREE] Wrote {len(context_files)} context file(s) to {context_dir}")

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
            session.commit()

            return {
                "branch_name": branch_name,
                "parent_commit": parent_commit_sha,
                "working_directory": str(worktree_path),
                "context_dir": str(context_dir),
            }

        except Exception as e:
            logger.error(f"[WORKTREE] Failed to create worktree for {agent_id}: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    # Upstream-compatible name for the same operation.
    def create_agent_worktree(
        self,
        agent_id: str,
        parent_agent_id: Optional[str] = None,
        base_commit_sha: Optional[str] = None,
        context_files: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Alias for create_agent_branch (creates an isolated worktree + branch)."""
        return self.create_agent_branch(agent_id, parent_agent_id, base_commit_sha, context_files)

    def switch_to_branch(self, branch_name: str) -> None:
        """No-op: each agent has its own worktree, so the main repo never switches.

        Kept for call-site compatibility.
        """
        logger.debug(f"[WORKTREE] switch_to_branch({branch_name}) is a no-op (worktree isolation)")

    def switch_to_main(self) -> None:
        """No-op under worktree isolation (main repo stays on the base branch)."""
        logger.debug("[WORKTREE] switch_to_main() is a no-op (worktree isolation)")

    # ── Commit operations ────────────────────────────────────────

    def _commit_in_worktree(self, agent_id: str, message: str, commit_type: str) -> Dict[str, Any]:
        """Stage and commit all changes in the agent's worktree."""
        repo = self._agent_repo(agent_id)
        repo.git.add("-A")

        if not repo.is_dirty() and not repo.untracked_files:
            return {"commit_sha": repo.head.commit.hexsha, "files_changed": 0, "message": "No changes"}

        repo.git.commit("-m", f"[Agent {agent_id}] {message}", "--no-verify")
        commit = repo.head.commit
        stats = commit.stats.total

        session = self.db_manager.get_session()
        try:
            session.add(WorktreeCommit(
                id=str(uuid.uuid4()),
                agent_id=agent_id,
                commit_sha=commit.hexsha,
                commit_type=commit_type,
                commit_message=message,
                files_changed=stats.get("files", 0),
                insertions=stats.get("insertions", 0),
                deletions=stats.get("deletions", 0),
            ))
            session.commit()
        finally:
            session.close()

        return {"commit_sha": commit.hexsha, "files_changed": stats.get("files", 0), "message": message}

    def commit_changes(self, agent_id: str, message: str, branch_name: Optional[str] = None) -> Dict[str, Any]:
        """Stage and commit all changes in the agent's worktree."""
        return self._commit_in_worktree(agent_id, message, "auto_save")

    def commit_for_validation(self, agent_id: str, iteration: int, message: Optional[str] = None) -> Dict[str, Any]:
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
            lock_file = self._acquire_merge_lock(agent_id)

            record = self._agent_record(session, agent_id)
            if not record:
                raise ValueError(f"No worktree record found for agent {agent_id}")
            branch_name = record.branch_name
            target_branch = self.config.base_branch
            logger.info(f"[WORKTREE:{agent_id}] Merging branch {branch_name} -> {target_branch}")

            # Commit any uncommitted work in the agent's worktree first
            try:
                wt_repo = Repo(record.worktree_path)
                wt_repo.git.add("-A")
                if wt_repo.is_dirty() or wt_repo.untracked_files:
                    wt_repo.git.commit("-m", f"[Agent {agent_id}] Final - Task completed", "--no-verify")
                    final = wt_repo.head.commit
                    session.add(WorktreeCommit(
                        id=str(uuid.uuid4()),
                        agent_id=agent_id,
                        commit_sha=final.hexsha,
                        commit_type="final",
                        commit_message=f"[Agent {agent_id}] Final - Task completed",
                        files_changed=final.stats.total.get("files", 0),
                    ))
            except Exception as e:
                logger.warning(f"[WORKTREE:{agent_id}] Could not finalize worktree commit: {e}")

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
                    self.main_repo.git.stash("push", "-u", "-m", f"Auto-stash before merge for {agent_id}")
                    stashed = True
                except GitCommandError:
                    pass

            conflicts_resolved = []
            try:
                self.main_repo.git.merge(branch_name, no_ff=True, m=f"Merge agent {agent_id} into {target_branch}")
                merge_commit_sha = self.main_repo.head.commit.hexsha
                status = "success"
                logger.info(f"[WORKTREE:{agent_id}] Merge completed (no conflicts)")
            except GitCommandError as e:
                err_str = str(e)
                if "CONFLICT" in err_str or "unresolved conflict" in err_str:
                    logger.info(f"[WORKTREE:{agent_id}] Conflicts detected, resolving (newest-file-wins)")
                    conflicts_resolved = self._resolve_conflicts(agent_id, session, self.main_repo)
                    self.main_repo.git.commit(
                        "-m", f"[Auto-Merge] Resolved conflicts for agent {agent_id}", "--no-verify",
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
            logger.info(f"[WORKTREE:{agent_id}] ========== MERGE COMPLETE ({elapsed_ms}ms) ==========")

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
            logger.error(f"[WORKTREE:{agent_id}] Merge failed: {e}", exc_info=True)
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
        """Merge the base branch into an agent's worktree to keep it up-to-date."""
        logger.info(f"[WORKTREE:{agent_id}] Merging main into {branch_name}")

        session = self.db_manager.get_session()
        lock_file = None
        try:
            lock_file = self._acquire_merge_lock(agent_id)
            base_ref = self.config.base_branch
            base_commit = self.main_repo.heads[base_ref].commit.hexsha

            wt_repo = self._agent_repo(agent_id)

            if wt_repo.head.commit.hexsha == base_commit:
                return {"status": "up_to_date", "total_conflicts": 0}

            conflicts_resolved = []
            try:
                wt_repo.git.merge(
                    base_commit, no_ff=True,
                    m=f"[Auto-Merge] Merged {base_ref} into {branch_name}",
                )
                status = "success"
            except GitCommandError as e:
                if "CONFLICT" in str(e):
                    conflicts_resolved = self._resolve_conflicts(agent_id, session, wt_repo)
                    wt_repo.git.commit(
                        "-m", f"[Auto-Merge] Resolved conflicts merging main into {branch_name}",
                        "--no-verify",
                    )
                    status = "conflict_resolved"
                else:
                    raise

            return {
                "status": status,
                "total_conflicts": len(conflicts_resolved),
                "conflicts_resolved": conflicts_resolved,
            }
        finally:
            if lock_file:
                self._release_merge_lock(lock_file, agent_id)
            session.close()

    # ── Conflict resolution ──────────────────────────────────────

    def _resolve_conflicts(self, agent_id: str, session: Session, repo: Optional[Repo] = None) -> List[Dict]:
        """Resolve merge conflicts using newest-file-wins strategy.

        Args:
            repo: Repo where the merge is in progress (defaults to main repo).
        """
        repo = repo or self.main_repo
        conflicted = repo.git.diff("--name-only", "--diff-filter=U").splitlines()
        logger.info(f"[WORKTREE:{agent_id}] Resolving {len(conflicted)} conflicts")

        resolved = []
        for file_path in conflicted:
            parent_ts = self._get_file_timestamp(repo, file_path, "HEAD")
            child_ts = self._get_file_timestamp(repo, file_path, "MERGE_HEAD")

            if parent_ts is None:
                parent_ts = datetime.utcnow()
            if child_ts is None:
                child_ts = datetime.utcnow()

            if child_ts > parent_ts:
                choice = "child"
                content = self._get_file_content(repo, file_path, "MERGE_HEAD")
            elif parent_ts > child_ts:
                choice = "parent"
                content = self._get_file_content(repo, file_path, "HEAD")
            else:
                choice = "tie_child"
                content = self._get_file_content(repo, file_path, "MERGE_HEAD")

            try:
                repo.git.rm("--cached", "-f", file_path)
            except GitCommandError:
                pass
            self._write_file_content(repo.working_dir, file_path, content)
            repo.git.add(file_path)

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
                    prepo.git.commit("-m", f"[Agent {parent_id}] Checkpoint before spawning child", "--no-verify")
                    commit = prepo.head.commit
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
                return prepo.head.commit.hexsha
        except Exception as e:
            logger.warning(f"[WORKTREE] Could not read parent {parent_id} worktree: {e}")

        # Parent merged/cleaned — branch from its recorded commit or main HEAD
        return parent.parent_commit_sha or self.main_repo.head.commit.hexsha

    def _get_file_timestamp(self, repo: Repo, file_path: str, ref: str = "HEAD") -> Optional[datetime]:
        try:
            commits = list(repo.iter_commits(ref, paths=file_path, max_count=1))
            if commits:
                return datetime.fromtimestamp(commits[0].committed_date)
        except Exception:
            pass
        return None

    def _get_file_content(self, repo: Repo, file_path: str, ref: str = "HEAD") -> str:
        """Get content of a file from a specific git ref (never from working dir)."""
        try:
            return repo.git.show(f"{ref}:{file_path}")
        except Exception:
            try:
                full_path = Path(repo.working_dir) / file_path
                if full_path.exists():
                    return full_path.read_text()
            except Exception:
                pass
            return ""

    def _write_file_content(self, repo_dir: str, file_path: str, content: str):
        full_path = Path(repo_dir) / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)

    def get_workspace_changes(self, agent_id: str, since_commit: Optional[str] = None) -> Dict[str, Any]:
        """Get the diff for an agent's changes within its worktree."""
        session = self.db_manager.get_session()
        try:
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
        finally:
            session.close()

    def get_agent_branch_path(self, agent_id: str) -> Optional[str]:
        """Get the working directory (worktree path) for an agent."""
        session = self.db_manager.get_session()
        try:
            record = self._agent_record(session, agent_id)
            if record and record.worktree_path:
                return record.worktree_path
            return str(self.config.project_root)
        finally:
            session.close()

    def merge_to_parent(self, agent_id: str) -> Dict[str, Any]:
        """Alias for merge_to_main — merges the agent's branch into the base branch."""
        return self.merge_to_main(agent_id)

    # ── Cleanup ──────────────────────────────────────────────────

    def _remove_worktree(self, worktree_path: str) -> None:
        """Remove a git worktree and its directory."""
        try:
            self.main_repo.git.worktree("remove", worktree_path, "--force")
        except GitCommandError:
            # Fall back to manual removal + prune
            try:
                if Path(worktree_path).exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)
                self.main_repo.git.worktree("prune")
            except Exception as e:
                logger.warning(f"[WORKTREE] Could not remove worktree {worktree_path}: {e}")

    def cleanup_worktree(self, agent_id: str, delete_branch: bool = False) -> Dict[str, Any]:
        """Remove an agent's worktree directory.

        Args:
            delete_branch: If True, also delete the branch (discard semantics —
                use for failed agents or after a successful merge). If False
                (default), the branch is preserved for history.
        """
        session = self.db_manager.get_session()
        try:
            record = self._agent_record(session, agent_id)
            if not record:
                return {"status": "not_found"}

            if record.worktree_path:
                self._remove_worktree(record.worktree_path)
                logger.info(f"[WORKTREE] Removed worktree {record.worktree_path}")

            if delete_branch:
                try:
                    self.main_repo.git.branch("-D", record.branch_name)
                    logger.info(f"[WORKTREE] Deleted branch {record.branch_name}")
                except GitCommandError as e:
                    logger.warning(f"[WORKTREE] Could not delete branch: {e}")

            record.merge_status = "cleaned"
            session.commit()
            return {
                "status": "cleaned",
                "branch": record.branch_name,
                "branch_preserved": not delete_branch,
            }
        finally:
            session.close()

    def cleanup_branch(self, agent_id: str) -> Dict[str, Any]:
        """Remove an agent's worktree and delete its branch (discard semantics)."""
        return self.cleanup_worktree(agent_id, delete_branch=True)

    def discard_agent(self, agent_id: str) -> Dict[str, Any]:
        """Discard a failed agent: remove worktree + branch, nothing merged.

        This is what replaces the Repair flow — failed work never touches main.
        """
        return self.cleanup_worktree(agent_id, delete_branch=True)

    def cleanup_all_stale_branches(self) -> Dict[str, Any]:
        """Clean up worktrees and branches from terminated/stale agents.

        1. Prune and remove stale worktrees.
        2. Merge active branches into main (newest-file-wins on conflict).
        3. Delete branches (force-delete unmergeable ones).
        """
        session = self.db_manager.get_session()
        cleaned: List[str] = []
        merged: List[str] = []
        failed: List[str] = []
        worktrees_cleaned = 0
        try:
            target_branch = self.config.base_branch
            if self.main_repo.active_branch.name != target_branch:
                try:
                    self.main_repo.heads[target_branch].checkout()
                except Exception:
                    pass

            # Step 1: remove all linked worktrees (except the main one)
            try:
                self.main_repo.git.worktree("prune")
                blocks = self.main_repo.git.worktree("list", "--porcelain").split("\n\n")
                for wt in blocks:
                    lines = wt.strip().split("\n")
                    if not lines or not lines[0].startswith("worktree "):
                        continue
                    wt_path = lines[0].split(" ", 1)[1]
                    if wt_path == str(self.main_repo.working_dir):
                        continue
                    self._remove_worktree(wt_path)
                    worktrees_cleaned += 1
            except GitCommandError:
                pass

            # Step 2: merge + delete tracked active branches
            records = session.query(AgentBranch).filter(
                AgentBranch.merge_status.in_(["active", None])
            ).all()
            tracked_branches = {r.branch_name for r in records}
            all_branches = [b.name for b in self.main_repo.branches]
            untracked_branches = [
                b for b in all_branches
                if b.startswith(("agent-", "autopilot-")) and b not in tracked_branches
            ]

            def _merge_and_delete(branch_name: str, agent_id: Optional[str]) -> None:
                try:
                    self.main_repo.git.rev_parse("--verify", branch_name)
                except GitCommandError:
                    cleaned.append(branch_name)
                    return
                try:
                    self.main_repo.git.merge(branch_name, no_ff=True, m=f"[Cleanup] Merged {branch_name}")
                    merged.append(branch_name)
                except GitCommandError as e:
                    if "CONFLICT" in str(e) and agent_id:
                        self._resolve_conflicts(agent_id, session, self.main_repo)
                        self.main_repo.git.commit("-m", f"[Cleanup] Resolved conflicts merging {branch_name}", "--no-verify")
                        merged.append(branch_name)
                    else:
                        try:
                            self.main_repo.git.merge("--abort")
                        except GitCommandError:
                            pass
                        try:
                            self.main_repo.git.branch("-D", branch_name)
                            cleaned.append(branch_name)
                            logger.info(f"[WORKTREE] Force-deleted unmergeable branch {branch_name}")
                        except GitCommandError:
                            failed.append(branch_name)
                        return
                try:
                    self.main_repo.git.branch("-D", branch_name)
                    if branch_name not in cleaned:
                        cleaned.append(branch_name)
                except GitCommandError:
                    failed.append(branch_name)

            for record in records:
                _merge_and_delete(record.branch_name, record.agent_id)
                record.merge_status = "cleaned"
            for branch_name in untracked_branches:
                _merge_and_delete(branch_name, None)

            session.commit()
            return {
                "cleaned": len(cleaned),
                "merged": len(merged),
                "failed": len(failed),
                "worktrees_cleaned": worktrees_cleaned,
                "branches": cleaned,
            }
        finally:
            session.close()


# Backward-compatible alias for call sites that still import WorktreeManager.
