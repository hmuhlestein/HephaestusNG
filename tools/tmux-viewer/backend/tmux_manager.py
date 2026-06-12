"""Core tmux session management - create, read, write, and kill tmux sessions."""

import logging
from typing import Optional, Dict, List
import libtmux

logger = logging.getLogger(__name__)


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
            output = pane.cmd("capture-pane", "-p", f"-S -{lines}").stdout
            return "\n".join(output) if output else ""
        except Exception as e:
            logger.error(f"Failed to capture output from '{session_name}': {e}")
            return ""

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
            sessions.append({
                "name": session.name,
                "session_id": session.session_id,
                "windows": len(list(session.windows)),
                "attached": any(
                    w.attached for w in session.windows
                ),
            })
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
