"""Agent termination — graceful shutdown, WIP commit, tmux cleanup. Extracted from AgentManager per design_docs/manager_py_decomposition_prompt.md."""

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.constants import CONTEXT_DIR_NAME
from src.core.database import (
    Agent,
    AgentLog,
    Task,
)

logger = logging.getLogger(__name__)

# How long to hold off killing an agent's tmux session after a message was
# just sent to it (Agent.pending_message_sent_at, set by AgentMessenger.
# send_message_to_agent) -- see _terminate_agent_sync's grace-period check.
# Unconditional fixed wait, not a poll for an actual response: detecting
# whether the agent addressed the message is a much harder, fuzzy problem
# the messaging hardening doesn't attempt to solve. Confirmed live (agent
# 335b2a1d, 2026-08-21): a task can reach genuine completion and trigger
# termination moments after a message was sent, with the agent never
# getting a chance to even notice it.
PENDING_MESSAGE_GRACE_SECONDS = 60


class Terminator:
    """Agent termination — graceful shutdown, WIP commit, tmux cleanup. Extracted from AgentManager per design_docs/manager_py_decomposition_prompt.md."""

    def __init__(self, agent_manager):
        self._agent_manager = agent_manager

    @property
    def db_manager(self):
        return self._agent_manager.db_manager

    @property
    def config(self):
        return self._agent_manager.config

    @property
    def branch_manager(self):
        return self._agent_manager.branch_manager

    @property
    def tmux_server(self):
        return self._agent_manager.tmux_server

    @property
    def llm_provider(self):
        return self._agent_manager.llm_provider

    @property
    def phase_manager(self):
        return self._agent_manager.phase_manager

    @property
    def _messenger(self):
        return self._agent_manager._messenger

    @property
    def _prompt_builder(self):
        return self._agent_manager._prompt_builder

    @property
    def _output_capture(self):
        return self._agent_manager._output_capture

    async def terminate_agent(self, agent_id: str):
        """Terminate an agent and clean up resources.

        Args:
            agent_id: ID of agent to terminate
        """
        # The real work (_terminate_agent_sync) is a long synchronous
        # chain: several tmux/git subprocess calls, a full capture-pane
        # scrollback read, a time.sleep(1) between SIGINT and SIGKILL, and
        # collect_task_cost's own DB+file cascade -- called directly here
        # (no executor anywhere in this file, confirmed live 2026-08-19
        # investigating intermittent multi-second /health stalls), every
        # one of those blocks the single-threaded asyncio event loop for
        # its full duration, on every task completion. Offloading the
        # whole method to a worker thread also makes the time.sleep(1)
        # harmless for free: blocking a worker thread is what it's for --
        # the problem was only ever blocking the loop's own thread.
        import asyncio

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._terminate_agent_sync, agent_id)

    def _wait_for_pane_idle(self, pane, cli_type: str, poll_interval: float = 0.5) -> None:
        """Poll capture-pane until it matches the CLI's own idle/ready
        pattern (CLIAgentInterface.get_health_check_pattern() -- the same
        "back at the input prompt" signal _wait_for_cli_ready polls for at
        startup, e.g. Claude Code's "›") or agents.termination_delay
        (hephaestus_config.yaml) elapses, whichever comes first.

        The shutdown-side mirror of _wait_for_cli_ready's startup polling:
        same idea (poll for a real signal instead of blocking a fixed
        amount of time), opposite transition (idle-after-a-turn instead of
        ready-for-the-first-prompt). agents.termination_delay is the
        ceiling here, not a flat wait -- an idle pattern match returns
        immediately, so a fast turn doesn't pay the full delay.
        """
        import re

        from src.core.simple_config import get_config
        from src.interfaces.cli_interface import get_cli_agent

        timeout = get_config().agents.agent_termination_delay
        try:
            pattern = get_cli_agent(cli_type).get_health_check_pattern()
        except Exception:
            # Unknown/unsupported cli_type -- no pattern to poll for, fall
            # back to the flat wait this replaces.
            time.sleep(timeout)
            return

        max_polls = max(1, int(timeout / poll_interval))
        for _ in range(max_polls):
            try:
                captured = pane.cmd("capture-pane", "-p", "-S", "-10")
                text = "\n".join(captured.stdout) if captured.stdout else ""
            except Exception:
                text = ""
            if text and re.search(pattern, text):
                return
            time.sleep(poll_interval)

    def _terminate_agent_sync(self, agent_id: str) -> None:
        logger.info(f"Terminating agent {agent_id}")

        session = self.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                logger.warning(f"Agent {agent_id} not found")
                return
            if agent.status == "terminated":
                # Idempotency guard, not a full fix for the underlying race:
                # this call and another terminate_agent(agent_id) call for
                # the SAME agent can both pass this check before either
                # commits its own terminated status (a classic check-then-
                # act race, since each runs in its own executor thread with
                # its own DB session) -- so this narrows the window rather
                # than closing it outright. Added for monitor.py's new
                # detect_zombie_agent: it can legitimately fire in the brief
                # gap between a task's completion handler committing
                # task.status="done" and that SAME handler's own (now
                # correctly non-dropped, see c1cc687) termination call
                # actually finishing. Without this, that overlap redundantly
                # re-runs the WIP commit, tmux/subprocess kills, and cost
                # collection below a second time for no benefit --
                # collect_task_cost's own per-session checkpoint already
                # makes ITS specific double-call harmless, but nothing else
                # here was.
                logger.debug(f"Agent {agent_id} already terminated -- skipping duplicate termination")
                return

            # Grace period: a message sent to this agent shortly before
            # termination was requested hasn't had a chance to be noticed
            # yet -- give it up to PENDING_MESSAGE_GRACE_SECONDS from when
            # it was sent (not a flat extra wait every time) before killing
            # its tmux session out from under it. Cleared and committed
            # before the wait so a crash/restart mid-sleep, or a second
            # concurrent termination attempt, doesn't re-trigger it.
            if agent.pending_message_sent_at:
                elapsed = (datetime.utcnow() - agent.pending_message_sent_at).total_seconds()
                remaining = PENDING_MESSAGE_GRACE_SECONDS - elapsed
                agent.pending_message_sent_at = None
                session.commit()
                if remaining > 0:
                    logger.info(
                        f"[TERMINATE] Agent {agent_id[:8]} has a message sent "
                        f"{elapsed:.0f}s ago -- waiting {remaining:.0f}s more "
                        "before terminating"
                    )
                    time.sleep(remaining)

            # Preserve any uncommitted work before teardown so a kill/restart is
            # non-destructive — a resume then continues from the committed branch
            # state instead of losing in-flight work. No-op if already clean/merged.
            try:
                wip = self.branch_manager.commit_changes(
                    agent_id, f"[WIP] Auto-saved on terminate of agent {agent_id[:8]}"
                )
                if isinstance(wip, dict) and wip.get("files_changed"):
                    logger.info(
                        f"[TERMINATE] Saved WIP for agent {agent_id[:8]}: "
                        f"{wip['commit_sha'][:8]} ({wip['files_changed']} file(s))"
                    )
            except Exception as e:
                # commit_changes -> _agent_repo requires an AgentBranch DB
                # record keyed by agent_id -- only ever created for the
                # legacy isolated-per-agent-worktree path (validators,
                # diagnostic agents). Every normal phase agent in a feature
                # pipeline runs against the SHARED feature worktree instead
                # (create_agent_for_task's shared_worktree branch), so this
                # raises for the common case, and previously the WIP-commit
                # promise silently no-op'd here with nothing but a DEBUG
                # log. That mattered once delete_feature/remove_project_design/
                # rerun_design started force-removing worktrees right after
                # terminating their agents -- uncommitted work was gone with
                # no recovery. Fall back to committing directly in the
                # worktree the agent's own current task says it was using.
                logger.debug(
                    f"[TERMINATE] WIP commit via agent-branch path skipped for "
                    f"{agent_id[:8]}: {e} -- trying shared-worktree fallback"
                )
                self._commit_wip_in_shared_worktree(agent_id, agent.current_task_id)

            # Remove the legacy isolated-per-agent worktree's checkout, if
            # this agent has one (AgentBranch record) -- a no-op for the
            # common shared-feature-worktree case (cleanup_worktree returns
            # "not_found" when no record exists, so this is safe to call
            # unconditionally). Branch preserved (delete_branch=False) for
            # history/audit; only the on-disk checkout is removed. Without
            # this, nothing on the normal termination path ever cleans up
            # this worktree class -- the WIP-commit above only preserves
            # the work, it doesn't merge or remove anything, and
            # discard_agent's only other caller is a CLI-fallback error
            # path during agent *creation*, not completion. Observed live:
            # validator/diagnostic agents' worktrees accumulating under
            # .worktrees/ indefinitely, each one a full checkout of the repo.
            try:
                self.branch_manager.cleanup_worktree(agent_id, delete_branch=False)
            except Exception as e:
                logger.debug(f"[TERMINATE] Worktree cleanup skipped for {agent_id[:8]}: {e}")

            # Capture pane PIDs and final output BEFORE killing the tmux session.
            # Termination fires the instant complete_my_task's HTTP handler
            # returns (spawn_background_task, no delay), but the agent's own
            # CLI keeps working after that tool call resolves -- its prompt
            # explicitly tells it to "wait for confirmation, do NOT exit
            # until you see the task marked as done" -- so there's no fixed
            # settle time that's both safe and fast: long enough for a slow
            # agent turn wastes time on every fast one, and a short one
            # isn't always enough. Confirmed live: even a flat 5s
            # (agents.termination_delay, tried first) still wasn't enough --
            # a scope_review agent was captured still mid "thinking"
            # animation 6.7s after termination started. See
            # _wait_for_pane_idle below for the poll-based replacement.
            pane_pids = []
            final_output = ""
            if agent.tmux_session_name:
                try:
                    import subprocess

                    result = subprocess.run(
                        [
                            "tmux",
                            "list-panes",
                            "-t",
                            agent.tmux_session_name,
                            "-F",
                            "#{pane_pid}",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=3,
                    )
                    if result.returncode == 0:
                        pane_pids = [
                            p.strip()
                            for p in result.stdout.strip().split("\n")
                            if p.strip()
                        ]
                except Exception as e:
                    # Without pane PIDs the orphan-kill below is skipped --
                    # child processes (opencode/claude/pi) can outlive the session.
                    logger.warning(f"Failed to collect pane PIDs for {agent.tmux_session_name}: {e}")

                try:
                    if self.tmux_server.has_session(agent.tmux_session_name):
                        for tmux_sess in self.tmux_server.sessions:
                            if tmux_sess.name == agent.tmux_session_name:
                                pane = tmux_sess.attached_window.attached_pane
                                self._wait_for_pane_idle(pane, agent.cli_type)
                                # Capture full scrollback for both tmux log and AgentLog.
                                full_scrollback = "\n".join(
                                    pane.cmd("capture-pane", "-p", "-S", "-").stdout
                                )
                                final_output = full_scrollback

                                # Write complete scrollback to .hephaestus/tmux/ (git-excluded run artifact).
                                _task = (
                                    session.query(Task)
                                    .filter_by(id=agent.current_task_id)
                                    .first()
                                )
                                if _task and _task.phase_id and _task.workflow_id:
                                    try:
                                        from src.core.database import (
                                            Phase as _Phase,
                                        )
                                        from src.core.database import (
                                            Workflow as _Workflow,
                                        )

                                        _phase = (
                                            session.query(_Phase)
                                            .filter_by(id=_task.phase_id)
                                            .first()
                                        )
                                        _wf = (
                                            session.query(_Workflow)
                                            .filter_by(id=_task.workflow_id)
                                            .first()
                                        )
                                        if _phase and _wf and _wf.working_directory:
                                            tmux_dir = (
                                                Path(_wf.working_directory)
                                                / CONTEXT_DIR_NAME
                                                / "tmux"
                                            )
                                            tmux_dir.mkdir(parents=True, exist_ok=True)
                                            log_file = (
                                                tmux_dir
                                                / f"{_phase.name}_{agent_id[:8]}.log"
                                            )

                                            clean_scrollback = full_scrollback
                                            log_file.write_text(clean_scrollback)
                                            logger.info(
                                                f"[TMUX-LOG] Final capture for "
                                                f"{_phase.name}/{agent_id[:8]}: "
                                                f"{len(clean_scrollback)} chars → {log_file.name}"
                                            )
                                    except Exception as _te:
                                        logger.debug(
                                            f"[TMUX-LOG] Final capture write failed: {_te}"
                                        )
                                break
                except Exception as e:
                    logger.debug(f"Could not capture output before terminate: {e}")

            # Final unconditional flush of the stability-tracked "clean"
            # transcript (see _flush_stable_transcript) -- must happen
            # before the session is killed below, since capture-pane can
            # no longer see anything once it's gone.
            if agent.tmux_session_name:
                try:
                    transcript_dir = self._output_capture._resolve_tmux_transcript_dir(agent)
                    if transcript_dir:
                        self._output_capture._flush_stable_transcript(
                            agent.tmux_session_name,
                            transcript_dir / f"{agent.tmux_session_name}.clean.log",
                        )
                except Exception as e:
                    logger.debug(f"[STABLE-TRANSCRIPT] Final flush failed: {e}")

            # Kill tmux session using subprocess (more reliable than libtmux)
            if agent.tmux_session_name:
                try:
                    import subprocess

                    result = subprocess.run(
                        ["tmux", "kill-session", "-t", agent.tmux_session_name],
                        capture_output=True,
                        timeout=5,
                    )
                    if result.returncode == 0:
                        logger.debug(f"Killed tmux session: {agent.tmux_session_name}")
                    else:
                        logger.debug(
                            f"Tmux session {agent.tmux_session_name} not found or already killed"
                        )
                except subprocess.TimeoutExpired:
                    logger.warning(
                        f"Timeout killing tmux session {agent.tmux_session_name}"
                    )
                except FileNotFoundError:
                    logger.debug("tmux not available, skipping session cleanup")
                except Exception as e:
                    logger.error(f"Failed to kill tmux session: {e}")

                # Kill any orphaned child processes (opencode, claude, pi) in the session
                # First try graceful SIGINT, then force SIGKILL
                for pane_pid in pane_pids:
                    try:
                        subprocess.run(["kill", "-2", "--", "-" + pane_pid], timeout=3)
                    except ProcessLookupError:
                        pass  # Process already gone
                    except Exception as e:
                        logger.warning(f"Failed to SIGINT pane pid {pane_pid}: {e}")
                if pane_pids:
                    time.sleep(1)
                for pane_pid in pane_pids:
                    try:
                        # Check if still alive before sending SIGKILL
                        result = subprocess.run(
                            ["kill", "-0", "-" + pane_pid],
                            capture_output=True,
                            timeout=3,
                        )
                        if result.returncode == 0:
                            subprocess.run(
                                ["kill", "-9", "--", "-" + pane_pid], timeout=3
                            )
                    except ProcessLookupError:
                        pass  # Process already gone
                    except Exception as e:
                        logger.warning(f"Failed to SIGKILL pane pid {pane_pid}: {e}")

            # Collect cost data before clearing agent references.
            # Once current_task_id and assigned_agent_id are cleared,
            # collect_task_cost can no longer discover the agent/session.
            if agent.current_task_id:
                try:
                    from src.services.cost_collection_service import collect_task_cost
                    collect_task_cost(agent.current_task_id)
                except Exception as e:
                    # Logged at warning, not debug (invisible at production
                    # log levels) -- billing/cost data for this agent's run
                    # is silently lost otherwise, with no visible sign it
                    # happened.
                    logger.warning(f"[COST-COLLECT] Failed on terminate for agent {agent_id[:8]}: {e}")

            # The DB half of termination -- the three-field invariant and
            # the release of any Task still pointing at this agent -- is
            # owned by engine_client.terminate_agent, so there is exactly
            # one implementation of it (Phase 2 §4.2). This method remains
            # the kill_tmux=True half: WIP commit, transcript capture,
            # SIGINT/SIGKILL, and the cost collection above, which must run
            # before the primitive clears current_task_id.
            #
            # The task release matters even though well-behaved callers
            # already reset their own task first: it only fires for a
            # caller that forgot. Observed live, a task sat "in_progress"
            # pointing at an already-terminated agent indefinitely --
            # current_task_id was correctly cleared on the agent side, so
            # that half of the invariant looked satisfied, but nothing ever
            # reset the task and no dispatch path picked it up again.
            from src.autopilot.orchestrator.engine_client import terminate_agent

            terminate_agent(agent_id, session=session)

            # Log termination with captured output
            log_entry = AgentLog(
                agent_id=agent_id,
                log_type="terminated",
                message="Agent terminated",
                details={
                    "terminated_at": datetime.utcnow().isoformat(),
                    "final_output": final_output,
                },
            )
            session.add(log_entry)

            session.commit()
            logger.info(f"Agent {agent_id} terminated successfully")

        except Exception as e:
            logger.error(f"Failed to terminate agent {agent_id}: {e}")
            session.rollback()
        finally:
            session.close()

    def _commit_wip_in_shared_worktree(self, agent_id: str, task_id: Optional[str]) -> None:
        """WIP-preservation fallback for agents on a shared feature worktree
        -- see the comment at its call site in terminate_agent for why
        commit_changes/_agent_repo can't reach these. Resolves the worktree
        via the agent's current task's workflow (the same working_directory
        every phase agent on that workflow shares) and commits directly,
        bypassing the AgentBranch/agent_worktrees machinery entirely. Best-
        effort: logs and returns on any failure rather than blocking
        termination on it.
        """
        if not task_id:
            return
        try:
            from src.core.database import Workflow

            session = self.db_manager.get_session()
            try:
                task = session.query(Task).filter_by(id=task_id).first()
                working_directory = None
                if task and task.workflow_id:
                    wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
                    if wf:
                        working_directory = wf.working_directory
            finally:
                session.close()

            if not working_directory or not Path(working_directory).exists():
                return

            import git as _git

            repo = _git.Repo(working_directory)
            repo.git.add("-A")
            if not repo.is_dirty() and not repo.untracked_files:
                return
            repo.git.commit(
                "-m", f"[Agent {agent_id}] [WIP] Auto-saved on terminate", "--no-verify"
            )
            logger.info(
                f"[TERMINATE] Saved WIP for shared-worktree agent {agent_id[:8]} "
                f"in {working_directory}"
            )
        except Exception as e:
            logger.warning(
                f"[TERMINATE] Shared-worktree WIP commit also failed for {agent_id[:8]}: {e}"
            )

