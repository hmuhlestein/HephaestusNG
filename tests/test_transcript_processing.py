"""Tests for transcript log ANSI processing and \\r collapsing."""

from unittest.mock import MagicMock, patch



def _make_agent(tmux_session_name="test_agent", status="working", current_task_id=None):
    """Create a mock agent object."""
    agent = MagicMock()
    agent.id = "test-agent-id"
    agent.tmux_session_name = tmux_session_name
    agent.status = status
    agent.current_task_id = current_task_id
    agent.cli_type = "pi"
    # _resolve_tmux_transcript_dir checks agent.working_directory before
    # falling back to task.workflow.working_directory (see its own
    # docstring) -- unset, a bare MagicMock() is truthy, so it wins over
    # this mock's real task/workflow chain and resolves to a bogus path.
    agent.working_directory = None
    return agent


def _write_transcript(tmp_path, session_name, content):
    """Write a transcript log file and return the path."""
    tmux_dir = tmp_path / ".hephaestus" / "tmux"
    tmux_dir.mkdir(parents=True)
    transcript = tmux_dir / f"{session_name}.transcript.log"
    transcript.write_bytes(content.encode("utf-8", errors="replace"))
    return transcript


class TestAnsiStripping:
    """Test that ANSI escape sequences are properly stripped or preserved."""

    def _process(self, content):
        """Run the transcript through the processing logic and return result."""
        import re

        text = content

        # Same logic as _read_transcript_log
        text = re.sub(r'\x1b\][^\x07]*\x07', '', text)  # OSC with BEL
        text = re.sub(r'\x1b\][^\x1b]*\x1b\\', '', text)  # OSC with ST (single backslash)
        text = re.sub(r'\x1b\[[?]?[0-9;]*[^0-9;m]', '', text)  # All CSI/DEC except m
        text = re.sub(r'\x1b[()][A-Za-z0-9]', '', text)  # Charset selection
        text = re.sub(r'\x1b[^\x1b\x5b\x5d]', '', text)  # Any other bare ESC

        # Collapse \r
        collapsed = []
        for line in text.split("\n"):
            if "\r" in line:
                line = line.rsplit("\r", 1)[-1]
            collapsed.append(line.rstrip())
        text = "\n".join(collapsed)

        return text

    def test_preserves_sgr_color_codes(self):
        """SGR color codes (\x1b[...m) should be preserved."""
        input_text = "\x1b[32mGreen text\x1b[0m and \x1b[38;2;138;190;183mRGB color\x1b[39m"
        result = self._process(input_text)
        assert "\x1b[32m" in result
        assert "\x1b[0m" in result
        assert "\x1b[38;2;138;190;183m" in result
        assert "\x1b[39m" in result
        assert "Green text" in result
        assert "RGB color" in result

    def test_strips_osc_hyperlinks(self):
        """OSC 8 hyperlinks (\x1b]8;;url\x07) should be stripped."""
        input_text = "before\x1b]8;;https://example.com\x07link text\x1b]8;;\x07after"
        result = self._process(input_text)
        assert "before" in result
        assert "link text" in result
        assert "after" in result
        assert "\x1b]8;" not in result

    def test_strips_osc_shell_integration(self):
        """OSC 133 shell integration should be stripped."""
        input_text = "\x1b]133;B\x07command output\x1b]133;C\x07"
        result = self._process(input_text)
        assert "command output" in result
        assert "\x1b]133;" not in result

    def test_strips_dec_private_modes(self):
        """DEC private modes (\x1b[?...h/l) should be stripped."""
        input_text = "\x1b[?2026h\x1b[?25ltext\x1b[?25h\x1b[?2026l"
        result = self._process(input_text)
        assert result.strip() == "text"

    def test_strips_cursor_movement(self):
        """CSI cursor movement sequences should be stripped."""
        input_text = "\x1b[5A\x1b[2B\x1b[1Gtext\x1b[3C"
        result = self._process(input_text)
        assert result.strip() == "text"

    def test_strips_erase_line(self):
        """CSI erase sequences should be stripped."""
        input_text = "\x1b[2Ktext\x1b[K"
        result = self._process(input_text)
        assert result.strip() == "text"

    def test_strips_charset_selection(self):
        """Charset selection sequences should be stripped."""
        input_text = "\x1b(Btext\x1b)0"
        result = self._process(input_text)
        assert result.strip() == "text"

    def test_mixed_ansi_and_text(self):
        """Complex mix of ANSI sequences and text should clean properly."""
        input_text = (
            "\x1b[2K\x1b[0m\x1b]8;;\x07"
            "\x1b[38;2;138;190;183m⠹\x1b[39m "
            "\x1b[38;2;128;128;128mWorking...\x1b[39m"
            "\x1b[0m\x1b]8;;\x07"
            "\x1b[?2026l\x1b[3B\x1b[1G\x1b[?25l\x1b[?2026h"
        )
        result = self._process(input_text)
        # Should keep colored text
        assert "⠹" in result
        assert "Working..." in result
        # Should strip control sequences
        assert "\x1b[2K" not in result
        assert "\x1b[?2026" not in result
        assert "\x1b[?25" not in result
        assert "\x1b]8;" not in result


