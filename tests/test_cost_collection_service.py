"""Tests for cost collection service — collectors, checkpoint, and task-cost wiring."""

import json
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from src.services.cost_collection_service import (
    ClaudeCodeCollector,
    CodexStubCollector,
    OpenCodeCollector,
    PiJsonlCollector,
    _discover_opencode_session,
    _discover_session_file,
    collect_task_cost,
)

# ── Helpers ─────────────────────────────────────────────────────


def _make_assistant_message(cost_total: float, model: str = "anthropic/claude-sonnet-4") -> str:
    """Build a realistic pi session JSONL line with usage."""
    return json.dumps(
        {
            "type": "message",
            "id": uuid.uuid4().hex[:8],
            "timestamp": "2026-07-21T12:00:00.000Z",
            "message": {
                "role": "assistant",
                "api": "openai-completions",
                "provider": "openrouter",
                "model": model,
                "usage": {
                    "input": 5000,
                    "output": 200,
                    "cacheRead": 100,
                    "cacheWrite": 0,
                    "reasoning": 50,
                    "totalTokens": 5350,
                    "cost": {
                        "input": 0.005,
                        "output": 0.0003,
                        "cacheRead": 0.00001,
                        "cacheWrite": 0,
                        "total": cost_total,
                    },
                },
            },
        }
    )


def _make_temp_jsonl(lines: list[str]) -> Path:
    """Write lines to a temp JSONL file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    os.close(fd)
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")
    return Path(path)


# ── PiJsonlCollector ────────────────────────────────────────────


class TestPiJsonlCollector:
    def test_collect_basic(self):
        """Basic cost collection from a pi JSONL file."""
        lines = [
            '{"type": "session", "id": "sess-abc123"}',
            _make_assistant_message(0.01),
            _make_assistant_message(0.02),
        ]

        f = _make_temp_jsonl(lines)
        collector = PiJsonlCollector()
        entries, checkpoint = collector.collect(
            session_id="sess-abc123",
            task_id="task-test",
            workflow_id="wf-test",
            agent_id="agent-test",
            session_file=f,
            checkpoint=0,
        )

        assert len(entries) == 2
        assert entries[0]["source"] == "pi"
        assert entries[0]["cost_usd"] == 0.01
        assert entries[0]["model"] == "anthropic/claude-sonnet-4"
        assert checkpoint == 3  # 3 lines total

    def test_exclude_non_assistant_lines(self):
        """Non-assistant and non-message lines are excluded."""
        lines = [
            '{"type": "session", "id": "sess-abc123"}',
            '{"type": "message", "message": {"role": "user", "content": "hello"}}',
            '{"type": "model_change", "model": "claude-sonnet-4"}',
        ]

        f = _make_temp_jsonl(lines)
        collector = PiJsonlCollector()
        entries, checkpoint = collector.collect(
            session_id="sess-abc123",
            task_id="task-test",
            workflow_id="wf-test",
            agent_id="agent-test",
            session_file=f,
            checkpoint=0,
        )

        assert len(entries) == 0
        assert checkpoint == 3

    def test_checkpoint_skips_processed(self):
        """Lines before checkpoint are skipped."""
        lines = [
            '{"type": "session", "id": "sess-abc"}',
            _make_assistant_message(0.01),
            _make_assistant_message(0.02),
            _make_assistant_message(0.03),
        ]

        f = _make_temp_jsonl(lines)
        collector = PiJsonlCollector()
        entries, checkpoint = collector.collect(
            session_id="sess-abc",
            task_id="task-test",
            workflow_id="wf-test",
            agent_id="agent-test",
            session_file=f,
            checkpoint=2,  # skip first 2 lines
        )

        assert len(entries) == 2
        assert entries[0]["cost_usd"] == 0.02
        assert entries[1]["cost_usd"] == 0.03
        assert checkpoint == 4

    def test_no_double_counting(self):
        """Two consecutive collects with advancing checkpoint don't double-count."""
        lines = [
            _make_assistant_message(0.01),
            _make_assistant_message(0.02),
        ]

        f = _make_temp_jsonl(lines)
        collector = PiJsonlCollector()

        entries1, cp1 = collector.collect("s", "t", "w", "a", f, checkpoint=0)
        assert len(entries1) == 2
        assert cp1 == 2

        entries2, cp2 = collector.collect("s", "t", "w", "a", f, checkpoint=cp1)
        assert len(entries2) == 0
        assert cp2 == 2

    def test_token_extraction(self):
        """Token counts are extracted from usage data."""
        line = json.dumps(
            {
                "type": "message",
                "id": "abc",
                "message": {
                    "role": "assistant",
                    "model": "openai/gpt-4o",
                    "usage": {
                        "input": 9430,
                        "output": 222,
                        "cacheRead": 512,
                        "cacheWrite": 0,
                        "reasoning": 99,
                        "cost": {"total": 0.005},
                    },
                },
            }
        )
        f = _make_temp_jsonl([line])
        collector = PiJsonlCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)

        assert len(entries) == 1
        assert entries[0]["input_tokens"] == 9430
        assert entries[0]["output_tokens"] == 222
        assert entries[0]["cache_read_tokens"] == 512
        assert entries[0]["reasoning_tokens"] == 99

    def test_skip_zero_cost(self):
        """Zero-cost entries are skipped."""
        line = _make_assistant_message(0.0)
        f = _make_temp_jsonl([line])
        collector = PiJsonlCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)
        assert len(entries) == 0

    def test_malformed_line_skipped(self):
        """Malformed JSON lines are skipped gracefully."""
        lines = [
            "not valid json!!!",
            _make_assistant_message(0.05),
        ]
        f = _make_temp_jsonl(lines)
        collector = PiJsonlCollector()
        entries, checkpoint = collector.collect("s", "t", "w", "a", f, checkpoint=0)
        assert len(entries) == 1
        assert entries[0]["cost_usd"] == 0.05

    def test_missing_file(self):
        """Missing session file returns empty entries."""
        collector = PiJsonlCollector()
        entries, checkpoint = collector.collect("s", "t", "w", "a", Path("/nonexistent/file.jsonl"), checkpoint=0)
        assert len(entries) == 0
        assert checkpoint == 0


