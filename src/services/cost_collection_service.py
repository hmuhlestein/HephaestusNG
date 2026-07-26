"""Cost collection service for Hephaestus.

Collects LLM usage data from various CLI tools (pi, Claude Code, OpenCode, Codex)
and writes CostEntry rows to the database. Triggers cost derivation rollup after
each collection.

Usage:
    from src.services.cost_collection_service import collect_task_cost

    # Called from task_completion_service when a task completes
    collect_task_cost(task_id)
"""

import json
import logging
import re
import sqlite3
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class CostCollector(ABC):
    """Abstract base class for cost collectors."""

    @abstractmethod
    def collect(
        self,
        session_id: str,
        task_id: str,
        workflow_id: str,
        agent_id: Optional[str],
        session_file: Path,
        checkpoint: int,
    ) -> Tuple[List[dict], int]:
        """Return new cost entries since checkpoint, and new checkpoint.

        Args:
            session_id: The session ID (for SessionCostCheckpoint lookup)
            task_id: The task ID to attribute costs to
            workflow_id: The workflow ID to attribute costs to
            agent_id: Optional agent ID
            session_file: Path to the session transcript file
            checkpoint: Lines already processed (from SessionCostCheckpoint)

        Returns:
            Tuple of (list of cost entry dicts, new checkpoint line count)
        """


class PiJsonlCollector(CostCollector):
    """Collector for pi session JSONL files.

    Reads message.usage.cost.total from assistant turns in pi session files.
    Checkpoint = lines_processed from SessionCostCheckpoint.
    """

    def collect(
        self,
        session_id: str,
        task_id: str,
        workflow_id: str,
        agent_id: Optional[str],
        session_file: Path,
        checkpoint: int,
    ) -> Tuple[List[dict], int]:
        """Collect cost entries from a pi session JSONL file."""
        entries = []
        lines_processed = checkpoint  # Preserve checkpoint if no new lines

        try:
            with open(session_file) as f:
                for line_num, line in enumerate(f, 1):
                    if line_num <= checkpoint:
                        continue

                    lines_processed = line_num

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Only process assistant messages with usage data
                    if data.get("type") != "message":
                        continue
                    message = data.get("message", {})
                    if message.get("role") != "assistant":
                        continue
                    usage = message.get("usage")
                    if not usage:
                        continue

                    # Extract cost data
                    cost_data = usage.get("cost", {})
                    cost_usd = cost_data.get("total", 0)
                    if cost_usd <= 0:
                        continue

                    # Extract model info
                    model = message.get("model")

                    # Extract token counts
                    input_tokens = usage.get("input", 0)
                    output_tokens = usage.get("output", 0)
                    cache_read = usage.get("cacheRead", 0)
                    cache_write = usage.get("cacheWrite", 0)
                    reasoning = usage.get("reasoning", 0)

                    entries.append(
                        {
                            "id": f"cost-{uuid.uuid4().hex[:8]}",
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "workflow_id": workflow_id,
                            "source": "pi",
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cache_read_tokens": cache_read,
                            "cache_write_tokens": cache_write,
                            "reasoning_tokens": reasoning,
                            "cost_usd": cost_usd,
                            "recorded_at": datetime.utcnow(),
                            "raw_usage": usage,
                        }
                    )

        except FileNotFoundError:
            logger.warning(f"Session file not found: {session_file}")
        except Exception as e:
            logger.error(f"Error collecting pi costs from {session_file}: {e}")

        return entries, lines_processed


