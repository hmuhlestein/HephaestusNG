"""Tests for cost collection service — collectors, checkpoint, and task-cost wiring."""

import json
import os
import tempfile
import uuid
from pathlib import Path

from src.services.cost_collection_service import (
    ClaudeCodeCollector,
    CodexStubCollector,
    OpenCodeCollector,
    PiJsonlCollector,
    _discover_session_file,
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


# ── OpenCodeCollector ───────────────────────────────────────────


class TestOpenCodeCollector:
    def test_collect_basic(self):
        """OpenCode collector reads JSON output."""
        data = {
            "cost": 0.25,
            "modelID": "anthropic/claude-sonnet-4",
            "tokens": {
                "input": 5000,
                "output": 1000,
                "reasoning": 100,
                "cache": {"read": 500, "write": 200},
            },
        }
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump(data, f)

        collector = OpenCodeCollector()
        entries, checkpoint = collector.collect("s", "t", "w", "a", Path(path), checkpoint=0)

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

    def test_missing_file(self):
        """Missing file returns empty."""
        collector = OpenCodeCollector()
        entries, cp = collector.collect("s", "t", "w", "a", Path("/nonexistent"), 0)
        assert entries == []
        assert cp == 0

    def test_zero_cost_skipped(self):
        """Zero cost returns empty."""
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump({"cost": 0}, f)

        collector = OpenCodeCollector()
        entries, _ = collector.collect("s", "t", "w", "a", Path(path), 0)
        assert len(entries) == 0


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
