"""Tests for transcript log ANSI processing and \\r collapsing."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _make_agent(tmux_session_name="test_agent", status="working", current_task_id=None):
    """Create a mock agent object."""
    agent = MagicMock()
    agent.id = "test-agent-id"
    agent.tmux_session_name = tmux_session_name
    agent.status = status
    agent.current_task_id = current_task_id
    agent.cli_type = "pi"
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
        import re

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
