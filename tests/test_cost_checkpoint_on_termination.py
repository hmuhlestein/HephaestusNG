"""Tests for cost checkpointing on agent termination.

External eval finding (§3.4): cost_entries stayed empty across every run
that didn't reach a normal task completion -- collect_cost_on_completion
only fires when a task reaches 'done'/'failed', so an agent that is
killed/times out/gets restarted mid-task reports zero cost even after
burning real tokens. Fixed by checkpointing cost from
engine_client.terminate_agent's _do_terminate -- the single shared
primitive every termination path (orphan reaper, mechanical recovery,
auto-restart, manual API kill, Terminator's own tmux teardown) is
guaranteed to pass through (see test_termination_invariant_single_writer.py).

These tests exercise the real collect_task_cost extraction/checkpoint
logic through a real (file-based, tmp_path) DB -- not mocks -- so a
regression in the wiring (wrong task_id, wrong ordering relative to
current_task_id being cleared, double-counting) shows up as a real
assertion failure against the cost_entries table.
"""

import asyncio
import json
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.database import Agent, CostEntry, DatabaseManager, Phase, Task, Workflow


@pytest.fixture
def term_db_env(tmp_path, monkeypatch):
    """Real file-based sqlite DB -- terminate_agent's standalone path and
    collect_task_cost each open their own get_db() session, so a shared
    in-memory (StaticPool) DB isn't representative; a tmp_path file is
    (same convention as TestTerminateAgentInvariant's orch_db_env)."""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_assistant_message(cost_total: float, tokens: int = 5000) -> str:
    """A pi session JSONL line with real usage, matching PiJsonlCollector's
    expected shape (message.usage.cost.total, message.usage.input/output)."""
    return json.dumps(
        {
            "type": "message",
            "id": uuid.uuid4().hex[:8],
            "message": {
                "role": "assistant",
                "model": "anthropic/claude-sonnet-4",
                "usage": {
                    "input": tokens,
                    "output": 200,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "reasoning": 0,
                    "cost": {"total": cost_total},
                },
            },
        }
    )


def _make_jsonl(tmp_path: Path, lines: list[str]) -> Path:
    f = tmp_path / f"{uuid.uuid4().hex[:8]}.jsonl"
    f.write_text("\n".join(lines) + "\n")
    return f


def _seed(db: DatabaseManager, *, task_status="in_progress", cli_type="pi"):
    """Seed a Workflow/Phase/Agent/Task tuple wired the way collect_task_cost
    (via _extract_session_id) needs to reconstruct a session id: workflow
    launch_params carrying project_id/design_slug, and a Phase whose name
    completes the (project, design, phase, model) tuple."""
    with db.session_scope() as session:
        session.add(
            Workflow(
                id="wf-1",
                name="w",
                phases_folder_path="config/workflows/test",
                working_directory="/tmp/term-cost-test",
                status="active",
                launch_params={"project_id": "/tmp/term-cost-test", "design_slug": "des-test"},
            )
        )
        session.add(
            Phase(
                id="phase-1",
                workflow_id="wf-1",
                order=1,
                name="development",
                description="test phase",
                done_definitions=["done"],
            )
        )
        session.add(
            Agent(
                id="agent-1",
                status="working",
                cli_type=cli_type,
                system_prompt="x",
                tmux_session_name="hephaestus-term-cost-test",
                cli_model="test-model",
                current_task_id="task-1",
            )
        )
        session.add(
            Task(
                id="task-1",
                workflow_id="wf-1",
                phase_id="phase-1",
                raw_description="x",
                done_definition="x",
                status=task_status,
                assigned_agent_id="agent-1",
            )
        )


