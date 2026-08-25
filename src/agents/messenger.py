"""Delivering a message to a specific agent's tmux session.

Extracted from AgentManager, which mixed this concern in with tmux session
lifecycle, prompt construction, and DB persistence for agent creation/
restart — see docs/SOLID_OO_REVIEW.md finding 3.1. AgentManager still
exposes send_message_to_agent (many callers depend on that public API) but
delegates to an AgentMessenger instance instead of implementing the tmux
plumbing itself.

broadcast_message_to_all_agents/send_direct_message stay on AgentManager
rather than moving here: both call self.send_message_to_agent, and several
tests patch that method at the AgentManager instance level and assert the
broadcast/direct-message loop invoked it. Moving the loop itself into this
class would have it call AgentMessenger's own send_message_to_agent
instead, silently bypassing that mock.
"""

import logging
import re

from src.core.database import utc_now

logger = logging.getLogger(__name__)

# A shell dropped into a continuation prompt (unterminated quote/paren/
# heredoc from a prior Bash tool call) waits here indefinitely. Matches
# zsh's PS2 variants (dquote>, quote>, cmdsubst>, heredoc>, bquote>) and
# bash's generic "> ".
_SHELL_CONTINUATION_RE = re.compile(r"^(dquote|quote|cmdsubst|heredoc|bquote)>\s*$|^>\s*$")


