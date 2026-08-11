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
import os
import re
import subprocess
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, List, Optional, Tuple

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _is_claude_pro_account() -> bool:
    """Detect if Claude Code is authenticated with a Pro/Team subscription.

    Pro and Team accounts have unlimited usage (within rate limits) for a
    flat monthly fee, so token counting is misleading. We detect this by
    running `claude auth status` and checking the subscriptionType field.

    Known subscription types:
    - "pro": $20/mo individual plan (unlimited Sonnet, limited Opus)
    - "team": $30/mo per seat (unlimited usage)
    - "enterprise": custom pricing (unlimited usage)
    - "max": higher-tier Pro with more Opus usage

    Returns True if subscription-based (Pro/Team/Enterprise/Max), False
    otherwise (API key billing, free tier, etc.)
    """
    try:
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            status = json.loads(result.stdout)
            sub_type = (status.get("subscriptionType") or "").lower()
            # All subscription types have flat-rate usage — don't count tokens
            if sub_type in ("pro", "max", "team", "enterprise"):
                logger.info(f"[COST] Claude account detected as '{sub_type}' — token counting disabled (flat-rate subscription)")
                return True
            # Also check authMethod — OAuth login (claude.ai) without an
            # explicit subscriptionType still means the user is on some
            # managed plan, not pure API key billing.
            auth_method = (status.get("authMethod") or "").lower()
            if auth_method == "claude.ai" and not sub_type:
                logger.info("[COST] Claude authenticated via claude.ai (no subscriptionType) — assuming flat-rate, token counting disabled")
                return True
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError) as e:
        logger.debug(f"[COST] Could not detect Claude subscription: {e}")
    return False