class TestCarriageReturnCollapsing:
    """Test that \\r sequences are properly collapsed."""

    def _collapse(self, content):
        """Run the \\r collapsing logic."""

        text = content
        collapsed = []
        for line in text.split("\n"):
            if "\r" in line:
                line = line.rsplit("\r", 1)[-1]
            collapsed.append(line.rstrip())
        return "\n".join(collapsed)

    def test_single_line_spinner(self):
        """Spinner redraws on a single line should collapse to final state."""
        input_text = "⠋ Working...\r⠙ Working...\r⠹ Working...\r⠸ Done"
        result = self._collapse(input_text)
        assert result == "⠸ Done"

    def test_multiple_lines_with_spinners(self):
        """Multiple lines with spinners should each collapse independently."""
        input_text = "line1\rupdated1\nline2\rupdated2"
        result = self._collapse(input_text)
        assert result == "updated1\nupdated2"

    def test_no_carriage_returns(self):
        """Lines without \\r should pass through unchanged."""
        input_text = "line1\nline2\nline3"
        result = self._collapse(input_text)
        assert result == "line1\nline2\nline3"

    def test_empty_lines_preserved(self):
        """Empty lines should be preserved (not stripped)."""
        input_text = "line1\n\nline3"
        result = self._collapse(input_text)
        assert result == "line1\n\nline3"

    def test_carriage_return_at_start(self):
        """\\r at start of line should keep the text after it."""
        input_text = "\roverwritten"
        result = self._collapse(input_text)
        assert result == "overwritten"

    def test_multiple_carriage_returns(self):
        """Multiple \\r on same line should keep only the last segment."""
        input_text = "first\rsecond\rthird"
        result = self._collapse(input_text)
        assert result == "third"

    def test_trailing_whitespace_stripped(self):
        """Trailing whitespace on each line should be stripped."""
        input_text = "text   \nmore text   "
        result = self._collapse(input_text)
        assert result == "text\nmore text"


class TestSeparatorFiltering:
    """Test that separator lines are filtered out."""

    def _filter(self, content):
        """Run the filtering logic (same as frontend)."""
        import re

        lines = content.split('\n')
        filtered = []
        for line in lines:
            # Strip ANSI codes for pattern matching (including DEC private modes)
            stripped = re.sub(r'\x1b\[[?]?[0-9;]*[a-zA-Z]', '', line)
            stripped = re.sub(r'\x1b\][^\x07]*\x07', '', stripped).strip()
            # Filter separators
            if re.match(r'^[─━═▬▪▫\-=\s]{20,}$', stripped):
                continue
            # Filter spinner-only lines: "⠋ Working..." or just "Working..."
            if re.match(r'^(?:[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*)?Working\.{0,3}$', stripped):
                continue
            filtered.append(line)
        return '\n'.join(filtered)

    def test_filters_unicode_separators(self):
        """Unicode box-drawing separators should be filtered."""
        input_text = "before\n────────────────────────────────────\nafter"
        result = self._filter(input_text)
        assert "before" in result
        assert "after" in result
        assert "────" not in result

    def test_filters_mixed_separators(self):
        """Mixed separator characters should be filtered."""
        input_text = "before\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\nafter"
        result = self._filter(input_text)
        assert "before" in result
        assert "after" in result
        assert "━━━━" not in result

    def test_filters_dash_separators(self):
        """Dash separators should be filtered."""
        input_text = "before\n----------------------------------------\nafter"
        result = self._filter(input_text)
        assert "before" in result
        assert "after" in result
        assert "----" not in result

    def test_preserves_short_separators(self):
        """Short separators (< 20 chars) should be preserved."""
        input_text = "before\n────────\nafter"
        result = self._filter(input_text)
        assert "────────" in result

    def test_filters_spinner_only_lines(self):
        """Lines with only spinner characters should be filtered."""
        input_text = "real content\n⠋ Working...\nmore content"
        result = self._filter(input_text)
        assert "real content" in result
        assert "more content" in result
        assert "⠋ Working..." not in result