class AgentMessenger:
    """Delivers a message to an agent via its tmux session."""

    def __init__(self, db_manager, agent_manager):
        self.db_manager = db_manager
        # FIX #2: Store reference to agent_manager instead of tmux_server
        # directly, so we always read the live tmux_server (tests reassign
        # agent_manager.tmux_server after construction).
        self._agent_manager = agent_manager

    @property
    def tmux_server(self):
        """Always read the live tmux_server from agent_manager."""
        return self._agent_manager.tmux_server

    def _pane_is_wedged(self, pane) -> bool:
        """True if the pane's most recent non-blank line is a shell
        continuation prompt rather than a normal prompt or the CLI's own
        interface.

        Happens when an agent's own Bash tool call leaves an unterminated
        quote/paren/heredoc: the underlying shell blocks waiting for the
        closing token and never returns control to the CLI. Sending a
        nudge into that pane with send_keys just types the message as more
        garbage input into the stuck shell -- it never reaches the agent.
        Observed live: three stacked Guardian "stuck or looping" nudges,
        none of which landed, because each one only extended the same
        dquote> continuation instead of being read by the CLI.
        """
        try:
            lines = pane.cmd("capture-pane", "-p", "-S", "-5").stdout
        except Exception:
            return False
        for line in reversed(lines or []):
            stripped = line.strip()
            if stripped:
                return bool(_SHELL_CONTINUATION_RE.match(stripped))
        return False

    async def send_message_to_agent(
        self, agent_id: str, message: str, session=None
    ) -> None:
        """Send a message to an agent's tmux session.

        Args:
            agent_id: Agent ID
            message: Message to send
            session: optional existing session to participate in the
                caller's own transaction (e.g. AgentManager.
                broadcast_message_to_all_agents/send_direct_message, which
                add an AgentLog entry per recipient in their own session)
                instead of opening/committing/closing a separate one. When
                supplied, the caller owns commit/rollback/close.
        """
        import asyncio
        import functools

        from src.core.database import Agent
        from src.interfaces import get_cli_agent

        # Every blocking call below (DB session I/O, tmux capture-pane/
        # send-keys shell-outs) is individually offloaded via run_in_executor
        # rather than wrapping the whole method in one executor call -- the
        # wedge-recovery and submit-confirmation steps need real, non-
        # blocking asyncio.sleep() pauses interleaved between them so other
        # coroutines keep running during those waits.
        loop = asyncio.get_event_loop()

        owns_session = session is None
        if owns_session:
            session = self.db_manager.get_session()
        try:
            agent = await loop.run_in_executor(
                None, lambda: session.query(Agent).filter_by(id=agent_id).first()
            )
            if not agent or not agent.tmux_session_name:
                logger.warning(f"Agent {agent_id} not found or no tmux session")
                return

            logger.debug(f"Sending message to tmux session: {agent.tmux_session_name}")

            has_session = await loop.run_in_executor(
                None, self.tmux_server.has_session, agent.tmux_session_name
            )
            logger.debug(f"has_session({agent.tmux_session_name}) = {has_session}")
            if not has_session:
                logger.warning(f"Tmux session {agent.tmux_session_name} not found")
                return

            # remain-on-exit keeps a crashed pane's session alive for
            # evidence, so has_session alone no longer implies "agent
            # alive" -- without this, a message gets silently sent into a
            # dead pane instead of surfacing that the agent is gone.
            pane_dead = await loop.run_in_executor(
                None, self._agent_manager.is_pane_dead, agent.tmux_session_name
            )
            if pane_dead:
                logger.warning(f"Tmux session {agent.tmux_session_name}'s pane is dead -- agent process has exited")
                return

            logger.debug(
                f"Finding session by iteration for message: {agent.tmux_session_name}"
            )
            tmux_session = None
            for tmux_sess in self.tmux_server.sessions:
                if tmux_sess.name == agent.tmux_session_name:
                    tmux_session = tmux_sess
                    break

            logger.debug(f"Session iteration result for message: {tmux_session}")
            if not tmux_session:
                logger.warning(f"Could not get tmux session {agent.tmux_session_name}")
                return

            # Get CLI agent interface
            cli_agent = get_cli_agent(agent.cli_type)
            formatted_message = cli_agent.format_message(message)

            # Send message
            pane = tmux_session.attached_window.attached_pane

            if await loop.run_in_executor(None, self._pane_is_wedged, pane):
                logger.warning(
                    f"Pane for agent {agent_id} is wedged in a shell continuation "
                    f"prompt (unterminated quote/paren from a prior command) -- "
                    f"sending Ctrl-C to break out before delivering the message, "
                    f"otherwise it would just be typed into the stuck shell as more "
                    f"garbage input and never reach the agent."
                )
                await loop.run_in_executor(
                    None, functools.partial(pane.send_keys, "C-c", enter=False)
                )
                await asyncio.sleep(0.5)
                if await loop.run_in_executor(None, self._pane_is_wedged, pane):
                    logger.warning(
                        f"Pane for agent {agent_id} still wedged after Ctrl-C -- "
                        f"sending the message anyway as a best effort."
                    )

            # Escape special shell characters to prevent glob/syntax errors
            # Wrap in quotes to prevent shell interpretation of [, ], etc.
            escaped_message = (
                formatted_message.replace('"', '\\"')
                .replace("$", "\\$")
                .replace("`", "\\`")
            )
            await loop.run_in_executor(
                None,
                functools.partial(pane.send_keys, f'"{escaped_message}"', enter=True),
            )

            # Wait a moment then send Enter to ensure message is submitted
            await asyncio.sleep(1)
            await loop.run_in_executor(
                None, functools.partial(pane.send_keys, "", enter=True)
            )

            # Verify delivery actually registered, retrying with a bare
            # Enter (not retyping -- the text is presumably still correctly
            # sitting in the input box, only the submit didn't take) if it
            # didn't. See CLIAgentInterface.message_queued_confirmation_
            # pattern's docstring for the live incident this closes: a
            # message sent while the CLI was deep in a long tool-call wait
            # left the raw text sitting inert, byte-for-byte identical
            # across dozens of screen redraws, with no visible error and
            # the caller blindly reporting success. Best-effort and fully
            # isolated in its own try/except -- this is a hardening layer
            # on top of the send above, not a new failure mode: any error
            # here (including a misbehaving/mocked cli_agent) must never
            # stop the last-activity update below from running.
            try:
                confirmation_pattern = cli_agent.message_queued_confirmation_pattern()
            except Exception:
                confirmation_pattern = None
            if isinstance(confirmation_pattern, str) and confirmation_pattern:
                try:
                    delivered = False
                    for attempt in range(3):
                        await asyncio.sleep(1)
                        captured = await loop.run_in_executor(
                            None,
                            functools.partial(pane.cmd, "capture-pane", "-p", "-S", "-20"),
                        )
                        pane_text = "\n".join(captured.stdout or [])
                        if re.search(confirmation_pattern, pane_text):
                            delivered = True
                            break
                        if formatted_message[:40] not in pane_text:
                            # Our own text is no longer visible sitting as
                            # raw unsubmitted input -- most likely the CLI
                            # was idle and processed it immediately rather
                            # than showing a "queued behind an active turn"
                            # hint. Nothing left to retry against.
                            delivered = True
                            break
                        if attempt < 2:
                            logger.warning(
                                f"Message to agent {agent_id} doesn't look queued yet "
                                f"(attempt {attempt + 1}/3) -- retrying submit"
                            )
                            await loop.run_in_executor(
                                None, functools.partial(pane.send_keys, "", enter=True)
                            )
                    if not delivered:
                        logger.error(
                            f"Message to agent {agent_id} still doesn't look queued "
                            "after 3 submit attempts -- it may not have been delivered"
                        )
                except Exception as verify_err:
                    logger.debug(
                        f"Delivery verification for agent {agent_id} itself "
                        f"failed (message was still sent above): {verify_err}"
                    )

            # Update last activity, and flag that a message is now sitting
            # with this agent -- Terminator.terminate_agent's grace-period
            # check reads this so a task completing (and terminating this
            # agent) right after a message was sent doesn't kill it before
            # it ever had a chance to notice.
            now = utc_now()
            agent.last_activity = now
            agent.pending_message_sent_at = now
            if owns_session:
                await loop.run_in_executor(None, session.commit)

            logger.debug(f"Sent message to agent {agent_id}")

        except Exception as e:
            logger.error(f"Failed to send message to agent: {e}")
            if owns_session:
                await loop.run_in_executor(None, session.rollback)
            else:
                raise
        finally:
            if owns_session:
                await loop.run_in_executor(None, session.close)