class ClaudeCodeCollector(CostCollector):
    """Collector for Claude Code session JSONL files.

    Unlike pi, Claude Code transcripts only contain raw token counts,
    not dollar amounts. This collector uses a price table to convert
    tokens to dollars.
    """

    # Price table: $/M tokens (as of 2026-07-21)
    # Update these when Anthropic reprices
    PRICES = {
        "claude-sonnet-4": {
            "input": 3.0,
            "output": 15.0,
            "cache_write_1h": 3.75,
            "cache_write_5m": 3.0,
            "cache_read": 0.30,
        },
        "claude-opus-4": {
            "input": 15.0,
            "output": 75.0,
            "cache_write_1h": 18.75,
            "cache_write_5m": 15.0,
            "cache_read": 1.50,
        },
        "claude-haiku-3.5": {
            "input": 0.80,
            "output": 4.0,
            "cache_write_1h": 1.0,
            "cache_write_5m": 0.80,
            "cache_read": 0.08,
        },
    }

    def collect(
        self,
        session_id: str,
        task_id: str,
        workflow_id: str,
        agent_id: Optional[str],
        session_file: Path,
        checkpoint: int,
    ) -> Tuple[List[dict], int]:
        """Collect cost entries from a Claude Code session JSONL file."""
        entries = []
        lines_processed = checkpoint  # Preserve checkpoint if no new lines

        try:
            with open(session_file) as f:
                for line_num, line in enumerate(f, 1):
                    if line_num <= checkpoint:
                        continue

                    lines_processed = line_num

                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # Only process assistant messages with usage data
                    if data.get("type") != "message":
                        continue
                    message = data.get("message", {})
                    if message.get("role") != "assistant":
                        continue
                    usage = message.get("usage")
                    if not usage:
                        continue

                    # Extract token counts
                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    cache_creation = usage.get("cache_creation", {})
                    cache_1h = cache_creation.get("ephemeral_1h_input_tokens", 0)
                    cache_5m = cache_creation.get("ephemeral_5m_input_tokens", 0)
                    cache_read = usage.get("cache_read_input_tokens", 0)

                    # Calculate cost using price table
                    # Default to sonnet prices if model unknown
                    model = message.get("model", "claude-sonnet-4")
                    prices = self.PRICES.get(model, self.PRICES["claude-sonnet-4"])

                    cost_usd = (
                        input_tokens * prices["input"] / 1_000_000
                        + output_tokens * prices["output"] / 1_000_000
                        + cache_1h * prices["cache_write_1h"] / 1_000_000
                        + cache_5m * prices["cache_write_5m"] / 1_000_000
                        + cache_read * prices["cache_read"] / 1_000_000
                    )

                    if cost_usd <= 0:
                        continue

                    entries.append(
                        {
                            "id": f"cost-{uuid.uuid4().hex[:8]}",
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "workflow_id": workflow_id,
                            "source": "claude_code",
                            "model": model,
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "cache_read_tokens": cache_read,
                            "cache_write_tokens": cache_1h + cache_5m,
                            "reasoning_tokens": 0,
                            "cost_usd": cost_usd,
                            "recorded_at": datetime.utcnow(),
                            "raw_usage": usage,
                        }
                    )

        except FileNotFoundError:
            logger.warning(f"Session file not found: {session_file}")
        except Exception as e:
            logger.error(f"Error collecting Claude Code costs from {session_file}: {e}")

        return entries, lines_processed