def _is_subscription_cli(cli_type: str) -> bool:
    """Check if a CLI tool uses a subscription model (flat-rate, no per-token cost).

    Currently detects:
    - Claude Code Pro/Team/Enterprise/Max
    - (Future: Cursor, Windsurf, etc. if they expose similar auth status)
    """
    if cli_type in ("claude_code", "claude"):
        return _is_claude_pro_account()
    # Add other subscription-based CLIs here as they're discovered
    # if cli_type == "cursor":
    #     return _is_cursor_pro_account()
    return False


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
                    if cost_usd < 0:
                        cost_usd = 0

                    # Extract model info
                    model = message.get("model")

                    # Extract token counts
                    input_tokens = usage.get("input", 0)
                    output_tokens = usage.get("output", 0)
                    cache_read = usage.get("cacheRead", 0)
                    cache_write = usage.get("cacheWrite", 0)
                    reasoning = usage.get("reasoning", 0)

                    # Skip entries with no token usage at all (not just zero
                    # cost -- local models have zero cost but real tokens).
                    if cost_usd <= 0 and input_tokens == 0 and output_tokens == 0:
                        continue

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

    # Price table: $/M tokens (hardcoded fallback; live data in config/model_pricing.json)
    PRICES = {
        "claude-opus-4": {
            "input": 15.0,
            "output": 75.0,
            "cache_write_1h": 30.0,
            "cache_write_5m": 18.75,
            "cache_read": 1.5,
        },
        "claude-opus-4.1": {
            "input": 15.0,
            "output": 75.0,
            "cache_write_1h": 30.0,
            "cache_write_5m": 18.75,
            "cache_read": 1.5,
        },
        "claude-opus-4.5": {
            "input": 5.0,
            "output": 25.0,
            "cache_write_1h": 10.0,
            "cache_write_5m": 6.25,
            "cache_read": 0.5,
        },
        "claude-opus-4.6": {
            "input": 5.0,
            "output": 25.0,
            "cache_write_1h": 10.0,
            "cache_write_5m": 6.25,
            "cache_read": 0.5,
        },
        "claude-opus-4.7": {
            "input": 5.0,
            "output": 25.0,
            "cache_write_1h": 10.0,
            "cache_write_5m": 6.25,
            "cache_read": 0.5,
        },
        "claude-opus-4.7-fast": {
            "input": 30.0,
            "output": 150.0,
            "cache_write_1h": 60.0,
            "cache_write_5m": 37.5,
            "cache_read": 3.0,
        },
        "claude-opus-4.8": {
            "input": 5.0,
            "output": 25.0,
            "cache_write_1h": 10.0,
            "cache_write_5m": 6.25,
            "cache_read": 0.5,
        },
        "claude-opus-4.8-fast": {
            "input": 10.0,
            "output": 50.0,
            "cache_write_1h": 20.0,
            "cache_write_5m": 12.5,
            "cache_read": 1.0,
        },
        "claude-opus-5": {
            "input": 5.0,
            "output": 25.0,
            "cache_write_1h": 10.0,
            "cache_write_5m": 6.25,
            "cache_read": 0.5,
        },
        "claude-opus-5-fast": {
            "input": 10.0,
            "output": 50.0,
            "cache_write_1h": 20.0,
            "cache_write_5m": 12.5,
            "cache_read": 1.0,
        },
        "claude-sonnet-4": {
            "input": 3.0,
            "output": 15.0,
            "cache_write_1h": 6.0,
            "cache_write_5m": 3.75,
            "cache_read": 0.3,
        },
        "claude-sonnet-4.5": {
            "input": 3.0,
            "output": 15.0,
            "cache_write_1h": 6.0,
            "cache_write_5m": 3.75,
            "cache_read": 0.3,
        },
        "claude-sonnet-4.6": {
            "input": 3.0,
            "output": 15.0,
            "cache_write_1h": 6.0,
            "cache_write_5m": 3.75,
            "cache_read": 0.3,
        },
        "claude-sonnet-5": {
            "input": 2.0,
            "output": 10.0,
            "cache_write_1h": 4.0,
            "cache_write_5m": 2.5,
            "cache_read": 0.2,
        },
        "claude-3-haiku": {
            "input": 0.25,
            "output": 1.25,
            "cache_write_1h": 0.5,
            "cache_write_5m": 0.31,
            "cache_read": 0.03,
        },
        "claude-haiku-4.5": {
            "input": 1.0,
            "output": 5.0,
            "cache_write_1h": 2.0,
            "cache_write_5m": 1.25,
            "cache_read": 0.1,
        },
        "claude-fable-5": {
            "input": 10.0,
            "output": 50.0,
            "cache_write_1h": 20.0,
            "cache_write_5m": 12.5,
            "cache_read": 1.0,
        },
    }
    _external_prices_loaded = False

    @classmethod
    def _load_external_prices(cls):
        """Merge config/model_pricing.json into PRICES (once)."""
        if cls._external_prices_loaded:
            return
        cls._external_prices_loaded = True
        pricing_file = Path(__file__).parent.parent.parent / "config" / "model_pricing.json"
        if not pricing_file.exists():
            return
        try:
            data = json.loads(pricing_file.read_text())
            for model, p in data.get("prices", {}).items():
                if model not in cls.PRICES:
                    cls.PRICES[model] = p
            logger.debug("Loaded external pricing from %s", pricing_file)
        except Exception:
            logger.warning("Failed to load %s, using hardcoded prices", pricing_file)

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
        self._load_external_prices()
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
                    # Claude Code uses type="assistant" (not type="message")
                    if data.get("type") not in ("assistant", "message"):
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
                    # Normalize model name (e.g. claude-opus-4-8 -> claude-opus-4)
                    model = message.get("model", "claude-sonnet-4")
                    # Try exact match first, then strip trailing version suffix
                    prices = self.PRICES.get(model)
                    if not prices:
                        model_normalized = re.sub(r"-\d+$", "", model, count=1)
                        if model_normalized != model:
                            prices = self.PRICES.get(model_normalized)
                    if not prices:
                        prices = self.PRICES["claude-sonnet-4"]

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
    """Collector for OpenCode one-shot invocations.

    OpenCode runs as one-shot (not persistent session), so each run
    corresponds to exactly one task. Cost is captured from stdout or DB.
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
        """Collect cost entry from OpenCode.

        For OpenCode, session_file is actually the stdout capture file.
        Each file contains one JSON object with cost data.
        """
        entries = []

        try:
            if not session_file.exists():
                return entries, 0

            with open(session_file) as f:
                data = json.load(f)

            # Extract cost data from OpenCode JSON output
            cost_usd = data.get("cost", 0)
            if cost_usd <= 0:
                return entries, 1

            model = data.get("modelID")
            tokens = data.get("tokens", {})

            entries.append(
                {
                    "id": f"cost-{uuid.uuid4().hex[:8]}",
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "workflow_id": workflow_id,
                    "source": "opencode",
                    "model": model,
                    "input_tokens": tokens.get("input", 0),
                    "output_tokens": tokens.get("output", 0),
                    "cache_read_tokens": tokens.get("cache", {}).get("read", 0),
                    "cache_write_tokens": tokens.get("cache", {}).get("write", 0),
                    "reasoning_tokens": tokens.get("reasoning", 0),
                    "cost_usd": cost_usd,
                    "recorded_at": datetime.utcnow(),
                    "raw_usage": data,
                }
            )

        except Exception as e:
            logger.error(f"Error collecting OpenCode costs: {e}")

        return entries, 1


class CodexUsageCollector(CostCollector):
    """Collect token deltas from Codex's cumulative usage events.

    Codex transcripts report cumulative token totals for the session. Cost is
    only estimated when all four Codex per-million-token environment rates are
    explicitly configured; transcripts do not contain billable account pricing.
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
        entries = []
        lines_processed = checkpoint
        model = None
        previous_usage = None

        try:
            with open(session_file) as f:
                for line_num, line in enumerate(f, 1):
                    lines_processed = line_num
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    payload = data.get("payload", {})
                    if data.get("type") == "turn_context":
                        model = payload.get("model") or model
                        continue
                    if (
                        data.get("type") != "event_msg"
                        or payload.get("type") != "token_count"
                    ):
                        continue

                    info = payload.get("info", {}).get("total_token_usage")
                    if not isinstance(info, dict):
                        continue
                    current_usage = {
                        "input_tokens": _nonnegative_int(info.get("input_tokens")),
                        "cached_input_tokens": _nonnegative_int(info.get("cached_input_tokens")),
                        "output_tokens": _nonnegative_int(info.get("output_tokens")),
                        "reasoning_output_tokens": _nonnegative_int(
                            info.get("reasoning_output_tokens")
                        ),
                    }
                    if previous_usage is None:
                        deltas = current_usage
                    else:
                        deltas = {
                            key: max(0, value - previous_usage[key])
                            for key, value in current_usage.items()
                        }
                    previous_usage = current_usage

                    if line_num <= checkpoint or not any(deltas.values()):
                        continue

                    rates = _codex_token_rates()
                    if rates:
                        cost_usd = sum(
                            deltas[field] * rates[field] / 1_000_000
                            for field in rates
                        )
                        cost_status = "estimated"
                    else:
                        cost_usd = 0.0
                        cost_status = "unavailable"

                    entries.append(
                        {
                            "id": f"cost-{uuid.uuid4().hex[:8]}",
                            "task_id": task_id,
                            "agent_id": agent_id,
                            "workflow_id": workflow_id,
                            "source": "codex",
                            "model": model,
                            "input_tokens": deltas["input_tokens"],
                            "output_tokens": deltas["output_tokens"],
                            "cache_read_tokens": deltas["cached_input_tokens"],
                            "cache_write_tokens": 0,
                            "reasoning_tokens": deltas["reasoning_output_tokens"],
                            "cost_usd": cost_usd,
                            "recorded_at": datetime.utcnow(),
                            "raw_usage": {
                                "token_usage": info,
                                "cost_status": cost_status,
                            },
                        }
                    )
        except FileNotFoundError:
            logger.warning(f"Session file not found: {session_file}")
        except OSError as exc:
            logger.error(f"Error collecting Codex usage from {session_file}: {exc}")

        return entries, lines_processed


