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
        state = agent_manager._output_capture._pane_stability_cache[session_name]
        assert state["committed"] == len(state["history"][-1]) - 2

    def test_third_identical_poll_confirms_previously_held_back_lines(
        self, agent_manager, tmux_session, tmp_path
    ):
        """3-poll confirmation (see _STABILITY_CONFIRMATIONS): a line must
        be unchanged across 3 consecutive polls before it commits, not 2
        -- so the withheld tail from the bootstrap poll shouldn't confirm
        until the THIRD poll here (2 more agreeing polls after bootstrap)."""
        session_name, session = tmux_session
        pane = session.attached_window.attached_pane
        pane.send_keys("echo final-marker-line", enter=True)
        time.sleep(0.5)

        clean_path = tmp_path / f"{session_name}.clean.log"
        agent_manager._poll_stable_transcript(session_name, clean_path)
        first_committed = agent_manager._output_capture._pane_stability_cache[session_name]["committed"]

        # Nothing changed in the pane -- one more agreeing poll still isn't
        # enough (only 2 of the required 3 consecutive polls so far). Not
        # asserting "final-marker-line not in ..." here -- the echoed
        # COMMAND itself ("echo final-marker-line") already committed at
        # bootstrap and contains that same substring; committed-count
        # equality is the real signal that nothing new confirmed yet.
        agent_manager._poll_stable_transcript(session_name, clean_path)
        second_committed = agent_manager._output_capture._pane_stability_cache[session_name]["committed"]
        assert second_committed == first_committed

        # Third agreeing poll -- now confirmed and committed.
        agent_manager._poll_stable_transcript(session_name, clean_path)
        third_committed = agent_manager._output_capture._pane_stability_cache[session_name]["committed"]

        assert third_committed > first_committed
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


class TestStabilityConfirmationRace:
    """Regression (live incident): 2-poll confirmation caught a real race
    -- pi's TUI briefly reserves a blank placeholder line for a command's
    output before streaming the real content in, and if that blank state
    happened to persist across exactly 2 polls (bad timing, not rare over
    a multi-hour session), the blank got permanently committed and the
    real content that filled in a moment later was silently skipped
    forever (append-only positions are never revisited). Observed live:
    "All checks passed!" / "EXIT: 0" from a ruff run replaced by two
    blank lines in the clean transcript, confirmed present in the raw
    capture-pane output the whole time. Uses a mocked _capture_pane_lines
    to deterministically reproduce the exact timing that a real tmux
    session can only hit by chance."""

    def test_blank_placeholder_matching_twice_is_not_committed_as_final(
        self, agent_manager, tmp_path, monkeypatch
    ):
        session_name = "sess-race"
        clean_path = tmp_path / f"{session_name}.clean.log"

        bootstrap = ["prompt-1", "$ ruff check .", "blank-placeholder"]
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: bootstrap
        )
        agent_manager._poll_stable_transcript(session_name, clean_path)

        # Two consecutive polls where the placeholder line hasn't been
        # replaced by real output yet -- exactly what would have
        # confirmed (wrongly) under the old 2-poll algorithm.
        still_blank = ["prompt-1", "$ ruff check .", "blank-placeholder"]
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: still_blank
        )
        agent_manager._poll_stable_transcript(session_name, clean_path)
        assert "blank-placeholder" not in clean_path.read_text()

        # The real content streams in on the next poll, before a 3rd
        # confirming poll of the blank ever happened -- proving the
        # placeholder was never locked in.
        real_content = ["prompt-1", "$ ruff check .", "All checks passed!"]
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: real_content
        )
        agent_manager._poll_stable_transcript(session_name, clean_path)
        assert "blank-placeholder" not in clean_path.read_text()
        assert "All checks passed!" not in clean_path.read_text()  # not yet 3x stable

        # Two more agreeing polls -- now genuinely stable, commits the
        # real content, never the placeholder.
        agent_manager._poll_stable_transcript(session_name, clean_path)
        agent_manager._poll_stable_transcript(session_name, clean_path)
        content = clean_path.read_text()
        assert "All checks passed!" in content
        assert "blank-placeholder" not in content


