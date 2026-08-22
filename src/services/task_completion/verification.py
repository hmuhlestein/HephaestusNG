"""Output-artifact and gate-result verification hard floors.

Extracted from src.services.task_completion_service.TaskCompletionService
per design_docs/phase_1b_decomposition.md section 4.4.

Covers:
  - verify_output_artifact (pre-commit output existence + OKF frontmatter)
  - verify_gate_result_schema (gated-phase structured JSON schema)
  - verify_no_open_tickets (open-bug-ticket blocking for dev/git_expert)
  - verify_output_survived_commit (post-commit re-check)

The two _old_name_map dicts are intentionally duplicated (byte-identical)
rather than deduplicated — the doc explicitly preserves this known
duplication (section 4.4).
"""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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

    from src.autopilot.spec import (
        get_phase_required_files,
        load_optional_phases,
        resolve_declared_output_path,
    )
    from src.core.constants import CONTEXT_DIR_NAME
    from src.core.database import Phase
    from src.core.simple_config import get_config

    config = get_config()

    if phase is None:
        phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if not phase:
        return None

    # Arbitration tasks are exempt from the phase's own declared output
    # files -- an arbiter's job is a decision (done_definition: "Write
    # arbitration_result.json"), not the phase's normal deliverable, so
    # holding it to e.g. design_review's challenge.md is checking for a
    # file the task was never asked to produce. created_by_agent_id (not
    # Agent.agent_type -- "arbitration" was never a member of Agent's own
    # CHECK constraint, see arbitration.py's _trigger_arbitration) is the
    # established way to identify these tasks elsewhere (phase_transitions.
    # py's own is_orphan/sibling checks use the same field). Confirmed
    # live: task 18cf5d78 (an arbitration task) was rejected "done" over a
    # missing challenge.md it was never supposed to write.
    from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY

    if task.created_by_agent_id == ARBITRATION_CREATED_BY:
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
    # got lost or was never recorded). Try to recover from a worktree
    # record before giving up.
    if task.workflow_id and not (wf and wf.working_directory):
        # Attempt recovery: an AgentWorktree row only exists for an agent
        # that fell back to an ISOLATED worktree (create_agent_for_task's
        # shared_worktree branch reuses wf.working_directory directly and
        # never creates one) -- which only happens when wf.working_directory
        # was already empty at THAT agent's own dispatch time.
        #
        # Prefer the CURRENTLY completing task's own agent's worktree
        # first -- it's the one this agent actually wrote its output to,
        # right now. Only fall back to the workflow's EARLIEST isolated
        # worktree when the current agent has none of its own record
        # (the original transient-tracking-gap case this recovery was
        # built for, where every phase still shares one worktree).
        #
        # The "always prefer earliest" version broke down once a
        # workflow could legitimately gain several DISCONNECTED isolated
        # worktrees over its life, not just one incidental gap -- e.g. a
        # completed workflow re-entered later via review-mode
        # request_changes or a manual phase rerun, each spawning its own
        # fresh isolated worktree since wf.working_directory is null by
        # then (the original worktree was already cleaned up after
        # merge). "Earliest" in that case picks some unrelated worktree
        # from an earlier redo round, not the one the agent that's
        # completing RIGHT NOW actually used -- observed live: an
        # architectural_review re-run after a development redo got its
        # own genuinely-written review.md rejected as "missing" because
        # verification checked a completely different, stale worktree.
        recovered = False
        from src.core.database import AgentWorktree
        from src.core.database import Task as _Task

        wt_candidates = []
        if task.assigned_agent_id:
            own_wt_record = (
                session.query(AgentWorktree)
                .filter_by(agent_id=task.assigned_agent_id)
                .first()
            )
            if own_wt_record:
                wt_candidates.append(own_wt_record)
        wt_candidates.append(
            session.query(AgentWorktree)
            .join(_Task, _Task.assigned_agent_id == AgentWorktree.agent_id)
            .filter(_Task.workflow_id == task.workflow_id)
            .order_by(AgentWorktree.created_at.asc())
            .first()
        )

        for wt_record in wt_candidates:
            if wt_record and wt_record.worktree_path and _Path(wt_record.worktree_path).is_dir():
                if wf:
                    wf.working_directory = wt_record.worktree_path
                    logger.info(f"[TASK-COMPLETE] Recovered working_directory for workflow {task.workflow_id[:8]} from agent worktree: {wt_record.worktree_path}")
                    recovered = True
                break
        if not recovered:
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

    # Search the task's own project's feature folder, not whichever
    # project the process-wide singleton currently points at -- with
    # two projects active simultaneously that can be a different repo,
    # silently missing a real output or matching a same-named file from
    # the wrong project.
    project_base_dir = None
    if wf and wf.project_id:
        from src.core.database import AutopilotProject

        proj = session.query(AutopilotProject).filter_by(id=wf.project_id).first()
        if proj and proj.base_dir:
            project_base_dir = proj.base_dir

    feature_dir = _Path(project_base_dir or config.paths.project_root) / CONTEXT_DIR_NAME / "features"
    missing = []
    invalid_frontmatter = []
    for declared_output in required_files:
        found_path = None
        # 1. Check the workflow's shared worktree (task.workflow_id can
        # legitimately be unset for tasks not tied to any workflow --
        # only the "has a workflow_id but no working_directory" case
        # above is treated as an error).
        if wf and wf.working_directory:
            found_path = resolve_declared_output_path(
                wf.working_directory, phase.name, declared_output
            )
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

        # Existence alone isn't enough for a declared .md (OKF) output: a
        # truncated/malformed frontmatter block passes this check, then
        # silently reads back as None everywhere downstream (okf_markdown.read_okf's
        # bare except-return-None) -- indistinguishable from never having
        # been written at all, surfacing much later as a confusing
        # "not found" at gate-scoring time instead of a clear rejection
        # here, at the one place that actually knows the file exists.
        if declared_output.endswith(".md"):
            from src.autopilot.okf_markdown import read_okf

            if read_okf(found_path) is None:
                invalid_frontmatter.append(f"{declared_output} (no valid OKF frontmatter block)")

            # Security review must include ash scan results. Fails closed,
            # not open, on a read error -- matching this function's own
            # established philosophy (see the comment above): an I/O
            # hiccup here previously skipped the check silently instead of
            # rejecting, letting a security review whose scan section
            # couldn't even be verified pass through undetected.
            # Basename comparison, not equality: an in-flight workflow's
            # Phase.outputs row was snapshotted from YAML at creation and may
            # still carry the old subdirectory-prefixed
            # "security_review/security.md", which never matched a bare
            # equality test -- so this MANDATORY check silently did not run
            # for those workflows either.
            if phase.name == "security_review" and _Path(declared_output).name == "security.md":
                try:
                    content = found_path.read_text(errors="replace")
                    if "Automated Scan Results" not in content and "ash_results" not in content.lower():
                        invalid_frontmatter.append(f"{declared_output} (missing 'Automated Scan Results' section — ash scan not included)")
                except Exception as e:
                    logger.warning(f"Failed to read {declared_output} to verify ash scan results: {e}")
                    invalid_frontmatter.append(f"{declared_output} (could not be read to verify ash scan results: {e})")

    if not missing and not invalid_frontmatter:
        return None

    # Optional phases may complete without their declared output(s).
    optional_phases = load_optional_phases(task.workflow_id)
    if phase.name in optional_phases:
        logger.info(f"Agent completed optional phase {phase.name} without {missing or invalid_frontmatter} — allowing")
        return None

    problems = []
    if missing:
        problems.append(f"missing: {', '.join(missing)}")
    if invalid_frontmatter:
        problems.append(f"not valid OKF: {', '.join(invalid_frontmatter)}")
    summary = "; ".join(problems)

    logger.warning(f"Agent claimed done on {phase.name} but {summary} — rejecting")
    task.status = "failed"
    task.failure_reason = f"Agent claimed completion but required output(s) invalid: {summary}"
    session.commit()
    return {
        "status": "failed",
        "message": f"Output validation failed: {summary}",
    }


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
        _feature_review_legacy_report,
        read_okf_report,
        validate_gate_result_schema,
    )
    from src.core.database import Phase

    if phase is None:
        phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if not phase or phase.name not in GATED_PHASES:
        return None

    # Same exemption as verify_output_artifact's -- an arbitration task's
    # done_definition is "Write arbitration_result.json with a decision",
    # not the gated phase's own structured report.
    from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY

    if task.created_by_agent_id == ARBITRATION_CREATED_BY:
        return None

    artifacts = GATE_RESULT_ARTIFACTS.get(phase.name)
    if not artifacts:
        return None
    report_filename = artifacts[0]

    wf = None
    if task.workflow_id:
        from src.core.database import Workflow

        wf = session.query(Workflow).filter_by(id=task.workflow_id).first()
    if not (wf and wf.working_directory):
        return None  # verify_output_artifact already surfaces this case.

    result, _ = read_okf_report(
        wf.working_directory,
        report_filename,
        subdir=GATE_RESULT_SUBDIR.get(phase.name),
        phase_name=phase.name,
    )
    if result is None and phase.name == "feature_review":
        # TEMPORARY (Phase 2 §4.9 follow-up) -- see
        # _feature_review_legacy_report's own docstring in spec.py.
        result, _ = _feature_review_legacy_report(wf.working_directory)
    error = validate_gate_result_schema(phase.name, result)
    if not error:
        return None

    logger.warning(f"Agent claimed done on {phase.name} but {report_filename} doesn't match the documented schema — rejecting: {error}")
    task.status = "failed"
    task.failure_reason = error
    session.commit()
    return {"status": "failed", "message": error}


