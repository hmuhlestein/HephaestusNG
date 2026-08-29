"""Core tmux session management - create, read, write, and kill tmux sessions."""

import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

import libtmux

logger = logging.getLogger(__name__)

# How many ancestor directories of the pane's cwd to check for a
# Hephaestus .hephaestus/tmux/ dir -- an agent's pane can `cd` anywhere
# during its work, so its cwd at read time isn't necessarily the worktree
# root the session started in.
_HEPHAESTUS_TMUX_SEARCH_DEPTH = 6

# Mirrors src/core/constants.py's TMUX_PANE_WIDTH -- this tool is
# standalone/reusable (its own requirements.txt, no dependency on the
# main app's src/ package), so it can't import that constant directly.
# Only used to auto-wrap a Hephaestus agent's raw transcript (the
# .hephaestus/tmux/ convention below is already Hephaestus-specific), so
# it must stay in sync with the real value if that ever changes.
_HEPHAESTUS_PANE_WIDTH = 150

_RAW_TRANSCRIPT_TOKEN_RE = re.compile(r'\x1b\[([0-9;?]*)([A-Za-z])|([^\x1b])', re.DOTALL)


def _reconstruct_raw_transcript(raw_bytes: bytes, width: int = _HEPHAESTUS_PANE_WIDTH) -> str:
    """Reconstruct readable rows from a raw pipe-pane transcript the way a
    real terminal would, instead of naively treating \\n as the only row
    boundary. TUI apps (Claude Code, pi) redraw in place using CSI
    G/C/D/B/A/H/f (cursor move) and K/J (erase), not just \\r/\\n --
    deleting those (as pipe-pane's own perl filter used to, and as a
    plain ANSI-strip would) throws away the position information they
    carry, concatenating text written before and after one as if always
    adjacent.

    This is a simplified sibling of
    src/agents/output_capture.py::AgentOutputCapture._read_transcript_log
    -- same cursor/row reconstruction core, without that function's
    Claude-Code/pi-specific chrome and progressive-redraw deduplication
    passes (this tool views arbitrary tmux sessions, not just Hephaestus
    agents, so baking in that much CLI-specific noise-filtering isn't
    appropriate here). Keep the two in sync if the reconstruction core
    itself changes.

    SGR color codes are tracked as a "pending" prefix attached to the
    next character written rather than occupying a column of their own --
    correctly interleaving them with arbitrary overwrites is a full
    terminal emulator's job. A run of SGR codes with nothing following
    before the row ends is simply dropped (cosmetic only).
    """
    text = raw_bytes.decode('utf-8', errors='replace')
    text = re.sub(r'\x1b\][^\x07]*\x07', '', text)  # OSC with BEL
    text = re.sub(r'\x1b\][^\x1b]*\x1b\\', '', text)  # OSC with ST
    text = re.sub(r'\x1b[()][A-Za-z0-9]', '', text)  # Charset selection
    text = re.sub(r'\x1b(?!\[)[^\x1b\x5b\x5d]', '', text)  # Any other bare ESC (not CSI)

    # Safety clamp against a corrupted/truncated escape sequence (e.g. a
    # partial write split mid-parameter) whose numeric parameter comes
    # out absurdly large -- without this, a single \x1b[999999999G would
    # try to pad one row to a billion elements. Generous relative to any
    # real terminal (width) or realistic session length (rows).
    _MAX_COL = 100_000
    _MAX_ROW = 100_000
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

    for m in _RAW_TRANSCRIPT_TOKEN_RE.finditer(text):
        params, letter, ch = m.group(1), m.group(2), m.group(3)
        if letter is not None:
            if letter == 'm':
                pending_sgr += m.group(0)
                continue
            parts = [int(p) for p in params.split(';') if p.isdigit()] if params else []
            n = parts[0] if parts else None
            if letter == 'G':
                cursor_col = min(max(0, (n or 1) - 1), _MAX_COL)
            elif letter == 'C':
                cursor_col = min(cursor_col + (n or 1), _MAX_COL)
            elif letter == 'D':
                cursor_col = max(0, cursor_col - (n or 1))
            elif letter == 'B':
                cursor_row = min(cursor_row + (n or 1), _MAX_ROW)
                _ensure_row(cursor_row)
            elif letter == 'A':
                cursor_row = max(0, cursor_row - (n or 1))
            elif letter in ('H', 'f'):
                row_n = parts[0] if len(parts) > 0 else 1
                col_n = parts[1] if len(parts) > 1 else 1
                cursor_row = min(max(0, row_n - 1), _MAX_ROW)
                _ensure_row(cursor_row)
                cursor_col = min(max(0, col_n - 1), _MAX_COL)
            elif letter == 'K':
                mode = n or 0
                row = rows[cursor_row]
                if mode == 0:
                    del row[cursor_col:]
                elif mode == 1:
                    for i in range(min(cursor_col, len(row))):
                        row[i] = " "
                elif mode == 2:
                    rows[cursor_row] = []
            elif letter == 'J':
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
            continue
        if ch == '\r':
            cursor_col = 0
        elif ch == '\n':
            _end_row()
        else:
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

    return "\n".join("".join(row).rstrip() for row in rows).strip("\n")


