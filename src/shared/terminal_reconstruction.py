"""Raw pty-byte -> terminal-row reconstruction, shared between
AgentOutputCapture._read_transcript_log (src/agents/output_capture.py) and
tools/tmux-viewer's own _reconstruct_raw_transcript.

Both consumers used to carry an independent, hand-copied version of this
exact state machine -- their docstrings said "keep the two in sync if the
reconstruction core itself changes," which in practice meant every bug fix
here (a CSI handler, a safety clamp, a width/height tuning) had to be
applied twice, by hand, in two files. Confirmed drifted at least once
before (a missing separator filter caught by a test on one side only) and
at least three times in one session while chasing render bugs live. This
module is the actual reconstruction engine; each consumer keeps its own
downstream processing (chrome/dedup filtering for the main app, a simpler
generic blank-collapsing pass for the standalone viewer) separate, since
those two genuinely diverge -- only the byte-parsing core was ever truly
identical.

No dependency on anything else under src/ (stdlib only: re, typing) so
importing this one file doesn't drag in this project's database/ORM stack
-- the standalone tmux-viewer tool's whole reason for not depending on
src/ in general.
"""

import re
from typing import List

# Bounds on how far a single CSI move can push the cursor -- not a real
# terminal limit, just a safety clamp against a corrupted/truncated escape
# sequence (e.g. a partial write split mid-parameter across two pipe-pane
# reads) whose numeric parameter comes out absurdly large. Without this, a
# single \x1b[999999999G would try to pad one row to a billion elements --
# confirmed live to exhaust multiple GB and hang within a second. Generous
# relative to any real terminal (width) or realistic session length
# (rows), so legitimate content is never affected.
DEFAULT_MAX_COL = 100_000
DEFAULT_MAX_ROW = 100_000

_TOKEN_RE = re.compile(r'\x1b\[([0-9;?]*)([A-Za-z])|([^\x1b])', re.DOTALL)


