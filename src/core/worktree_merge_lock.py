"""Exclusive, timeout-bounded lock guarding one repo's merge-to-main section.

Extracted from WorktreeManager (SOLID review 4.5), which fused git
plumbing, DB persistence, and this fcntl-based locking together in one
class. Pure file-locking, no git or DB coupling.
"""

import fcntl
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class MergeLockManager:
    """Backed by a lock file at `lock_path` (typically
    `<repo>/.git/.hephaestus_merge_lock`)."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path

    def acquire(self, agent_id: str, timeout: int = 300):
        """Acquire the lock, blocking (with periodic retry) up to timeout
        seconds. Returns the open lock file handle; pass it to release()."""
        logger.info(f"[WORKTREE:{agent_id}] Acquiring merge lock (timeout={timeout}s)")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)

        lock_file = open(self.lock_path, "w")
        start_time = time.time()

        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elapsed = time.time() - start_time
                logger.info(
                    f"[WORKTREE:{agent_id}] Merge lock acquired after {elapsed:.2f}s"
                )
                return lock_file
            except IOError:
                elapsed = time.time() - start_time
                if elapsed > timeout:
                    lock_file.close()
                    raise TimeoutError(
                        f"[WORKTREE:{agent_id}] Failed to acquire merge lock after {timeout}s"
                    )
                if int(elapsed) % 10 == 0:
                    logger.info(
                        f"[WORKTREE:{agent_id}] Waiting for merge lock... ({elapsed:.0f}s)"
                    )
                time.sleep(0.5)

    def release(self, lock_file, agent_id: str) -> None:
        """Release a lock file handle returned by acquire()."""
        logger.info(f"[WORKTREE:{agent_id}] Releasing merge lock")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()
        except Exception as e:
            logger.error(f"[WORKTREE:{agent_id}] Error releasing lock: {e}")
