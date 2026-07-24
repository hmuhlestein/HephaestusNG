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
            from src.core.database import Workflow

            wf = session.query(Workflow).filter_by(id=task.workflow_id).first()

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
                    f"Cannot verify output artifacts: workflow {task.workflow_id} has no recorded working_directory. This is a system error, not something to fix by re-doing the task -- flag it."
                ),
            }

        feature_dir = _Path(config.project_root) / CONTEXT_DIR_NAME / "features"
        missing = []
        invalid_json = []
        for declared_output in required_files:
            found_path = None
            # 1. Check the workflow's shared worktree (task.workflow_id can
            # legitimately be unset for tasks not tied to any workflow --
            # only the "has a workflow_id but no working_directory" case
            # above is treated as an error).
            if wf and wf.working_directory:
                for candidate in [
                    # docs/<phase.name>/ is the one sanctioned subdirectory
                    # this phase's own CRITICAL PATH RULE tells it to write
                    # to -- checked first, not guessed at: iterating every
                    # subdirectory of docs/ risked treating a DIFFERENT
                    # feature's (or an earlier retry pass's) leftover file
                    # as proof this task's own agent produced its required
                    # output.
                    _Path(wf.working_directory) / "docs" / phase.name / declared_output,
                    _Path(wf.working_directory) / "docs" / declared_output,
                    _Path(wf.working_directory) / declared_output,
                    # Some phases (e.g. Phase 0's Feature Architect) write
                    # their declared output to the git-excluded .hephaestus/
                    # dir as an internal orchestration artifact rather than
                    # a docs/ deliverable.
                    _Path(wf.working_directory) / CONTEXT_DIR_NAME / declared_output,
                ]:
                    if candidate.exists():
                        found_path = candidate
                        break
            # 2. Check feature folder
            if found_path is None and feature_dir.exists():
                for d in sorted(feature_dir.iterdir(), reverse=True):
                    candidate = d / "docs" / declared_output
                    if candidate.exists():
                        found_path = candidate
                        break
            if found_path is None:
                missing.append(declared_output)
                continue

            # Existence alone isn't enough for a declared .json output: a
            # truncated/malformed write passes this check, then silently
            # reads back as None everywhere downstream (read_result's bare
            # except-return-None) -- indistinguishable from never having
            # been written at all, surfacing much later as a confusing
            # "not found" at gate-scoring time instead of a clear rejection
            # here, at the one place that actually knows the file exists.
            if declared_output.endswith(".json"):
                try:
                    import json as _json

                    _json.loads(found_path.read_text())
                except Exception as e:
                    invalid_json.append(f"{declared_output} ({e})")

        if not missing and not invalid_json:
            return None

        # Optional phases may complete without their declared output(s).
        optional_phases = load_optional_phases(task.workflow_id)
        if phase.name in optional_phases:
            logger.info(f"Agent completed optional phase {phase.name} without {missing or invalid_json} — allowing")
            return None

        problems = []
        if missing:
            problems.append(f"missing: {', '.join(missing)}")
        if invalid_json:
            problems.append(f"not valid JSON: {', '.join(invalid_json)}")
        summary = "; ".join(problems)

        logger.warning(f"Agent claimed done on {phase.name} but {summary} — rejecting")
        task.status = "failed"
        task.failure_reason = f"Agent claimed completion but required output(s) invalid: {summary}"
        session.commit()
        return {
            "status": "failed",
            "message": f"Output validation failed: {summary}",
        }

    @staticmethod
    def verify_gate_result_schema(session, task, phase=None) -> Optional[Dict[str, Any]]:
        """Schema hard floor for gated phases: reject 'done' when the
        phase's structured JSON result exists (verify_output_artifact
        already covers it being missing) but has none of the keys its
        score_* function actually reads.

        Complements verify_output_artifact -- that checks the file EXISTS,
        this checks it looks like the documented schema. Observed live: a
        QA agent wrote a custom nested shape instead of the documented flat
        one; every field score_qa reads defaulted silently to "everything
        passed" (including critical_issues and requirements_met, which
        nothing else independently re-verifies), so the gate's judgement
        checks never actually ran against real content.
        """
        from src.autopilot.spec import (
            GATE_RESULT_ARTIFACTS,
            GATE_RESULT_SUBDIR,
            GATED_PHASES,
            read_result,
            validate_gate_result_schema,
        )
        from src.core.database import Phase

        if phase is None:
            phase = session.query(Phase).filter_by(id=task.phase_id).first()
        if not phase or phase.name not in GATED_PHASES:
            return None

        artifacts = GATE_RESULT_ARTIFACTS.get(phase.name)
        if not artifacts:
            return None
        json_filename = artifacts[0]  # JSON result is always listed first.

        wf = None
        if task.workflow_id:
            from src.core.database import Workflow

            wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
        if not (wf and wf.working_directory):
            return None  # verify_output_artifact already surfaces this case.

        result = read_result(
            wf.working_directory,
            json_filename,
            subdir=GATE_RESULT_SUBDIR.get(phase.name),
            phase_name=phase.name,
        )
        error = validate_gate_result_schema(phase.name, result)
        if not error:
            return None

        logger.warning(f"Agent claimed done on {phase.name} but {json_filename} doesn't match the documented schema — rejecting: {error}")
        task.status = "failed"
        task.failure_reason = error
        session.commit()
        return {"status": "failed", "message": error}

    @staticmethod
    def verify_no_open_tickets(session, task, phase=None) -> Optional[Dict[str, Any]]:
        """Open-ticket hard floor for development and git_commit_push: reject
        'done' while unresolved bug tickets exist for this workflow.

        development.yaml's own prompt already tells the agent to check for
        and fix open bug tickets (QA/security findings) before considering
        its work complete -- this is the same class of enforcement as
        verify_output_artifact: a prompt instruction alone is
        compliance-dependent, so a hard floor here means "fixed and marked
        resolved" is actually required, not just requested.

        Also applies to git_commit_push -- the literal last phase before a
        feature ships. security_review creates tickets for its findings but
        has no content-scored workflow.yaml gate of its own (unlike
        qa_validation/product_validation, which score their own result
        files); its only enforcement path is this same check firing when the
        pipeline happens to route back through development. If qa_validation
        and product_validation both pass on their own separate criteria
        without the pipeline ever revisiting development, a security ticket
        could otherwise stay open all the way to git commit -- shipping code
        with a known, already-reported security issue no gate ever rejected.
        Checking again at the true end of the pipeline closes that gap
        regardless of which path a given run took to get there.

        Not applied to QA/security_review themselves -- those are the phases
        that CREATE these tickets in the first place and must not be blocked
        by their own findings.
        """
        from src.core.database import Phase, Ticket

        if phase is None:
            phase = session.query(Phase).filter_by(id=task.phase_id).first()
        if not phase or phase.name not in ("development", "git_commit_push"):
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

        # Full ticket id, not a truncated prefix -- this message instructs
        # the agent to call change_ticket_status/resolve_ticket with it
        # directly. A truncated id (e.g. "ticket-6" from "ticket-6805c19f-
        # ...") reads as a plausible complete id since real ids already
        # start with "ticket-", but it isn't a real, resolvable id.
        # Observed live: an agent tried to resolve a ticket using exactly
        # this kind of truncated-looking id and got "Ticket not found".
        titles = [f"{t.id}: {t.title}" for t in open_tickets[:5]]
        logger.warning(f"Agent claimed done on {phase.name} but {len(open_tickets)} bug ticket(s) remain unresolved — rejecting")
        task.status = "failed"
        task.failure_reason = f"{len(open_tickets)} open bug ticket(s) not yet resolved: " + "; ".join(titles)
        session.commit()
        # git_commit_push isn't the phase equipped to fix code — its own
        # retry loop would just hit this same rejection again. The message
        # is phrased for whichever agent reads it (development, if this
        # fires there directly; otherwise whoever investigates the resulting
        # failed task) rather than assuming the rejected agent itself can act
        # on it.
        fix_instruction = (
            "Fix the underlying issue for each, then call change_ticket_status/resolve_ticket before retrying update_task_status(done)."
            if phase.name == "development"
            else ("This phase cannot fix code itself — the workflow needs to route back to development to resolve these before git_commit_push can proceed.")
        )
        return {
            "status": "failed",
            "message": (f"Cannot mark done: {len(open_tickets)} open bug ticket(s) still unresolved — {'; '.join(titles)}. {fix_instruction}"),
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

        item_re = re.compile(r"^\s*\d+\.\s+\*\*(.+?)\*\*\s*-?\s*(.*)$", re.MULTILINE)

        recommendations = []
        current_priority = "medium"
        heading_re = re.compile(r"^###\s*(.+)$", re.MULTILINE)
        # Walk headings and items in document order so each item picks up
        # whichever priority heading most recently preceded it.
        markers = sorted(
            [(m.start(), "heading", m.group(1)) for m in heading_re.finditer(section)] + [(m.start(), "item", m) for m in item_re.finditer(section)],
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

        recommendations = TaskCompletionService._parse_forensics_recommendations(report_text)
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
                logger.warning(f"[FORENSICS_TICKETS] Failed to create ticket for '{rec['title']}': {e}")
        logger.info(f"[FORENSICS_TICKETS] Created {created}/{len(recommendations)} tickets from forensics_report.md")
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

        incomplete = session.query(Task).filter_by(phase_id=phase.id).filter(Task.status.in_(["pending", "assigned", "in_progress", "failed"])).count()
        if incomplete != 0:
            return

        from src.core.log_context import set_log_context
        set_log_context(task=task.id, phase=phase.name, workflow=task.workflow_id or "")

        # Phase complete — fire the gate now
        wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
        if not (wf and wf.working_directory):
            return

        import functools
        from pathlib import Path

        # build_phase_output may run pytest (Enhancement 1: independent test
        # verification). Run it in a thread pool executor so the async event
        # loop is not blocked by a potentially multi-minute subprocess call.
        loop = asyncio.get_event_loop()
        phase_output = await loop.run_in_executor(
            None,
            functools.partial(build_phase_output, phase.name, Path(wf.working_directory)),
        )
        logger.info(f"[SPEC-GATE] {phase.name}: gate fired from completion path, phase_output={phase_output}")
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
            logger.info(f"[SPEC-GATE] {phase.name}: GOTO {result.get('target_phase')} (score too low)")
            # task.action/action_target_phase already set by
            # mark_phase_complete's own _tag_completing_task -- only
            # has_results is this caller's own responsibility.
            task.has_results = True
            session.commit()

            # mark_phase_complete only decides the goto and closes THIS
            # phase's execution -- it deliberately doesn't create the
            # target phase's task itself (that's _fire_phase_transition's
            # job, mirrored here). Without this, the target phase's
            # PhaseExecution stays "completed" from its own earlier,
            # unrelated pass, so _advance_phases's background sweep never
            # sees it as needing new work -- it just keeps marching
            # forward by phase order from the highest completed phase and
            # picks the next PENDING phase instead, silently skipping the
            # goto entirely. Observed live: an adversarial_review gate
            # found 4 BLOCKER findings and decided "GOTO development", but
            # the pipeline proceeded straight to security_review with the
            # blockers never addressed.
            from src.autopilot.orchestrator import _create_phase_task
            from src.core.database import Phase, PhaseExecution

            metadata = result.get("metadata") or {}
            spec_gate = metadata.get("spec_gate", {})
            feedback = spec_gate.get("reason") or result.get("reason") or None

            # Same fix as _fire_phase_transition's identical block in
            # orchestrator.py: a "result_missing" gate reason only means
            # the file read came up empty at this evaluation instant, not
            # that the agent didn't do the work -- if it left a real
            # completion_notes summary, that's a more accurate account of
            # what actually happened and the next phase's corrective task
            # should see that instead of a "missing" message that
            # contradicts the real work already done. `task` here is
            # already the completing task itself (this function runs from
            # its own update_task_status call), so no extra lookup needed.
            if spec_gate.get("result_missing") and task.completion_notes:
                feedback = task.completion_notes

            # Reset stale phase executions at/after the target phase.
            # Same fix as _handle_evaluation_goto in phase_manager.py:
            # without this, phases after the target keep their "completed"
            # status from a prior pass and get re-evaluated without running.
            target_order = session.query(Phase.order).filter_by(id=result["target_phase_id"]).scalar()
            if target_order is not None:
                stale = (
                    session.query(PhaseExecution)
                    .join(Phase, PhaseExecution.phase_id == Phase.id)
                    .filter(
                        Phase.workflow_id == task.workflow_id,
                        Phase.order >= target_order,
                        PhaseExecution.status.in_(["in_progress", "completed"]),
                    )
                    .all()
                )
                for s in stale:
                    s.status = "pending"
                    s.completed_at = None
                    s.task_creation_claimed_at = None
                if stale:
                    session.commit()

            await loop.run_in_executor(
                None,
                functools.partial(
                    _create_phase_task,
                    task.workflow_id,
                    result["target_phase_id"],
                    result.get("target_phase"),
                    "goto",
                    logger,
                    feedback=feedback,
                    source_phase_name=phase.name,
                ),
            )
        elif result.get("action") == "continue":
            logger.info(f"[SPEC-GATE] {phase.name}: PASSED (score >= 0.7)")
            # _start_next_phase (called by mark_phase_complete's
            # _advance_or_complete_with_phase_info) flips the next phase's
            # PhaseExecution to "in_progress" but does NOT create its Task.
            # The sweep's _advance_phases would pick that up on its next
            # tick via Case 0b (in_progress with no tasks) or Case 1
            # (completed + pending successor), but if a concurrent sweep
            # tick already iterated and acted on a stale view it won't
            # re-read the snapshot. Same as the goto path above (which
            # always calls _create_phase_task directly): create the task
            # here so the spec gate doesn't depend on a lucky sweep tick.
            target_phase_id = result.get("target_phase_id")
            target_phase_name = result.get("target_phase")
            if target_phase_id:
                from src.autopilot.orchestrator import _create_phase_task
                await loop.run_in_executor(
                    None,
                    functools.partial(
                        _create_phase_task,
                        task.workflow_id,
                        target_phase_id,
                        target_phase_name,
                        "continue",
                        logger,
                        source_phase_name=phase.name,
                    ),
                )

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

            with server_state.db_manager.session_scope() as session:
                task = session.query(Task).filter_by(id=task_id).first()
                if task:
                    task.status = "validation_in_progress"
                    logger.info(f"Task {task_id} validation spawned successfully, validator: {validator_id}")
                else:
                    logger.error(f"Task {task_id} not found during validation update")

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

            try:
                with server_state.db_manager.session_scope() as session:
                    task = session.query(Task).filter_by(id=task_id).first()
                    if task:
                        task.status = "failed"
                        task.failure_reason = f"Validation spawning failed: {str(e)}"

                    await server_state.agent_manager.terminate_agent(agent_id)
            except Exception as inner_e:
                # FIX #17: Don't let task-update/termination errors propagate
                # and lose the original validation failure context (session_scope
                # already rolled back before re-raising here).
                logger.error(f"Failed to update task/terminate agent after validation failure: {inner_e}")

            # FIX #11/#17: Defer queue processing to avoid nested I/O in except block.
            try:
                from src.core.app_context import trigger_queue_processing

                trigger_queue_processing()
            except Exception as qe:
                logger.error(f"Failed to trigger queue processing after validation failure: {qe}")

    @staticmethod
    def verify_output_survived_commit(session, task, phase=None) -> Optional[Dict[str, Any]]:
        """Second half of the output-existence hard floor: verify_output_artifact
        confirms the declared file(s) are in the worktree BEFORE 'done' is
        accepted; this re-checks the exact same worktree paths AFTER
        commit_and_link_ticket runs, to catch the file having vanished in
        between.

        That gap is real, not theoretical: an agent whose shell cwd drifted
        outside its worktree mid-task can still pass the first check (an
        earlier pass genuinely wrote the file into the worktree) while its
        LAST write -- the one actually on disk when the request completes --
        landed somewhere else entirely (e.g. the main repo checkout).
        commit_and_link_ticket's `git add -A` then finds nothing dirty and
        silently commits nothing. Observed live: exactly this sequence let a
        full security_review report and its code fixes complete as "done"
        with zero trace in git history.

        Only called after a successful commit_and_link_ticket, so a `None`
        commit SHA there is the actual trigger for this to matter -- but the
        check itself is a plain existence check, independent of whether a
        commit was made (an unchanged-because-already-committed file is
        exactly as fine as a freshly committed one).

        Returns a rejection response dict (mirroring verify_output_artifact's
        shape) if a required file is missing now, else None.
        """
        from pathlib import Path as _Path

        from src.autopilot.spec import get_phase_required_files
        from src.core.constants import CONTEXT_DIR_NAME
        from src.core.database import Phase, Workflow

        if phase is None:
            phase = session.query(Phase).filter_by(id=task.phase_id).first()
        if not phase:
            return None

        required_files = get_phase_required_files(phase, task.workflow_id)
        if not required_files:
            return None

        wf = session.query(Workflow).filter_by(id=task.workflow_id).first() if task.workflow_id else None
        if not (wf and wf.working_directory):
            return None  # verify_output_artifact already surfaces this case.

        missing = []
        for declared_output in required_files:
            found = False
            for candidate in [
                _Path(wf.working_directory) / "docs" / phase.name / declared_output,
                _Path(wf.working_directory) / "docs" / declared_output,
                _Path(wf.working_directory) / declared_output,
                _Path(wf.working_directory) / CONTEXT_DIR_NAME / declared_output,
            ]:
                if candidate.exists():
                    found = True
                    break
            if not found:
                missing.append(declared_output)

        if not missing:
            return None

        logger.error(
            f"Task {task.id} (phase {phase.name}) claimed done and passed the "
            f"pre-commit output check, but {missing} is gone from the worktree "
            "after commit -- the agent's actual last write landed somewhere "
            "else. Failing the task instead of letting the loss go silent."
        )
        task.status = "failed"
        task.failure_reason = (
            f"Output {', '.join(missing)} was present when checked but is "
            "missing from the worktree after commit -- your last write to it "
            "likely landed outside your assigned Working Directory (check "
            "your shell's cwd). Redo the output inside your Working "
            "Directory and mark done again."
        )
        session.commit()
        return {
            "status": "failed",
            "message": task.failure_reason,
        }

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
            from pathlib import Path

            wt_path = None

            # Shared-worktree path (normal autopilot): use the workflow's directory.
            if task.workflow_id:
                from src.core.database import Workflow

                wf_row = session.query(Workflow).filter_by(id=task.workflow_id).first()
                if wf_row and wf_row.working_directory:
                    wt_path = wf_row.working_directory

            # Legacy per-agent worktree fallback.
            if not wt_path and hasattr(server_state, "branch_manager"):
                record = server_state.branch_manager._agent_record(session, agent_id)
                if record and record.worktree_path:
                    wt_path = record.worktree_path

            if wt_path and Path(wt_path).is_dir():
                phase_obj = session.query(Phase).filter_by(id=task.phase_id).first() if task.phase_id else None
                phase_label = phase_obj.name if phase_obj else (task.phase_id[:8] if task.phase_id else "unknown")

                wt_repo = Repo(wt_path)
                wt_repo.git.add("-A")
                if wt_repo.is_dirty(index=True) or wt_repo.untracked_files:
                    summary_str = (summary or "").strip()
                    subject = f"phase({phase_label}): " + (summary_str[:60] if summary_str else f"task {task.id[:8]} done")
                    msg = subject if not summary_str or len(summary_str) <= 60 else f"{subject}\n\n{summary_str}"
                    wt_repo.git.commit("-m", msg, "--no-verify")
                    merge_commit_sha = wt_repo.head.commit.hexsha
                    logger.info(f"[COMMIT] phase({phase_label}) agent {agent_id[:8]}: {merge_commit_sha[:8]}")
                else:
                    logger.debug(f"[COMMIT] phase agent {agent_id[:8]}: nothing to commit")
        except Exception as e:
            logger.warning(f"Failed to commit after task done for agent {agent_id[:8]}: {e}")

        if task.ticket_id and merge_commit_sha:
            try:
                logger.info(f"Auto-linking commit {merge_commit_sha} to ticket {task.ticket_id}")
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

    @staticmethod
    def collect_cost_on_completion(task_id: str) -> None:
        """Collect cost data from CLI session when a task completes.

        Called from update_task_status handler when task status is set to 'done'.
        Reads the CLI session transcript (pi JSONL, Claude Code, etc.) and
        writes CostEntry rows for any new usage since the last checkpoint.

        Args:
            task_id: The completed task's ID
        """
        try:
            from src.services.cost_collection_service import collect_task_cost

            collect_task_cost(task_id)
        except Exception as e:
            logger.warning(f"Cost collection failed for task {task_id[:8]}: {e}")