class TestEndToEnd:
    """End-to-end tests combining all processing steps."""

    def _full_process(self, content):
        """Run the full processing pipeline."""
        import re

        text = content

        # Strip ANSI (same as _read_transcript_log)
        text = re.sub(r'\x1b\][^\x07]*\x07', '', text)
        text = re.sub(r'\x1b\][^\x1b]*\x1b\\', '', text)
        text = re.sub(r'\x1b\[[?]?[0-9;]*[^0-9;m]', '', text)
        text = re.sub(r'\x1b[()][A-Za-z0-9]', '', text)
        text = re.sub(r'\x1b[^\x1b\x5b\x5d]', '', text)

        # Collapse \r
        collapsed = []
        for line in text.split("\n"):
            if "\r" in line:
                line = line.rsplit("\r", 1)[-1]
            collapsed.append(line.rstrip())
        text = "\n".join(collapsed)

        # Filter separators and spinners (same as frontend)
        filtered = []
        for line in text.split("\n"):
            stripped = re.sub(r'\x1b\[[?]?[0-9;]*[a-zA-Z]', '', line)
            stripped = re.sub(r'\x1b\][^\x07]*\x07', '', stripped).strip()
            if re.match(r'^[─━═▬▪▫\-=\s]{20,}$', stripped):
                continue
            if re.match(r'^(?:[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s*)?Working\.{0,3}$', stripped):
                continue
            filtered.append(line)
        text = "\n".join(filtered)

        return text

    def test_real_terminal_output(self):
        """Process realistic terminal output with colors and spinners."""
        input_text = (
            "\x1b[2K\x1b[0m\x1b]8;;\x07\n"
            "\x1b[2K \x1b[38;2;138;190;183m⠹\x1b[39m "
            "\x1b[38;2;128;128;128mWorking...\x1b[39m"
            "\x1b[0m\x1b]8;;\x07"
            "\x1b[?2026l\x1b[3B\x1b[1G\x1b[?25l\x1b[?2026h\n"
            "\x1b[2K\x1b[0m\x1b]8;;\x07\n"
            "────────────────────────────────────────\n"
            "actual output line 1\n"
            "actual output line 2\n"
            "⠋ Working...\n"
            "\x1b[32mDone!\x1b[0m"
        )
        result = self._full_process(input_text)

        # Should keep actual content
        assert "actual output line 1" in result
        assert "actual output line 2" in result
        assert "Done!" in result
        assert "\x1b[32m" in result  # Color code preserved

        # Should strip control sequences
        assert "\x1b[2K" not in result
        assert "\x1b[?2026" not in result
        assert "\x1b]8;" not in result

        # Should filter separators and spinners
        assert "────" not in result
        assert "⠋ Working..." not in result

    def test_spinner_redraw_collapse(self):
        """Spinner redraws should collapse to final state."""
        input_text = (
            "\x1b[38;2;138;190;183m⠋\x1b[39m Working...\r"
            "\x1b[38;2;138;190;183m⠙\x1b[39m Working...\r"
            "\x1b[38;2;138;190;183m⠹\x1b[39m Working...\r"
            "\x1b[38;2;138;190;183m⠸\x1b[39m Done!"
        )
        result = self._full_process(input_text)
        assert "Done!" in result
        # Should not contain intermediate spinner states
        assert result.count("Working...") == 0 or "Done!" in result.split("\n")[-1]

    def test_partial_redraw_same_tool_deduplication(self):
        """Partial redraws with same tool name but diverging paths are deduplicated."""
        import re
        tool_re = re.compile(r'^(\s*(?:read|write|edit|bash|subagent|mcp)\s+)(.*)')

        lines = [
            'read /Users/hmuh',
            'read ~/code/applitnator/.worktrees/wt_feature/src/config.py',
            '',
        ]
        filtered_lines = [l for l in lines if l.strip()]
        deduped = []
        i = 0
        while i < len(filtered_lines):
            current = filtered_lines[i].strip()
            if i + 1 < len(filtered_lines):
                next_line = filtered_lines[i + 1].strip()
                if next_line.startswith(current) and len(next_line) > len(current):
                    i += 1
                    continue
                cur_match = tool_re.match(current)
                next_match = tool_re.match(next_line)
                if cur_match and next_match and cur_match.group(1) == next_match.group(1):
                    if len(current) < len(next_line):
                        i += 1
                        continue
            deduped.append(filtered_lines[i])
            i += 1

        assert 'read /Users/hmuh' not in deduped
        assert 'read ~/code/applitnator/.worktrees/wt_feature/src/config.py' in deduped


