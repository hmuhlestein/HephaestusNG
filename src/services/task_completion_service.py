"""Task-completion side effects: memory persistence, output-artifact
validation, spec-gate firing, validator spawning, and git commit + ticket
linking.

Extracted from src/mcp/server.py's update_task_status, a single 450-line
route handler that fused all five of these concerns inline — see
docs/SOLID_OO_REVIEW.md finding 1.2. Each method here corresponds 1:1 to a
step of that handler, moved verbatim (not redesigned) to keep this a
low-risk extraction; the handler itself now just sequences these calls.
"""

import asyncio
import logging
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class TaskCompletionService:
    """Side effects triggered when an agent reports a task's status."""

    @staticmethod
    async def record_learnings(
        session,
        agent_id: str,
        task_id: str,
        key_learnings: list,
        code_changes: list,
    ) -> None:
        """Embed and persist each reported learning as a Memory."""
        from src.core.app_context import get_app_state
        from src.core.database import Memory

        server_state = get_app_state()

        for learning in key_learnings:
            embedding = await server_state.llm_provider.generate_embedding(learning)

            memory_id = str(uuid.uuid4())
            await server_state.vector_store.store_memory(
                collection="agent_memories",
                memory_id=memory_id,
                embedding=embedding,
                content=learning,
                metadata={
                    "agent_id": agent_id,
                    "task_id": task_id,
                    "memory_type": "learning",
                    "code_changes": code_changes,
                },
            )

            memory = Memory(
                id=memory_id,
                agent_id=agent_id,
                content=learning,
                memory_type="learning",
                embedding_id=memory_id,
                related_task_id=task_id,
                related_files=code_changes,
            )
            session.add(memory)

    @staticmethod
    def verify_output_artifact(session, task, phase=None) -> Optional[Dict[str, Any]]:
        """Output-existence hard floor: reject 'done' when any of the phase's
        own YAML-declared output files (Phase.outputs) is missing from the
        worktree/feature folder.

        Every phase with at least one checkable declared output gets this
        floor now, not just a hardcoded handful — a phase can no longer
        silently skip producing its report with zero consequence.

        Args:
            phase: Pass the caller's already-fetched Phase row to skip
                re-querying it (update_task_status's self-review gate fetches
                the same row moments earlier for the same task_id). If not
                given, fetched here as before.

        Returns a rejection response dict (already committed to DB) if a
        required file is missing, else None (caller should continue).
        """
        from pathlib import Path as _Path

        from src.autopilot.spec import get_phase_required_files, load_optional_phases
        from src.core.constants import CONTEXT_DIR_NAME
        from src.core.database import Phase
        from src.core.simple_config import get_config

        config = get_config()

        if phase is None:
            phase = session.query(Phase).filter_by(id=task.phase_id).first()
        if not phase:
            return None

        required_files = get_phase_required_files(phase, task.workflow_id)
        if not required_files:
            return None

        wf = None
        if task.workflow_id:
            from src.core.database import Workflow as _WF

            wf = session.query(_WF).filter_by(id=task.workflow_id).first()

        # wf.working_directory missing here is not "the agent didn't write the
        # file" -- it's a worktree-tracking bug (the workflow's shared worktree
        # got lost or was never recorded). Surface that distinctly instead of
        # silently searching some other directory for the file: a fallback
        # here would hide exactly the kind of bug that produced it (see the
        # cleanup_all_stale_branches race fixed in worktree_manager.py, which
        # this depended on staying fixed rather than being routed around).
        if task.workflow_id and not (wf and wf.working_directory):
            logger.error(
                f"Task {task.id} (phase {phase.name}): workflow {task.workflow_id} "
                "has no working_directory -- cannot verify output artifacts. "
                "This indicates a worktree-tracking bug, not a missing agent output."
            )
            return {
                "status": "failed",
                "message": (
                    f"Cannot verify output artifacts: workflow {task.workflow_id} "
                    "has no recorded working_directory. This is a system error, not "
                    "something to fix by re-doing the task -- flag it."
                ),
            }

        feature_dir = _Path(config.project_root) / CONTEXT_DIR_NAME / "features"
        missing = []
        for declared_output in required_files:
            found = False
            # 1. Check the workflow's shared worktree (task.workflow_id can
            # legitimately be unset for tasks not tied to any workflow --
            # only the "has a workflow_id but no working_directory" case
            # above is treated as an error).
            if wf and wf.working_directory:
                for candidate in [
                    _Path(wf.working_directory) / "docs" / declared_output,
                    _Path(wf.working_directory) / declared_output,
                    # Some phases (e.g. Phase 0's Feature Architect) write
                    # their declared output to the git-excluded .hephaestus/
                    # dir as an internal orchestration artifact rather than
                    # a docs/ deliverable.
                    _Path(wf.working_directory) / CONTEXT_DIR_NAME / declared_output,
                ]:
                    if candidate.exists():
                        found = True
                        break
            # 2. Check feature folder
            if not found and feature_dir.exists():
                for d in sorted(feature_dir.iterdir(), reverse=True):
                    candidate = d / "docs" / declared_output
                    if candidate.exists():
                        found = True
                        break
            if not found:
                missing.append(declared_output)

        if not missing:
            return None

        # Optional phases may complete without their declared output(s).
        optional_phases = load_optional_phases(task.workflow_id)
        if phase.name in optional_phases:
            logger.info(
                f"Agent completed optional phase {phase.name} without {missing} — allowing"
            )
            return None

        logger.warning(
            f"Agent claimed done on {phase.name} but {missing} not found — rejecting"
        )
        task.status = "failed"
        task.failure_reason = (
            f"Agent claimed completion but required output(s) missing: {', '.join(missing)}"
        )
        session.commit()
        return {
            "status": "failed",
            "message": f"Output validation failed: missing {', '.join(missing)}",
        }

    @staticmethod
    def verify_no_open_tickets(session, task, phase=None) -> Optional[Dict[str, Any]]:
        """Open-ticket hard floor for the development phase: reject 'done'
        while unresolved bug tickets exist for this workflow.

        development.yaml's own prompt already tells the agent to check for
        and fix open bug tickets (QA/security findings) before considering
        its work complete -- this is the same class of enforcement as
        verify_output_artifact: a prompt instruction alone is
        compliance-dependent, so a hard floor here means "fixed and marked
        resolved" is actually required, not just requested.

        Only applies to the development phase -- QA/security_review are the
        phases that CREATE these tickets in the first place and must not be
        blocked by their own findings.
        """
        from src.core.database import Phase, Ticket

        if phase is None:
            phase = session.query(Phase).filter_by(id=task.phase_id).first()
        if not phase or phase.name != "development":
            return None
        if not task.workflow_id:
            return None

        open_tickets = (
            session.query(Ticket)
            .filter(
                Ticket.workflow_id == task.workflow_id,
                Ticket.ticket_type == "bug",
                Ticket.is_resolved.is_(False),
            )
            .all()
        )
        if not open_tickets:
            return None

        titles = [f"{t.id[:8]}: {t.title}" for t in open_tickets[:5]]
        logger.warning(
            f"Agent claimed done on development but {len(open_tickets)} bug "
            f"ticket(s) remain unresolved — rejecting"
        )
        task.status = "failed"
        task.failure_reason = (
            f"{len(open_tickets)} open bug ticket(s) not yet resolved: "
            + "; ".join(titles)
        )
        session.commit()
        return {
            "status": "failed",
            "message": (
                f"Cannot mark done: {len(open_tickets)} open bug ticket(s) still "
                f"unresolved — {'; '.join(titles)}. Fix the underlying issue for "
                "each, then call change_ticket_status/resolve_ticket before "
                "retrying update_task_status(done)."
            ),
        }

    @staticmethod
    def _parse_forensics_recommendations(report_text: str) -> list:
        """Extract actionable recommendations from a forensics_report.md.

        Expected shape (what agents actually produce — see
        forensics_analysis.yaml's own example):

            ## Recommendations for Future Pipeline Runs

            ### High Priority
            1. **Title** - description

            ### Medium Priority
            ...

        Falls back to "medium" priority for any numbered item found outside
        a recognized High/Medium/Low subheading, so a differently-formatted
        report still yields tickets instead of silently producing none.
        """
        import re

        match = re.search(
            r"^##\s*Recommendations.*?$(.*?)(?=^##\s|\Z)",
            report_text,
            re.MULTILINE | re.DOTALL | re.IGNORECASE,
        )
        if not match:
            return []
        section = match.group(1)

        item_re = re.compile(
            r"^\s*\d+\.\s+\*\*(.+?)\*\*\s*-?\s*(.*)$", re.MULTILINE
        )

        recommendations = []
        current_priority = "medium"
        heading_re = re.compile(r"^###\s*(.+)$", re.MULTILINE)
        # Walk headings and items in document order so each item picks up
        # whichever priority heading most recently preceded it.
        markers = sorted(
            [(m.start(), "heading", m.group(1)) for m in heading_re.finditer(section)]
            + [(m.start(), "item", m) for m in item_re.finditer(section)],
            key=lambda t: t[0],
        )
        for _, kind, payload in markers:
            if kind == "heading":
                text = payload.lower()
                if "high" in text:
                    current_priority = "high"
                elif "medium" in text:
                    current_priority = "medium"
                elif "low" in text:
                    current_priority = "low"
            else:
                title = payload.group(1).strip()
                description = payload.group(2).strip() or title
                recommendations.append(
                    {
                        "title": title,
                        "description": description,
                        "priority": current_priority,
                    }
                )
        return recommendations

    @staticmethod
    async def create_tickets_from_forensics_report(session, task) -> int:
        """Auto-create tickets from a completed forensics_analysis report.

        forensics_analysis.yaml's done_definitions mandate "Tickets created
        for actionable findings", but this is exactly the kind of mechanical,
        easy-to-skip step an agent drops once the more interesting analysis
        work is done — observed live: an agent wrote a genuinely thorough
        report with 7 concrete recommendations and saved memory entries, but
        never called hephaestus_create_ticket even once. Same class of gap
        as the ash security scan (src/autopilot/orchestrator.py
        _run_ash_scan) — don't trust the agent to remember a mandated but
        tedious step; do it at the orchestrator/service level instead.

        Best-effort: failures for individual recommendations (or the whole
        thing, e.g. no BoardConfig for this workflow) are logged and
        swallowed rather than blocking the task's "done" status, since
        ticket creation is a side effect, not a correctness gate.

        Returns the number of tickets created.
        """
        from pathlib import Path as _Path

        from src.core.database import Phase, Workflow
        from src.services.ticket_service import TicketService

        phase = session.query(Phase).filter_by(id=task.phase_id).first()
        if not phase or phase.name != "forensics_analysis":
            return 0

        wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
        if not wf or not wf.working_directory:
            return 0

        report_path = _Path(wf.working_directory) / "docs" / "forensics_report.md"
        if not report_path.exists():
            return 0

        try:
            report_text = report_path.read_text()
        except OSError as e:
            logger.warning(f"[FORENSICS_TICKETS] Could not read {report_path}: {e}")
            return 0

        recommendations = TaskCompletionService._parse_forensics_recommendations(
            report_text
        )
        if not recommendations:
            return 0

        created = 0
        for rec in recommendations:
            try:
                await TicketService.create_ticket(
                    workflow_id=task.workflow_id,
                    agent_id=task.assigned_agent_id or "forensics-auto",
                    title=rec["title"][:200],
                    description=rec["description"],
                    ticket_type="improvement",
                    priority=rec["priority"],
                    task_id=task.id,
                    phase_id=task.phase_id,
                    tags=["forensics-auto"],
                )
                created += 1
            except Exception as e:
                logger.warning(
                    f"[FORENSICS_TICKETS] Failed to create ticket for "
                    f"'{rec['title']}': {e}"
                )
        logger.info(
            f"[FORENSICS_TICKETS] Created {created}/{len(recommendations)} "
            f"tickets from forensics_report.md"
        )
        return created

    @staticmethod
    async def fire_spec_gate_if_ready(session, task) -> None:
        """When a gated phase's last task completes, fire the phase-completion
        gate immediately instead of waiting for the monitor's next poll.

        The orchestrator's _advance_phases only fires when the next phase is
        still pending — if it's already in_progress, the gate would be
        missed without this.

        build_phase_output may run pytest (Enhancement 1: independent test
        verification), which can block for up to several minutes. This method
        is async so it can offload that work to a thread pool executor rather
        than blocking the event loop.
        """
        from src.autopilot.spec import GATED_PHASES, build_phase_output
        from src.core.database import DatabaseManager as _DbMgr
        from src.core.database import Phase, Task, Workflow
        from src.phases import PhaseManager

        phase = session.query(Phase).filter_by(id=task.phase_id).first()
        if not phase or phase.name not in GATED_PHASES:
            return

        incomplete = (
            session.query(Task)
            .filter_by(phase_id=phase.id)
            .filter(Task.status.in_(["pending", "assigned", "in_progress", "failed"]))
            .count()
        )
        if incomplete != 0:
            return

        # Phase complete — fire the gate now
        wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
        if not (wf and wf.working_directory):
            return

        import functools
        from pathlib import Path as _P

        # build_phase_output may run pytest (Enhancement 1: independent test
        # verification). Run it in a thread pool executor so the async event
        # loop is not blocked by a potentially multi-minute subprocess call.
        loop = asyncio.get_event_loop()
        phase_output = await loop.run_in_executor(
            None,
            functools.partial(build_phase_output, phase.name, _P(wf.working_directory)),
        )
        logger.info(
            f"[SPEC-GATE] {phase.name}: gate fired from completion path, phase_output={phase_output}"
        )
        pm = PhaseManager(_DbMgr())
        pm.workflow_id = task.workflow_id
        result = pm.mark_phase_complete(
            phase.id,
            "Phase completed (spec gate fired from update_task_status)",
            phase_output=phase_output,
        )
        if result.get("action") == "already_completed":
            logger.info(f"[SPEC-GATE] {phase.name}: already completed by another caller")
        elif result.get("action") == "goto" and result.get("target_phase_id"):
            logger.info(
                f"[SPEC-GATE] {phase.name}: GOTO {result.get('target_phase')} (score too low)"
            )
            task.action = "goto"
            task.has_results = True
            session.commit()
        elif result.get("action") == "continue":
            logger.info(f"[SPEC-GATE] {phase.name}: PASSED (score >= 0.7)")

    @staticmethod
    async def spawn_validation(
        agent_id: str,
        task_id: str,
        task_workflow_id: Optional[str],
        task_validation_iteration: int,
    ) -> None:
        """Commit the agent's work and spawn a validator agent for a task
        marked 'done' with validation enabled.

        Runs as a background asyncio task, mirroring create_task's
        fire-and-forget pattern. On failure, marks the task failed and
        terminates the agent instead of leaving it dangling.
        """
        from src.core.app_context import get_app_state

        server_state = get_app_state()

        try:
            logger.info(f"Starting validation process for task {task_id}")

            commit_sha = None
            if hasattr(server_state, "branch_manager"):
                try:
                    commit_result = server_state.branch_manager.commit_for_validation(
                        agent_id=agent_id,
                        iteration=task_validation_iteration,
                    )
                    commit_sha = commit_result.get("commit_sha")
                except Exception as e:
                    logger.warning(f"Failed to create validation commit: {e}")

            from src.validation.validator_agent import spawn_validator_agent

            validator_id = await spawn_validator_agent(
                validation_type="task",
                target_id=task_id,
                workflow_id=task_workflow_id,
                commit_sha=commit_sha or "HEAD",
                db_manager=server_state.db_manager,
                branch_manager=getattr(server_state, "branch_manager", None),
                agent_manager=server_state.agent_manager,
                original_agent_id=agent_id,
            )

            from src.core.database import Task

            session = server_state.db_manager.get_session()
            try:
                task = session.query(Task).filter_by(id=task_id).first()
                if task:
                    task.status = "validation_in_progress"
                    session.commit()
                    logger.info(
                        f"Task {task_id} validation spawned successfully, validator: {validator_id}"
                    )
                else:
                    logger.error(f"Task {task_id} not found during validation update")
            except Exception:
                # FIX #12: Rollback on commit failure to avoid leaking a
                # dirty session back into the connection pool.
                session.rollback()
                raise
            finally:
                session.close()

            await server_state.broadcast_update(
                {
                    "type": "validation_started",
                    "task_id": task_id,
                    "validator_id": validator_id,
                    "original_agent_id": agent_id,
                }
            )

        except Exception as e:
            logger.error(f"Failed to spawn validation for task {task_id}: {e}")
            from src.core.database import Task

            session = server_state.db_manager.get_session()
            try:
                task = session.query(Task).filter_by(id=task_id).first()
                if task:
                    task.status = "failed"
                    task.failure_reason = f"Validation spawning failed: {str(e)}"
                    session.commit()

                await server_state.agent_manager.terminate_agent(agent_id)
            except Exception as inner_e:
                # FIX #17: Don't let task-update/termination errors propagate
                # and lose the original validation failure context.
                # FIX #12: Rollback on commit failure before closing session.
                session.rollback()
                logger.error(
                    f"Failed to update task/terminate agent after validation failure: {inner_e}"
                )
            finally:
                session.close()

            # FIX #11/#17: Defer queue processing to avoid nested I/O in except block.
            try:
                from src.core.app_context import trigger_queue_processing

                trigger_queue_processing()
            except Exception as qe:
                logger.error(f"Failed to trigger queue processing after validation failure: {qe}")

    @staticmethod
    async def commit_and_link_ticket(session, agent_id: str, task, summary: str) -> Optional[str]:
        """Commit the agent's changes in the shared worktree, and if the
        task has a ticket_id, auto-link the resulting commit to it.

        Returns the commit SHA if a commit was made, else None.
        """
        from git import Repo

        from src.core.app_context import get_app_state
        from src.core.database import Phase

        server_state = get_app_state()
        from src.services.ticket_service import TicketService

        merge_commit_sha = None
        try:
            from pathlib import Path as _P

            wt_path = None

            # Shared-worktree path (normal autopilot): use the workflow's directory.
            if task.workflow_id:
                from src.core.database import Workflow as _WF

                wf_row = session.query(_WF).filter_by(id=task.workflow_id).first()
                if wf_row and wf_row.working_directory:
                    wt_path = wf_row.working_directory

            # Legacy per-agent worktree fallback.
            if not wt_path and hasattr(server_state, "branch_manager"):
                record = server_state.branch_manager._agent_record(session, agent_id)
                if record and record.worktree_path:
                    wt_path = record.worktree_path

            if wt_path and _P(wt_path).is_dir():
                phase_obj = (
                    session.query(Phase).filter_by(id=task.phase_id).first()
                    if task.phase_id
                    else None
                )
                phase_label = (
                    phase_obj.name
                    if phase_obj
                    else (task.phase_id[:8] if task.phase_id else "unknown")
                )

                wt_repo = Repo(wt_path)
                wt_repo.git.add("-A")
                if wt_repo.is_dirty(index=True) or wt_repo.untracked_files:
                    summary_str = (summary or "").strip()
                    subject = f"phase({phase_label}): " + (
                        summary_str[:60] if summary_str else f"task {task.id[:8]} done"
                    )
                    msg = (
                        subject
                        if not summary_str or len(summary_str) <= 60
                        else f"{subject}\n\n{summary_str}"
                    )
                    wt_repo.git.commit("-m", msg, "--no-verify")
                    merge_commit_sha = wt_repo.head.commit.hexsha
                    logger.info(
                        f"[COMMIT] phase({phase_label}) agent {agent_id[:8]}: {merge_commit_sha[:8]}"
                    )
                else:
                    logger.debug(f"[COMMIT] phase agent {agent_id[:8]}: nothing to commit")
        except Exception as e:
            logger.warning(f"Failed to commit after task done for agent {agent_id[:8]}: {e}")

        if task.ticket_id and merge_commit_sha:
            try:
                logger.info(
                    f"Auto-linking commit {merge_commit_sha} to ticket {task.ticket_id}"
                )
                await TicketService.link_commit(
                    ticket_id=task.ticket_id,
                    agent_id=agent_id,
                    commit_sha=merge_commit_sha,
                    commit_message=f"Task {task.id} completed and merged",
                    link_method="auto_task_completion",
                )
                logger.info(f"Commit {merge_commit_sha} linked to ticket {task.ticket_id}")

                await server_state.broadcast_update(
                    {
                        "type": "ticket_commit_linked",
                        "ticket_id": task.ticket_id,
                        "task_id": task.id,
                        "agent_id": agent_id,
                        "commit_sha": merge_commit_sha,
                    }
                )
            except Exception as e:
                logger.error(f"Failed to auto-link commit to ticket: {e}")
                # Don't fail the task if ticket operations fail

        return merge_commit_sha
