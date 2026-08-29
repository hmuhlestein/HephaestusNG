"""Tests for src/agents/pty_filter.py -- the standalone script piped
through tmux's pipe-pane to durably capture an agent's raw pty bytes.

Runs the actual script as a subprocess (matching how pipe-pane invokes
it) rather than importing filter_stream() directly, so these also catch
issues in the script's own __main__ wiring (stdin/stdout fd handling)
that an in-process import would miss.
"""

import os
import select
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "src" / "agents" / "pty_filter.py"


def _run_filter(input_bytes: bytes, timeout: float = 5.0) -> bytes:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        input=input_bytes,
        capture_output=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, f"filter exited {proc.returncode}, stderr={proc.stderr!r}"
    return proc.stdout


class TestPtyFilterStripsCorrectly:
    def test_keeps_sgr_color_codes(self):
        out = _run_filter(b"\x1b[32mgreen\x1b[39m")
        assert out == b"\x1b[32mgreen\x1b[39m"

    def test_keeps_cursor_position_csi_g(self):
        """CHA (absolute column) -- output_capture.py's row reconstruction
        depends on this surviving to correctly place text that skips an
        unchanged prefix."""
        out = _run_filter(b"hello\x1b[81Gworld")
        assert b"\x1b[81G" in out

    def test_keeps_cursor_movement_csi_bcdah(self):
        for letter in (b"B", b"C", b"D", b"A"):
            out = _run_filter(b"x\x1b[1" + letter + b"y")
            assert b"\x1b[1" + letter in out, f"CSI {letter} was stripped"

    def test_keeps_cursor_position_csi_h(self):
        out = _run_filter(b"\x1b[5;10Hx")
        assert b"\x1b[5;10H" in out

    def test_keeps_erase_csi_j_and_k(self):
        out = _run_filter(b"x\x1b[Ky\x1b[2Jz")
        assert b"\x1b[K" in out
        assert b"\x1b[2J" in out

    def test_strips_dec_private_mode(self):
        """Cursor visibility / bracketed paste / alt-screen toggles carry
        no text-layout information and must not survive."""
        out = _run_filter(b"before\x1b[?25lafter")
        assert b"\x1b[?25l" not in out
        assert out == b"beforeafter"

    def test_strips_osc_with_bel(self):
        out = _run_filter(b"before\x1b]0;window title\x07after")
        assert out == b"beforeafter"

    def test_strips_osc_with_string_terminator(self):
        out = _run_filter(b"before\x1b]0;window title\x1b\\after")
        assert out == b"beforeafter"

    def test_strips_charset_selection(self):
        out = _run_filter(b"before\x1b(0after")
        assert out == b"beforeafter"

    def test_strips_bare_esc_not_otherwise_matched(self):
        out = _run_filter(b"before\x1bZafter")
        assert out == b"beforeafter"

    def test_plain_text_passes_through_unchanged(self):
        out = _run_filter(b"just plain text\nwith a newline\n")
        assert out == b"just plain text\nwith a newline\n"

    def test_carriage_return_survives(self):
        """\\r itself is not a CSI/OSC sequence -- collapsing spinner
        redraws is output_capture.py's job when it later reads this raw
        file, not this filter's."""
        out = _run_filter(b"spin1\rspin2\rdone")
        assert out == b"spin1\rspin2\rdone"


class TestPtyFilterFlushesWithoutLineBuffering:
    def test_data_with_no_trailing_newline_is_flushed_immediately(self):
        """The whole reason this exists as os.read()-in-a-loop instead of
        line-based reading: modern TUIs redraw via \\r + cursor-
        positioning, not literal \\n. A line-buffered filter would sit on
        this input forever. Confirmed live: a real transcript sat frozen
        for an agent's entire multi-minute run this exact way."""
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            proc.stdin.write(b"status: \x1b[1mworking\x1b[22m (no newline here)")
            proc.stdin.flush()

            ready, _, _ = select.select([proc.stdout], [], [], 2.0)
            assert ready, "no output arrived within 2s -- filter is buffering on input"
            data = os.read(proc.stdout.fileno(), 4096)
            assert data == b"status: \x1b[1mworking\x1b[22m (no newline here)"
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)

    def test_multiple_writes_each_flush_independently(self):
        proc = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            for chunk in (b"first", b"second", b"third"):
                proc.stdin.write(chunk)
                proc.stdin.flush()
                ready, _, _ = select.select([proc.stdout], [], [], 2.0)
                assert ready, f"chunk {chunk!r} was not flushed through promptly"
                assert os.read(proc.stdout.fileno(), 4096) == chunk
        finally:
            proc.stdin.close()
            proc.wait(timeout=5)


class TestPtyFilterHandlesEmptyAndLargeInput:
    def test_empty_input_produces_empty_output_and_exits(self):
        out = _run_filter(b"")
        assert out == b""

    def test_large_input_completes_quickly(self):
        big = (b"x" * 1000 + b"\x1b[32mcolor\x1b[39m\n") * 2000
        t0 = time.time()
        out = _run_filter(big, timeout=15)
        elapsed = time.time() - t0
        assert elapsed < 10, f"took {elapsed:.2f}s for ~{len(big)} bytes"
        assert b"\x1b[32mcolor\x1b[39m" in out
