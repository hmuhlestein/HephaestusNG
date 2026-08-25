"""Merge-conflict resolution: newest-file-wins.

Extracted from WorktreeManager (SOLID review 4.5), which fused git
plumbing, DB persistence, and this conflict-resolution policy together in
one class. Kept as a plain class (not a strategy interface/ABC) since
there is exactly one resolution policy in use and none planned -- see
WorktreeManager.merge_to_main's own comment on why the config field that
used to imply a pluggable strategy was removed rather than wired up.
"""

import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from git import GitCommandError, Repo
from sqlalchemy.orm import Session

from src.core.database import MergeConflictResolution, utc_now

logger = logging.getLogger(__name__)


class ConflictResolver:
    """Resolves git merge conflicts by keeping whichever side (parent's
    HEAD vs. the merging branch's MERGE_HEAD) last modified each file,
    and records each resolution as a MergeConflictResolution row."""

    def resolve(self, agent_id: str, session: Session, repo: Repo) -> List[Dict]:
        conflicted = repo.git.diff("--name-only", "--diff-filter=U").splitlines()
        logger.info(f"[WORKTREE:{agent_id}] Resolving {len(conflicted)} conflicts")

        resolved = []
        for file_path in conflicted:
            parent_ts = self._get_file_timestamp(repo, file_path, "HEAD")
            child_ts = self._get_file_timestamp(repo, file_path, "MERGE_HEAD")

            # WARNING-3: When both timestamps are None, log warning and default
            # to parent (prefer upstream committed code over unverified agent work)
            if parent_ts is None and child_ts is None:
                logger.warning(
                    f"[WORKTREE:{agent_id}] Cannot determine timestamps for "
                    f"{file_path} — defaulting to parent (upstream)"
                )
                choice = "parent"
                content = self._get_file_content(repo, file_path, "HEAD")
            else:
                if parent_ts is None:
                    parent_ts = utc_now()
                if child_ts is None:
                    child_ts = utc_now()

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

            session.add(
                MergeConflictResolution(
                    id=str(uuid.uuid4()),
                    agent_id=agent_id,
                    file_path=file_path,
                    parent_modified_at=parent_ts,
                    child_modified_at=child_ts,
                    resolution_choice=choice,
                )
            )

            resolved.append({"file": file_path, "resolution": choice})

        session.flush()
        return resolved

    def _get_file_timestamp(
        self, repo: Repo, file_path: str, ref: str = "HEAD"
    ) -> Optional[datetime]:
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
        # Sanitize surrogate characters from garbled tmux output
        full_path.write_text(content.encode("utf-8", errors="replace").decode("utf-8"))
