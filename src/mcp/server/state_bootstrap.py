"""One-time startup steps that run inside ServerState.initialize(), before
any service (agent_manager, phase_manager, etc.) is constructed.

Extracted from ServerState (SOLID review 1.6). Both functions only ever
touched self.db_manager, nothing else on the class, so neither needed to be
a method -- and grouping them here separates "get the DB and config into the
shape managers expect" from "compose the managers", which is what
ServerState.initialize() itself does.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def migrate_is_active_column(db_manager) -> None:
    """Add is_active column to autopilot_projects if missing."""
    import sqlalchemy

    try:
        with db_manager.get_session() as session:
            session.execute(sqlalchemy.text("ALTER TABLE autopilot_projects ADD COLUMN is_active BOOLEAN DEFAULT 0"))
            session.commit()
            logger.info("Migrated: added is_active column to autopilot_projects")
    except Exception:
        pass  # Column already exists


def resync_incomplete_phase_prompts_from_yaml(db_manager) -> None:
    """Re-render Phase.description/done_definitions/additional_notes/
    outputs/next_steps from the current on-disk workflow YAML, for every
    phase that hasn't reached a terminal (completed/skipped) execution
    state yet.

    PhaseManager.start_execution snapshots these fields from YAML ONCE, at
    workflow-creation time, and nothing ever re-reads them from YAML
    afterward -- a prompt fix landed in config/workflows/*/*.yaml has zero
    effect on any already-created workflow no matter how many times the
    backend restarts (restarting only refreshes WorkflowDefinition.
    phases_config, the template; already-materialized Phase rows are a
    separate, frozen copy). Confirmed live 2026-09-03: a task-suffixed-
    filename fix shipped to architectural_review.yaml/adversarial_review.
    yaml/etc. did not change an in-flight workflow's actual dispatched
    prompt at all -- its Phase rows still carried the old bare-filename
    instructions from whenever that workflow was created, weeks earlier,
    and had to be hand-patched live to unblock it. This makes that class
    of hand-patch unnecessary going forward.

    Idempotent (a no-op UPDATE when nothing changed) -- safe to run on
    every startup, mirroring migrate_is_active_column's own convention.
    Deliberately narrow: only the prompt-TEXT fields above are touched.
    working_directory/cli_tool/cli_model/etc. are runtime-resolved at
    phase-creation time (see start_execution's own phase_wd handling) and
    are left alone here, not re-derived from YAML.

    Skips a phase whose PhaseExecution is already 'completed' or
    'skipped' -- its report was already scored and its historical prompt
    should stay as-is. A phase with no PhaseExecution row, or one still
    pending/in_progress/failed, is resynced: this never touches an agent
    that's already dispatched (its prompt was already sent, independent
    of this row), it only changes what the NEXT dispatch or retry sees.
    """
    from src.core.database import Phase, PhaseExecution, Workflow
    from src.phases.phase_manager import substitute_params, substitute_params_in_list
    from src.workflow_registry import get_all_workflow_definitions

    TERMINAL_STATUSES = {"completed", "skipped"}

    def _serialize_for_text(value):
        if value is None or value == "null":
            return None
        if isinstance(value, (list, dict)):
            import json

            return json.dumps(value)
        return value

    try:
        definitions_by_id = {d.id: d for d in get_all_workflow_definitions()}
    except Exception as e:
        logger.warning(f"Could not load workflow definitions for phase-prompt resync: {e}")
        return

    updated = 0
    try:
        with db_manager.get_session() as session:
            # A completed workflow's phases are, by definition, all done --
            # skip the whole workflow rather than querying its phases only
            # to filter every one of them out individually below. 'failed'
            # and 'paused' are deliberately still scanned (both can be
            # retried/resumed later), matching the per-phase terminal-status
            # check's own reasoning just below.
            workflows = session.query(Workflow).filter(Workflow.status != "completed").all()
            for workflow in workflows:
                defn = definitions_by_id.get(workflow.definition_id)
                if not defn:
                    continue
                fresh_by_name = {p.name: p for p in defn.phases}
                launch_params = workflow.launch_params or {}

                phases = session.query(Phase).filter_by(workflow_id=workflow.id).all()
                if not phases:
                    continue
                # One query per workflow, not one per phase (N+1) -- this
                # function scans every non-completed workflow on every
                # startup, and a phase-at-a-time PhaseExecution lookup
                # scales with total phase count across the whole system
                # rather than workflow count.
                executions_by_phase_id = {
                    e.phase_id: e
                    for e in session.query(PhaseExecution)
                    .filter(PhaseExecution.phase_id.in_([p.id for p in phases]))
                    .all()
                }
                for phase in phases:
                    fresh = fresh_by_name.get(phase.name)
                    if not fresh:
                        continue
                    execution = executions_by_phase_id.get(phase.id)
                    if execution and execution.status in TERMINAL_STATUSES:
                        continue

                    description = fresh.description
                    additional_notes = fresh.additional_notes
                    done_definitions = fresh.done_definitions
                    outputs = fresh.outputs
                    next_steps = fresh.next_steps
                    if launch_params:
                        description = substitute_params(description, launch_params)
                        if additional_notes:
                            additional_notes = substitute_params(additional_notes, launch_params)
                        if done_definitions:
                            done_definitions = substitute_params_in_list(done_definitions, launch_params)
                        if outputs:
                            outputs = substitute_params_in_list(outputs, launch_params)
                        if next_steps:
                            next_steps = substitute_params_in_list(next_steps, launch_params)

                    new_additional_notes = _serialize_for_text(additional_notes)
                    new_outputs = _serialize_for_text(outputs)
                    new_next_steps = _serialize_for_text(next_steps)

                    if (
                        phase.description == description
                        and phase.done_definitions == done_definitions
                        and phase.additional_notes == new_additional_notes
                        and phase.outputs == new_outputs
                        and phase.next_steps == new_next_steps
                    ):
                        continue

                    phase.description = description
                    phase.done_definitions = done_definitions
                    phase.additional_notes = new_additional_notes
                    phase.outputs = new_outputs
                    phase.next_steps = new_next_steps
                    updated += 1
            session.commit()
        if updated:
            logger.info(f"Resynced {updated} phase prompt(s) from current YAML")
    except Exception as e:
        logger.warning(f"Phase prompt resync failed: {e}")


def load_active_project(db_manager, config) -> None:
    """Load active project from DB and apply to config before managers init."""
    from src.core.database import AutopilotProject

    try:
        with db_manager.get_session() as session:
            active = session.query(AutopilotProject).filter_by(is_active=True).first()
            if active:
                config.git.main_repo_path = Path(active.base_dir)
                config.paths.project_root = Path(active.base_dir)
                logger.info(f"Active project loaded: {active.name} ({active.base_dir})")
            else:
                # Auto-activate the default or first project
                proj = session.query(AutopilotProject).filter_by(is_default=True).first()
                if not proj:
                    proj = session.query(AutopilotProject).first()
                if proj:
                    proj.is_active = True
                    session.commit()
                    config.git.main_repo_path = Path(proj.base_dir)
                    config.paths.project_root = Path(proj.base_dir)
                    logger.info(f"Auto-activated project: {proj.name} ({proj.base_dir})")
    except Exception as e:
        logger.warning(f"Could not load active project: {e}")