# ── ClaudeCodeCollector ─────────────────────────────────────────


class TestClaudeCodeCollector:
    def test_token_pricing(self):
        """Claude Code collector converts tokens to dollars via price table."""
        line = json.dumps(
            {
                "type": "message",
                "id": "cc1",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4",
                    "usage": {
                        "input_tokens": 1_000_000,  # 1M input tokens
                        "output_tokens": 100_000,  # 100k output tokens
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 0,
                            "ephemeral_5m_input_tokens": 0,
                        },
                    },
                },
            }
        )
        f = _make_temp_jsonl([line])
        collector = ClaudeCodeCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)

        assert len(entries) == 1
        # Expected: 1M * $3/M + 100k * $15/M = $3.0 + $1.5 = $4.5
        assert abs(entries[0]["cost_usd"] - 4.5) < 0.01
        assert entries[0]["source"] == "claude_code"

    def test_cache_1h_cost(self):
        """Cache 1h tokens use the correct (higher) cache-write rate."""
        line = json.dumps(
            {
                "type": "message",
                "id": "cc2",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 1_000_000,
                            "ephemeral_5m_input_tokens": 0,
                        },
                    },
                },
            }
        )
        f = _make_temp_jsonl([line])
        collector = ClaudeCodeCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)

        assert len(entries) == 1
        # Expected: 1M * $3.75/M = $3.75
        assert abs(entries[0]["cost_usd"] - 3.75) < 0.01

    def test_cache_read_cost(self):
        """Cache-read tokens use the cheapest rate."""
        line = json.dumps(
            {
                "type": "message",
                "id": "cc3",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4",
                    "usage": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 1_000_000,
                        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
                    },
                },
            }
        )
        f = _make_temp_jsonl([line])
        collector = ClaudeCodeCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)

        assert len(entries) == 1
        # Expected: 1M * $0.30/M = $0.30
        assert abs(entries[0]["cost_usd"] - 0.30) < 0.01

    def test_unknown_model_defaults_to_sonnet(self):
        """Unknown model defaults to claude-sonnet-4 pricing."""
        line = json.dumps(
            {
                "type": "message",
                "id": "cc4",
                "message": {
                    "role": "assistant",
                    "model": "unknown-model-xyz",
                    "usage": {
                        "input_tokens": 1_000_000,
                        "output_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
                    },
                },
            }
        )
        f = _make_temp_jsonl([line])
        collector = ClaudeCodeCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)

        assert len(entries) == 1
        # Default sonnet input: $3/M → 1M * $3/M = $3.0
        assert abs(entries[0]["cost_usd"] - 3.0) < 0.01

    def test_skip_zero_cost(self):
        """Zero-cost entries are skipped."""
        line = json.dumps(
            {
                "type": "message",
                "id": "cc5",
                "message": {
                    "role": "assistant",
                    "model": "claude-sonnet-4",
                    "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0, "cache_creation": {}},
                },
            }
        )
        f = _make_temp_jsonl([line])
        collector = ClaudeCodeCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)
        assert len(entries) == 0


