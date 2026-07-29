"""Integration tests for the stability-tracked ("clean") transcript --
tmux's own capture-pane (cursor positioning, overwrites, and line
wrapping already correctly resolved) polled and diffed across
consecutive calls, instead of trying to reconstruct correct text from
raw pipe-pane pty bytes.

Regression context: the raw-byte approach could never be made fully
correct by stripping escape codes harder -- a cursor-positioning escape
that jumps back and overwrites part of a line can't be turned into
correct text by removing just the escape code, only by actually applying
the cursor movement it represents (i.e. real terminal emulation).
Confirmed live: plain ASCII letters vanished from the middle of
otherwise-intact words in a raw transcript (e.g. "filringw" for
"filtering was"), scattered irregularly through a long streamed string.
"""

import time
import uuid

import libtmux
import pytest

from src.agents.manager import AgentManager
from src.core.database import Agent, DatabaseManager, Task


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    return manager


@pytest.fixture
def agent_manager(db_manager):
    from unittest.mock import Mock

    return AgentManager(db_manager, Mock())


@pytest.fixture
def tmux_session(request):
    server = libtmux.Server()
    session_name = f"test_stable_{uuid.uuid4().hex[:8]}"
    if server.has_session(session_name):
        server.kill_session(session_name)
    session = server.new_session(session_name=session_name, window_name="test", attach=False)

    def _cleanup():
        try:
            if server.has_session(session_name):
                server.kill_session(session_name)
        except Exception:
            pass

    request.addfinalizer(_cleanup)
    return session_name, session


class TestPollStableTranscript:
    def test_bootstrap_commits_all_but_last_two_lines(self, agent_manager, tmux_session, tmp_path):
        session_name, session = tmux_session
        pane = session.attached_window.attached_pane
        for i in range(6):
            pane.send_keys(f"echo line-{i}", enter=True)
        time.sleep(0.5)

        clean_path = tmp_path / f"{session_name}.clean.log"
        agent_manager._poll_stable_transcript(session_name, clean_path)

        assert clean_path.exists()
        content = clean_path.read_text()
        assert "line-0" in content
        # The bootstrap withholds the last 2 lines pending confirmation --
        # with 6 echoed commands (each producing a prompt+command line and
        # an output line), the very last couple of lines shouldn't be
        # committed yet.
        state = agent_manager._pane_stability_cache[session_name]
        assert state["committed"] == len(state["lines"]) - 2

    def test_second_identical_poll_confirms_previously_held_back_lines(
        self, agent_manager, tmux_session, tmp_path
    ):
        session_name, session = tmux_session
        pane = session.attached_window.attached_pane
        pane.send_keys("echo final-marker-line", enter=True)
        time.sleep(0.5)

        clean_path = tmp_path / f"{session_name}.clean.log"
        agent_manager._poll_stable_transcript(session_name, clean_path)
        first_committed = agent_manager._pane_stability_cache[session_name]["committed"]

        # Nothing changed in the pane -- a second poll should see the
        # same content at the same positions and confirm (commit) the
        # previously-withheld tail.
        agent_manager._poll_stable_transcript(session_name, clean_path)
        second_committed = agent_manager._pane_stability_cache[session_name]["committed"]

        assert second_committed > first_committed
        assert "final-marker-line" in clean_path.read_text()

    def test_still_changing_line_is_withheld_until_it_settles(
        self, agent_manager, tmux_session, tmp_path
    ):
        session_name, session = tmux_session
        pane = session.attached_window.attached_pane
        pane.send_keys("echo before-spinner", enter=True)
        time.sleep(0.3)

        clean_path = tmp_path / f"{session_name}.clean.log"
        agent_manager._poll_stable_transcript(session_name, clean_path)

        # Simulate an in-place redraw (spinner-style \r update) on a NEW
        # line after the bootstrap -- send several distinct ticks with no
        # trailing newline between polls.
        pane.send_keys("printf 'tick-1'", enter=True)
        time.sleep(0.3)
        agent_manager._poll_stable_transcript(session_name, clean_path)
        assert "tick-1" not in clean_path.read_text()

        pane.send_keys("", enter=True)  # move past the printf, no trailing \n issue
        time.sleep(0.3)
        agent_manager._poll_stable_transcript(session_name, clean_path)
        # Two consecutive polls with the settled prompt line now agreeing
        # -- tick-1 (now a stable, no-longer-changing line) should commit.
        agent_manager._poll_stable_transcript(session_name, clean_path)
        assert "tick-1" in clean_path.read_text()


class TestFlushStableTranscript:
    def test_flush_commits_everything_unconditionally(self, agent_manager, tmux_session, tmp_path):
        session_name, session = tmux_session
        pane = session.attached_window.attached_pane
        for i in range(4):
            pane.send_keys(f"echo flush-line-{i}", enter=True)
        time.sleep(0.5)

        clean_path = tmp_path / f"{session_name}.clean.log"
        agent_manager._poll_stable_transcript(session_name, clean_path)
        # Bootstrap withholds the last 2 lines pending confirmation --
        # committed must be strictly less than the full captured pane.
        state = agent_manager._pane_stability_cache[session_name]
        assert state["committed"] < len(state["lines"])

        agent_manager._flush_stable_transcript(session_name, clean_path)
        content = clean_path.read_text()
        assert "flush-line-0" in content
        assert "flush-line-3" in content
        # Everything the bootstrap withheld (plus anything captured fresh
        # by flush's own final capture-pane call) must now be committed.
        assert content.count("\n") >= len(state["lines"])
        assert session_name not in getattr(agent_manager, "_pane_stability_cache", {})


class TestGetAgentOutputUsesCleanTranscript:
    def test_live_agent_output_comes_from_clean_transcript(
        self, agent_manager, db_manager, tmux_session, tmp_path
    ):
        session_name, session = tmux_session
        pane = session.attached_window.attached_pane
        pane.send_keys("echo hello-from-clean-transcript", enter=True)
        pane.send_keys("echo goodbye-from-clean-transcript", enter=True)
        time.sleep(0.5)

        from src.core.database import Workflow

        task_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        workflow_id = str(uuid.uuid4())
        session_db = db_manager.get_session()
        session_db.add(
            Workflow(
                id=workflow_id, name="t", phases_folder_path="/tmp",
                status="active", definition_id="autopilot",
                working_directory=str(tmp_path),
            )
        )
        session_db.add(
            Task(
                id=task_id, workflow_id=workflow_id, raw_description="r",
                done_definition="d", status="in_progress",
            )
        )
        session_db.add(
            Agent(
                id=agent_id, system_prompt="p", status="working", cli_type="claude",
                tmux_session_name=session_name, current_task_id=task_id,
            )
        )
        session_db.commit()
        session_db.close()

        output = agent_manager.get_agent_output(agent_id, lines=100)
        assert "hello-from-clean-transcript" in output
        assert (tmp_path / ".hephaestus" / "tmux" / f"{session_name}.clean.log").exists()