class TestPollStableTranscriptDiscontinuity:
    """Regression: once a long-running session's total scrollback exceeds
    tmux's history-limit, capture-pane's window start shifts (the oldest
    lines fall off the top) -- detected here as the first line differing
    between polls. The original discontinuity handling treated any such
    shift as total loss and re-appended the ENTIRE current window,
    duplicating everything already committed in earlier polls every time
    a long session crossed this boundary. Uses a mocked _capture_pane_lines
    (not a real tmux session) since manufacturing 1000+ real scrollback
    lines to trigger this organically would be slow and flaky -- the fix
    operates purely on the returned line lists, so this is a faithful,
    fast substitute."""

    def test_partial_scroll_reanchors_without_reappending_committed_content(
        self, agent_manager, tmp_path, monkeypatch
    ):
        session_name = "sess-scroll"
        clean_path = tmp_path / f"{session_name}.clean.log"

        first_window = [f"line-{i}" for i in range(10)]
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: first_window
        )
        agent_manager._poll_stable_transcript(session_name, clean_path)
        committed_before = agent_manager._output_capture._pane_stability_cache[session_name]["committed"]
        assert committed_before > 0
        content_before = clean_path.read_text()

        # Simulate the oldest 3 lines scrolling off the top (history-limit
        # exceeded) -- the window shifts, so position 0 no longer matches,
        # but line-9 (the last line of the previous window) is still
        # present, just at a different index.
        scrolled_window = [f"line-{i}" for i in range(3, 10)] + ["line-10", "line-11"]
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: scrolled_window
        )
        agent_manager._poll_stable_transcript(session_name, clean_path)

        content_after = clean_path.read_text()
        # Nothing already committed before the scroll should be written
        # again -- the file must only ever grow by content, not duplicate.
        assert content_after == content_before
        for already_committed_line in first_window[:committed_before]:
            assert content_after.count(already_committed_line) == 1

    def test_scroll_past_last_committed_line_falls_back_to_full_reset(
        self, agent_manager, tmp_path, monkeypatch
    ):
        """If the anchor itself has scrolled out of the window entirely
        (more output arrived in one interval than history-limit allows),
        there's nothing to re-anchor on -- the original reset-and-dump-
        everything behavior is the correct, if rare, fallback."""
        session_name = "sess-scroll-2"
        clean_path = tmp_path / f"{session_name}.clean.log"

        first_window = [f"line-{i}" for i in range(5)]
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: first_window
        )
        agent_manager._poll_stable_transcript(session_name, clean_path)

        # Entirely disjoint window -- none of the previous lines appear.
        disjoint_window = [f"unrelated-{i}" for i in range(5)]
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: disjoint_window
        )
        agent_manager._poll_stable_transcript(session_name, clean_path)

        content = clean_path.read_text()
        assert "unrelated-0" in content
        state = agent_manager._output_capture._pane_stability_cache[session_name]
        assert state["committed"] == len(disjoint_window)


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
        state = agent_manager._output_capture._pane_stability_cache[session_name]
        assert state["committed"] < len(state["history"][-1])

        agent_manager._flush_stable_transcript(session_name, clean_path)
        content = clean_path.read_text()
        assert "flush-line-0" in content
        assert "flush-line-3" in content
        # Everything the bootstrap withheld (plus anything captured fresh
        # by flush's own final capture-pane call) must now be committed.
        assert content.count("\n") >= len(state["history"][-1])
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

    def test_empty_clean_transcript_falls_back_to_live_capture_pane_not_raw(
        self, agent_manager, db_manager, tmp_path, monkeypatch
    ):
        """Regression (live incident): when clean.log hasn't committed
        anything yet -- a fresh session, or mid-stream on a long response
        that hasn't settled anywhere -- get_agent_output used to fall
        through to the RAW pipe-pane transcript, which re-shows every
        intermediate \\r-redrawn state of a streaming line as its own
        separate line (the exact duplication problem the clean transcript
        exists to avoid). It must prefer a live, unprocessed
        capture-pane snapshot (tmux's own correct rendering, just not yet
        "confirmed stable") over the raw transcript instead."""
        from src.core.database import Workflow

        session_name = "sess-empty-clean"
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

        # _poll_stable_transcript withholds everything (clean.log stays
        # empty/nonexistent) -- simulates a fresh session or an
        # unsettled, still-streaming response.
        monkeypatch.setattr(agent_manager._output_capture, "_poll_stable_transcript", lambda *a, **k: None)
        monkeypatch.setattr(
            agent_manager._output_capture, "_capture_pane_lines", lambda _sn: ["live-correctly-rendered-line"]
        )
        monkeypatch.setattr(
            agent_manager._output_capture, "_read_transcript_log", lambda *a, **k: "raw-duplicated-streaming-line\nraw-duplicated-streaming-line-longer"
        )

        output = agent_manager.get_agent_output(agent_id, lines=100)

        assert "live-correctly-rendered-line" in output
        assert "raw-duplicated-streaming-line" not in output

    def test_terminated_agent_prefers_clean_transcript_over_garbled_raw(
        self, agent_manager, db_manager, tmp_path, monkeypatch
    ):
        """Regression: get_agent_output used to try the raw pipe-pane
        transcript FIRST for terminated agents, only falling back to the
        clean transcript's own data (via AgentLog.final_output) if that
        came up empty -- even though terminate_agent() already does a
        final unconditional flush of clean.log right before killing the
        session, specifically so it stays authoritative after death. A
        terminated agent with a perfectly good clean.log therefore still
        returned garbled text reconstructed from the raw transcript by
        regex alone (e.g. cursor-painted text collapsing together with no
        spaces). The clean transcript must win."""
        from src.core.database import Workflow

        session_name = "sess-terminated-clean"
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
            Agent(
                id=agent_id, system_prompt="p", status="terminated", cli_type="claude",
                tmux_session_name=session_name, current_task_id=None,
            )
        )
        session_db.add(
            Task(
                id=task_id, workflow_id=workflow_id, raw_description="r",
                done_definition="d", status="done", assigned_agent_id=agent_id,
            )
        )
        session_db.commit()
        session_db.close()

        tmux_dir = tmp_path / ".hephaestus" / "tmux"
        tmux_dir.mkdir(parents=True)
        (tmux_dir / f"{session_name}.clean.log").write_text("clean-final-line\n")
        (tmux_dir / f"{session_name}.transcript.log").write_text(
            "garbledrawpipepanebytes\n"
        )

        # Session is gone (agent terminated) -- capture-pane can't see it.
        monkeypatch.setattr(agent_manager._output_capture, "_capture_pane_lines", lambda _sn: None)

        output = agent_manager.get_agent_output(agent_id, lines=100)

        assert "clean-final-line" in output
        assert "garbledrawpipepanebytes" not in output