# ── CodexStubCollector ──────────────────────────────────────────


class TestCodexStubCollector:
    def test_returns_empty(self):
        """Stub collector always returns empty."""
        collector = CodexStubCollector()
        entries, checkpoint = collector.collect("s", "t", "w", "a", Path("/fake"), checkpoint=10)
        assert entries == []
        assert checkpoint == 10


# ── OpenCode test helpers ────────────────────────────────────────


def _write_session_rows(path: Path, rows: list[dict]) -> Path:
    """Create/populate a SQLite file at `path` with a `session` table matching the real schema."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE session (
            id TEXT PRIMARY KEY,
            directory TEXT NOT NULL,
            time_created INTEGER NOT NULL,
            model TEXT,
            cost REAL DEFAULT 0 NOT NULL,
            tokens_input INTEGER DEFAULT 0 NOT NULL,
            tokens_output INTEGER DEFAULT 0 NOT NULL,
            tokens_reasoning INTEGER DEFAULT 0 NOT NULL,
            tokens_cache_read INTEGER DEFAULT 0 NOT NULL,
            tokens_cache_write INTEGER DEFAULT 0 NOT NULL
        )
        """
    )
    for row in rows:
        conn.execute(
            "INSERT INTO session (id, directory, time_created, model, cost, tokens_input, "
            "tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["id"],
                row["directory"],
                row["time_created"],
                row.get("model"),
                row.get("cost", 0),
                row.get("tokens_input", 0),
                row.get("tokens_output", 0),
                row.get("tokens_reasoning", 0),
                row.get("tokens_cache_read", 0),
                row.get("tokens_cache_write", 0),
            ),
        )
    conn.commit()
    conn.close()
    return path


def _make_opencode_db(rows: list[dict]) -> Path:
    """Create a temp SQLite file with a `session` table matching the real schema."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return _write_session_rows(Path(path), rows)


def _append_session_row(path: Path, row: dict) -> None:
    """Insert one more row into an existing opencode.db-shaped SQLite file."""
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO session (id, directory, time_created, model, cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            row["directory"],
            row["time_created"],
            row.get("model"),
            row.get("cost", 0),
            row.get("tokens_input", 0),
            row.get("tokens_output", 0),
            row.get("tokens_reasoning", 0),
            row.get("tokens_cache_read", 0),
            row.get("tokens_cache_write", 0),
        ),
    )
    conn.commit()
    conn.close()


def _ms(dt: datetime) -> int:
    """Convert a naive UTC datetime to real UTC epoch-ms (matching OpenCode's own
    session.time_created values), independent of the host's local timezone."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


# ── OpenCodeCollector ───────────────────────────────────────────