class TestKilledAgentGetsCostRecorded:
    """An agent killed/terminated mid-task (never reaches 'done') must
    still get a cost_entries row for the tokens it actually consumed."""

    def test_terminate_agent_records_cost_for_never_completed_task(self, term_db_env, tmp_path):
        from src.autopilot.orchestrator.engine_client import terminate_agent

        _seed(term_db_env)
        session_file = _make_jsonl(
            tmp_path, [_make_assistant_message(0.05), _make_assistant_message(0.03)]
        )

        with patch(
            "src.services.cost_collection_service._discover_session_file",
            return_value=session_file,
        ):
            result = terminate_agent("agent-1")

        assert result is True

        with term_db_env.session_scope() as session:
            entries = session.query(CostEntry).filter_by(task_id="task-1").all()
            agent = session.query(Agent).filter_by(id="agent-1").first()

        assert len(entries) == 2, "killed agent's real usage was not recorded"
        assert sum(e.cost_usd for e in entries) == pytest.approx(0.08)
        # The termination invariant must still hold -- cost collection is a
        # side effect, not a substitute for it.
        assert agent.status == "terminated"
        assert agent.current_task_id is None
        assert agent.terminated_at is not None

    def test_checkpoint_runs_before_current_task_id_is_cleared(self, term_db_env, tmp_path):
        """collect_task_cost must see the pre-termination task/agent
        linkage -- if the checkpoint ran after current_task_id was cleared,
        collect_task_cost would have nothing to attribute the cost to."""
        from src.autopilot.orchestrator.engine_client import terminate_agent

        _seed(term_db_env)
        session_file = _make_jsonl(tmp_path, [_make_assistant_message(0.10)])

        with patch(
            "src.services.cost_collection_service._discover_session_file",
            return_value=session_file,
        ):
            terminate_agent("agent-1")

        with term_db_env.session_scope() as session:
            entries = session.query(CostEntry).filter_by(task_id="task-1", agent_id="agent-1").all()
        assert len(entries) == 1
        assert entries[0].cost_usd == pytest.approx(0.10)


class TestNoDoubleCounting:
    """A task that completes normally, then has its agent terminated
    immediately after, must be checkpointed once -- not twice."""

    def test_completion_then_termination_does_not_double_count(self, term_db_env, tmp_path):
        from src.autopilot.orchestrator.engine_client import terminate_agent
        from src.services.task_completion.cost import collect_cost_on_completion

        _seed(term_db_env, task_status="done")
        session_file = _make_jsonl(
            tmp_path, [_make_assistant_message(0.05), _make_assistant_message(0.03)]
        )

        with patch(
            "src.services.cost_collection_service._discover_session_file",
            return_value=session_file,
        ):
            # 1. Normal completion path -- what _complete_task_normally does
            # on every "done"/"failed" transition.
            collect_cost_on_completion("task-1")

            with term_db_env.session_scope() as session:
                entries_after_completion = (
                    session.query(CostEntry).filter_by(task_id="task-1").all()
                )
            assert len(entries_after_completion) == 2
            total_after_completion = sum(e.cost_usd for e in entries_after_completion)
            assert total_after_completion == pytest.approx(0.08)

            # 2. Termination follows moments later (background_loops.
            # terminate_agents_and_process_queue calls this right after a
            # task completes) -- current_task_id is still "task-1" at this
            # point, nothing has cleared it yet.
            terminate_agent("agent-1")

        with term_db_env.session_scope() as session:
            entries_after_termination = (
                session.query(CostEntry).filter_by(task_id="task-1").all()
            )
        assert len(entries_after_termination) == 2, (
            "termination re-recorded the same usage the completion path "
            "already collected -- checkpoint was not respected"
        )
        assert sum(e.cost_usd for e in entries_after_termination) == pytest.approx(0.08)

    def test_two_terminate_calls_for_the_same_agent_do_not_double_count(self, term_db_env, tmp_path):
        """Belt-and-suspenders: even calling collect_cost_on_termination
        twice back-to-back for the same task must not double the total."""
        from src.services.task_completion.cost import collect_cost_on_termination

        _seed(term_db_env)
        session_file = _make_jsonl(tmp_path, [_make_assistant_message(0.05)])

        with patch(
            "src.services.cost_collection_service._discover_session_file",
            return_value=session_file,
        ):
            collect_cost_on_termination("task-1", "agent-1")
            collect_cost_on_termination("task-1", "agent-1")

        with term_db_env.session_scope() as session:
            entries = session.query(CostEntry).filter_by(task_id="task-1").all()
        assert len(entries) == 1
        assert entries[0].cost_usd == pytest.approx(0.05)


