"""Raw pty-byte filter piped through tmux's pipe-pane, stripping terminal
control sequences that carry no row/column/color information while
preserving those that do.

Invoked directly as a subprocess by launch_pipeline.py's
_create_tmux_session (`python3 pty_filter.py >> transcript.log`), NOT
imported -- it must stay dependency-free (stdlib only) since it runs
detached from this project's own sys.path/PYTHONPATH.

Two properties are load-bearing, not incidental:

1. Output must be flushed after every write, not left to Python's own
   buffering. Without this, even a filter that has processed a chunk
   won't push it to disk promptly.

2. Input must come from a true short read (os.read(0, N), a thin wrapper
   over the raw read(2) syscall), not line-based reading (iterating
   stdin, or .readline()). Line-based reading waits for "\n" before
   there's anything to flush at all -- modern TUIs (Claude Code, pi)
   redraw mostly via \r + cursor-positioning escapes, not literal "\n".
   Confirmed live: a transcript sat frozen at exactly the byte offset of
   the launch command's own trailing newline for an agent's entire
   multi-minute run, while tmux capture-pane on the same live session
   showed extensive fresh output the whole time -- flushing alone (a
   prior fix, when this was a Perl one-liner) never got the chance to
   flush anything because the filter was still blocked waiting for a
   "\n" that wasn't coming. os.read(0, 65536) returns as soon as ANY
   data is available on the pipe (exactly like tmux's own pipe-pane
   delivery), so each read pairs with an immediate write+flush of
   whatever arrived. A multi-byte escape sequence split across two reads
   won't be matched by either substitution pass and survives unstripped
   in the transcript -- a rare cosmetic imperfection, not a functional
   blocker, unlike minutes of frozen scrollback.

This used to be a `perl -e '...'` one-liner with identical regexes.
Python instead of Perl: Perl isn't guaranteed to be present on every
platform this might eventually run on (notably Windows, which doesn't
ship it), while Python already is -- it's this project's own runtime.

CSI sequences ending in one of ABCDGHJKf (cursor up/down/forward/back/
absolute-column/absolute-position, erase display/line) carry the
row/column information a TUI's in-place redraws depend on -- Claude Code
and pi redraw via these, not just \r/\n. Stripping them (the old
behavior) silently destroys that information before it ever reaches
disk: two pieces of text written before and after a stripped CSI G (jump
to column N, skipping an unchanged prefix) end up directly concatenated
with no separation. output_capture.py's _read_transcript_log
reconstructs rows FROM these sequences (plus \r/\n), so they need to
survive here to be reconstructable at all. Only m (SGR/color) and these
position/erase codes are excluded from the strip below -- everything
else (cursor visibility, bracketed paste, alt-screen toggles, etc.) has
no bearing on text layout and stays stripped.
"""

import os
import re
import sys

_STRIP_PATTERNS = [
    re.compile(rb'\x1b\][^\x07]*\x07'),  # OSC with BEL
    re.compile(rb'\x1b\][^\x1b]*\x1b\\'),  # OSC with ST
    re.compile(rb'\x1b\[[?]?[0-9;]*[^0-9;mABCDGHJKf]'),  # All CSI/DEC except color + cursor/erase
    re.compile(rb'\x1b[()][A-Za-z0-9]'),  # Charset selection
    re.compile(rb'\x1b[^\x1b\x5b\x5d]'),  # Any other bare ESC sequences
]


def filter_stream(read_fd: int, out) -> None:
    """Read raw pty bytes from read_fd until EOF, stripping non-position
    control sequences, writing+flushing each filtered chunk to out."""
    while True:
        buf = os.read(read_fd, 65536)
        if not buf:
            break
        for pattern in _STRIP_PATTERNS:
            buf = pattern.sub(b'', buf)
        out.write(buf)
        out.flush()


if __name__ == "__main__":
    filter_stream(0, sys.stdout.buffer)