class TestOpenCodeCollector:
    def test_collect_basic(self):
        """OpenCode collector reads a pre-aggregated session row from opencode.db."""
        db_path = _make_opencode_db(
            [
                {
                    "id": "ses_abc",
                    "directory": "/proj",
                    "time_created": _ms(datetime.utcnow()),
                    "model": json.dumps({"id": "anthropic/claude-sonnet-4", "providerID": "anthropic"}),
                    "cost": 0.25,
                    "tokens_input": 5000,
                    "tokens_output": 1000,
                    "tokens_reasoning": 100,
                    "tokens_cache_read": 500,
                    "tokens_cache_write": 200,
                }
            ]
        )

        collector = OpenCodeCollector(session_row_id="ses_abc")
        entries, checkpoint = collector.collect("s", "t", "w", "a", db_path, checkpoint=0)

        assert len(entries) == 1
        assert entries[0]["cost_usd"] == 0.25
        assert entries[0]["source"] == "opencode"
        assert entries[0]["model"] == "anthropic/claude-sonnet-4"
        assert entries[0]["input_tokens"] == 5000
        assert entries[0]["output_tokens"] == 1000
        assert entries[0]["reasoning_tokens"] == 100
        assert entries[0]["cache_read_tokens"] == 500
        assert entries[0]["cache_write_tokens"] == 200
        assert checkpoint == 1

    def test_already_collected_checkpoint_short_circuits(self):
        """checkpoint >= 1 returns immediately without querying."""
        collector = OpenCodeCollector(session_row_id="ses_abc")
        entries, cp = collector.collect("s", "t", "w", "a", Path("/nonexistent"), checkpoint=1)
        assert entries == []
        assert cp == 1

    def test_no_session_row_id(self):
        """No session_row_id set returns empty, checkpoint unchanged."""
        collector = OpenCodeCollector()
        entries, cp = collector.collect("s", "t", "w", "a", Path("/nonexistent"), checkpoint=0)
        assert entries == []
        assert cp == 0

    def test_missing_row(self):
        """Row not found for the given id returns empty, checkpoint advances to 1."""
        db_path = _make_opencode_db([])
        collector = OpenCodeCollector(session_row_id="ses_missing")
        entries, cp = collector.collect("s", "t", "w", "a", db_path, checkpoint=0)
        assert entries == []
        assert cp == 1

    def test_zero_cost_skipped(self):
        """Zero cost still advances checkpoint but returns no entry."""
        db_path = _make_opencode_db([{"id": "ses_zero", "directory": "/proj", "time_created": _ms(datetime.utcnow()), "cost": 0}])
        collector = OpenCodeCollector(session_row_id="ses_zero")
        entries, cp = collector.collect("s", "t", "w", "a", db_path, checkpoint=0)
        assert entries == []
        assert cp == 1

    def test_malformed_model_json_falls_back_to_raw(self):
        """A non-JSON model column value is used as-is."""
        db_path = _make_opencode_db(
            [
                {
                    "id": "ses_raw",
                    "directory": "/proj",
                    "time_created": _ms(datetime.utcnow()),
                    "model": "not-json",
                    "cost": 0.1,
                }
            ]
        )
        collector = OpenCodeCollector(session_row_id="ses_raw")
        entries, _ = collector.collect("s", "t", "w", "a", db_path, checkpoint=0)
        assert entries[0]["model"] == "not-json"


# ── _discover_opencode_session ───────────────────────────────────