class TestTmuxHistoryLimit:
    """Regression: history-limit was set to a bare 1000 on the assumption
    that the raw pipe-pane transcript "already captures the full
    transcript", making a larger value "no benefit" -- but the viewer's
    clean transcript is built entirely from tmux's own capture-pane
    (_poll_stable_transcript), which is itself bounded by history-limit,
    not from the raw pipe-pane file. A poll interval whose agent produces
    more than history-limit lines of new output forces
    _poll_stable_transcript's lossy full-reset fallback, permanently
    dropping everything before it. Confirmed live: an architecture_design
    session's clean transcript started mid-paragraph, with nothing before
    that point recoverable. Verifies the real tmux option, not just the
    call arguments, since libtmux's own set_option/show_option round trip
    is part of what needs to be correct here."""

    def test_new_session_gets_a_generous_history_limit(self, agent_manager, request):

        session_name = f"test_history_limit_{uuid.uuid4().hex[:8]}"
        server = agent_manager._launch.tmux_server

        def _cleanup():
            try:
                if server.has_session(session_name):
                    server.kill_session(session_name)
            except Exception:
                pass

        request.addfinalizer(_cleanup)

        agent_manager._launch._create_tmux_session(session_name)

        session = server.sessions.get(session_name=session_name)
        limit = int(session.show_option("history-limit"))
        assert limit >= 50000, (
            f"history-limit={limit} is too small -- a burst of agent "
            "output between two polls can exceed it, forcing a lossy "
            "transcript reset (see _poll_stable_transcript)"
        )