def verify_no_open_tickets(session, task, phase=None) -> Optional[Dict[str, Any]]:
    """Open-ticket hard floor for development and git_expert: reject
    'done' while unresolved bug tickets exist for this workflow.

    development.yaml's own prompt already tells the agent to check for
    and fix open bug tickets (QA/security findings) before considering
    its work complete -- this is the same class of enforcement as
    verify_output_artifact: a prompt instruction alone is
    compliance-dependent, so a hard floor here means "fixed and marked
    resolved" is actually required, not just requested.

    Also applies to git_expert -- the literal last phase before a
    feature ships. security_review's own gate covers only what it failed
    to FIX (security.md's unresolved_count, scored by
    score_security_review): the medium/low findings it deliberately
    tickets instead of fixing are by design NOT gate input, so they have
    no scored path of their own and would otherwise ride all the way to
    the commit unchallenged -- shipping code with a known,
    already-reported security issue no gate ever rejected. Checking again
    at the true end of the pipeline closes that gap regardless of which
    path a given run took to get there.

    (Until security_review became a genuinely gated phase, this check was
    its ONLY enforcement path of any kind, firing only when the pipeline
    happened to route back through development -- its workflow.yaml
    conditions were configured but unreachable, since it declared no
    `spec_gate: true` and build_phase_output returned {} for it. That is
    fixed; this check now backstops the ticketed findings rather than
    standing in for the whole gate.)

    Not applied to QA/security_review themselves -- those are the phases
    that CREATE these tickets in the first place and must not be blocked
    by their own findings.
    """
    from src.core.database import Phase, Ticket

    if phase is None:
        phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if not phase or phase.name not in ("development", "git_expert"):
        return None
    if not task.workflow_id:
        return None

    # Same exemption as verify_output_artifact's -- an arbitration task's
    # job is a goto/fail/continue decision, not fixing bug tickets, even
    # if it happens to fire for development/git_expert.
    from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY

    if task.created_by_agent_id == ARBITRATION_CREATED_BY:
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
    # the agent to call update_ticket_status(new_status='shipped') with it
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
    # git_expert isn't the phase equipped to fix code -- its own
    # retry loop would just hit this same rejection again. The message
    # is phrased for whichever agent reads it (development, if this
    # fires there directly; otherwise whoever investigates the resulting
    # failed task) rather than assuming the rejected agent itself can act
    # on it.
    fix_instruction = (
        "Fix the underlying issue for each, then call update_ticket_status(new_status='shipped') before retrying update_task_status(done)."
        if phase.name == "development"
        else ("This phase cannot fix code itself — the workflow needs to route back to development to resolve these before git_expert can proceed.")
    )
    return {
        "status": "failed",
        "message": (f"Cannot mark done: {len(open_tickets)} open bug ticket(s) still unresolved — {'; '.join(titles)}. {fix_instruction}"),
    }


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
    from src.autopilot.spec import (
        OUTPUT_NAME_ALIASES,
        get_phase_required_files,
        resolve_declared_output_path,
    )
    from src.core.database import Phase, Workflow

    if phase is None:
        phase = session.query(Phase).filter_by(id=task.phase_id).first()
    if not phase:
        return None

    # Same exemption as verify_output_artifact's -- see its comment.
    from src.autopilot.orchestrator.arbitration import ARBITRATION_CREATED_BY

    if task.created_by_agent_id == ARBITRATION_CREATED_BY:
        return None

    required_files = get_phase_required_files(phase, task.workflow_id)
    if not required_files:
        return None

    wf = session.query(Workflow).filter_by(id=task.workflow_id).first() if task.workflow_id else None
    if not (wf and wf.working_directory):
        return None  # verify_output_artifact already surfaces this case.

    missing = []
    for declared_output in required_files:
        found = resolve_declared_output_path(wf.working_directory, phase.name, declared_output) is not None
        # Also check if the file exists in git (already committed) --
        # resolve_declared_output_path only checks the worktree's current
        # state, but a file already committed and then removed from the
        # working tree by something else is still "not lost," which is
        # this function's whole concern (catching a genuine loss, not a
        # normal post-commit state).
        if not found:
            old_name = OUTPUT_NAME_ALIASES.get(declared_output)
            for name in [declared_output] + ([old_name] if old_name else []):
                try:
                    from git import Repo
                    repo = Repo(wf.working_directory)
                    for commit in repo.iter_commits(paths=f"**/{name}", max_count=5):
                        found = True
                        break
                except Exception as e:
                    logger.warning(
                        f"Git history check for {name} failed in {wf.working_directory}: {e}"
                    )
                if found:
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