class TestFreshAgentNoWork:
    """An agent terminated before doing any real work must record zero
    cost, not raise."""

    def test_agent_never_assigned_a_task_terminates_cleanly(self, term_db_env):
        from src.autopilot.orchestrator.engine_client import terminate_agent

        with term_db_env.session_scope() as session:
            session.add(
                Agent(
                    id="agent-fresh", status="starting", cli_type="pi",
                    system_prompt="x", current_task_id=None,
                )
            )

        result = terminate_agent("agent-fresh")
        assert result is True

        with term_db_env.session_scope() as session:
            entries = session.query(CostEntry).filter_by(agent_id="agent-fresh").all()
            agent = session.query(Agent).filter_by(id="agent-fresh").first()
        assert entries == []
        assert agent.status == "terminated"
        assert agent.terminated_at is not None

    def test_agent_with_task_but_no_session_file_records_zero_cost(self, term_db_env):
        """Task/agent exist, but nothing was ever written to a transcript
        (e.g. killed before the CLI produced its first turn) -- no session
        file is discoverable. Must no-op, not error."""
        from src.autopilot.orchestrator.engine_client import terminate_agent

        _seed(term_db_env)

        with patch(
            "src.services.cost_collection_service._discover_session_file",
            return_value=None,
        ):
            result = terminate_agent("agent-1")

        assert result is True
        with term_db_env.session_scope() as session:
            entries = session.query(CostEntry).filter_by(task_id="task-1").all()
        assert entries == []


class TestEventLoopSafety:
    """Several call sites (feature_routes.py, queue_routes.py,
    design_file_routes.py, lifecycle.py, orphan_reaper.py) call
    terminate_agent() directly and synchronously from inside a running
    asyncio event loop, with no thread-pool offload. The cost checkpoint
    must not block that loop -- it dispatches onto run_in_executor instead
    of reading the transcript file inline when a loop is running."""

    @pytest.mark.asyncio
    async def test_checkpoint_dispatches_to_executor_when_loop_is_running(self, term_db_env, tmp_path):
        from src.autopilot.orchestrator.engine_client import terminate_agent

        _seed(term_db_env)
        session_file = _make_jsonl(tmp_path, [_make_assistant_message(0.05)])

        with patch(
            "src.services.cost_collection_service._discover_session_file",
            return_value=session_file,
        ):
            # Called directly from this coroutine (a running loop exists),
            # exactly like the async route handlers above -- must return
            # immediately rather than blocking on the file read.
            result = terminate_agent("agent-1")
            assert result is True

            # The checkpoint runs on a background thread; poll briefly for
            # it to land instead of asserting immediately (which would
            # just assert the dispatch itself, not the fire-and-forget
            # behavior). Must stay INSIDE this patch's `with` block -- the
            # background thread doesn't necessarily run before the patch
            # is torn down otherwise, and would then call the real
            # (unpatched) _discover_session_file, correctly finding no
            # session and recording nothing.
            for _ in range(50):
                with term_db_env.session_scope() as session:
                    entries = session.query(CostEntry).filter_by(task_id="task-1").all()
                if entries:
                    break
                await asyncio.sleep(0.05)

        assert len(entries) == 1
        assert entries[0].cost_usd == pytest.approx(0.05)

    def test_checkpoint_runs_inline_with_no_running_loop(self, term_db_env, tmp_path):
        """The common case (Terminator, AutoRestart, mechanical_recovery
        all call terminate_agent from inside a run_in_executor worker
        thread, which has no running loop) must collect synchronously --
        confirmed by the entry already existing the instant the call
        returns, with no polling needed."""
        from src.autopilot.orchestrator.engine_client import terminate_agent

        _seed(term_db_env)
        session_file = _make_jsonl(tmp_path, [_make_assistant_message(0.05)])

        with patch(
            "src.services.cost_collection_service._discover_session_file",
            return_value=session_file,
        ):
            terminate_agent("agent-1")

        with term_db_env.session_scope() as session:
            entries = session.query(CostEntry).filter_by(task_id="task-1").all()
        assert len(entries) == 1
