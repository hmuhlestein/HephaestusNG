"""Tests for cost collection service — collectors, checkpoint, and task-cost wiring."""

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import Agent, Base, CostEntry, SessionCostCheckpoint, Task, Workflow
from src.services.cost_collection_service import (
    ClaudeCodeCollector,
    CodexUsageCollector,
    OpenCodeCollector,
    PiJsonlCollector,
    _discover_codex_session_file,
    _discover_session_file,
    _extract_session_id,
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


def _make_codex_token_count(
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
    reasoning_output_tokens: int,
) -> str:
    """Build a Codex cumulative token-count event."""
    return json.dumps(
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_input_tokens,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": reasoning_output_tokens,
                    }
                },
            },
        }
    )


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

    def test_skip_zero_cost_zero_tokens(self):
        """Zero-cost entries with zero tokens are skipped (no usage at all)."""
        line = _make_assistant_message(0.0)
        # Override to have zero tokens
        data = json.loads(line)
        data["message"]["usage"]["input"] = 0
        data["message"]["usage"]["output"] = 0
        f = _make_temp_jsonl([json.dumps(data)])
        collector = PiJsonlCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)
        assert len(entries) == 0

    def test_keep_zero_cost_with_tokens(self):
        """Zero-cost entries with real tokens are kept (local models)."""
        line = _make_assistant_message(0.0)
        f = _make_temp_jsonl([line])
        collector = PiJsonlCollector()
        entries, _ = collector.collect("s", "t", "w", "a", f, checkpoint=0)
        assert len(entries) == 1
        assert entries[0]["cost_usd"] == 0.0
        assert entries[0]["input_tokens"] == 5000

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
        # Expected: 1M * $6.0/M = $6.00 (1h cache write rate for sonnet-4)
        assert abs(entries[0]["cost_usd"] - 6.00) < 0.01

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


# ── CodexUsageCollector ─────────────────────────────────────────


class TestCodexUsageCollector:
    def test_collects_cumulative_token_deltas(self):
        """Codex cumulative token events are converted to per-event deltas."""
        lines = [
            json.dumps({"type": "session_meta", "payload": {"session_id": str(uuid.uuid4())}}),
            json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}}),
            _make_codex_token_count(100, 10, 20, 5),
            _make_codex_token_count(180, 30, 50, 12),
        ]
        f = _make_temp_jsonl(lines)

        entries, checkpoint = CodexUsageCollector().collect("s", "t", "w", "a", f, 0)

        assert checkpoint == 4
        assert len(entries) == 2
        assert entries[0]["source"] == "codex"
        assert entries[0]["model"] == "gpt-5.6-terra"
        assert entries[0]["cost_usd"] == 0.0
        assert entries[1]["input_tokens"] == 80
        assert entries[1]["cache_read_tokens"] == 20
        assert entries[1]["output_tokens"] == 30
        assert entries[1]["reasoning_tokens"] == 7

    def test_checkpoint_uses_prior_cumulative_total(self):
        """A checkpoint still needs earlier totals as the delta baseline."""
        lines = [
            _make_codex_token_count(100, 10, 20, 5),
            _make_codex_token_count(180, 30, 50, 12),
        ]
        f = _make_temp_jsonl(lines)

        entries, checkpoint = CodexUsageCollector().collect("s", "t", "w", "a", f, 1)

        assert checkpoint == 2
        assert len(entries) == 1
        assert entries[0]["input_tokens"] == 80
        assert entries[0]["cache_read_tokens"] == 20

    def test_uses_explicitly_configured_rates(self, monkeypatch):
        monkeypatch.setenv("CODEX_INPUT_COST_PER_MILLION", "2")
        monkeypatch.setenv("CODEX_CACHED_INPUT_COST_PER_MILLION", "0.5")
        monkeypatch.setenv("CODEX_OUTPUT_COST_PER_MILLION", "8")
        monkeypatch.setenv("CODEX_REASONING_COST_PER_MILLION", "8")
        f = _make_temp_jsonl([_make_codex_token_count(1_000_000, 0, 1_000_000, 0)])

        entries, _ = CodexUsageCollector().collect("s", "t", "w", "a", f, 0)

        assert entries[0]["cost_usd"] == 10.0
        assert entries[0]["raw_usage"]["cost_status"] == "estimated"

    def test_missing_file_preserves_checkpoint(self):
        entries, checkpoint = CodexUsageCollector().collect(
            "s", "t", "w", "a", Path("/nonexistent/file.jsonl"), 10
        )
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