class OpenCodeCollector(CostCollector):
    """Collector for OpenCode sessions via opencode.db.

    OpenCode's `session` table stores pre-aggregated cost/token totals
    per session row -- no per-turn tailing needed. Each agent launch
    mints exactly one fresh session row (OpenCodeAgent never resumes),
    so checkpoint is a simple 0/1 "already collected" guard, not a
    line count.
    """

    def __init__(self, session_row_id: Optional[str] = None):
        self.session_row_id = session_row_id

    def collect(
        self,
        session_id: str,
        task_id: str,
        workflow_id: str,
        agent_id: Optional[str],
        session_file: Path,
        checkpoint: int,
    ) -> Tuple[List[dict], int]:
        """Collect a cost entry from opencode.db.

        For OpenCode, session_file is the opencode.db path and
        checkpoint is 0 (not yet collected) or 1 (already collected).
        """
        entries: List[dict] = []

        if checkpoint >= 1:
            return entries, checkpoint

        if not self.session_row_id:
            return entries, checkpoint

        try:
            conn = sqlite3.connect(f"file:{session_file}?mode=ro", uri=True)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, model FROM session WHERE id = ?",
                    (self.session_row_id,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.Error as e:
            logger.error(f"Error reading opencode.db for session {self.session_row_id}: {e}")
            return entries, checkpoint

        if not row or (row["cost"] or 0) <= 0:
            return entries, 1

        model = None
        if row["model"]:
            try:
                model = json.loads(row["model"]).get("id")
            except json.JSONDecodeError:
                model = row["model"]

        entries.append(
            {
                "id": f"cost-{uuid.uuid4().hex[:8]}",
                "task_id": task_id,
                "agent_id": agent_id,
                "workflow_id": workflow_id,
                "source": "opencode",
                "model": model,
                "input_tokens": row["tokens_input"] or 0,
                "output_tokens": row["tokens_output"] or 0,
                "cache_read_tokens": row["tokens_cache_read"] or 0,
                "cache_write_tokens": row["tokens_cache_write"] or 0,
                "reasoning_tokens": row["tokens_reasoning"] or 0,
                "cost_usd": row["cost"],
                "recorded_at": datetime.utcnow(),
                "raw_usage": dict(row),
            }
        )

        return entries, 1


class CodexStubCollector(CostCollector):
    """Stub collector for Codex CLI.

    Codex is not yet installed/available for inspection.
    Logs a warning and returns empty.
    """

    def collect(
        self,
        session_id: str,
        task_id: str,
        workflow_id: str,
        agent_id: Optional[str],
        session_file: Path,
        checkpoint: int,
    ) -> Tuple[List[dict], int]:
        """Stub: Codex collection not supported."""
        logger.warning(f"Codex cost collection not supported for task {task_id[:8]} (session {session_id[:8]})")
        return [], checkpoint


def _discover_session_file(session_id: str, cwd: str) -> Optional[Path]:
    """Discover a pi session file by session_id and cwd.

    Pi session files are stored in ~/.pi/agent/sessions/<sanitized_cwd>/
    with filename pattern <timestamp>_<session_id>.jsonl

    Args:
        session_id: The deterministic session ID
        cwd: The agent's working directory

    Returns:
        Path to session file, or None if not found
    """
    # SECURITY: Sanitize cwd to prevent path traversal
    # Reject paths with obvious traversal attempts
    if ".." in cwd or "~" in cwd:
        logger.warning(f"Rejected session file discovery with suspicious cwd: {cwd}")
        return None

    # Sanitize: replace slashes and special chars
    sanitized = re.sub(r"[^a-zA-Z0-9_.\-]", "-", cwd)
    # Collapse multiple dashes
    sanitized = re.sub(r"-+", "-", sanitized)
    # Remove leading/trailing dashes
    sanitized = sanitized.strip("-")

    sessions_dir = Path.home() / ".pi" / "agent" / "sessions" / f"--{sanitized}--"

    # SECURITY: Verify the resolved path is within expected directory
    try:
        resolved = sessions_dir.resolve()
        base = (Path.home() / ".pi" / "agent" / "sessions").resolve()
        if not str(resolved).startswith(str(base)):
            logger.warning(f"Session path escapes base directory: {resolved}")
            return None
    except (OSError, ValueError):
        return None

    if not sessions_dir.exists():
        return None

    matches = list(sessions_dir.glob(f"*_{session_id}.jsonl"))
    if not matches:
        return None

    # Verify first line contains matching session ID
    try:
        with open(matches[0]) as f:
            first = json.loads(f.readline())
            if first.get("id") == session_id:
                return matches[0]
    except Exception:
        pass

    return None


def _discover_opencode_session(cwd: str, agent_created_at: datetime) -> Optional[Tuple[Path, str]]:
    """Discover the OpenCode session row matching an agent's cwd and launch time.

    OpenCode assigns no deterministic session ID Hephaestus controls (unlike
    pi's --session-id / Claude Code's uuid5): `opencode run -s <id>` errors
    with "Session not found" for an ID that doesn't already exist, so it
    can't be used to create-with-ID. Correlation is by directory
    (session.directory is a literal, unsanitized path) and a time window
    bounded by [agent_created_at, now].

    Returns (db_path, session.id) for the most recent in-window match, or
    None if the DB doesn't exist or no session matches.
    """
    db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

    # SECURITY: Verify the resolved path is within expected directory
    try:
        resolved = db_path.resolve()
        base = (Path.home() / ".local" / "share" / "opencode").resolve()
        if not str(resolved).startswith(str(base)):
            logger.warning(f"OpenCode DB path escapes base directory: {resolved}")
            return None
    except (OSError, ValueError):
        return None

    if not db_path.exists():
        return None

    start_ms = int(agent_created_at.timestamp() * 1000)
    end_ms = int(datetime.utcnow().timestamp() * 1000)

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, time_created FROM session WHERE directory = ? AND time_created >= ? AND time_created <= ? ORDER BY time_created DESC",
                (cwd, start_ms, end_ms),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as e:
        logger.error(f"Error querying opencode.db for cwd {cwd}: {e}")
        return None

    if not rows:
        logger.debug(f"No OpenCode session found for cwd {cwd} in window [{start_ms}, {end_ms}]")
        return None

    if len(rows) > 1:
        discarded = [row["id"] for row in rows[1:]]
        logger.debug(f"Multiple OpenCode sessions matched cwd {cwd}; using most recent, discarded: {discarded}")

    return db_path, rows[0]["id"]


def collect_task_cost(task_id: str) -> None:
    """Entry point for cost collection on task completion.

    Called from task_completion_service when a task completes.
    Looks up task → agent → session_id → discovers session file
    → delegates to appropriate collector → writes CostEntry rows.

    Args:
        task_id: The completed task's ID
    """
    from src.core.cost_derivation import record_cost
    from src.core.database import Agent, SessionCostCheckpoint, Task, get_db

    with get_db() as db:
        task = db.query(Task).filter_by(id=task_id).first()
        if not task:
            logger.warning(f"[COST-COLLECT] Task {task_id[:8]} not found")
            return

        if not task.workflow_id:
            logger.debug(f"[COST-COLLECT] Task {task_id[:8]} has no workflow — skipping")
            return

        # Find the agent that was assigned to this task
        agent = None
        if task.assigned_agent_id:
            agent = db.query(Agent).filter_by(id=task.assigned_agent_id).first()

        if not agent:
            logger.debug(f"[COST-COLLECT] Task {task_id[:8]} has no assigned agent — skipping")
            return

        # Get session ID from agent's launch params or task metadata
        # The session ID is typically stored in the agent's tmux session name
        # or passed via --session-id flag
        session_id = _extract_session_id(agent, task)
        if not session_id:
            logger.debug(f"[COST-COLLECT] No session ID for task {task_id[:8]} agent {agent.id[:8]} — skipping")
            return

        # Discover session file based on CLI type
        cli_type = agent.cli_type or "pi"
        session_file = None
        opencode_session_row_id = None

        if cli_type == "pi":
            # Discover pi session file
            cwd = _get_agent_cwd(db, agent, task)
            if cwd:
                session_file = _discover_session_file(session_id, cwd)
        elif cli_type == "claude_code":
            # Claude Code session files in ~/.claude/projects/<sanitized_cwd>/
            cwd = _get_agent_cwd(db, agent, task)
            if cwd:
                # SECURITY: Sanitize cwd to prevent path traversal
                if ".." in cwd or "~" in cwd:
                    logger.warning(f"Rejected Claude Code session discovery with suspicious cwd: {cwd}")
                else:
                    sanitized = re.sub(r"[^a-zA-Z0-9_.\-]", "-", cwd)
                    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
                    claude_dir = Path.home() / ".claude" / "projects" / sanitized

                    # SECURITY: Verify the resolved path is within expected directory
                    try:
                        resolved = claude_dir.resolve()
                        base = (Path.home() / ".claude" / "projects").resolve()
                        if not str(resolved).startswith(str(base)):
                            logger.warning(f"Claude Code path escapes base directory: {resolved}")
                        else:
                            if claude_dir.exists():
                                matches = list(claude_dir.glob(f"*_{session_id}.jsonl"))
                                if matches:
                                    session_file = matches[0]
                    except (OSError, ValueError):
                        pass
        elif cli_type == "opencode":
            cwd = _get_agent_cwd(db, agent, task)
            if cwd:
                result = _discover_opencode_session(cwd, agent.created_at)
                if result:
                    db_path, session_row_id = result
                    session_file = db_path
                    opencode_session_row_id = session_row_id
        elif cli_type == "codex":
            pass  # Stub

        if not session_file:
            logger.debug(f"[COST-COLLECT] No session file found for {cli_type} session {session_id[:8]} — skipping")
            return

        # Checkpoint key: normally the shared Hephaestus session_id (correct for
        # pi/claude_code, which resume the same transcript across retries/shared
        # roles). OpenCode never resumes -- every launch mints a fresh opencode.db
        # session row even when it shares a Hephaestus session_id with a prior task
        # (e.g. same session_role reused across phases per get_session_id()) -- so
        # its checkpoint must be keyed by the per-launch session_row_id instead, or
        # a second launch under the same session_id would find the first launch's
        # checkpoint already at 1 and skip collection entirely, silently dropping
        # its cost.
        checkpoint_key = opencode_session_row_id if cli_type == "opencode" and opencode_session_row_id else session_id

        # Get or create checkpoint
        checkpoint_row = db.query(SessionCostCheckpoint).filter_by(session_id=checkpoint_key).first()
        checkpoint = checkpoint_row.lines_processed if checkpoint_row else 0

        # Select collector
        collectors = {
            "pi": PiJsonlCollector(),
            "claude_code": ClaudeCodeCollector(),
            "opencode": OpenCodeCollector(session_row_id=opencode_session_row_id),
            "codex": CodexStubCollector(),
        }
        collector = collectors.get(cli_type)
        if not collector:
            logger.warning(f"[COST-COLLECT] Unknown CLI type: {cli_type}")
            return

        # Collect entries
        entries, new_checkpoint = collector.collect(
            session_id=session_id,
            task_id=task_id,
            workflow_id=task.workflow_id,
            agent_id=agent.id,
            session_file=session_file,
            checkpoint=checkpoint,
        )

        # Write entries and trigger derivation
        for entry_data in entries:
            record_cost(
                db=db,
                cost_usd=entry_data["cost_usd"],
                source=entry_data["source"],
                task_id=entry_data["task_id"],
                agent_id=entry_data["agent_id"],
                workflow_id=entry_data["workflow_id"],
                model=entry_data.get("model"),
                input_tokens=entry_data.get("input_tokens", 0),
                output_tokens=entry_data.get("output_tokens", 0),
                cache_read_tokens=entry_data.get("cache_read_tokens", 0),
                cache_write_tokens=entry_data.get("cache_write_tokens", 0),
                reasoning_tokens=entry_data.get("reasoning_tokens", 0),
                raw_usage=entry_data.get("raw_usage"),
            )

        # Update checkpoint
        if checkpoint_row:
            checkpoint_row.lines_processed = new_checkpoint
            checkpoint_row.updated_at = datetime.utcnow()
        else:
            checkpoint_row = SessionCostCheckpoint(
                session_id=checkpoint_key,
                lines_processed=new_checkpoint,
                updated_at=datetime.utcnow(),
            )
            db.add(checkpoint_row)

        db.commit()

        if entries:
            total_cost = sum(e["cost_usd"] for e in entries)
            logger.info(f"[COST-COLLECT] Collected {len(entries)} entries (${total_cost:.4f}) for task {task_id[:8]} from {cli_type}")


def _extract_session_id(agent: Any, task: Any) -> Optional[str]:
    """Extract session ID from agent or task metadata.

    The session ID is the deterministic ID generated by get_session_id()
    in src/autopilot/phases.py, passed via --session-id flag at launch.
    It's stored in the agent's tmux session name or launch params.
    """
    # Try to get from agent's tmux session name
    # Session name format: hephaestus-<project>-<design>-<role>-<session_id_suffix>
    if agent.tmux_session_name:
        # Extract session ID suffix from tmux session name
        parts = agent.tmux_session_name.split("-")
        if len(parts) >= 2:
            # Last part is typically the session ID suffix
            return "-".join(parts[1:])  # Skip "hephaestus" prefix

    # Try to reconstruct from task/workflow context
    # This would need access to get_session_id() logic
    # For now, return None and log
    logger.debug(f"Could not extract session ID from agent {agent.id[:8]} (tmux: {agent.tmux_session_name})")
    return None


def _get_agent_cwd(db: Session, agent: Any, task: Any) -> Optional[str]:
    """Get the agent's working directory.

    Uses the task's workflow's working directory, or falls back to
    the agent's worktree path.

    Args:
        db: Database session (reuses caller's session)
        agent: The Agent object
        task: The Task object
    """
    from src.core.database import AgentWorktree, Workflow

    # Try workflow's working directory
    if task.workflow_id:
        wf = db.query(Workflow).filter_by(id=task.workflow_id).first()
        if wf and wf.working_directory:
            return wf.working_directory

    # Try agent's worktree
    worktree = db.query(AgentWorktree).filter_by(agent_id=agent.id).first()
    if worktree:
        return worktree.worktree_path

    return None