class TestReadTranscriptLogReal:
    """Tests calling the REAL AgentManager._read_transcript_log.

    The other classes in this file test inline COPIES of the filtering
    logic, which is exactly how the backend filter silently drifted from
    the frontend's (the backend was missing the separator filter entirely
    while the copy-based tests kept passing). These tests exercise the
    actual production code path against a transcript file on disk.
    """

    def _run(self, tmp_path, content, lines=200):
        from unittest.mock import MagicMock

        from src.agents.manager import AgentManager
        from src.agents.output_capture import AgentOutputCapture

        _write_transcript(tmp_path, "test_agent", content)

        mgr = AgentManager.__new__(AgentManager)
        task = MagicMock()
        task.workflow.working_directory = str(tmp_path)
        task.workflow.project_id = None
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = task
        mgr.db_manager = MagicMock()
        mgr.db_manager.get_session.return_value = session
        mgr._output_capture = AgentOutputCapture(mgr.db_manager, MagicMock())

        agent = _make_agent(current_task_id="t1")
        return mgr._read_transcript_log(agent, lines)

    def test_filters_separator_lines(self, tmp_path):
        """pi's block-separator lines (each char SGR-wrapped) are chrome,
        not content -- and leaving them in breaks the redraw dedup by
        splitting adjacent redraw frames."""
        sep = "".join("\x1b[38;2;129;162;190m─\x1b[39m" for _ in range(50))
        content = f"real line\n{sep}\nanother line\n"
        result = self._run(tmp_path, content)
        assert "real line" in result
        assert "another line" in result
        assert "─" not in result

    def test_filters_claude_codes_own_status_bar_chrome(self, tmp_path):
        """Regression, confirmed live (task 644d6e0b, agent 53a88e56):
        chrome_re only recognized pi's status-bar patterns. A claude
        agent's repeated re-renders of its OWN chrome (spinner+stats,
        tip line, version banner, permission bar) were classified
        'content' instead, which defeats the separator-collapse logic
        the same way leaving pi's own chrome unclassified would -- the
        separator pair bracketing it no longer has only blank/sep
        between it and the next real content, so the block-boundary
        collapse never fires. Net effect: every spinner tick left its
        own near-duplicate status line with the surrounding whitespace
        never collapsing down to a single blank line."""
        sep = "".join("\x1b[38;2;129;162;190m─\x1b[39m" for _ in range(50))
        content = (
            "real line one\n"
            f"{sep}\n"
            "✶ Choreographing… (1m 14s · ↓ 3.2k tokens)\n"
            "⎿ Tip: Use /btw to ask a quick side question without interrupting Claude's current work\n"
            "globalVersion: 2.1.238 · latestVersion: 2.1.259\n"
            "⏵⏵ bypass permissions on (shift+tab to cycle) · esc to interrupt · ← for agents\n"
            f"{sep}\n"
            "real line two\n"
        )
        result = self._run(tmp_path, content)
        assert "real line one" in result
        assert "real line two" in result
        assert "Choreographing" not in result
        assert "Tip: Use /btw" not in result
        assert "globalVersion" not in result
        assert "bypass permissions" not in result
        # The whole chrome block between the two separators must collapse
        # to a single blank line, not one blank per filtered-out chrome
        # line (four in this case) -- exactly the "many blank lines" bug.
        assert "\n\n\n" not in result

    def test_dedups_progressive_typing_redraws_across_separators(self, tmp_path):
        """pi re-renders a command line as it streams ($ cd /, $ cd /Users,
        ...), with a separator line between frames. All partial frames must
        collapse to the final one."""
        sep = "".join("\x1b[38;2;129;162;190m─\x1b[39m" for _ in range(50))
        content = (
            f"$ cd /\n{sep}\n"
            f"$ cd /Users/x\n{sep}\n"
            "$ cd /Users/x/proj && pytest\n"
        )
        result = self._run(tmp_path, content)
        assert result.count("$ cd") == 1
        assert "$ cd /Users/x/proj && pytest" in result

    def test_dedups_identical_consecutive_lines(self, tmp_path):
        content = "All 147 tests pass. Running more:\nAll 147 tests pass. Running more:\n"
        result = self._run(tmp_path, content)
        assert result.count("All 147 tests pass") == 1

    def test_dedups_repeated_block_redraws(self, tmp_path):
        """pi re-emits a whole past block (mcp call + JSON body) in dim gray
        once its result arrives -- the first copy must be dropped."""
        block = (
            " mcp call hephaestus_save_memory\n"
            " {\n"
            '   "content": "something",\n'
            '   "agent_id": "abc"\n'
            " }\n"
        )
        content = block + block + " ✅ Memory saved!\n"
        result = self._run(tmp_path, content)
        assert result.count("mcp call hephaestus_save_memory") == 1
        assert result.count('"content": "something"') == 1
        assert "✅ Memory saved!" in result

    def test_filter_runs_before_tail_not_after(self, tmp_path):
        """Regression: the tail (last N lines) used to be taken from the
        RAW transcript BEFORE any filtering ran. For a live agent (status
        != terminated) with a small `lines` limit, that could cut a
        duplicated block in half -- the first copy falls outside the raw
        window, the second survives alone, and the block-repeat dedup
        (which needs BOTH copies present to detect and collapse the
        pattern) has nothing to match against. Observed live: a live
        agent's output showed visible duplication that was absent once
        the same agent terminated and the whole file got filtered.
        Filtering must run on the WHOLE file first, with the tail applied
        to the already-deduped result."""
        filler = "\n".join(f"filler line {i}" for i in range(50))
        block = (
            " mcp call hephaestus_save_memory\n"
            " {\n"
            '   "content": "something",\n'
            '   "agent_id": "abc"\n'
            " }\n"
        )
        content = filler + "\n" + block + block + " done\n"

        # lines=9 puts the raw (pre-filter) tail boundary exactly inside
        # the first block copy, at its "content" line -- so the raw
        # window contains a truncated fragment of copy 1 (content/
        # agent_id/}) immediately followed by all of copy 2. Comparing
        # just that window, the two aren't adjacent-identical (different
        # preceding lines), so the old raw-tail-first code couldn't
        # collapse them -- both "content" lines would survive.
        result = self._run(tmp_path, content, lines=9)

        assert result.count('"content": "something"') == 1

    def test_drops_ansi_only_lines(self, tmp_path):
        """A line containing only ANSI codes renders as literal garbage in
        views that don't convert ANSI to HTML. Genuine blank lines are
        fine (kept for spacing); a NON-empty line must have visible text."""
        content = "real content\n\x1b[48;2;40;40;50m \x1b[49m\x1b[0m\nmore content\n"
        result = self._run(tmp_path, content)
        for line in result.split("\n"):
            import re as _re

            if not line:
                continue
            visible = _re.sub(r"\x1b\[[?]?[0-9;]*[a-zA-Z]", "", line).strip()
            assert visible, f"ANSI-only line survived: {line!r}"

    def test_background_color_reset_survives_a_huge_blank_gap(self, tmp_path):
        """Regression (reported live: git_expert c5e7a2a3's output rendered
        with a white/gray background from partway through to the end of
        its transcript): Claude Code sets a background color for a status
        banner, then jumps the cursor far down a tall pane
        (TMUX_PANE_HEIGHT) before resetting it -- landing the reset on an
        otherwise-empty row. That row's clean text is blank like any
        other, and used to have its raw bytes (including the reset)
        discarded entirely by the blank-line handling, letting the
        background bleed into every real line that followed."""
        content = (
            "before\n"
            "\x1b[100mBanner \x1b[39mtext\n"
            "\x1b[500B\x1b[49m\x1b[K\n"
            "after content\n"
        )
        result = self._run(tmp_path, content)
        assert "before" in result
        assert "after content" in result
        idx_after = result.index("after content")
        prefix = result[:idx_after]
        last_set = prefix.rfind("\x1b[100m")
        last_reset = max(prefix.rfind("\x1b[49m"), prefix.rfind("\x1b[0m"))
        assert last_set != -1, "test setup sanity: banner's bg-set should be present"
        assert last_reset > last_set, (
            f"background color never reset before real content resumed: {prefix!r}"
        )

    def test_separators_become_single_blank_lines(self, tmp_path):
        """Separators mark pi's block boundaries -- each run must become
        exactly ONE blank line (visual spacing), never multiple. A blank
        line between two real content lines (not chrome-adjacent) is now
        preserved as real content instead of always being treated as pi
        per-line padding noise -- see TestBlankLinesAreAllowed."""
        sep = "".join("\x1b[38;2;129;162;190m─\x1b[39m" for _ in range(50))
        content = (
            "block one line a\n"
            "\n"
            "block one line b\n"
            f"{sep}\n\n{sep}\n\n{sep}\n"
            "block two\n"
        )
        result = self._run(tmp_path, content)
        lines = result.split("\n")
        assert lines == ["block one line a", "", "block one line b", "", "block two"]

    def test_dedups_midline_growing_redraws_with_constant_tail(self, tmp_path):
        """Observed live (qa_validation agent): pi re-renders a read line
        with a constant `:200-299` tail while the path grows in the
        MIDDLE, switching from absolute to ~-abbreviated form mid-stream.
        Plain prefix comparison can't catch any of these frames."""
        content = (
            "All tests pass. Let me verify:\n"
            "read ...:200-299\n"
            "read /:200-299\n"
            "read /Users/hmuh:200-299\n"
            "read ~/code/s:200-299\n"
            "read ~/code/sotto/.worktrees:200-299\n"
            "read ~/code/sotto/.worktrees/wt_feature/docs:200-299\n"
            "read ~/code/sotto/.worktrees/wt_feature/docs/requirements.md:200-299\n"
        )
        result = self._run(tmp_path, content)
        assert result.count("read") == 1
        assert (
            "read ~/code/sotto/.worktrees/wt_feature/docs/requirements.md:200-299"
            in result
        )
        assert "All tests pass. Let me verify:" in result

    def test_isolated_gap_match_is_not_collapsed(self, tmp_path):
        """Two legitimately similar consecutive lines (one insertable into
        the other at a single point) must survive when isolated -- only
        CHAINS of such pairs are redraw frames. Reading several
        __init__.py files in a row is a real agent pattern."""
        content = (
            "read src/__init__.py\n"
            "read src/sub/__init__.py\n"
        )
        result = self._run(tmp_path, content)
        assert "read src/__init__.py" in result
        assert "read src/sub/__init__.py" in result

    def test_streaming_chrome_separators_do_not_become_blanks(self, tmp_path):
        """While streaming, pi re-renders its bottom status-bar chrome
        (separator pair + shell prompt + stats + MCP line) on EVERY frame.
        Those separators are chrome, not block boundaries -- converting
        them to blank lines put a blank between every pair of content
        lines. A separator earns a blank only when followed by content."""
        sep = "─" * 50
        frame_chrome = (
            f"{sep}\n\n{sep}\n"
            "~/code/sotto/.worktrees/wt_feature (feature/branch)\n"
            "↑62k ↓4.6k R629k CH88.3% $0.033 6.1%/1.0M (auto)\n"
            "MCP: 1/1 servers\n"
            "⠧ Working...\n"
        )
        content = (
            "import sys\n"
            + frame_chrome
            + "sys.path.insert(0, 'src')\n"
            + frame_chrome
            + "from sotto.llm import factory\n"
        )
        result = self._run(tmp_path, content)
        lines = result.split("\n")
        assert lines == [
            "import sys",
            "sys.path.insert(0, 'src')",
            "from sotto.llm import factory",
        ]

    def test_tool_invocation_lines_get_blank_line_above(self, tmp_path):
        """Block spacing is derived from content, not stream separators --
        every separator in a live pi stream belongs to the per-frame
        status-bar chrome (measured 217/217 in a real transcript), so
        separator-based spacing either put blanks everywhere or nowhere.
        Each tool-invocation line starts a visual block and gets exactly
        one blank line above it."""
        content = (
            " mcp call hephaestus_update_task_status\n"
            ' { "status": "done" }\n'
            " ✅ Task done successfully\n"
            " write ~/code/proj/docs/qa.md\n"
            " # QA Report\n"
            " $ pytest -q\n"
            " 10 passed\n"
        )
        result = self._run(tmp_path, content)
        lines = result.split("\n")
        assert lines == [
            " mcp call hephaestus_update_task_status",
            ' { "status": "done" }',
            " ✅ Task done successfully",
            "",
            " write ~/code/proj/docs/qa.md",
            " # QA Report",
            "",
            " $ pytest -q",
            " 10 passed",
        ]

    def _make_manager_and_agent(self, tmp_path, content, status="working"):
        """Like _run, but returns the (manager, agent) pair instead of
        calling _read_transcript_log immediately -- needed to exercise the
        instance-level cache across multiple calls on the SAME manager
        (each _run() call builds a fresh AgentManager, which would never
        share a cache)."""
        from unittest.mock import MagicMock

        from src.agents.manager import AgentManager
        from src.agents.output_capture import AgentOutputCapture

        _write_transcript(tmp_path, "test_agent", content)

        mgr = AgentManager.__new__(AgentManager)
        task = MagicMock()
        task.workflow.working_directory = str(tmp_path)
        task.workflow.project_id = None
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = task
        mgr.db_manager = MagicMock()
        mgr.db_manager.get_session.return_value = session
        mgr._output_capture = AgentOutputCapture(mgr.db_manager, MagicMock())

        agent = _make_agent(current_task_id="t1", status=status)
        return mgr, agent

    def test_unchanged_file_uses_cache_not_reprocessed(self, tmp_path):
        """Regression: filtering a whole transcript is real work (ANSI
        strip + redraw/dedup passes) -- up to seconds for a large,
        long-running agent. A live agent gets polled roughly every
        second, and pipe-pane only ever appends, so between polls with no
        new output the file is byte-for-byte unchanged and the previous
        filtered result is still exactly correct. The second call must
        not re-open/re-read the file at all."""
        import builtins

        mgr, agent = self._make_manager_and_agent(tmp_path, "hello\nworld\n")

        first = mgr._read_transcript_log(agent, 200)
        assert "hello" in first

        real_open = builtins.open
        with patch("builtins.open") as mock_open:
            mock_open.side_effect = AssertionError(
                "file was re-opened on a cache-hit poll"
            )
            second = mgr._read_transcript_log(agent, 200)

        assert second == first

    def test_changed_file_invalidates_cache(self, tmp_path):
        """The cache must not go stale forever -- once the file actually
        grows (a real new poll's worth of agent output), the next read
        must reflect it, not keep serving the old cached result. The
        cache key is (mtime, size) together, so a size change alone
        invalidates it regardless of filesystem mtime resolution."""
        mgr, agent = self._make_manager_and_agent(tmp_path, "hello\n")
        first = mgr._read_transcript_log(agent, 200)
        assert "world" not in first

        transcript = tmp_path / ".hephaestus" / "tmux" / "test_agent.transcript.log"
        with open(transcript, "a") as f:
            f.write("world\n")

        second = mgr._read_transcript_log(agent, 200)
        assert "world" in second

    def test_cache_respects_tail_limit_per_call(self, tmp_path):
        """A cache hit must still apply THIS call's own `lines` tail limit
        -- the cache stores the fully-filtered (untailed) result once, and
        two callers asking for different tail lengths against the same
        cached file must each get their own length back."""
        content = "\n".join(f"line {i}" for i in range(50))
        mgr, agent = self._make_manager_and_agent(tmp_path, content)

        full = mgr._read_transcript_log(agent, 200)
        assert len(full.split("\n")) == 50

        short = mgr._read_transcript_log(agent, 5)
        assert len(short.split("\n")) == 5
        assert short.split("\n") == full.split("\n")[-5:]

    def test_terminated_agent_result_also_cached(self, tmp_path):
        """A terminated agent's transcript file never changes again -- its
        full (ANSI-strip + dedup) history should only ever be computed
        once, not on every future request for it."""

        mgr, agent = self._make_manager_and_agent(
            tmp_path, "hello\nworld\n", status="terminated"
        )

        first = mgr._read_transcript_log(agent, 200)

        with patch("builtins.open") as mock_open:
            mock_open.side_effect = AssertionError(
                "file was re-opened on a cache-hit poll"
            )
            second = mgr._read_transcript_log(agent, 200)

        assert second == first

    def test_strips_trailing_pane_padding_before_sgr_resets(self, tmp_path):
        """pi pads every row to full pane width with background-colored
        spaces, with the SGR resets AFTER the padding -- so rstrip alone
        never removes it. In wrapping views the padding wraps into what
        looks like a blank line after every row. The padding must go; the
        codes must stay (a dropped reset bleeds color into the next line)."""
        line = (
            "\x1b[48;2;40;40;50m import sys"
            + " " * 130
            + "\x1b[49m\x1b[0m"
        )
        result = self._run(tmp_path, line + "\nnext line\n")
        first = result.split("\n")[0]
        assert "import sys" in first
        assert "  " not in first.replace("\x1b", "")  # no padding runs left
        assert first.endswith("\x1b[49m\x1b[0m")  # resets preserved

    def test_keeps_real_content_and_colors(self, tmp_path):
        """The filter must not eat actual output -- including SGR colors,
        which the frontend renders."""
        content = (
            "\x1b[32m124 passed\x1b[0m in 12.01s\n"
            "Coverage HTML written to dir htmlcov\n"
            "FAIL Required test coverage of 80% not reached.\n"
        )
        result = self._run(tmp_path, content)
        assert "124 passed" in result
        assert "\x1b[32m" in result
        assert "Coverage HTML written to dir htmlcov" in result
        assert "FAIL Required test coverage of 80% not reached." in result


