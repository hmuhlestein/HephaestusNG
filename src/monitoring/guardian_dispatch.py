"""Guardian trajectory analysis and related helpers.

Extracted from MonitoringLoop: cluster C — Guardian dispatch for each
active agent, plus supporting helpers (past-summary retrieval, trajectory
health tracking, missing-tmux-session handling, tmux-log persistence).

Both this cluster and cluster B (mechanical recovery) call AutoRestart
for the same reason: an agent that ignores steering or shows a terminal
failure pattern needs its tmux session killed and its task reset. The
AutoRestart collaborator owns that logic.

See docs/SOLID_OO_REVIEW.md and design_docs/phase_1b_decomposition.md §4.3.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.core.constants import CONTEXT_DIR_NAME, WORKTREES_SUBDIR
from src.core.database import (
    Agent,
    AgentLog,
    GuardianAnalysis,
    Task,
)

logger = logging.getLogger(__name__)


class GuardianDispatcher:
    """Per-agent Guardian trajectory analysis and steering."""

    def __init__(
        self,
        db_manager,
        agent_manager,
        config,
        guardian,
        phase_manager,
        auto_restart,
        trajectory_context,
        guardian_summaries_cache,
    ):
        self.db_manager = db_manager
        self.agent_manager = agent_manager
        self.config = config
        self.guardian = guardian
        self.phase_manager = phase_manager
        self._auto_restart = auto_restart
        self.trajectory_context = trajectory_context
        self.guardian_summaries_cache = guardian_summaries_cache

    async def guardian_analysis_for_agent(
        self, agent: Agent
    ) -> Optional[Dict[str, Any]]:
        """Perform Guardian analysis for a single agent.

        Args:
            agent: Agent to analyze

        Returns:
            Guardian analysis result or None if failed
        """
        from src.core.log_context import set_log_context
        set_log_context(agent=agent.id, task=agent.current_task_id or "")
        session = self.db_manager.get_session()
        try:
            # Skip agents that are too young (grace period for spin-up)
            agent_age_seconds = (datetime.utcnow() - agent.created_at).total_seconds()
            if agent_age_seconds < self.config.guardian_min_agent_age_seconds:
                logger.debug(
                    f"Skipping Guardian analysis for agent {agent.id} "
                    f"(age: {agent_age_seconds:.0f}s, min: {self.config.guardian_min_agent_age_seconds}s)"
                )
                return None

            # The orchestrator runs in-process (AutopilotService), not as a tmux
            # agent — never health-check or "recreate" it for a missing tmux session
            # (that was a 60s phantom-restart loop after the Tier 2 in-process move).
            if agent.agent_type == "orchestrator":
                logger.debug(
                    f"Skipping orchestrator agent {agent.id[:8]} (runs in-process)"
                )
                return None

            # Special handling for agents with missing tmux sessions
            if (
                agent.tmux_session_name
                and not self.agent_manager.tmux_server.has_session(
                    agent.tmux_session_name
                )
            ):
                # Check if task is already done before restarting
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == "done":
                    logger.info(
                        f"Agent {agent.id} has missing tmux session but task {task.id[:8]} is done — not restarting"
                    )
                    return None
                logger.warning(
                    f"Agent {agent.id} has missing tmux session {agent.tmux_session_name}, recreating"
                )
                await self.handle_missing_tmux_session(agent)
                return None

            # Get agent output
            tmux_output = self.agent_manager.get_agent_output(
                agent.id,
                lines=self.config.tmux_output_lines,
            )

            if not tmux_output:
                logger.warning(f"No output from agent {agent.id}")
                return None

            # Persist scrollback to docs/tmux/ so the forensics agent can read it.
            # Use the session already open in this try block to avoid a second round-trip.
            if agent.current_task_id:
                try:
                    from src.core.database import Phase as _Phase

                    _task = (
                        session.query(Task).filter_by(id=agent.current_task_id).first()
                    )
                    if _task and _task.phase_id:
                        _phase = (
                            session.query(_Phase).filter_by(id=_task.phase_id).first()
                        )
                        if _phase:
                            self.write_agent_tmux_log(
                                agent.id, _phase.name, tmux_output
                            )
                except Exception:
                    pass  # non-fatal; don't interrupt the monitoring cycle

            # DETECT: Agent exited to command line (shows $, %, >>>, bquote>)
            if self.guardian.detect_agent_exited(tmux_output):
                # Check if task is already done before restarting
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == "done":
                    logger.info(
                        f"Agent {agent.id[:8]} exited but task {task.id[:8]} is done — not restarting"
                    )
                    return None
                logger.warning(
                    f"Agent {agent.id[:8]} exited to command line — restarting"
                )
                await self.handle_missing_tmux_session(agent)
                return None

            # Detect garbled TUI output (CLI rendering corruption)
            # Get TUI status patterns from this agent's own CLI interface --
            # not a global default, since a mixed fleet (e.g. pi + claude)
            # would otherwise check every agent's output against pi's
            # patterns regardless of what CLI it's actually running.
            tui_patterns = None
            try:
                from src.interfaces.cli_interface import get_cli_agent

                cli_agent = get_cli_agent(agent.cli_type)
                tui_patterns = cli_agent.get_tui_status_patterns()
            except Exception:
                pass  # No CLI agent configured — use no patterns (strictest check)
            if self.guardian.detect_garbled_output(
                tmux_output, tui_patterns=tui_patterns
            ):
                task = session.query(Task).filter_by(id=agent.current_task_id).first()
                if task and task.status == "done":
                    logger.info(
                        f"Agent {agent.id[:8]} garbled but task done — not restarting"
                    )
                    return None
                logger.warning(
                    f"Agent {agent.id[:8]} has garbled TUI output — restarting"
                )
                await self.handle_missing_tmux_session(agent)
                return None

            # Get past summaries for this agent
            past_summaries = self.get_past_summaries_for_agent(agent.id)

            # Perform Guardian analysis with trajectory thinking
            analysis = await self.guardian.analyze_agent_with_trajectory(
                agent=agent,
                tmux_output=tmux_output,
                past_summaries=past_summaries,
            )

            # Cache the summary
            self.guardian_summaries_cache[agent.id] = {
                "summary": analysis,
                "timestamp": datetime.utcnow(),
            }

            # Execute steering if needed
            if analysis.get("needs_steering", False):
                # Compute consecutive-stuck count up front so it's available
                # both for the signal emitted below and the auto-restart
                # check further down (previously computed after the signal
                # was emitted, so the signal's metadata always saw a
                # not-yet-assigned value and silently reported 0).
                past = self.get_past_summaries_for_agent(agent.id, limit=5)
                consecutive_stuck = sum(
                    1
                    for s in past
                    if s.get("needs_steering")
                    and s.get("steering_type") in ("stuck", "idle")
                )

                # Enhancement 4: Emit signal to orchestrator
                from src.monitoring.signals import (
                    MonitorSignal,
                    SignalType,
                    get_signal_queue,
                )

                steering_type = analysis.get("steering_type", "general")
                signal_type_map = {
                    "stuck": SignalType.STUCK_PATTERN,
                    "idle": SignalType.STUCK_PATTERN,
                    "drifting": SignalType.TRAJECTORY_DEVIATION,
                    "off_track": SignalType.TRAJECTORY_DEVIATION,
                    "over_engineering": SignalType.TRAJECTORY_DEVIATION,
                }
                signal_type = signal_type_map.get(
                    steering_type, SignalType.STUCK_PATTERN
                )
                task = await self.guardian._get_agent_task(agent)
                workflow_id = task.get("workflow_id") if task else None
                if workflow_id:
                    get_signal_queue().emit(
                        MonitorSignal(
                            type=signal_type,
                            workflow_id=workflow_id,
                            agent_id=agent.id,
                            confidence=0.7,
                            evidence=f"Guardian detected {steering_type}: "
                            f"{analysis.get('summary', '')[:100]}",
                            metadata={
                                "steering_type": steering_type,
                                "consecutive_flags": consecutive_stuck,
                            },
                        )
                    )

                await self.guardian.steer_agent(
                    agent=agent,
                    steering_type=analysis.get("steering_type", "general"),
                    message=analysis.get(
                        "steering_message"
                    ),  # Guardian should map from steering_recommendation
                )

                # Auto-restart if agent keeps ignoring steering
                if consecutive_stuck >= self.config.max_ignored_steering:
                    # Check if agent has recent activity before restarting
                    if agent.last_activity:
                        idle_seconds = (
                            datetime.utcnow() - agent.last_activity
                        ).total_seconds()
                        if idle_seconds < 300:
                            logger.info(
                                f"Agent {agent.id[:8]} marked stuck but was active {idle_seconds:.0f}s ago — not restarting"
                            )
                        else:
                            logger.warning(
                                f"Agent {agent.id[:8]} ignored steering {consecutive_stuck} times. "
                                f"Auto-restarting..."
                            )
                            await self._auto_restart.restart_agent(agent)
                    else:
                        logger.warning(
                            f"Agent {agent.id[:8]} ignored steering {consecutive_stuck} times. "
                            f"Auto-restarting..."
                        )
                        await self._auto_restart.restart_agent(agent)

            # Update agent health based on trajectory alignment
            await self.update_agent_health_from_trajectory(agent, analysis)

            return analysis

        except Exception as e:
            logger.error(f"Guardian analysis failed for agent {agent.id}: {e}")
            return None
        finally:
            session.close()


    def get_past_summaries_for_agent(
        self, agent_id: str, limit: int = 10
    ) -> List[Dict[str, Any]]:
        """Get past Guardian summaries for an agent.

        Args:
            agent_id: Agent ID
            limit: Maximum number of summaries to return

        Returns:
            List of past summaries
        """
        with self.db_manager.session_scope() as session:
            # Get past Guardian summaries from dedicated table
            analyses = (
                session.query(GuardianAnalysis)
                .filter(GuardianAnalysis.agent_id == agent_id)
                .order_by(GuardianAnalysis.timestamp.desc())
                .limit(limit)
                .all()
            )

            summaries = []
            for analysis in reversed(analyses):  # Reverse to get chronological order
                # Convert to dict format expected by Guardian
                summary = {
                    "current_phase": analysis.current_phase,
                    "trajectory_aligned": analysis.trajectory_aligned,
                    "alignment_score": analysis.alignment_score,
                    "needs_steering": analysis.needs_steering,
                    "steering_type": analysis.steering_type,
                    "trajectory_summary": analysis.trajectory_summary,
                    "accumulated_goal": analysis.accumulated_goal,
                    "timestamp": analysis.timestamp.isoformat()
                    if analysis.timestamp
                    else None,
                }
                summaries.append(summary)

            # If new tables don't have data yet, fallback to old AgentLog method
            if not summaries:
                logs = (
                    session.query(AgentLog)
                    .filter(
                        AgentLog.agent_id == agent_id,
                        AgentLog.log_type.in_(
                            ["guardian_analysis", "guardian_summary"]
                        ),
                    )
                    .order_by(AgentLog.created_at.desc())
                    .limit(limit)
                    .all()
                )

                for log in reversed(logs):
                    if log.details:
                        summaries.append(log.details)

            return summaries


    async def update_agent_health_from_trajectory(
        self, agent: Agent, analysis: Dict[str, Any]
    ):
        """Update agent health based on trajectory analysis.

        PARENT-CHILD MODEL: Parent monitors via tmux peek and task progress.
        Guardian trajectory analysis is a signal for last-resort steering.
        health_check_failures is incremented when trajectory is off-track,
        so the Guardian can decide whether to intervene.
        """
        with self.db_manager.session_scope() as session:
            db_agent = session.query(Agent).filter_by(id=agent.id).first()
            if not db_agent:
                return

            # Track health_check_failures for Guardian last-resort steering
            if analysis.get("trajectory_aligned", True):
                # Agent is on track — reset failures so it recovers. This
                # also counts as real progress, mirroring the mechanical-
                # recovery detector's "tmux output changed" touch above:
                # refresh last_activity.
                db_agent.health_check_failures = 0
                db_agent.last_activity = datetime.utcnow()
            else:
                alignment_score = analysis.get("alignment_score", 0.5)
                if alignment_score < 0.3:
                    db_agent.health_check_failures += 2
                elif alignment_score < 0.5:
                    db_agent.health_check_failures += 1
                # Deliberately NOT touching last_activity here. Doing so
                # unconditionally (on every Guardian cycle, aligned or not)
                # defeated the max_ignored_steering auto-restart check
                # above: a persistently stuck agent that keeps failing
                # trajectory analysis would look "recently active" one
                # cycle later purely because Guardian ran, not because it
                # made progress -- silently disabling the restart's
                # idle_seconds >= 300 gate.

            # Save to dedicated Guardian analysis table
            guardian_analysis = GuardianAnalysis(
                agent_id=agent.id,
                current_phase=analysis.get("current_phase"),
                trajectory_aligned=analysis.get("trajectory_aligned", True),
                alignment_score=analysis.get("alignment_score", 1.0),
                needs_steering=analysis.get("needs_steering", False),
                steering_type=analysis.get("steering_type"),
                steering_recommendation=analysis.get("steering_recommendation"),
                trajectory_summary=analysis.get("trajectory_summary", "No summary"),
                last_claude_message_marker=analysis.get(
                    "last_claude_message_marker"
                ),  # NEW
                accumulated_goal=analysis.get("accumulated_goal"),
                current_focus=analysis.get("current_focus"),
                session_duration=analysis.get("session_duration"),
                conversation_length=analysis.get("conversation_length"),
                details=analysis,
            )
            session.add(guardian_analysis)

            # Also keep a simplified log entry for backwards compatibility
            summary_log = AgentLog(
                agent_id=agent.id,
                log_type="guardian_analysis",
                message=f"Guardian: {analysis.get('current_phase', 'unknown')} phase, "
                f"score={analysis.get('alignment_score', 0):.2f}, "
                f"aligned={analysis.get('trajectory_aligned', False)}",
                details={
                    "guardian_analysis_id": guardian_analysis.id
                },  # Reference to the full analysis
            )
            session.add(summary_log)


    async def handle_missing_tmux_session(self, agent: Agent):
        """Handle an agent with a missing tmux session by restarting it.

        Args:
            agent: Agent with missing tmux session
        """
        logger.info(f"Handling missing tmux session for agent {agent.id}")

        # Use the restart agent functionality which will recreate the tmux session
        await self.agent_manager.restart_agent(
            agent.id, f"Tmux session {agent.tmux_session_name} was missing, recreating"
        )


    def write_agent_tmux_log(
        self, agent_id: str, phase_name: str, tmux_output: str
    ) -> None:
        """Write the agent's full tmux scrollback to docs/tmux/<phase>_<agent_id>.log.

        Called on every monitor cycle — overwrites so the file always contains
        the complete captured session up to the most recent poll. The forensics
        phase reads these files for a full picture of what each agent did.
        """
        if (
            not tmux_output
            or not self.phase_manager
            or not self.phase_manager.workflow_id
        ):
            return
        try:
            from pathlib import Path

            from src.core.database import Workflow

            session = self.db_manager.get_session()
            try:
                wf = (
                    session.query(Workflow)
                    .filter_by(id=self.phase_manager.workflow_id)
                    .first()
                )
                wd = wf.working_directory if wf else None
            finally:
                session.close()

            if not wd:
                return

            # Resolve to project root so logs survive worktree removal.
            # The working_directory may be a worktree (.worktrees/wt_*);
            # walk up past the .worktrees dir to get the stable project root.
            wd_path = Path(wd)
            if WORKTREES_SUBDIR in wd_path.parts:
                for parent in wd_path.parents:
                    if parent.name == WORKTREES_SUBDIR:
                        wd_path = parent.parent
                        break

            # .hephaestus/ is git-excluded — run artifacts never get committed
            tmux_dir = wd_path / CONTEXT_DIR_NAME / "tmux"
            tmux_dir.mkdir(parents=True, exist_ok=True)
            log_file = tmux_dir / f"{phase_name}_{agent_id[:8]}.log"
            log_file.write_text(tmux_output)
            logger.debug(
                f"[TMUX-LOG] {phase_name}/{agent_id[:8]}: wrote {len(tmux_output)} chars"
            )

            # Update the manifest so forensics can enumerate logs without ls truncation.
            import json as _json

            manifest_path = tmux_dir / "tmux_log_manifest.json"
            manifest: dict = {}
            if manifest_path.exists():
                try:
                    manifest = _json.loads(manifest_path.read_text())
                except Exception:
                    manifest = {}
            manifest[f"{phase_name}_{agent_id[:8]}"] = str(log_file)
            manifest_path.write_text(_json.dumps(manifest, indent=2))
        except Exception as e:
            logger.error(f"[TMUX-LOG] Failed to write log for {agent_id[:8]}: {e}")