class TestDiscoverCodexSessionFile:
    def test_uses_worktree_session_mapping(self, tmp_path, monkeypatch):
        """The stable Hephaestus ID resolves to Codex's transcript UUID."""
        heph_session_id = "hephaestus-project-design-development"
        codex_session_id = str(uuid.uuid4())
        working_directory = tmp_path / "worktree"
        session_map = working_directory / ".hephaestus" / "codex_sessions.json"
        session_map.parent.mkdir(parents=True)
        session_map.write_text(json.dumps({heph_session_id: codex_session_id}))

        transcript = tmp_path / ".codex" / "sessions" / "2026" / "08" / "11" / "rollout.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"session_id": codex_session_id, "cwd": str(working_directory)},
                }
            )
            + "\n"
        )
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        assert _discover_codex_session_file(heph_session_id, str(working_directory)) == transcript


# ── collect_task_cost (adversarial review B-1 / B-2) ────────────


@pytest.fixture
def cost_db_session():
    """In-memory SQLite session used in place of get_db()'s real DB."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = session_local()
    try:
        yield session
    finally:
        session.close()


def _make_task_agent_workflow(db, cli_type="pi", session_suffix="sess-abc123"):
    """Create the minimal Task/Agent/Workflow rows collect_task_cost needs."""
    from src.core.database import Phase
    workflow = Workflow(
        id="wf-1",
        name="test",
        phases_folder_path="config/workflows/test",
        working_directory="/tmp/test-cwd",
        launch_params={"project_path": "/tmp/test-project", "design_id": "des-test"},
    )
    phase = Phase(
        id="phase-1",
        workflow_id=workflow.id,
        order=1,
        name="development",
        description="test phase",
        done_definitions=["done"],
    )
    agent = Agent(
        id="agent-1",
        system_prompt="test",
        cli_type=cli_type,
        tmux_session_name=f"hephaestus-{session_suffix}",
        cli_model="test-model",
    )
    task = Task(
        id="task-1",
        raw_description="test",
        done_definition="test",
        workflow_id=workflow.id,
        assigned_agent_id=agent.id,
        phase_id=phase.id,
    )
    db.add_all([workflow, phase, agent, task])
    db.commit()
    return task, agent, workflow


class TestExtractSessionId:
    """_extract_session_id must reconstruct the EXACT id get_session_id
    produced at launch time (src/agents/launch_pipeline.py's
    _resolve_session_id), or cost collection silently stops finding any
    session file at all -- get_session_id is now scoped by workflow_id
    (see tests/test_get_session_id.py), so this reconstruction must
    include it too."""

    def test_matches_get_session_id_with_workflow_id(self, cost_db_session):
        from src.autopilot.phases import get_session_id

        task, agent, workflow = _make_task_agent_workflow(cost_db_session)

        with patch("src.core.database.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False
            extracted = _extract_session_id(agent, task)

        expected = get_session_id(
            "/tmp/test-project", "des-test", "development", model="test-model", workflow_id=workflow.id
        )
        assert extracted == expected

    def test_different_workflow_id_yields_different_session_id(self, cost_db_session):
        """Regression guard: without workflow_id in the reconstruction, two
        tasks from different workflow attempts of the same project+design+
        phase+model would wrongly reconstruct the SAME session id."""
        task_a, agent_a, _ = _make_task_agent_workflow(cost_db_session, session_suffix="a")

        cost_db_session.add(Workflow(
            id="wf-2",
            name="test-2",
            phases_folder_path="config/workflows/test",
            working_directory="/tmp/test-cwd",
            launch_params={"project_path": "/tmp/test-project", "design_id": "des-test"},
        ))
        cost_db_session.commit()
        task_a.workflow_id = "wf-1"  # unchanged, for clarity
        task_b = Task(
            id="task-2",
            raw_description="test",
            done_definition="test",
            workflow_id="wf-2",
            assigned_agent_id=agent_a.id,
            phase_id=task_a.phase_id,
        )
        cost_db_session.add(task_b)
        cost_db_session.commit()

        with patch("src.core.database.get_db") as mock_get_db:
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False
            id_a = _extract_session_id(agent_a, task_a)
            id_b = _extract_session_id(agent_a, task_b)

        assert id_a != id_b


class TestCollectTaskCostRealtimeVsFallback:
    """B-1: pi extension real-time POSTs and the JSONL fallback must not both run."""

    def test_skips_jsonl_fallback_when_realtime_pi_entries_exist(self, cost_db_session):
        """If source='pi' CostEntry rows already exist for this task (the
        extension posted them in real time), collect_task_cost must not
        also tail the JSONL transcript and re-record the same turns.
        """
        task, agent, _ = _make_task_agent_workflow(cost_db_session)

        # Simulate the extension having already POSTed one turn's cost
        # in real time via /api/autopilot/cost-entries.
        cost_db_session.add(
            CostEntry(
                id="cost-realtime1",
                task_id=task.id,
                agent_id=agent.id,
                workflow_id=task.workflow_id,
                source="pi",
                cost_usd=0.01,
                recorded_at=datetime.utcnow(),
            )
        )
        cost_db_session.commit()

        # The JSONL transcript for the same session contains that same
        # turn (plus another) -- this is what the fallback would tail if
        # it ran, reproducing B-1's double count.
        lines = [_make_assistant_message(0.01), _make_assistant_message(0.02)]
        session_file = _make_temp_jsonl(lines)

        with (
            patch("src.core.database.get_db") as mock_get_db,
            patch(
                "src.services.cost_collection_service._discover_session_file",
                return_value=session_file,
            ),
        ):
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False

            collect_task_cost(task.id)

        entries = cost_db_session.query(CostEntry).filter_by(task_id=task.id).all()
        assert len(entries) == 1, "JSONL fallback ran despite real-time pi entries already existing — turns were double-counted"
        assert entries[0].id == "cost-realtime1"

    def test_jsonl_fallback_still_runs_when_no_realtime_entries_exist(self, cost_db_session):
        """Sanity check: the fallback must still work normally (no
        regression) when the extension never posted anything for this task.
        """
        task, agent, _ = _make_task_agent_workflow(cost_db_session)

        lines = [_make_assistant_message(0.01), _make_assistant_message(0.02)]
        session_file = _make_temp_jsonl(lines)

        with (
            patch("src.core.database.get_db") as mock_get_db,
            patch(
                "src.services.cost_collection_service._discover_session_file",
                return_value=session_file,
            ),
        ):
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False

            collect_task_cost(task.id)

        entries = cost_db_session.query(CostEntry).filter_by(task_id=task.id).all()
        assert len(entries) == 2
        assert {e.source for e in entries} == {"pi"}

    def test_unrelated_agent_entry_does_not_suppress_fallback(self, cost_db_session):
        """A source='pi' CostEntry for this task_id posted under an agent_id
        that isn't the task's assigned agent must not be treated as proof
        the real-time extension reported in for this task's own session --
        otherwise a mismatched/forged entry would silently suppress this
        task's real cost data instead of just being ignored.
        """
        task, agent, _ = _make_task_agent_workflow(cost_db_session)

        cost_db_session.add(
            Agent(id="agent-other", system_prompt="test", cli_type="pi", tmux_session_name="hephaestus-other")
        )
        cost_db_session.add(
            CostEntry(
                id="cost-forged",
                task_id=task.id,
                agent_id="agent-other",
                workflow_id=task.workflow_id,
                source="pi",
                cost_usd=0.0,
                recorded_at=datetime.utcnow(),
            )
        )
        cost_db_session.commit()

        lines = [_make_assistant_message(0.01), _make_assistant_message(0.02)]
        session_file = _make_temp_jsonl(lines)

        with (
            patch("src.core.database.get_db") as mock_get_db,
            patch(
                "src.services.cost_collection_service._discover_session_file",
                return_value=session_file,
            ),
        ):
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False

            collect_task_cost(task.id)

        entries = cost_db_session.query(CostEntry).filter_by(task_id=task.id, agent_id=agent.id).all()
        assert len(entries) == 2, "fallback was suppressed by an unrelated agent's cost entry"

    def test_collects_codex_usage_from_mapped_transcript(self, cost_db_session):
        """Codex dispatch uses its transcript collector and persists tokens."""
        task, agent, _ = _make_task_agent_workflow(cost_db_session, cli_type="codex")
        session_file = _make_temp_jsonl(
            [
                json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}}),
                _make_codex_token_count(100, 10, 20, 5),
            ]
        )

        with (
            patch("src.core.database.get_db") as mock_get_db,
            patch("src.services.cost_collection_service._extract_session_id", return_value="heph-session"),
            patch("src.services.cost_collection_service._discover_codex_session_file", return_value=session_file),
        ):
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False

            collect_task_cost(task.id)

        entry = cost_db_session.query(CostEntry).filter_by(task_id=task.id).one()
        assert entry.source == "codex"
        assert entry.cost_usd == 0.0
        assert entry.input_tokens == 100
        assert entry.cache_read_tokens == 10


class TestCollectTaskCostPartialFailure:
    """B-2: one bad entry must not discard the rest of the batch, and (fixed
    regression) must not cause the OTHER, valid entry in the same batch to
    be permanently, silently lost either.

    The checkpoint used to advance past the whole batch unconditionally,
    regardless of failures -- since collection is checkpoint-driven and
    fires once per task-completion event, that permanently skipped the
    failed entry's transcript line on every future call. Now the checkpoint
    only advances once every entry in the batch is confirmed recorded, and
    each entry gets a deterministic id (stable across retries of the same
    checkpoint window) so retrying an already-recorded entry safely no-ops
    instead of double-counting it."""

    def _poison_second_entry(self, session_file):
        """Force the second collected entry to be invalid so record_cost()
        raises ValueError partway through the batch."""
        real_collect = PiJsonlCollector.collect

        def _poisoned_collect(self, *args, **kwargs):
            entries, checkpoint = real_collect(self, *args, **kwargs)
            assert len(entries) == 2
            entries[1]["cost_usd"] = -5.0
            return entries, checkpoint

        return patch.object(PiJsonlCollector, "collect", _poisoned_collect)

    def test_bad_entry_does_not_discard_the_valid_one(self, cost_db_session):
        task, agent, _ = _make_task_agent_workflow(cost_db_session)
        lines = [_make_assistant_message(0.01), _make_assistant_message(0.02)]
        session_file = _make_temp_jsonl(lines)

        with (
            patch("src.core.database.get_db") as mock_get_db,
            patch(
                "src.services.cost_collection_service._discover_session_file",
                return_value=session_file,
            ),
            self._poison_second_entry(session_file),
        ):
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False

            collect_task_cost(task.id)

        entries = cost_db_session.query(CostEntry).filter_by(task_id=task.id).all()
        assert len(entries) == 1, "the valid entry was discarded along with the bad one"
        assert entries[0].cost_usd == 0.01

    def test_checkpoint_not_advanced_past_a_failed_entry(self, cost_db_session):
        task, agent, _ = _make_task_agent_workflow(cost_db_session)
        lines = [_make_assistant_message(0.01), _make_assistant_message(0.02)]
        session_file = _make_temp_jsonl(lines)

        with (
            patch("src.core.database.get_db") as mock_get_db,
            patch(
                "src.services.cost_collection_service._discover_session_file",
                return_value=session_file,
            ),
            self._poison_second_entry(session_file),
        ):
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False

            collect_task_cost(task.id)

        checkpoint_row = cost_db_session.query(SessionCostCheckpoint).first()
        assert checkpoint_row is None, (
            "checkpoint must not advance past a failed entry -- doing so "
            "permanently skips that transcript line on every future "
            "collection, silently undercounting cost forever"
        )

    def test_retry_recovers_the_valid_entry_without_duplicating_it(self, cost_db_session):
        """A second collect_task_cost call for the same still-unadvanced
        checkpoint must re-attempt the whole batch: the already-recorded
        valid entry hits its own deterministic id (no duplicate row), the
        permanently-invalid entry fails again (still no checkpoint
        advance, still logged) -- this is a deliberate, informed tradeoff:
        a genuinely permanent failure now retries (and logs) indefinitely
        instead of ever silently succeeding, which is preferable to the
        old behavior of one log line followed by permanent silent data
        loss."""
        task, agent, _ = _make_task_agent_workflow(cost_db_session)
        lines = [_make_assistant_message(0.01), _make_assistant_message(0.02)]
        session_file = _make_temp_jsonl(lines)

        with (
            patch("src.core.database.get_db") as mock_get_db,
            patch(
                "src.services.cost_collection_service._discover_session_file",
                return_value=session_file,
            ),
            self._poison_second_entry(session_file),
        ):
            mock_get_db.return_value.__enter__ = lambda self: cost_db_session
            mock_get_db.return_value.__exit__ = lambda self, *a: False

            collect_task_cost(task.id)
            collect_task_cost(task.id)  # retry

        entries = cost_db_session.query(CostEntry).filter_by(task_id=task.id).all()
        assert len(entries) == 1, "retry duplicated the already-recorded valid entry"
        assert entries[0].cost_usd == 0.01