def _nonnegative_int(value: Any) -> int:
    """Convert a transcript token field to a non-negative integer."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _codex_token_rates() -> Optional[dict[str, float]]:
    """Read explicitly configured Codex USD-per-million-token estimates.

    Codex subscription usage cannot be inferred from a transcript. Set all
    four variables only when the account's billing rates are known:
    CODEX_INPUT_COST_PER_MILLION, CODEX_CACHED_INPUT_COST_PER_MILLION,
    CODEX_OUTPUT_COST_PER_MILLION, and CODEX_REASONING_COST_PER_MILLION.
    """
    variables = {
        "input_tokens": "CODEX_INPUT_COST_PER_MILLION",
        "cached_input_tokens": "CODEX_CACHED_INPUT_COST_PER_MILLION",
        "output_tokens": "CODEX_OUTPUT_COST_PER_MILLION",
        "reasoning_output_tokens": "CODEX_REASONING_COST_PER_MILLION",
    }
    try:
        rates = {field: float(os.environ[name]) for field, name in variables.items()}
    except (KeyError, ValueError):
        return None
    return rates if all(rate >= 0 for rate in rates.values()) else None


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


def _discover_codex_session_file(session_id: str, cwd: str) -> Optional[Path]:
    """Find the Codex transcript recorded for a Hephaestus session.

    Codex assigns a UUID itself.  ``CodexAgent.record_session`` persists that
    UUID in the worktree so it can be associated with Hephaestus's stable
    session ID here.
    """
    if ".." in cwd or "~" in cwd:
        logger.warning(f"Rejected Codex session discovery with suspicious cwd: {cwd}")
        return None

    try:
        session_map = json.loads(
            (Path(cwd) / ".hephaestus" / "codex_sessions.json").read_text()
        )
        codex_session_id = str(uuid.UUID(session_map.get(session_id)))
    except (OSError, ValueError, json.JSONDecodeError, TypeError, AttributeError):
        return None

    sessions_dir = Path.home() / ".codex" / "sessions"
    try:
        base = sessions_dir.resolve()
        if not sessions_dir.exists():
            return None
        for transcript in sorted(sessions_dir.glob("**/*.jsonl"), reverse=True):
            if not transcript.is_file() or not transcript.resolve().is_relative_to(base):
                continue
            with transcript.open() as f:
                metadata = json.loads(f.readline())
            payload = metadata.get("payload", {})
            if (
                metadata.get("type") == "session_meta"
                and payload.get("session_id") == codex_session_id
            ):
                return transcript
    except (OSError, ValueError, json.JSONDecodeError, TypeError):
        return None

    return None


def collect_task_cost(task_id: str) -> None:
    """Entry point for cost collection on task completion.

    Called from task_completion_service when a task completes.
    Looks up task → agent → session_id → discovers session file
    → delegates to appropriate collector → writes CostEntry rows.

    Args:
        task_id: The completed task's ID
    """
    from src.core.cost_derivation import record_cost
    from src.core.database import Agent, CostEntry, SessionCostCheckpoint, Task, get_db

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

        # Fallback: if assigned_agent_id was cleared (e.g. by terminate_agent
        # resetting stray tasks), check if any agent still has current_task_id
        # pointing at this task.
        if not agent:
            agent = db.query(Agent).filter_by(current_task_id=task_id).first()

        if not agent:
            logger.debug(f"[COST-COLLECT] Task {task_id[:8]} has no assigned agent — skipping")
            return

        # Discover session file based on CLI type
        cli_type = agent.cli_type or "pi"

        # Skip token-based cost collection for subscription CLIs (Pro/Team/etc.)
        # These accounts pay a flat monthly fee — counting tokens is misleading.
        if _is_subscription_cli(cli_type):
            logger.debug(f"[COST-COLLECT] Task {task_id[:8]} agent {agent.id[:8]} uses {cli_type} subscription — skipping token cost collection")
            return

        # The pi extension posts costs to /api/autopilot/cost-entries in
        # real time as turns complete (source="pi" CostEntry rows). If any
        # such rows already exist for this task's assigned agent, the
        # extension was active for this session and is the source of truth
        # — tailing the JSONL transcript here as well would re-record the
        # same turns a second time (they were never checkpointed by the
        # real-time POST path). Trust the extension exclusively once it's
        # proven active, rather than double-counting. Scoped to this task's
        # assigned agent (not just task_id) so a cost entry posted for an
        # unrelated agent can't be mistaken for proof this task's own
        # session already reported in, which would suppress its real cost
        # data entirely.
        if cli_type == "pi":
            has_realtime_entries = db.query(CostEntry).filter_by(task_id=task_id, agent_id=agent.id, source="pi").first() is not None
            if has_realtime_entries:
                logger.debug(f"[COST-COLLECT] Task {task_id[:8]} already has real-time pi cost entries — skipping JSONL fallback to avoid double-counting")
                return

        # Get session ID from agent's launch params or task metadata
        # The session ID is typically stored in the agent's tmux session name
        # or passed via --session-id flag
        session_id = _extract_session_id(agent, task)
        if not session_id:
            logger.debug(f"[COST-COLLECT] No session ID for task {task_id[:8]} agent {agent.id[:8]} — skipping")
            return

        # Get or create checkpoint
        checkpoint_row = db.query(SessionCostCheckpoint).filter_by(session_id=session_id).first()
        checkpoint = checkpoint_row.lines_processed if checkpoint_row else 0

        session_file = None

        if cli_type == "pi":
            # Discover pi session file
            cwd = _get_agent_cwd(db, agent, task)
            if cwd:
                session_file = _discover_session_file(session_id, cwd)
        elif cli_type in ("claude_code", "claude"):
            # Claude Code session files in ~/.claude/projects/<sanitized_cwd>/
            cwd = _get_agent_cwd(db, agent, task)
            if cwd:
                # SECURITY: Sanitize cwd to prevent path traversal
                if ".." in cwd or "~" in cwd:
                    logger.warning(f"Rejected Claude Code session discovery with suspicious cwd: {cwd}")
                else:
                    # Claude Code sanitizes cwd to a directory name by replacing
                    # / -> -, . -> -, _ -> -.  This matches the observed directory
                    # names in ~/.claude/projects/.
                    sanitized = cwd.replace("/", "-").replace(".", "-").replace("_", "-")
                    claude_dir = Path.home() / ".claude" / "projects" / sanitized

                    # SECURITY: Verify the resolved path is within expected directory
                    try:
                        resolved = claude_dir.resolve()
                        base = (Path.home() / ".claude" / "projects").resolve()
                        if not str(resolved).startswith(str(base)):
                            logger.warning(f"Claude Code path escapes base directory: {resolved}")
                        else:
                            if claude_dir.exists():
                                # Claude Code files are named {uuid}.jsonl where
                                # uuid = uuid5(NAMESPACE_URL, session_id)
                                session_uuid = _session_id_to_uuid(session_id)
                                session_file_candidate = claude_dir / f"{session_uuid}.jsonl"
                                if session_file_candidate.exists():
                                    session_file = session_file_candidate
                                else:
                                    # Fallback: try glob pattern
                                    matches = list(claude_dir.glob(f"*_{session_id}.jsonl"))
                                    if matches:
                                        session_file = matches[0]
                    except (OSError, ValueError):
                        pass
        elif cli_type == "opencode":
            # OpenCode uses one-shot capture, not session tailing
            # This path would need stdout capture file
            pass
        elif cli_type == "codex":
            cwd = _get_agent_cwd(db, agent, task)
            if cwd:
                session_file = _discover_codex_session_file(session_id, cwd)

        if not session_file:
            logger.debug(f"[COST-COLLECT] No session file found for {cli_type} session {session_id[:8]} — skipping")
            return

        # Select collector
        collectors = {
            "pi": PiJsonlCollector(),
            "claude_code": ClaudeCodeCollector(),
            "claude": ClaudeCodeCollector(),
            "opencode": OpenCodeCollector(),
            "codex": CodexUsageCollector(),
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

        # Write entries and trigger derivation. Each entry is committed
        # individually so a single bad entry (e.g. a validation error) can't
        # roll back and silently discard entries already recorded earlier in
        # this batch, or skip the checkpoint update below.
        failed_count = 0
        for entry_data in entries:
            try:
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
                db.commit()
            except Exception as e:
                db.rollback()
                failed_count += 1
                logger.error(f"[COST-COLLECT] Failed to record cost entry for task {task_id[:8]}: {e}")

        if failed_count:
            logger.error(f"[COST-COLLECT] {failed_count}/{len(entries)} cost entries failed to record for task {task_id[:8]} — skipped, rest of batch still processed")

        # Update checkpoint
        if checkpoint_row:
            checkpoint_row.lines_processed = new_checkpoint
            checkpoint_row.updated_at = datetime.utcnow()
        else:
            checkpoint_row = SessionCostCheckpoint(
                session_id=session_id,
                lines_processed=new_checkpoint,
                updated_at=datetime.utcnow(),
            )
            db.add(checkpoint_row)

        db.commit()

        if entries:
            total_cost = sum(e["cost_usd"] for e in entries)
            logger.info(f"[COST-COLLECT] Collected {len(entries)} entries (${total_cost:.4f}) for task {task_id[:8]} from {cli_type}")


def _extract_session_id(agent: Any, task: Any) -> Optional[str]:
    """Reconstruct the deterministic session ID for this agent/task.

    The session ID is generated by get_session_id() in
    src/autopilot/phases.py from (project_id, design_slug, phase_name,
    model) and passed to pi via --session-id.  It is NOT stored on the
    agent or task rows, so we reconstruct it from the workflow's
    launch_params and the task's phase.
    """
    from src.core.database import Phase, Workflow, get_db

    if not task.workflow_id:
        return None

    try:
        with get_db() as db:
            wf = db.query(Workflow).filter_by(id=task.workflow_id).first()
            if not wf or not wf.launch_params:
                return None

            lp = wf.launch_params if isinstance(wf.launch_params, dict) else {}
            project_id = lp.get("project_id") or lp.get("project_path", "")
            design_slug = lp.get("design_slug") or lp.get("design_id") or lp.get("feature_id", "")

            phase_name = ""
            if task.phase_id:
                phase = db.query(Phase).filter_by(id=task.phase_id).first()
                if phase:
                    phase_name = phase.name

            if not project_id or not design_slug or not phase_name:
                return None

            model = agent.cli_model or ""
            from src.autopilot.phases import get_session_id

            return get_session_id(project_id, design_slug, phase_name, model=model)
    except Exception as e:
        logger.debug(f"Could not reconstruct session ID for agent {agent.id[:8]}: {e}")
        return None


def _session_id_to_uuid(session_id: str) -> str:
    """Derive the UUID that Claude Code uses as its session file name.

    ClaudeCodeAgent.get_launch_command uses uuid5(NAMESPACE_URL, session_id)
    to derive a valid UUID from the deterministic session ID.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, session_id))


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
