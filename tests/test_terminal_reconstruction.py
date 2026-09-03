"""Direct unit tests for src/shared/terminal_reconstruction.py -- the
row/cursor CSI-parsing engine extracted from AgentOutputCapture's own
_read_transcript_log and tools/tmux-viewer's _reconstruct_raw_transcript,
which used to be two independently hand-copied implementations of the
same state machine. These tests exercise the shared engine directly;
tests/test_transcript_processing.py exercises it indirectly through the
main app's full filtering pipeline.
"""

from src.shared.terminal_reconstruction import reconstruct_terminal_rows


class TestReconstructTerminalRows:
    def test_plain_newlines_become_separate_rows(self):
        assert reconstruct_terminal_rows("line one\nline two\n", width=80) == [
            "line one",
            "line two",
            "",
        ]

    def test_csi_g_absolute_column_overwrites_in_place(self):
        """CHA (cursor to absolute column) followed by new text overwrites
        starting at that column rather than concatenating -- the exact
        case that used to collapse into
        '...identified\"withnofindingslisted' when CSI G was stripped."""
        result = reconstruct_terminal_rows("hello\x1b[1Gworld", width=80)
        assert result == ["world"]

    def test_csi_b_cursor_down_creates_intervening_blank_rows(self):
        # CSI B only moves the row -- the column stays wherever it was
        # (after "top", i.e. 3), so "bottom" starts padded from there.
        result = reconstruct_terminal_rows("top\x1b[3Bbottom", width=80)
        assert result == ["top", "", "", "   bottom"]

    def test_auto_wrap_at_width_starts_a_new_row(self):
        result = reconstruct_terminal_rows("abcdef", width=3)
        assert result == ["abc", "def"]

    def test_csi_k_erase_in_line_truncates_from_cursor(self):
        result = reconstruct_terminal_rows("hello world\x1b[6G\x1b[K", width=80)
        assert result == ["hello"]

    def test_sgr_pending_prefix_flushes_onto_the_row_it_belongs_to(self):
        """A trailing SGR code with no character after it before the row
        ends must land on THAT row, not bleed onto the next one's first
        character."""
        result = reconstruct_terminal_rows("a\x1b[31m\nb", width=80)
        assert result[0] == "a\x1b[31m"
        assert result[1] == "b"

    def test_absurd_csi_parameter_is_clamped_not_a_hang(self):
        import time

        start = time.time()
        result = reconstruct_terminal_rows("x\x1b[999999999999G y", width=80)
        elapsed = time.time() - start
        assert elapsed < 5
        assert len(result[0]) <= 100_001

    def test_carriage_return_resets_column_without_new_row(self):
        result = reconstruct_terminal_rows("spin1\rspin2", width=80)
        assert result == ["spin2"]

    def test_returns_rows_unstripped_and_uncollapsed(self):
        """The engine does no post-processing (rstrip, blank collapsing) --
        that's every caller's own responsibility, and callers disagree on
        how to do it."""
        result = reconstruct_terminal_rows("a   \n\n\nb", width=80)
        assert result == ["a   ", "", "", "b"]
