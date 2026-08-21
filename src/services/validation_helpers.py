"""Validation helpers for result submission."""

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def validate_file_path(file_path: str, allowed_root: "str | Path | None" = None) -> None:
    """
    Validate file path to prevent directory traversal attacks.

    Args:
        file_path: Path to validate
        allowed_root: When given, the resolved path must stay within this
            directory (resolve() + is_relative_to(), matching
            cost_collection_service.py's session-file containment check).
            Omitted by every current caller: neither result-submission call
            site has a workflow/worktree root available at the point it
            validates the path, and no single global root (e.g. the
            server's own cwd) is correct for every legitimate result-file
            location -- worktrees and system temp directories both need to
            keep working. A caller that later gains a real root to check
            against should pass it; until then this stays the same
            traversal-substring check it always was, just phrased against
            path segments instead of the raw string, and evaluated after
            resolve() so a symlink can't hide a traversal.

    Raises:
        ValueError: If path is invalid, contains traversal attempts, or
            (with allowed_root given) resolves outside that root.
    """
    raw = Path(file_path)
    resolved = raw.resolve()

    # ".." as a path segment (Path.parts) in the raw, unresolved input, not
    # a substring of the raw string: the original substring check
    # both under- and over-matched -- it would false-positive on a
    # filename that merely contains ".." as text (e.g. "notes..final.md"),
    # while still missing the real gap this item exists to close: an
    # absolute path needing no ".." at all to point somewhere unsafe
    # (e.g. "/etc/passwd"), which resolve() + allowed_root below catches
    # when a caller has a real root to check against.
    if ".." in raw.parts:
        raise ValueError(
            f"Invalid file path - directory traversal detected: {file_path}"
        )

    roots = (
        [Path(allowed_root).resolve()]
        if allowed_root is not None
        else _default_allowed_roots()
    )
    if roots and not any(_within(resolved, r) for r in roots):
        raise ValueError(
            f"Invalid file path - outside allowed directories: {file_path}"
        )

    # Additional safety check
    try:
        # This will raise if path doesn't exist or is invalid
        str(resolved)
    except Exception as e:
        raise ValueError(f"Invalid file path: {file_path}") from e


def validate_file_size(file_path: str, max_size_kb: int = 100) -> None:
    """
    Validate that file size is within limits.

    Args:
        file_path: Path to the file
        max_size_kb: Maximum allowed size in KB

    Raises:
        ValueError: If file is too large
    """
    file_size = os.path.getsize(file_path)
    max_size_bytes = max_size_kb * 1024

    if file_size > max_size_bytes:
        size_kb = file_size / 1024
        raise ValueError(
            f"File too large: {size_kb:.2f}KB exceeds maximum of {max_size_kb}KB"
        )


def validate_markdown_format(file_path: str) -> None:
    """
    Validate that file is in markdown format.

    Args:
        file_path: Path to the file

    Raises:
        ValueError: If file is not markdown
    """
    path = Path(file_path)

    # Check file extension
    if not path.suffix.lower() == ".md":
        raise ValueError(f"File must be markdown (.md), got: {path.suffix}")

    # Could add additional validation here (e.g., check for valid markdown syntax)
    # For now, just checking extension


def validate_task_ownership(db, task_id: str, agent_id: str) -> None:
    """
    Validate that the agent owns the task.

    Args:
        db: Database session
        task_id: ID of the task
        agent_id: ID of the agent

    Raises:
        ValueError: If task doesn't exist or isn't assigned to agent
    """
    from src.core.database import Task

    task = db.query(Task).filter_by(id=task_id).first()

    if not task:
        raise ValueError(f"Task not found: {task_id}")

    if task.assigned_agent_id != agent_id:
        raise ValueError(
            f"Task {task_id} is not assigned to agent {agent_id}. "
            f"Assigned to: {task.assigned_agent_id}"
        )


def _within(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except (OSError, ValueError):
        return False


def _default_allowed_roots() -> "list[Path]":
    """Where a result file may legitimately live when no caller supplies a root.

    Banning absolute paths outright is not an option: the server resolves and
    opens this path in its OWN process, while the agent that produced it runs
    in a worktree, so a relative path would resolve against the wrong
    directory. Absolute IS the contract. Containment is what actually closes
    the gap -- "/etc/passwd" needs no ".." to escape, so the segment check
    above never sees it.

    Roots are the two locations the previous docstring named as needing to
    keep working -- the repo and its worktrees -- plus the system temp dir,
    which is where every current caller's files actually are. Returning an
    empty list disables the check rather than failing closed: if config cannot
    be read at all we are in a degraded environment, and refusing every result
    submission is a worse failure than the one being guarded against.
    """
    roots: "list[Path]" = []
    config_read = False
    try:
        from src.core.simple_config import get_config

        config = get_config()
        config_read = True
        for attr, source in (
            ("main_repo_path", config.git),
            ("project_root", config.paths),
            ("worktree_base_path", config.paths),
        ):
            value = getattr(source, attr, None)
            if not value:
                continue
            try:
                candidate = Path(value).resolve()
            except OSError:
                continue
            if _too_broad_to_contain(candidate):
                # Both main_repo_path and project_root default to Path.cwd().
                # Launch the server from "/" or the home directory and the
                # containment check silently degrades to "anything on this
                # machine" -- a guard that depends on the launch directory is
                # not a guard. Drop the root and say so, rather than keep a
                # check that only appears to be enforcing something.
                logger.warning(
                    f"[validate_file_path] Ignoring config.{attr}={candidate} as "
                    "an allowed root: too broad to contain anything. Result files "
                    "outside the remaining roots will be rejected -- set "
                    "main_repo_path/project_root explicitly if this is wrong."
                )
                continue
            roots.append(candidate)
    except Exception:
        pass

    if not config_read:
        # Degraded environment: without config we cannot know the repo or
        # worktree base, and rejecting every result written there would be a
        # worse failure than the traversal this guards. Fall back to cwd so
        # legitimate in-repo paths keep working.
        try:
            roots.append(Path.cwd().resolve())
        except OSError:
            pass

    for extra in (tempfile.gettempdir(), "/private/var/folders"):
        try:
            roots.append(Path(extra).resolve())
        except OSError:
            pass
    return roots


def _too_broad_to_contain(root: Path) -> bool:
    """True if `root` is so broad that containing a path within it means nothing.

    The filesystem root and the user's home directory (or any ancestor of it)
    are never legitimate containment boundaries -- everything the process can
    read lives under them.
    """
    if root == Path(root.anchor):
        return True
    try:
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return False
    return root == home or home.is_relative_to(root)