class TestDiscoverOpencodeSession:
    def test_no_db_file(self, tmp_path, monkeypatch):
        """Returns None when opencode.db doesn't exist."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _discover_opencode_session("/proj", datetime.utcnow())
        assert result is None

    def test_single_match(self, tmp_path, monkeypatch):
        """A single in-window session is returned."""
        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        agent_created = datetime.utcnow() - timedelta(minutes=5)
        _write_session_rows(
            db_path,
            [{"id": "ses_1", "directory": "/proj", "time_created": _ms(datetime.utcnow())}],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _discover_opencode_session("/proj", agent_created)
        assert result == (db_path, "ses_1")

    def test_empty_result(self, tmp_path, monkeypatch):
        """No matching directory returns None."""
        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        _write_session_rows(
            db_path,
            [{"id": "ses_1", "directory": "/other", "time_created": _ms(datetime.utcnow())}],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _discover_opencode_session("/proj", datetime.utcnow() - timedelta(minutes=5))
        assert result is None

    def test_multiple_matches_picks_most_recent(self, tmp_path, monkeypatch):
        """Tie-break policy: most recent time_created in-window wins."""
        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        agent_created = datetime.utcnow() - timedelta(minutes=10)
        older = datetime.utcnow() - timedelta(minutes=5)
        newer = datetime.utcnow() - timedelta(minutes=1)
        _write_session_rows(
            db_path,
            [
                {"id": "ses_old", "directory": "/proj", "time_created": _ms(older)},
                {"id": "ses_new", "directory": "/proj", "time_created": _ms(newer)},
            ],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _discover_opencode_session("/proj", agent_created)
        assert result == (db_path, "ses_new")

    def test_directory_mismatch(self, tmp_path, monkeypatch):
        """Session with a different directory is not matched."""
        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        _write_session_rows(
            db_path,
            [{"id": "ses_1", "directory": "/proj-other", "time_created": _ms(datetime.utcnow())}],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _discover_opencode_session("/proj", datetime.utcnow() - timedelta(minutes=5))
        assert result is None

    def test_session_before_window_excluded(self, tmp_path, monkeypatch):
        """A session created before agent_created_at is excluded."""
        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        agent_created = datetime.utcnow()
        too_early = agent_created - timedelta(minutes=1)
        _write_session_rows(
            db_path,
            [{"id": "ses_early", "directory": "/proj", "time_created": _ms(too_early)}],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _discover_opencode_session("/proj", agent_created)
        assert result is None

    def test_session_after_now_excluded(self, tmp_path, monkeypatch):
        """A session created after 'now' is excluded."""
        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        too_late = datetime.utcnow() + timedelta(minutes=5)
        _write_session_rows(
            db_path,
            [{"id": "ses_late", "directory": "/proj", "time_created": _ms(too_late)}],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _discover_opencode_session("/proj", datetime.utcnow() - timedelta(minutes=5))
        assert result is None

    def test_finds_session_using_real_utc_epoch_regardless_of_host_tz(self, tmp_path, monkeypatch):
        """B-1 regression: naive-datetime .timestamp() assumes *local* time, so
        computing the window bounds from naive UTC datetimes without attaching
        tzinfo=utc shifts both bounds away from real UTC epoch-ms by the host's
        UTC offset -- a genuinely-UTC session.time_created (written by OpenCode's
        own runtime, unaffected by this bug) then never falls inside the window
        on any host whose local timezone isn't UTC.

        The session's time_created here is computed independently via
        calendar.timegm (always interprets a naive tuple as UTC, ignoring the
        host's local TZ) rather than via the production/test-fixture .timestamp()
        conversion under test, so this checks the result against a real,
        TZ-independent UTC epoch value rather than just checking both sides of
        the comparison agree with each other. This machine's ambient timezone is
        already non-UTC (MDT), which is exactly the condition this bug requires
        to reproduce -- no TZ manipulation needed.
        """
        import calendar

        if time.timezone == 0 and time.altzone == 0:
            pytest.skip("Host timezone is already UTC; this regression only reproduces on a non-UTC host.")

        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"

        now_utc = datetime.utcnow()
        agent_created = now_utc - timedelta(minutes=5)
        session_created = now_utc - timedelta(minutes=1)
        session_time_ms = calendar.timegm(session_created.timetuple()) * 1000

        _write_session_rows(
            db_path,
            [{"id": "ses_tz", "directory": "/proj", "time_created": session_time_ms}],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        result = _discover_opencode_session("/proj", agent_created)

        assert result == (db_path, "ses_tz")


# ── _discover_session_file ──────────────────────────────────────


class TestDiscoverSessionFile:
    def test_rejects_path_traversal(self):
        """Rejects cwd with path traversal sequences."""
        result = _discover_session_file("sess-abc", "/some/path/../../../etc")
        assert result is None

    def test_rejects_tilde(self):
        """Rejects cwd with tilde."""
        result = _discover_session_file("sess-abc", "~/sneaky/path")
        assert result is None

    def test_no_session_dir(self):
        """Returns None when sessions directory doesn't exist."""
        result = _discover_session_file("sess-nonexistent", "/completely/fake/path/xyz123")
        assert result is None


# ── collect_task_cost integration (opencode) ─────────────────────


class TestCollectTaskCostOpenCode:
    def _seed(self, db_manager, cwd: str):
        """Create Agent/Task/Workflow rows for an opencode task in the given cwd."""
        from src.core.database import Agent, Task, Workflow

        session = db_manager.get_session()
        agent = Agent(
            id=f"agent-{uuid.uuid4().hex[:8]}",
            system_prompt="test",
            cli_type="opencode",
            tmux_session_name=f"hephaestus-proj-design-role-testsession-{uuid.uuid4().hex[:8]}",
            created_at=datetime.utcnow() - timedelta(minutes=5),
        )
        workflow = Workflow(
            id=f"wf-{uuid.uuid4().hex[:8]}",
            name="test-workflow",
            phases_folder_path="./sample-phases",
            working_directory=cwd,
        )
        session.add_all([agent, workflow])
        session.flush()
        task = Task(
            id=f"task-{uuid.uuid4().hex[:8]}",
            raw_description="test task",
            done_definition="done",
            assigned_agent_id=agent.id,
            workflow_id=workflow.id,
            status="done",
        )
        session.add(task)
        session.commit()
        task_id, agent_id = task.id, agent.id
        session.close()
        return task_id, agent_id

    def test_writes_cost_entry_and_checkpoint(self, db_manager, tmp_path, monkeypatch):
        """A matching opencode.db session row produces a CostEntry and checkpoint."""
        from src.core.database import CostEntry, SessionCostCheckpoint

        cwd = "/proj"
        task_id, _ = self._seed(db_manager, cwd)

        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        _write_session_rows(
            db_path,
            [
                {
                    "id": "ses_int1",
                    "directory": cwd,
                    "time_created": _ms(datetime.utcnow()),
                    "model": json.dumps({"id": "anthropic/claude-sonnet-4"}),
                    "cost": 0.42,
                    "tokens_input": 100,
                    "tokens_output": 50,
                }
            ],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        collect_task_cost(task_id)

        session = db_manager.get_session()
        entries = session.query(CostEntry).filter_by(task_id=task_id).all()
        assert len(entries) == 1
        assert entries[0].source == "opencode"
        assert entries[0].cost_usd == 0.42

        # Checkpoint is keyed by the discovered opencode session_row_id, not the
        # shared Hephaestus session_id -- see test_shared_hephaestus_session_id_
        # does_not_drop_second_launch for why.
        checkpoint = session.query(SessionCostCheckpoint).filter_by(session_id="ses_int1").first()
        assert checkpoint is not None
        assert checkpoint.lines_processed == 1
        session.close()

    def test_second_call_does_not_double_record(self, db_manager, tmp_path, monkeypatch):
        """A second collect_task_cost call for the same session doesn't write a second entry."""
        from src.core.database import CostEntry

        cwd = "/proj"
        task_id, _ = self._seed(db_manager, cwd)

        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        _write_session_rows(
            db_path,
            [{"id": "ses_int2", "directory": cwd, "time_created": _ms(datetime.utcnow()), "cost": 0.1}],
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        collect_task_cost(task_id)
        collect_task_cost(task_id)

        session = db_manager.get_session()
        entries = session.query(CostEntry).filter_by(task_id=task_id).all()
        assert len(entries) == 1
        session.close()

    def test_no_opencode_db_present(self, db_manager, tmp_path, monkeypatch):
        """No opencode.db on disk: no CostEntry written, no exception raised."""
        from src.core.database import CostEntry

        cwd = "/proj"
        task_id, _ = self._seed(db_manager, cwd)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        collect_task_cost(task_id)

        session = db_manager.get_session()
        entries = session.query(CostEntry).filter_by(task_id=task_id).all()
        assert len(entries) == 0
        session.close()

    def test_shared_hephaestus_session_id_does_not_drop_second_launch(self, db_manager, tmp_path, monkeypatch):
        """Two independent OpenCode launches sharing a Hephaestus session_id (e.g.
        the same session_role reused across phases, per get_session_id()) must
        each get their own checkpoint -- OpenCode never resumes, so every launch
        mints a fresh, unrelated opencode.db session row. If the checkpoint were
        keyed by the shared session_id (as pi/claude_code correctly are), the
        second launch's collection would find the checkpoint already at 1 from
        the first launch and skip querying its own session row entirely, silently
        losing its cost.

        Session rows are written sequentially (launch 1's collection runs while
        only its own row exists) so directory+time-window discovery deterministically
        resolves each task to its own row -- isolating the checkpoint-key bug from
        the separate, already-covered multi-match tie-break behavior.
        """
        from src.core.database import CostEntry

        cwd = "/proj"
        task1_id, _ = self._seed(db_manager, cwd)
        task2_id, _ = self._seed(db_manager, cwd)

        opencode_dir = tmp_path / ".local" / "share" / "opencode"
        opencode_dir.mkdir(parents=True)
        db_path = opencode_dir / "opencode.db"
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        base = datetime.utcnow()
        _write_session_rows(
            db_path,
            [{"id": "ses_launch1", "directory": cwd, "time_created": _ms(base - timedelta(seconds=2)), "cost": 0.10}],
        )

        with patch(
            "src.services.cost_collection_service._extract_session_id",
            return_value="shared-session-id",
        ):
            collect_task_cost(task1_id)

            _append_session_row(
                db_path,
                {"id": "ses_launch2", "directory": cwd, "time_created": _ms(base - timedelta(seconds=1)), "cost": 0.20},
            )
            collect_task_cost(task2_id)

        session = db_manager.get_session()
        entries = session.query(CostEntry).filter(CostEntry.task_id.in_([task1_id, task2_id])).all()
        assert len(entries) == 2
        costs = sorted(e.cost_usd for e in entries)
        assert costs == [0.10, 0.20]
        session.close()