class TmuxSessionManager:
    """Manages tmux sessions for agent terminal viewing.

    Provides a clean interface to:
    - Create detached tmux sessions with custom working directories and env vars
    - Capture real-time terminal output from any pane
    - Send keystrokes/messages into running agent sessions
    - List all managed sessions
    - Kill sessions on demand

    Usage:
        manager = TmuxSessionManager(session_prefix="agent")

        # Create a session
        session = manager.create_session("my-agent", working_directory="/path/to/project")

        # Capture output
        output = manager.get_output("my-agent", lines=500)

        # Send a message
        manager.send_message("my-agent", "hello from the viewer")

        # Kill a session
        manager.kill_session("my-agent")
    """

    # Cap on _transcript_backfill_cache -- nothing ever evicts an entry, so
    # this process-lifetime dict would otherwise grow monotonically against
    # uptime x session volume with no bound at all. FIFO eviction (oldest
    # entry first, relying on dict insertion order) is enough since each
    # entry is write-once and never re-read once its session is stale.
    _TRANSCRIPT_BACKFILL_CACHE_MAX = 200

    def __init__(self, session_prefix: str = "agent"):
        """Initialize the tmux session manager.

        Args:
            session_prefix: Prefix for auto-generated session names.
                           Named sessions use the provided name directly.
        """
        self.server = libtmux.Server()
        self.session_prefix = session_prefix
        # Lazily-computed, session-lifetime cache of each session's raw-
        # transcript backfill (see _get_raw_transcript_backfill) -- read
        # and filtered ONCE per session, not on every poll, since it can
        # be a large file and its whole purpose is recovering EARLY
        # content .clean.log missed, not tracking live changes. Bounded by
        # _TRANSCRIPT_BACKFILL_CACHE_MAX (see that constant's comment).
        self._transcript_backfill_cache: Dict[str, str] = {}

    def create_session(
        self,
        session_name: str,
        working_directory: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None,
    ) -> libtmux.Session:
        """Create a new detached tmux session.

        Args:
            session_name: Unique name for the session.
            working_directory: Directory to start the session in.
            env_vars: Environment variables to export in the shell.

        Returns:
            The created tmux session object.

        Raises:
            RuntimeError: If the session cannot be created.
        """
        if self.server.has_session(session_name):
            logger.warning(f"Session '{session_name}' already exists, killing it")
            self.kill_session(session_name)

        kwargs = {
            "session_name": session_name,
            "window_name": "main",
            "attach": False,
        }
        if working_directory:
            kwargs["start_directory"] = working_directory

        try:
            session = self.server.new_session(**kwargs)
        except Exception as e:
            raise RuntimeError(f"Failed to create tmux session '{session_name}': {e}")

        if env_vars:
            pane = session.attached_window.attached_pane
            for key, value in env_vars.items():
                pane.send_keys(f'export {key}="{value}"', enter=True)

        logger.info(f"Created tmux session: {session_name}")
        return session

    def get_output(self, session_name: str, lines: int = 200) -> str:
        """Capture recent output from a tmux session's active pane.

        Live `capture-pane` only ever returns whatever currently fits in
        tmux's own scrollback -- which is effectively just the visible
        pane height for a full-screen TUI CLI (Claude Code, Codex, pi)
        running in the alternate screen buffer, since tmux does not
        scroll back the alt screen at all. Prefer a Hephaestus-launched
        agent's own `.clean.log` (maintained continuously by the main
        HephaestusNG backend's stability-tracked transcript poller,
        already correctly terminal-rendered -- see
        AgentOutputCapture._poll_stable_transcript) when one exists,
        falling back to live capture-pane only for sessions with no such
        file (e.g. a plain shell, or a session this tool created itself).

        Args:
            session_name: Name of the tmux session.
            lines: Number of lines to capture from the scrollback buffer.

        Returns:
            Terminal output text, or empty string if session not found.
        """
        session = self._find_session(session_name)
        if not session:
            logger.warning(f"Session '{session_name}' not found")
            return ""

        try:
            pane = session.attached_window.attached_pane
        except Exception as e:
            logger.error(f"Failed to get pane for '{session_name}': {e}")
            return ""

        backfill = self._get_raw_transcript_backfill(session_name, pane)

        clean_log = self._find_hephaestus_clean_log(session_name, pane)
        if clean_log is not None:
            try:
                text = clean_log.read_text(errors="replace")
                out_lines = text.splitlines()
                if lines > 0:
                    out_lines = out_lines[-lines:]
                live_text = "\n".join(out_lines)
                return f"{backfill}\n\n[... below: continues live ...]\n\n{live_text}" if backfill else live_text
            except Exception as e:
                logger.warning(
                    f"Failed to read Hephaestus clean log for '{session_name}' "
                    f"({clean_log}), falling back to capture-pane: {e}"
                )

        try:
            output = pane.cmd("capture-pane", "-p", f"-S -{lines}").stdout
            live_text = "\n".join(output) if output else ""
            if backfill:
                return f"{backfill}\n\n[... below: continues live ...]\n\n{live_text}" if live_text else backfill
            return live_text
        except Exception as e:
            logger.error(f"Failed to capture output from '{session_name}': {e}")
            return backfill

    def _find_hephaestus_clean_log(self, session_name: str, pane) -> Optional[Path]:
        """Look for `.hephaestus/tmux/{session_name}.clean.log` at or above
        the pane's current working directory. Returns None if the pane's
        cwd can't be read, no such file exists within the search depth, or
        the file exists but is empty (nothing stable written yet -- let
        the capture-pane fallback handle that case instead)."""
        try:
            cwd = pane.pane_current_path
        except Exception:
            cwd = None
        if not cwd:
            return None

        current = Path(cwd)
        for _ in range(_HEPHAESTUS_TMUX_SEARCH_DEPTH):
            candidate = current / ".hephaestus" / "tmux" / f"{session_name}.clean.log"
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
            if current.parent == current:
                break
            current = current.parent
        return None

    def _find_hephaestus_raw_transcript(self, session_name: str, pane) -> Optional[Path]:
        """Same search as _find_hephaestus_clean_log, for the raw pipe-pane
        `{session_name}.transcript.log` instead."""
        try:
            cwd = pane.pane_current_path
        except Exception:
            cwd = None
        if not cwd:
            return None

        current = Path(cwd)
        for _ in range(_HEPHAESTUS_TMUX_SEARCH_DEPTH):
            candidate = current / ".hephaestus" / "tmux" / f"{session_name}.transcript.log"
            if candidate.exists() and candidate.stat().st_size > 0:
                return candidate
            if current.parent == current:
                break
            current = current.parent
        return None

    def _get_raw_transcript_backfill(self, session_name: str, pane) -> str:
        """Recover the TRUE beginning of a live agent's session, which
        .clean.log structurally cannot have: it's built by polling
        capture-pane only while a viewer is actively open (see
        AgentOutputCapture._poll_stable_transcript), so anything from
        before the first poll -- or that scrolled off the alt-screen pane
        between infrequent polls -- is gone from it forever. The raw
        pipe-pane transcript has no such gap; it captures every byte
        continuously from session start. Read and reconstructed ONCE per
        session (cached for this manager's lifetime, not re-read on every
        poll -- it's a backfill for what's permanently missing at the
        start, not a live-updating source), so get_output prepends it in
        front of the normal live clean_log/capture-pane content instead
        of replacing it.

        Returns "" if there's nothing to backfill with (no such file, or
        already cached as empty).
        """
        if session_name in self._transcript_backfill_cache:
            return self._transcript_backfill_cache[session_name]

        backfill = ""
        transcript_path = self._find_hephaestus_raw_transcript(session_name, pane)
        if transcript_path is not None:
            try:
                raw_bytes = transcript_path.read_bytes()
                backfill = _reconstruct_raw_transcript(raw_bytes)
            except Exception as e:
                logger.warning(
                    f"Failed to read/reconstruct raw transcript for '{session_name}' "
                    f"({transcript_path}): {e}"
                )
                backfill = ""

        if len(self._transcript_backfill_cache) >= self._TRANSCRIPT_BACKFILL_CACHE_MAX:
            oldest_session_name = next(iter(self._transcript_backfill_cache))
            del self._transcript_backfill_cache[oldest_session_name]
        self._transcript_backfill_cache[session_name] = backfill
        return backfill

    def send_message(self, session_name: str, message: str, enter: bool = True) -> bool:
        """Send a message (keystrokes) to a tmux session.

        Args:
            session_name: Name of the tmux session.
            message: Text to send as keystrokes.
            enter: Whether to send an Enter key after the message.

        Returns:
            True if message was sent successfully.
        """
        session = self._find_session(session_name)
        if not session:
            logger.warning(f"Session '{session_name}' not found")
            return False

        try:
            pane = session.attached_window.attached_pane
            pane.send_keys(message, enter=enter)
            logger.debug(f"Sent message to '{session_name}' ({len(message)} chars)")
            return True
        except Exception as e:
            logger.error(f"Failed to send message to '{session_name}': {e}")
            return False

    def kill_session(self, session_name: str) -> bool:
        """Kill a tmux session by name.

        Args:
            session_name: Name of the session to kill.

        Returns:
            True if session was killed, False if not found.
        """
        session = self._find_session(session_name)
        if not session:
            return False

        try:
            session.kill_session()
            logger.info(f"Killed tmux session: {session_name}")
            return True
        except Exception as e:
            logger.error(f"Failed to kill session '{session_name}': {e}")
            return False

    def list_sessions(self, prefix_filter: Optional[str] = None) -> List[Dict]:
        """List all tmux sessions, optionally filtered by prefix.

        Args:
            prefix_filter: If provided, only return sessions whose names start with this prefix.

        Returns:
            List of dicts with session info: {name, session_id, windows, attached}.
        """
        sessions = []
        for session in self.server.sessions:
            if prefix_filter and not session.name.startswith(prefix_filter):
                continue
            sessions.append(
                {
                    "name": session.name,
                    "session_id": session.session_id,
                    "windows": len(list(session.windows)),
                    "attached": any(w.attached for w in session.windows),
                }
            )
        return sessions

    def session_exists(self, session_name: str) -> bool:
        """Check if a tmux session exists.

        Args:
            session_name: Name of the session to check.

        Returns:
            True if session exists.
        """
        return self.server.has_session(session_name)

    def _find_session(self, session_name: str) -> Optional[libtmux.Session]:
        """Find a session by name using iteration (avoids deprecated get_by_id).

        Args:
            session_name: Name of the session to find.

        Returns:
            The session object, or None if not found.
        """
        if not self.server.has_session(session_name):
            return None

        for session in self.server.sessions:
            if session.name == session_name:
                return session
        return None
