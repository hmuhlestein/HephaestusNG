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
        logger.info(f"Terminating agent {agent_id}")

        session = self.db_manager.get_session()
        try:
            agent = session.query(Agent).filter_by(id=agent_id).first()
            if not agent:
                logger.warning(f"Agent {agent_id} not found")
                return

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

            # Capture pane PIDs and final output BEFORE killing the tmux session
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
                except Exception:
                    pass

                try:
                    if self.tmux_server.has_session(agent.tmux_session_name):
                        for tmux_sess in self.tmux_server.sessions:
                            if tmux_sess.name == agent.tmux_session_name:
                                pane = tmux_sess.attached_window.attached_pane
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
                    transcript_dir = self._resolve_tmux_transcript_dir(agent)
                    if transcript_dir:
                        self._flush_stable_transcript(
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
                    except Exception:
                        pass
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
                    except Exception:
                        pass

            # Collect cost data before clearing agent references.
            # Once current_task_id and assigned_agent_id are cleared,
            # collect_task_cost can no longer discover the agent/session.
            if agent.current_task_id:
                try:
                    from src.services.cost_collection_service import collect_task_cost
                    collect_task_cost(agent.current_task_id)
                except Exception as e:
                    logger.debug(f"[COST-COLLECT] Failed on terminate for agent {agent_id[:8]}: {e}")

            # Update agent status
            agent.status = "terminated"
            agent.current_task_id = None  # Clear stale reference
            agent.terminated_at = datetime.utcnow()

            # Safety net: release any task still pointing at this
            # now-terminated agent. Every well-behaved caller already
            # resets its own task's status/assigned_agent_id before
            # calling terminate_agent (e.g. the session-limit and
            # connection-error fallback paths in monitor.py) -- by the
            # time we get here, assigned_agent_id no longer points at
            # this agent, so this is a no-op for them. It only fires for
            # a caller that forgot, closing the gap at the shared
            # primitive instead of requiring every one of terminate_agent's
            # ~15 call sites to remember it. Observed live: a task sat
            # "in_progress" pointing at an already-terminated agent
            # indefinitely -- current_task_id was correctly cleared on the
            # agent side (satisfying that half of the termination
            # invariant) but nothing ever reset the task, so no dispatch
            # path ever picked it up again.
            stray_tasks = (
                session.query(Task)
                .filter_by(assigned_agent_id=agent_id)
                .filter(Task.status.in_(["assigned", "in_progress", "pending"]))
                .all()
            )
            for stray in stray_tasks:
                logger.warning(
                    f"[TERMINATE] Task {stray.id[:8]} still pointed at "
                    f"terminated agent {agent_id[:8]} -- resetting to pending"
                )
                stray.status = "pending"
                stray.assigned_agent_id = None

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