def reconstruct_terminal_rows(
    text: str,
    width: int,
    max_col: int = DEFAULT_MAX_COL,
    max_row: int = DEFAULT_MAX_ROW,
) -> List[str]:
    """Reconstruct one string per terminal row from already-decoded pty
    text, the way a real terminal would -- instead of treating \\n as the
    only row boundary and stripping every cursor-movement CSI sequence as
    if it had no effect.

    TUI apps (Claude Code, pi) redraw in place using CSI G (cursor to
    absolute column -- skips re-sending an unchanged prefix), CSI C/D
    (relative column move), CSI B/A (row up/down), and CSI H/f (absolute
    cursor position), not just \\r/\\n. Deleting those sequences outright
    (an ANSI-strip's default behavior) throws away the position
    information they carry, so text written before and after one gets
    concatenated as if they were always adjacent. Observed live: "The
    adversarial review identified\\"" + CSI 81G (jump to column 81) +
    "with no findings listed" collapsed into
    "...identified\\"withnofindingslisted" -- and, at a larger scale, an
    entire ~700KB/14000-\\r stretch of one real session had no \\n at all
    (a live-redrawn status block relies entirely on CSI G/C/B, never
    \\n), collapsing minutes of real content into one unreadable line.

    SGR color codes are tracked as a "pending" prefix attached to the
    next character written, rather than occupying a column of their own
    -- correctly interleaving them with arbitrary overwrites is a full
    terminal emulator's job. A trailing run of SGR codes with no
    following character before the row ends is flushed onto that row
    once it ends (\\n or auto-wrap) rather than carried forward to prefix
    the next row's first character with a code that belongs here
    instead.

    Callers are responsible for: decoding raw bytes to `text`, stripping
    OSC/charset/bare-ESC sequences this function doesn't itself interpret
    (only CSI `\\x1b[...` is handled here), and any post-processing on the
    returned rows (rstrip, blank-line collapsing, chrome filtering) --
    this function only does the row/cursor state machine, un-opinionated
    about what happens to its output.

    Returns one string per reconstructed row, each the raw concatenation
    of that row's cells (SGR codes and characters interleaved) -- NOT
    rstripped, NOT blank-collapsed. Every caller already does its own
    version of both afterward, and they don't agree on how (e.g. the main
    app's trailing-pad strip has to keep a reset code that a plain
    rstrip would drop), so doing either here would just be a third,
    differently-wrong version to later strip back out.
    """
    rows: List[List[str]] = [[]]
    cursor_row = 0
    cursor_col = 0
    pending_sgr = ""

    def _ensure_row(r: int) -> None:
        while len(rows) <= r:
            rows.append([])

    def _end_row() -> None:
        nonlocal pending_sgr, cursor_row, cursor_col
        if pending_sgr:
            rows[cursor_row].append(pending_sgr)
            pending_sgr = ""
        cursor_row += 1
        _ensure_row(cursor_row)
        cursor_col = 0

    for m in _TOKEN_RE.finditer(text):
        params, letter, ch = m.group(1), m.group(2), m.group(3)
        if letter is not None:
            if letter == 'm':
                pending_sgr += m.group(0)
                continue
            parts = [int(p) for p in params.split(';') if p.isdigit()] if params else []
            n = parts[0] if parts else None
            if letter == 'G':  # CHA -- move to absolute column n
                cursor_col = min(max(0, (n or 1) - 1), max_col)
            elif letter == 'C':  # CUF -- move forward n columns
                cursor_col = min(cursor_col + (n or 1), max_col)
            elif letter == 'D':  # CUB -- move back n columns
                cursor_col = max(0, cursor_col - (n or 1))
            elif letter == 'B':  # CUD -- move down n rows
                cursor_row = min(cursor_row + (n or 1), max_row)
                _ensure_row(cursor_row)
            elif letter == 'A':  # CUU -- move up n rows
                cursor_row = max(0, cursor_row - (n or 1))
            elif letter in ('H', 'f'):  # CUP -- absolute row;col (1-indexed)
                row_n = parts[0] if len(parts) > 0 else 1
                col_n = parts[1] if len(parts) > 1 else 1
                cursor_row = min(max(0, row_n - 1), max_row)
                _ensure_row(cursor_row)
                cursor_col = min(max(0, col_n - 1), max_col)
            elif letter == 'K':  # EL -- erase in line
                mode = n or 0
                row = rows[cursor_row]
                if mode == 0:
                    del row[cursor_col:]
                elif mode == 1:
                    for i in range(min(cursor_col, len(row))):
                        row[i] = " "
                elif mode == 2:
                    rows[cursor_row] = []
            elif letter == 'J':  # ED -- erase in display
                mode = n or 0
                if mode == 0:
                    del rows[cursor_row][cursor_col:]
                    del rows[cursor_row + 1:]
                elif mode == 1:
                    for i in range(min(cursor_col, len(rows[cursor_row]))):
                        rows[cursor_row][i] = " "
                    for r in range(cursor_row):
                        rows[r] = []
                else:
                    rows = [[]]
                    cursor_row = 0
            # Any other CSI is rare enough here to ignore outright, same
            # as a blanket strip would.
            continue
        if ch == '\r':
            cursor_col = 0
        elif ch == '\n':
            _end_row()
        else:
            # Auto-wrap: a real terminal moves to a new row once writing
            # would overflow the pane's fixed width, instead of extending
            # the row forever. Without this, a long wrapped paragraph (no
            # explicit \n or CSI B between its visual rows, just the
            # terminal's own implicit wrap) stays one enormous row, and
            # CSI G/C column jumps meant for a SHORT wrapped row get
            # misapplied against that whole thing.
            if cursor_col >= width:
                _end_row()
            row = rows[cursor_row]
            while len(row) <= cursor_col:
                row.append(" ")
            row[cursor_col] = pending_sgr + ch
            pending_sgr = ""
            cursor_col += 1

    if pending_sgr:
        rows[cursor_row].append(pending_sgr)

    return ["".join(row) for row in rows]
