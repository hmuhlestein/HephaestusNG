"""Core tmux session management - create, read, write, and kill tmux sessions."""

import logging
from pathlib import Path
from typing import Dict, List, Optional

import libtmux

logger = logging.getLogger(__name__)

# How many ancestor directories of the pane's cwd to check for a
# Hephaestus .hephaestus/tmux/ dir -- an agent's pane can `cd` anywhere
# during its work, so its cwd at read time isn't necessarily the worktree
# root the session started in.
_HEPHAESTUS_TMUX_SEARCH_DEPTH = 6


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

    def __init__(self, session_prefix: str = "agent"):
        """Initialize the tmux session manager.

        Args:
            session_prefix: Prefix for auto-generated session names.
                           Named sessions use the provided name directly.
        """
        self.server = libtmux.Server()
        self.session_prefix = session_prefix

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

        clean_log = self._find_hephaestus_clean_log(session_name, pane)
        if clean_log is not None:
            try:
                text = clean_log.read_text(errors="replace")
                out_lines = text.splitlines()
                if lines > 0:
                    out_lines = out_lines[-lines:]
                return "\n".join(out_lines)
            except Exception as e:
                logger.warning(
                    f"Failed to read Hephaestus clean log for '{session_name}' "
                    f"({clean_log}), falling back to capture-pane: {e}"
                )

        try:
            output = pane.cmd("capture-pane", "-p", f"-S -{lines}").stdout
            return "\n".join(output) if output else ""
        except Exception as e:
            logger.error(f"Failed to capture output from '{session_name}': {e}")
            return ""

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