class TestReadTranscriptLogCursorReconstruction:
    """Real terminals redraw in place using CSI cursor-movement sequences
    (G/C/D/B/A/H/f), not just \\r/\\n -- Claude Code and pi rely on this
    heavily. Regression: those sequences used to be stripped outright
    (same blanket "strip everything but SGR" the launch_pipeline.py pipe-
    pane filter itself used to apply), discarding the position information
    they carry -- text written before and after one got concatenated as if
    always adjacent. Observed live (agent 8389d7e0): "The adversarial
    review identified\\"" + CSI 81G (jump to column 81, skipping an
    unchanged prefix) + "with no findings listed" collapsed into
    "...identified\\"withnofindingslisted"."""

    def _run(self, tmp_path, content, lines=200):
        return TestReadTranscriptLogReal()._run(tmp_path, content, lines)

    def test_csi_g_absolute_column_jump_preserves_the_gap(self, tmp_path):
        content = 'The adversarial review identified"\x1b[81Gwith no findings listed.\n'
        result = self._run(tmp_path, content)
        line = [l for l in result.split("\n") if "adversarial" in l][0]
        # Column 81 (1-indexed) is index 80 -- "with" must land exactly
        # there, not get concatenated directly onto "identified\"".
        assert not line.startswith('The adversarial review identified"with')
        assert line.index("with no findings") == 80

    def test_csi_b_cursor_down_starts_a_new_row(self, tmp_path):
        """A live-redrawn multi-row status block moves between its rows
        via CSI B/A, not \\n -- treating it as a row boundary (like \\n)
        instead of silently deleting it keeps those rows from being
        flattened into one unreadable line."""
        content = "row one\r\x1b[1Brow two\n"
        result = self._run(tmp_path, content)
        lines = [l for l in result.split("\n") if l.strip()]
        assert "row one" in lines
        assert "row two" in lines

    def test_csi_h_cursor_position_moves_to_absolute_row_and_column(self, tmp_path):
        content = "first\n\x1b[1;1Hoverwritten\n"
        result = self._run(tmp_path, content)
        lines = result.split("\n")
        assert lines[0] == "overwritten"

    def test_auto_wraps_at_the_pane_width_instead_of_one_giant_row(self, tmp_path):
        """Without auto-wrap, a long paragraph with no explicit \\n or CSI
        B between its visual rows (the terminal's own implicit wrap)
        collapses into one enormous row -- observed live, 706KB/711KB of
        one real session's transcript was a single \\n-less span. A CSI G
        column jump meant for a short wrapped row then gets misapplied
        against that whole accumulated blob instead."""
        from src.core.constants import TMUX_PANE_WIDTH

        content = ("x" * (TMUX_PANE_WIDTH + 20)) + "\n"
        result = self._run(tmp_path, content)
        lines = [l for l in result.split("\n") if l]
        assert len(lines) >= 2
        assert len(lines[0]) <= TMUX_PANE_WIDTH

    def test_csi_k_erase_in_line_truncates_stale_tail(self, tmp_path):
        content = "hello world\r\x1b[Khi\n"
        result = self._run(tmp_path, content)
        first = result.split("\n")[0]
        assert first == "hi"

    def test_absurd_csi_parameter_does_not_hang_or_exhaust_memory(self, tmp_path):
        """A corrupted/truncated escape sequence (e.g. a partial write
        split mid-parameter across two pipe-pane reads) can produce a
        numeric CSI parameter far beyond anything a real terminal would
        ever send. Without a clamp, a single \\x1b[999999999G tries to
        pad one row to a billion elements -- confirmed to exhaust
        multiple GB and hang within a second. Must complete quickly
        regardless of what garbage the parameter contains."""
        import time

        content = "hello\x1b[999999999Gworld\n" + "x\x1b[888888888B" * 5 + "\n"
        t0 = time.time()
        result = self._run(tmp_path, content)
        elapsed = time.time() - t0
        assert elapsed < 5, f"took {elapsed:.2f}s -- clamp not applied"
        assert "hello" in result
        assert "world" in result
