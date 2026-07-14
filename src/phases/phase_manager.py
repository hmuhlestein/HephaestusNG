"""Phase manager for runtime orchestration of workflow phases."""

import json
import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import joinedload

from src.core.constants import CONTEXT_DIR_NAME, WORKTREES_SUBDIR
from src.core.database import DatabaseManager, Phase, PhaseExecution, Task, Workflow
from src.core.database import WorkflowDefinition as DBWorkflowDefinition
from src.core.simple_config import get_config
from src.phases.models import PhaseContext, PhasesConfig
from src.phases.phase_loader import PhaseLoader
from src.sdk.models import Phase as SdkPhase
from src.sdk.models import WorkflowDefinition
from src.workflow_engine.orchestrator import (
    OrchestratorConfig,
    WorkflowOrchestrator,
)

logger = logging.getLogger(__name__)


def substitute_params(text: str, params: Dict[str, Any]) -> str:
    """Replace {param_name} placeholders with actual values.

    Args:
        text: Text containing {param_name} placeholders
        params: Dictionary of parameter name -> value

    Returns:
        Text with placeholders replaced
    """
    if not text or not params:
        return text

    result = text
    for key, value in params.items():
        placeholder = f"{{{key}}}"
        result = result.replace(placeholder, str(value) if value is not None else "")
    return result


def substitute_params_in_list(items: List[str], params: Dict[str, Any]) -> List[str]:
    """Replace {param_name} placeholders in a list of strings.

    Args:
        items: List of strings containing placeholders
        params: Dictionary of parameter name -> value

    Returns:
        List with placeholders replaced in each item
    """
    if not items or not params:
        return items

    return [substitute_params(item, params) for item in items]


class PhaseManager:
    """Manages workflow phases at runtime with support for multiple concurrent workflows."""

    def __init__(self, db_manager: DatabaseManager):
        """Initialize phase manager.

        Args:
            db_manager: Database manager instance
        """
        self.db_manager = db_manager

        # Legacy single workflow support (for backward compatibility)
        self.active_workflow: Optional[WorkflowDefinition] = None
        self.workflow_id: Optional[str] = None

        # Multi-workflow support
        self.definitions: Dict[
            str, DBWorkflowDefinition
        ] = {}  # definition_id -> definition
        self.active_executions: Dict[str, str] = {}  # workflow_id -> definition_id

        # Orchestrator instances cache (per workflow_id) to persist state
        self._orchestrators: Dict[str, "WorkflowOrchestrator"] = {}

        self.phases_config_cache: Dict[
            str, PhasesConfig
        ] = {}  # Cache for workflow configs

    def load_active_workflow(self) -> Optional[str]:
        """Load the first active workflow from the database.

        This is called on monitor startup to resume tracking an existing workflow.
        If multiple active workflows exist, loads the one with the most tasks.

        Returns:
            Workflow ID if found, None otherwise
        """
        session = self.db_manager.get_session()
        try:
            # Find ALL active workflows
            all_workflows = (
                session.query(Workflow)
                .filter_by(status="active")
                .order_by(Workflow.created_at.desc())
                .all()
            )

            if not all_workflows:
                logger.info("[DIAGNOSTIC] No active workflows found in database")
                return None

            logger.info(f"[DIAGNOSTIC] Found {len(all_workflows)} active workflows:")

            # Check task count for each workflow
            workflow_with_tasks = None
            max_tasks = 0

            for wf in all_workflows:
                task_count = session.query(Task).filter_by(workflow_id=wf.id).count()
                done_count = (
                    session.query(Task)
                    .filter_by(workflow_id=wf.id, status="done")
                    .count()
                )
                failed_count = (
                    session.query(Task)
                    .filter_by(workflow_id=wf.id, status="failed")
                    .count()
                )
                active_count = (
                    session.query(Task)
                    .filter(
                        Task.workflow_id == wf.id,
                        Task.status.in_(["pending", "assigned", "in_progress"]),
                    )
                    .count()
                )

                logger.info(f"[DIAGNOSTIC]   - {wf.name} (ID: {wf.id[:8]}...)")
                logger.info(f"[DIAGNOSTIC]     Created: {wf.created_at}")
                logger.info(
                    f"[DIAGNOSTIC]     Tasks: {task_count} total ({done_count} done, {failed_count} failed, {active_count} active)"
                )
                logger.info(f"[DIAGNOSTIC]     Phases folder: {wf.phases_folder_path}")

                if task_count > max_tasks:
                    max_tasks = task_count
                    workflow_with_tasks = wf

            # Select the workflow with the most tasks (or newest if tie)
            workflow = workflow_with_tasks if workflow_with_tasks else all_workflows[0]

            logger.info(
                f"[DIAGNOSTIC] Selected workflow: {workflow.name} (ID: {workflow.id[:8]}...)"
            )
            logger.info(
                f"[DIAGNOSTIC] Reason: {'Most tasks' if workflow == workflow_with_tasks and max_tasks > 0 else 'Newest created'}"
            )
            logger.info(f"[DIAGNOSTIC] Phases folder: {workflow.phases_folder_path}")

            # Load the workflow definition from the phases folder
            try:
                workflow_def = PhaseLoader.load_phases_from_folder(
                    workflow.phases_folder_path
                )
                self.active_workflow = workflow_def
                self.workflow_id = workflow.id

                logger.info(
                    f"[DIAGNOSTIC] Successfully loaded workflow '{workflow.name}' with {len(workflow_def.phases)} phases"
                )
                logger.info(
                    f"[DIAGNOSTIC] PhaseManager.workflow_id set to: {self.workflow_id[:8]}..."
                )

                return self.workflow_id

            except Exception as e:
                logger.error(
                    f"[DIAGNOSTIC] Failed to load workflow definition from {workflow.phases_folder_path}: {e}"
                )
                logger.warning(
                    "[DIAGNOSTIC] Will set workflow_id anyway to allow diagnostic agent to work"
                )
                # Even if we can't load the full definition, set the workflow_id
                # so diagnostic checks can still run
                self.workflow_id = workflow.id
                return self.workflow_id

        except Exception as e:
            logger.error(f"[DIAGNOSTIC] Failed to load active workflow: {e}")
            return None
        finally:
            session.close()

    def initialize_workflow(
        self,
        workflow_def: WorkflowDefinition,
        phases_config: Optional["PhasesConfig"] = None,
        folder_path: str = "",
    ) -> str:
        """Initialize a workflow and its phases in the database.

        If a workflow with the same name already exists, updates its phases_folder_path
        instead of creating a new one. This allows config updates on service restart.

        Args:
            workflow_def: Workflow definition loaded from YAML (sdk WorkflowDefinition)
            phases_config: Phases configuration for ticket tracking and result handling
            folder_path: Optional path to the workflow config directory for DB storage

        Returns:
            Workflow ID
        """
        session = self.db_manager.get_session()

        try:
            # SINGLE WORKFLOW POLICY: Check if ANY active workflow exists
            # We maintain only ONE workflow at a time - reuse it on restart
            existing_workflow = (
                session.query(Workflow)
                .filter(Workflow.status.in_(["active", "paused"]))
                .first()
            )

            if existing_workflow:
                # Reuse existing workflow - update phases folder path
                logger.info(
                    f"♻️  Reusing existing workflow '{existing_workflow.name}' (ID: {existing_workflow.id})"
                )
                _folder = folder_path or ""
                logger.info(
                    f"   Updating phases_folder_path from {existing_workflow.phases_folder_path} to {_folder}"
                )

                existing_workflow.phases_folder_path = _folder
                # Update the name to match the current workflow definition
                existing_workflow.name = workflow_def.name
                session.commit()

                workflow_id = existing_workflow.id
                logger.info("✅ Updated workflow with new phases folder path")
            else:
                # Create new workflow record
                workflow_id = str(uuid.uuid4())
                workflow = Workflow(
                    id=workflow_id,
                    name=workflow_def.name,
                    phases_folder_path=folder_path or "",
                    status="active",
                )
                session.add(workflow)

                # Only create phase records for NEW workflows
                for phase_def in workflow_def.phases:
                    phase_id = str(uuid.uuid4())
                    # phase_def is a sdk Phase dataclass; use .id as the order value
                    # Convert lists to JSON strings for Text columns
                    import json as _json
                    outputs_raw = (
                        phase_def.outputs
                        if isinstance(phase_def.outputs, list)
                        else ([phase_def.outputs] if phase_def.outputs else [])
                    )
                    outputs_val = _json.dumps(outputs_raw) if outputs_raw else "[]"
                    next_steps_raw = (
                        phase_def.next_steps
                        if isinstance(phase_def.next_steps, list)
                        else ([phase_def.next_steps] if phase_def.next_steps else [])
                    )
                    next_steps_val = _json.dumps(next_steps_raw) if next_steps_raw else "[]"
                    phase = Phase(
                        id=phase_id,
                        workflow_id=workflow_id,
                        order=phase_def.id,
                        name=phase_def.name,
                        description=phase_def.description,
                        done_definitions=phase_def.done_definitions,
                        additional_notes=phase_def.additional_notes,
                        outputs=outputs_val,
                        next_steps=next_steps_val,
                        working_directory=phase_def.working_directory,
                        validation=(
                            {
                                "enabled": phase_def.validation.enabled,
                                "criteria": phase_def.validation.criteria,
                            }
                            if phase_def.validation
                            else None
                        ),
                        thinking_level=phase_def.thinking_level,
                    )
                    session.add(phase)

                    # Create initial execution record
                    execution = PhaseExecution(
                        id=str(uuid.uuid4()),
                        phase_id=phase_id,
                        workflow_execution_id=workflow_id,
                        status="pending",
                    )
                    session.add(execution)

                # Create BoardConfig if ticket tracking is enabled
                if (
                    phases_config
                    and phases_config.enable_tickets
                    and phases_config.board_config
                ):
                    from src.core.database import BoardConfig

                    board_id = f"board-{str(uuid.uuid4())}"
                    # Read global defaults for human approval
                    config = get_config()
                    default_human_review = getattr(
                        config, "default_human_review", False
                    )
                    default_approval_timeout = getattr(
                        config, "default_approval_timeout", 1800
                    )

                    board_config = BoardConfig(
                        id=board_id,
                        workflow_id=workflow_id,
                        name=f"{workflow_def.name} Board",
                        columns=phases_config.board_config.get("columns", []),
                        ticket_types=phases_config.board_config.get(
                            "ticket_types", ["task"]
                        ),
                        default_ticket_type=phases_config.board_config.get(
                            "default_ticket_type", "task"
                        ),
                        initial_status=phases_config.board_config.get(
                            "initial_status", "backlog"
                        ),
                        auto_assign=phases_config.board_config.get(
                            "auto_assign", False
                        ),
                        require_comments_on_status_change=phases_config.board_config.get(
                            "require_comments_on_status_change", False
                        ),
                        allow_reopen=phases_config.board_config.get(
                            "allow_reopen", True
                        ),
                        track_time=phases_config.board_config.get("track_time", False),
                        # Human approval settings (with global defaults, can be overridden in board_config)
                        ticket_human_review=phases_config.board_config.get(
                            "ticket_human_review", default_human_review
                        ),
                        approval_timeout_seconds=phases_config.board_config.get(
                            "approval_timeout_seconds", default_approval_timeout
                        ),
                    )
                    session.add(board_config)
                    logger.info(
                        f"Created BoardConfig for workflow '{workflow_def.name}' with {len(phases_config.board_config.get('columns', []))} columns"
                    )

                session.commit()
                logger.info(
                    f"Created new workflow '{workflow_def.name}' with {len(workflow_def.phases)} phases"
                )

            # Store as active workflow
            self.active_workflow = workflow_def
            self.workflow_id = workflow_id

            return workflow_id

        except Exception as e:
            logger.error(f"Failed to initialize workflow: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    def get_phase_for_task(
        self,
        phase_id: Optional[str] = None,
        order: Optional[int] = None,
        requesting_agent_id: Optional[str] = None,
        workflow_id: Optional[str] = None,
    ) -> Optional[str]:
        """Get phase ID for task creation.

        Args:
            phase_id: Explicit phase ID (for cross-phase task creation)
            order: Phase order number (for cross-phase task creation)
            requesting_agent_id: ID of the agent creating the task
            workflow_id: Explicit workflow ID to use (for multi-workflow support)

        Returns:
            Phase ID or None if not found
        """
        # If explicit phase_id provided, use it (cross-phase task creation)
        if phase_id:
            return phase_id

        # Use provided workflow_id, falling back to the singleton for backward compatibility
        target_workflow_id = workflow_id or self.workflow_id

        # If phase order provided, find that phase (cross-phase task creation)
        if order is not None and target_workflow_id:
            session = self.db_manager.get_session()
            try:
                phase = (
                    session.query(Phase)
                    .filter_by(workflow_id=target_workflow_id, order=order)
                    .first()
                )
                return phase.id if phase else None
            finally:
                session.close()

        # If agent is creating the task, use the agent's current phase
        if requesting_agent_id and requesting_agent_id != "claude-mcp":
            session = self.db_manager.get_session()
            try:
                # Find the agent's current task and its phase
                from src.core.database import Agent, Task

                agent = session.query(Agent).filter_by(id=requesting_agent_id).first()
                if agent and agent.current_task_id:
                    task = (
                        session.query(Task).filter_by(id=agent.current_task_id).first()
                    )
                    if task and task.phase_id:
                        return task.phase_id
            finally:
                session.close()

        # Default to first pending/in_progress phase
        return self.get_current_phase_id()

    def get_current_phase_id(self) -> Optional[str]:
        """Get the current active phase ID.

        Returns:
            Phase ID of the current active phase
        """
        if not self.workflow_id:
            return None

        session = self.db_manager.get_session()
        try:
            # Find first non-completed phase
            execution = (
                session.query(PhaseExecution)
                .join(Phase)
                .filter(
                    Phase.workflow_id == self.workflow_id,
                    PhaseExecution.status.in_(["pending", "in_progress"]),
                )
                .order_by(Phase.order)
                .first()
            )

            return execution.phase_id if execution else None
        finally:
            session.close()

    def get_phase_context(self, phase_id: str) -> Optional[PhaseContext]:
        """Get context for a specific phase.

        Args:
            phase_id: Phase ID

        Returns:
            PhaseContext or None if not found
        """
        logger.info(f"=== GET_PHASE_CONTEXT DEBUG for phase_id: {phase_id} ===")
        logger.info(f"PhaseManager workflow_id: {self.workflow_id}")
        logger.debug(f"PhaseManager active_workflow: {self.active_workflow}")

        session = self.db_manager.get_session()
        try:
            logger.info(f"Querying database for phase with id: {phase_id}")
            phase = session.query(Phase).filter_by(id=phase_id).first()
            logger.info(f"Database query result: {phase}")

            if not phase:
                logger.warning(f"No phase found in database with id: {phase_id}")
                # List all phases for debugging
                all_phases = session.query(Phase).all()
                logger.info(
                    f"All phases in database: {[(p.id, p.name, p.order) for p in all_phases]}"
                )
                return None

            logger.info(
                f"Found phase: {phase.name} (order: {phase.order}) in workflow: {phase.workflow_id}"
            )

            # Get all phases in workflow
            all_phases = (
                session.query(Phase)
                .filter_by(workflow_id=phase.workflow_id)
                .order_by(Phase.order)
                .all()
            )

            # Convert DB Phase rows to sdk Phase objects
            sdk_phases = []
            current_sdk_phase = None
            for p in all_phases:
                # Reconstruct outputs/next_steps as lists
                def _to_list(val):
                    if val is None:
                        return []
                    if isinstance(val, list):
                        return val
                    return [val]

                sdk_phase = SdkPhase(
                    id=p.order,
                    name=p.name,
                    description=p.description or "",
                    done_definitions=p.done_definitions or [],
                    working_directory=p.working_directory or ".",
                    additional_notes=p.additional_notes or "",
                    outputs=_to_list(p.outputs),
                    next_steps=_to_list(p.next_steps),
                    cli_tool=p.cli_tool,
                    cli_model=p.cli_model,
                    thinking_level=p.thinking_level,
                )
                sdk_phases.append(sdk_phase)
                if p.id == phase_id:
                    current_sdk_phase = sdk_phase

            if not current_sdk_phase:
                return None

            # Count tasks
            active_tasks = (
                session.query(Task)
                .filter_by(
                    phase_id=phase_id,
                )
                .filter(Task.status.in_(["pending", "assigned", "in_progress"]))
                .count()
            )

            completed_tasks = (
                session.query(Task).filter_by(phase_id=phase_id, status="done").count()
            )

            # Get execution status
            execution = (
                session.query(PhaseExecution).filter_by(phase_id=phase_id).first()
            )
            status = execution.status if execution else "pending"

            return PhaseContext(
                phase_id=phase_id,
                workflow_id=phase.workflow_id,
                phase=current_sdk_phase,
                all_phases=sdk_phases,
                current_status=status,
                active_tasks=active_tasks,
                completed_tasks=completed_tasks,
            )

        finally:
            session.close()

    def check_phase_completion(self, phase_id: str) -> bool:
        """Check if a phase is complete based on its done_definitions.

        Args:
            phase_id: Phase ID to check

        Returns:
            True if phase is complete
        """
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                return False

            # Check if all tasks in phase are complete.
            # "failed" is intentionally excluded: failed is a terminal state, and
            # a phase that has some done + some failed tasks (but no active tasks)
            # should still advance. The stalled-phase handler in the monitor covers
            # the all-failed (done_tasks == 0) case separately.
            incomplete_tasks = (
                session.query(Task)
                .filter_by(phase_id=phase_id)
                .filter(Task.status.in_(["pending", "assigned", "in_progress"]))
                .count()
            )

            if incomplete_tasks > 0:
                return False

            # Check if phase has any completed tasks
            completed_tasks = (
                session.query(Task).filter_by(phase_id=phase_id, status="done").count()
            )

            # Phase is complete if it has completed tasks and no incomplete ones
            return completed_tasks > 0

        finally:
            session.close()

    @staticmethod
    def _close_execution(
        session, execution, status: str, summary: Optional[str] = None
    ) -> None:
        """Shared completion-write boilerplate for PhaseExecution rows.

        Extracted so the same status/completed_at/summary/commit sequence
        isn't hand-copied at every branch of mark_phase_complete (SOLID
        review 2.12 — this exact block was duplicated 3+ times).
        """
        execution.status = status
        execution.completed_at = datetime.utcnow()
        if summary is not None:
            execution.completion_summary = summary
        session.commit()

    def _advance_or_complete(self, session, phase_id: str) -> Dict[str, Any]:
        """Start the next phase, or complete the workflow if there isn't one."""
        next_started = self._start_next_phase(session, phase_id)
        if not next_started:
            self._complete_workflow(session)
            return {
                "action": "continue",
                "target_phase": None,
                "target_phase_id": None,
                "should_continue": False,
            }
        return {
            "action": "continue",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": True,
        }

    def _advance_or_complete_with_phase_info(
        self, session, phase_id: str
    ) -> Dict[str, Any]:
        """Like _advance_or_complete, but includes next phase info in the result.

        FIX #19: Shared by _handle_force_continue and _handle_evaluation_continue
        to eliminate duplicated advance-or-complete logic.
        """
        result = self._advance_or_complete(session, phase_id)
        if result.get("should_continue"):
            next_phase = self._find_next_phase(session, phase_id)
            result["target_phase"] = next_phase.name if next_phase else None
            result["target_phase_id"] = next_phase.id if next_phase else None
        return result

    def _handle_force_continue(self, session, phase, execution, summary) -> Dict[str, Any]:
        # FIX #19: Delegate to _advance_or_complete_with_phase_info.
        self._close_execution(session, execution, "completed", summary)
        return self._advance_or_complete_with_phase_info(session, phase.id)

    def _handle_force_fail(self, session, execution, summary) -> Dict[str, Any]:
        self._close_execution(session, execution, "failed", summary)
        self._fail_workflow(session, summary)
        return {
            "action": "fail",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": False,
        }

    def _handle_force_goto(
        self, session, phase, execution, summary, target_phase_name: str, reason: str
    ) -> Dict[str, Any]:
        """Same effect as _handle_evaluation_goto, driven by an explicit
        target/reason instead of an orchestrator Evaluation object -- used
        after arbitration resolves with a "goto" decision."""
        self._close_execution(session, execution, "completed", summary)

        target_phase = self._find_phase_by_name_or_order(
            session, phase.workflow_id, target_phase_name
        )
        if not target_phase:
            logger.warning(f"[ARBITRATE] Goto target phase not found: {target_phase_name}")
            return self._advance_or_complete(session, phase.id)

        logger.info(f"[ARBITRATE] Goto phase {target_phase.name} from {phase.name}")
        stale = (
            session.query(PhaseExecution)
            .join(Phase)
            .filter(
                Phase.workflow_id == phase.workflow_id,
                Phase.order >= target_phase.order,
                Phase.order < phase.order,
                PhaseExecution.status == "in_progress",
            )
            .all()
        )
        for s in stale:
            s.status = "completed"
            s.completed_at = datetime.utcnow()
        if stale:
            session.commit()

        return {
            "action": "goto",
            "target_phase": target_phase.name,
            "target_phase_id": target_phase.id,
            "should_continue": True,
            "reason": reason,
            "metadata": {},
        }

    def _handle_sequential_mode(self, session, phase, execution, summary) -> Dict[str, Any]:
        self._close_execution(session, execution, "completed", summary)
        logger.info(f"Marked phase {phase.name} as complete (sequential mode)")
        return self._advance_or_complete(session, phase.id)

    def _handle_evaluation_continue(
        self, session, phase, execution, summary, evaluation
    ) -> Dict[str, Any]:
        # FIX #19: Delegate to _advance_or_complete_with_phase_info.
        self._close_execution(session, execution, "completed", summary)
        return self._advance_or_complete_with_phase_info(session, phase.id)

    def _handle_evaluation_skip(
        self, session, phase, execution, summary, evaluation
    ) -> Dict[str, Any]:
        # SKIP is "continue, but logged differently" per OrchestrationAction's
        # own docstring — it was previously undispatched here and silently
        # fell through to a generic continue with no logging (SOLID review
        # 2.12). Behavior matches CONTINUE; only the log message differs.
        self._close_execution(session, execution, "completed", summary)
        logger.info(f"Skipping past phase {phase.name}: {evaluation.reason}")
        next_started = self._start_next_phase(session, phase.id)
        if not next_started:
            self._complete_workflow(session)
            return {
                "action": "continue",
                "target_phase": None,
                "target_phase_id": None,
                "should_continue": False,
            }
        next_phase = self._find_next_phase(session, phase.id)
        return {
            "action": "continue",
            "target_phase": next_phase.name if next_phase else None,
            "target_phase_id": next_phase.id if next_phase else None,
            "should_continue": True,
        }

    def _handle_evaluation_retry(
        self, session, phase, execution, summary, evaluation
    ) -> Dict[str, Any]:
        execution.status = "pending"
        execution.started_at = None
        # Same reset as _start_next_phase -- the task-creation/evaluation
        # claim is one-time-per-cycle, not permanent. Without this, a
        # retried phase would find its claim already set from the attempt
        # just evaluated and never get a fresh task created for the retry.
        execution.task_creation_claimed_at = None
        session.commit()

        logger.info(
            f"Retrying phase {phase.name} "
            f"({evaluation.metadata.get('retry_count', 0)}/"
            f"{evaluation.metadata.get('max_retries', '?')})"
        )
        return {
            "action": "retry",
            "target_phase": phase.name,
            "target_phase_id": phase.id,
            "should_continue": True,
            # Same reason as _handle_evaluation_goto's "reason"/"metadata" --
            # threaded through to the retried task's description.
            "reason": evaluation.reason,
            "metadata": evaluation.metadata,
        }

    def _handle_evaluation_goto(
        self, session, phase, execution, summary, evaluation
    ) -> Dict[str, Any]:
        self._close_execution(session, execution, "completed", summary)

        # Find target phase — Monitor will create task+agent
        target_phase = self._find_phase_by_name_or_order(
            session, phase.workflow_id, evaluation.target_phase
        )
        if target_phase:
            logger.info(f"Goto phase {target_phase.name} from {phase.name}")
            # Reset any phase_executions between target and current that are
            # still "in_progress" — these are stale records from a prior pass
            # that were never closed when the pipeline rewound.
            stale = (
                session.query(PhaseExecution)
                .join(Phase)
                .filter(
                    Phase.workflow_id == phase.workflow_id,
                    Phase.order >= target_phase.order,
                    Phase.order < phase.order,
                    PhaseExecution.status == "in_progress",
                )
                .all()
            )
            for s in stale:
                s.status = "completed"
                s.completed_at = datetime.utcnow()
            if stale:
                session.commit()
                logger.info(
                    f"Reset {len(stale)} stale in_progress phase(s) before GOTO to {target_phase.name}"
                )
            return {
                "action": "goto",
                "target_phase": target_phase.name,
                "target_phase_id": target_phase.id,
                "should_continue": True,
                # Threaded through to the new task's description by
                # _create_phase_task -- without this, a goto triggered by a
                # gated phase finding real issues (e.g. adversarial_review's
                # BLOCKER count) produced a task indistinguishable from a
                # fresh "implement per architecture" task, with zero
                # reference to what was actually flagged. The agent had to
                # independently rediscover the problem instead of being told.
                "reason": evaluation.reason,
                "metadata": evaluation.metadata,
            }
        else:
            logger.warning(f"Target phase not found: {evaluation.target_phase}")
            return self._advance_or_complete(session, phase.id)

    def _handle_evaluation_arbitrate(
        self, session, phase, execution, summary, evaluation
    ) -> Dict[str, Any]:
        # Budget exhausted — orchestrator._trigger_arbitration (called by
        # _fire_phase_transition right after this returns) spawns an
        # arbitration agent; once it writes arbitration_result.json,
        # _maybe_resolve_arbitration resumes the pipeline (continue/goto) or
        # fails it (never leaves it silently paused for a human).
        #
        # Status must be "in_progress", NOT "pending": _case_completed_
        # with_successor picks its target by "next pending phase with
        # order > the latest COMPLETED phase's order" -- not "the next
        # phase in full pipeline order". A phase reopened as "pending"
        # while LATER phases are already completed (the common case here:
        # the phase whose gate fired this arbitrate decision closes its
        # own execution to "completed" before handing off) is invisible to
        # that ordering logic, and _advance_phases races ahead to whatever
        # pending phase comes after the latest completed one instead --
        # completely bypassing the phase actually awaiting arbitration.
        # "in_progress" (with a task already existing -- the arbitration
        # task) reads as a normal active phase to every other case.
        execution.status = "in_progress"
        # Same reset as _start_next_phase/_handle_evaluation_retry -- see
        # those for why this one-time-per-cycle claim must not survive a
        # phase being reopened for further work.
        execution.task_creation_claimed_at = None
        session.commit()
        logger.warning(
            f"[ARBITRATE] Phase {phase.name} needs arbitration: {evaluation.reason}"
        )
        return {
            "action": "arbitrate",
            "target_phase": phase.name,
            "target_phase_id": phase.id,
            "should_continue": True,
            "arbitration_metadata": evaluation.metadata,
        }

    def _handle_evaluation_fail(
        self, session, phase, execution, summary, evaluation
    ) -> Dict[str, Any]:
        self._close_execution(
            session, execution, "failed", f"Failed: {evaluation.reason}"
        )
        logger.error(f"Phase {phase.name} failed: {evaluation.reason}")
        self._fail_workflow(session, evaluation.reason)
        return {
            "action": "fail",
            "target_phase": None,
            "target_phase_id": None,
            "should_continue": False,
        }

    # Dispatch table for orchestrator.evaluate() results — replaces a long
    # if/elif chain over OrchestrationAction so a new action is registered
    # here instead of silently falling through the final default (this is
    # exactly what happened to SKIP before this refactor; see
    # _handle_evaluation_skip). SOLID review finding 2.12.
    # FIX #22: Use string-key dispatch instead of unbound function references.
    # Handler method names follow the pattern _handle_evaluation_{action.value}.
    _EVALUATION_ACTION_VALUES = {
        "continue",
        "skip",
        "retry",
        "goto",
        "arbitrate",
        "fail",
    }

    def mark_phase_complete(
        self,
        phase_id: str,
        summary: str = "",
        phase_output: Dict[str, Any] = None,
        force_action: str = None,
        force_target_phase: str = None,
        force_reason: str = None,
    ) -> Dict[str, Any]:
        """Mark a phase as complete and evaluate with orchestrator.

        Args:
            phase_id: Phase ID to mark complete
            summary: Completion summary
            phase_output: Output from the phase for orchestrator evaluation
            force_action: If set ("continue", "goto", or "fail"), skip orchestrator
                evaluation and use this action directly. Used after arbitration
                resolves.
            force_target_phase: Required when force_action == "goto" -- the
                target phase name/order the arbiter chose.
            force_reason: Threaded into the next task's description when
                force_action == "goto", same as a normal goto's reason.

        Returns:
            Dict with keys:
                - action: 'continue' | 'goto' | 'retry' | 'fail' | 'arbitrate'
                - target_phase: phase name/order for goto (None for continue/retry/fail)
                - target_phase_id: UUID of target phase (None for continue/retry/fail)
                - should_continue: bool
        """
        session = self.db_manager.get_session()
        try:
            phase = session.query(Phase).filter_by(id=phase_id).first()
            if not phase:
                return {
                    "action": "continue",
                    "target_phase": None,
                    "target_phase_id": None,
                    "should_continue": True,
                }

            execution = (
                session.query(PhaseExecution).filter_by(phase_id=phase_id).first()
            )

            if not execution:
                return {
                    "action": "continue",
                    "target_phase": None,
                    "target_phase_id": None,
                    "should_continue": True,
                }

            # Idempotency guard: if phase is already completed, don't re-evaluate.
            # This prevents race conditions where the spec gate and _advance_phases
            # both try to mark the same phase complete.
            if execution.status == "completed":
                logger.debug(
                    f"Phase {phase.name} already completed — skipping duplicate mark_phase_complete"
                )
                # Return 'already_completed' so callers know NOT to create tasks.
                # The transition was already handled by the first caller.
                return {
                    "action": "already_completed",
                    "target_phase": None,
                    "target_phase_id": None,
                    "should_continue": False,
                }

            # Arbitration override: skip evaluation and use the resolved action.
            if force_action == "continue":
                return self._handle_force_continue(session, phase, execution, summary)
            elif force_action == "goto":
                return self._handle_force_goto(
                    session, phase, execution, summary, force_target_phase, force_reason
                )
            elif force_action == "fail":
                return self._handle_force_fail(session, execution, summary)

            # Get orchestrator config
            orchestrator = self._get_orchestrator(session, phase.workflow_id)

            # If no orchestrator config or sequential mode, use simple flow
            if not orchestrator or orchestrator.config.type == "sequential":
                return self._handle_sequential_mode(session, phase, execution, summary)

            # Sync the persisted GOTO counter into the orchestrator before
            # evaluating. WorkflowOrchestrator.total_gotos is in-memory only,
            # and a fresh PhaseManager (hence a fresh, uncached orchestrator)
            # gets constructed on nearly every mark_phase_complete call (see
            # task_completion_service.py's fire_spec_gate_if_ready and
            # autopilot/orchestrator.py's periodic sweep) — without this, the
            # counter silently reset to 0 every time and max_total_gotos never
            # actually fired, letting a failing gate goto-loop forever.
            workflow_row = (
                session.query(Workflow).filter_by(id=phase.workflow_id).first()
            )
            orchestrator.total_gotos = (workflow_row.total_gotos or 0) if workflow_row else 0

            # Same sync for the per-phase RETRY budget (eval_point.max_retries)
            # -- WorkflowOrchestrator.phase_retry_counts is the same kind of
            # in-memory-only dict as total_gotos was, and gets reset for the
            # same reason (fresh orchestrator per call). Only this phase's
            # entry needs syncing since evaluate() only touches phase_name's.
            orchestrator.phase_retry_counts[phase.name] = phase.retry_count or 0

            # Evaluating mode - use orchestrator to decide flow
            phase_history = self._get_phase_history(session, phase.workflow_id)
            evaluation = orchestrator.evaluate(
                phase_name=phase.name,
                phase_output=phase_output or {},
                phase_history=phase_history,
            )

            if workflow_row is not None and orchestrator.total_gotos != (
                workflow_row.total_gotos or 0
            ):
                workflow_row.total_gotos = orchestrator.total_gotos
                session.commit()

            new_phase_retry_count = orchestrator.phase_retry_counts.get(phase.name, 0)
            if new_phase_retry_count != (phase.retry_count or 0):
                phase.retry_count = new_phase_retry_count
                session.commit()

            logger.info(
                f"Orchestrator evaluated {phase.name}: "
                f"action={evaluation.action.value}, reason={evaluation.reason}"
            )

            # FIX #22: Use getattr dispatch instead of unbound function table.
            action_value = evaluation.action.value
            if action_value in self._EVALUATION_ACTION_VALUES:
                handler = getattr(self, f"_handle_evaluation_{action_value}")
                return handler(session, phase, execution, summary, evaluation)

            return {
                "action": "continue",
                "target_phase": None,
                "target_phase_id": None,
                "should_continue": True,
            }

        except Exception as e:
            logger.error(f"Failed to mark phase complete: {e}")
            session.rollback()
            return {
                "action": "continue",
                "target_phase": None,
                "target_phase_id": None,
                "should_continue": True,
            }
        finally:
            session.close()

    def _get_orchestrator(
        self, session, workflow_id: str
    ) -> Optional[WorkflowOrchestrator]:
        """Get orchestrator for a workflow (cached to persist state)."""
        try:
            # Return cached orchestrator if exists
            if workflow_id in self._orchestrators:
                return self._orchestrators[workflow_id]

            workflow = session.query(Workflow).filter_by(id=workflow_id).first()
            if not workflow or not workflow.definition_id:
                return None

            definition = (
                session.query(DBWorkflowDefinition)
                .filter_by(id=workflow.definition_id)
                .first()
            )
            if not definition or not definition.orchestrator_config:
                return None

            config = OrchestratorConfig.from_dict(definition.orchestrator_config)
            if config.type == "sequential":
                return None

            # Create and cache orchestrator
            orchestrator = WorkflowOrchestrator(config)
            self._orchestrators[workflow_id] = orchestrator
            return orchestrator
        except Exception as e:
            logger.error(f"Failed to get orchestrator: {e}")
            return None

    def _get_phase_history(self, session, workflow_id: str) -> List[Dict[str, Any]]:
        """Get history of completed phases."""
        executions = (
            session.query(PhaseExecution)
            .join(Phase)
            .filter(Phase.workflow_id == workflow_id)
            .all()
        )

        return [
            {
                "phase": ex.phase.name if ex.phase else "unknown",
                "status": ex.status,
                "summary": ex.completion_summary,
                "completed_at": ex.completed_at.isoformat()
                if ex.completed_at
                else None,
            }
            for ex in executions
            if ex.status in ("completed", "failed")
        ]

    def _find_phase_by_name_or_order(
        self, session, workflow_id: str, name_or_order
    ) -> Optional[Phase]:
        """Find phase by name or order number."""
        # Try by name first
        phase = (
            session.query(Phase)
            .filter_by(workflow_id=workflow_id)
            .filter(Phase.name == name_or_order)
            .first()
        )

        if phase:
            return phase

        # Try by order
        try:
            order = int(name_or_order)
            phase = (
                session.query(Phase)
                .filter_by(workflow_id=workflow_id, order=order)
                .first()
            )
            return phase
        except (ValueError, TypeError):
            return None

    def _start_phase(self, session, phase_id: str) -> None:
        """Start a specific phase."""
        execution = session.query(PhaseExecution).filter_by(phase_id=phase_id).first()

        if execution and execution.status == "pending":
            execution.status = "in_progress"
            execution.started_at = datetime.utcnow()
            session.commit()
            logger.info(f"Started phase {phase_id}")

    def _fail_workflow(self, session, reason: str) -> None:
        """Mark workflow as failed."""
        if self.workflow_id:
            workflow = session.query(Workflow).filter_by(id=self.workflow_id).first()
            if workflow:
                workflow.status = "failed"
                session.commit()
                logger.error(f"Workflow {self.workflow_id} failed: {reason}")

    def _find_next_phase(self, session, current_phase_id: str):
        """Find the next phase after current one (without starting it)."""
        current = session.query(Phase).filter_by(id=current_phase_id).first()
        if not current:
            return None
        return (
            session.query(Phase)
            .filter(
                Phase.workflow_id == current.workflow_id, Phase.order > current.order
            )
            .order_by(Phase.order)
            .first()
        )

    def _start_next_phase(self, session, current_phase_id: str) -> bool:
        """Start the next phase after current one completes.

        Args:
            session: Database session
            current_phase_id: Current phase ID

        Returns:
            True if a next phase was started, False if this was the last phase
        """
        current_phase = session.query(Phase).filter_by(id=current_phase_id).first()
        if not current_phase:
            return False

        # Don't advance phases on a completed workflow — stale mark_phase_complete
        # calls from the spec-gate or 3a path can fire after _complete_workflow runs.
        workflow = (
            session.query(Workflow).filter_by(id=current_phase.workflow_id).first()
        )
        if not workflow or workflow.status not in ("active", "paused"):
            logger.debug(
                f"[PHASE] _start_next_phase skipped — workflow is {getattr(workflow, 'status', 'missing')}"
            )
            return False

        # Find next phase
        next_phase = (
            session.query(Phase)
            .filter(
                Phase.workflow_id == current_phase.workflow_id,
                Phase.order > current_phase.order,
            )
            .order_by(Phase.order)
            .first()
        )

        if next_phase:
            # Update execution status for pending or completed phases
            # (completed = re-run after goto reconvergence)
            execution = (
                session.query(PhaseExecution).filter_by(phase_id=next_phase.id).first()
            )

            if execution and execution.status in ("pending", "completed"):
                execution.status = "in_progress"
                execution.started_at = datetime.utcnow()
                # Reset the task-creation/evaluation claim (see orchestrator.py's
                # _claim_phase_task_creation) -- it's a one-time-per-cycle lock,
                # not a permanent one. Without this reset, a phase re-run after
                # goto reconvergence would find its claim already set from the
                # PREVIOUS cycle and never let a new task get created for it.
                execution.task_creation_claimed_at = None
                session.commit()

                logger.info(f"Started next phase: {next_phase.name}")
            return True

        return False

    def _complete_workflow(self, session) -> None:
        """Mark the workflow as completed when the last phase finishes."""
        if not self.workflow_id:
            return

        workflow = session.query(Workflow).filter_by(id=self.workflow_id).first()
        if workflow and workflow.status == "active":
            workflow.status = "completed"
            session.commit()
            logger.info(f"Workflow {self.workflow_id} completed (all phases done)")
            self._populate_feature_folder(session, workflow)

    def _populate_feature_folder(self, session, workflow) -> None:
        """Create .hephaestus/features/<dir>/ and copy all run artifacts into it.

        The worktree is intentionally kept after git_commit_push merges so both
        committed docs/ and git-excluded .hephaestus/tmux/ logs are available here.
        """
        try:
            import shutil as _shutil
            from datetime import datetime as _dt
            from pathlib import Path as _P

            wt_path = workflow.working_directory if workflow.working_directory else None
            if not wt_path:
                logger.debug("[FEATURE-FOLDER] No worktree path — skipping")
                return

            wt = _P(wt_path)

            # Derive the real project root for the features output dir.
            project_path = wt
            if WORKTREES_SUBDIR in str(project_path):
                base = project_path
                while base.name != WORKTREES_SUBDIR and base.parent != base:
                    base = base.parent
                project_path = (
                    base.parent if base.name == WORKTREES_SUBDIR else project_path
                )

            if not project_path.is_dir():
                logger.warning(
                    f"[FEATURE-FOLDER] Project root {project_path} not found"
                )
                return

            # Prefer design name over workflow definition name so the folder reads
            # "add_calculator" not "Autopilot_Multi-Agent_Pipeline".
            from src.core.database import AutopilotDesign as _AD2

            _design_label = None
            if workflow.design_id:
                _d = session.query(_AD2).filter_by(id=workflow.design_id).first()
                _design_label = _d.name if _d else None
            design_name = (
                (_design_label or workflow.name or "feature")
                .replace(" ", "_")
                .replace("/", "_")
            )
            safe_name = "".join(
                c if c.isalnum() or c in "-_" else "_" for c in design_name
            )[:40]
            timestamp = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
            feature_dir = (
                project_path
                / CONTEXT_DIR_NAME
                / "features"
                / f"{timestamp}_{safe_name}"
            )
            docs_dir = feature_dir / "docs"
            docs_dir.mkdir(parents=True, exist_ok=True)

            _DOC_EXTENSIONS = {".md", ".json", ".txt", ".log", ".csv", ".html"}

            # 1. Production artifacts from worktree's docs/ (merged to main but
            #    the worktree copy is canonical and complete here).
            wt_docs = wt / "docs"
            if wt_docs.is_dir():
                for f in wt_docs.rglob("*"):
                    if (
                        f.is_file()
                        and f.suffix in _DOC_EXTENSIONS
                        and "tmux" not in f.parts
                    ):
                        rel = f.relative_to(wt_docs)
                        dest = docs_dir / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if not dest.exists():
                            _shutil.copy2(str(f), str(dest))

            # 2. feature_report.html → feature dir root (where UI expects it)
            for candidate in [
                docs_dir / "feature_report.html",
                wt_docs / "feature_report.html",
            ]:
                if candidate.is_file():
                    dest = feature_dir / "feature_report.html"
                    if not dest.exists():
                        _shutil.copy2(str(candidate), str(dest))
                    break

            # 3. Tmux logs — written to project root's .hephaestus/tmux/ so they
            #    survive worktree removal by git_commit_push. Also check the
            #    worktree itself as a fallback for any stragglers.
            tmux_dest = feature_dir / "tmux"
            for tmux_src in [
                project_path / CONTEXT_DIR_NAME / "tmux",
                wt / CONTEXT_DIR_NAME / "tmux",
            ]:
                if tmux_src.is_dir():
                    tmux_dest.mkdir(exist_ok=True)
                    for f in tmux_src.glob("*.log"):
                        dest = tmux_dest / f.name
                        if not dest.exists():
                            _shutil.copy2(str(f), str(dest))
                            logger.info(f"[FEATURE-FOLDER] Copied tmux log {f.name}")

            # 4. Link design → feature folder in DB
            from src.core.database import AutopilotDesign as _AD
            from src.core.status_derivation import derive_design_status

            _design_name_for_metrics = safe_name
            if workflow.design_id:
                design = session.query(_AD).filter_by(id=workflow.design_id).first()
                if design:
                    design.feature_folder = str(feature_dir)
                    _design_name_for_metrics = design.name or safe_name
                    session.commit()
                    # Don't unconditionally mark the design "completed" here —
                    # a design can have multiple features/workflows, and this
                    # workflow finishing doesn't mean sibling features have
                    # too. Derive the real rollup status instead (H-3 /
                    # SOLID review 2.1: this was the most severe instance of
                    # bypassing the centralized status derivation, able to
                    # mark a multi-feature design "completed" the moment the
                    # first feature's workflow finished).
                    derived_status = derive_design_status(
                        session, design.id, write_back=True
                    )
                    if derived_status == "completed" and not design.completed_at:
                        design.completed_at = _dt.utcnow()
                        session.commit()
                    logger.info(
                        f"[FEATURE-FOLDER] Design {design.id[:8]} → "
                        f"{feature_dir.name} (status={derived_status})"
                    )

            # 5. Write pipeline_metrics.json so forensics has real timestamps even
            #    if the orchestrator finalization code never runs (e.g. it was restarted).
            #    The orchestrator will overwrite this with a more complete version if it runs.
            import json as _json

            metrics_path = docs_dir / "pipeline_metrics.json"
            if not metrics_path.exists():
                try:
                    _metrics = {
                        "design_name": _design_name_for_metrics,
                        "workflow_id": self.workflow_id,
                        "project_path": str(project_path),
                        "docs_dir": str(docs_dir),
                        "feature_folder": str(feature_dir),
                        "completed_at": _dt.utcnow().isoformat() + "Z",
                        "stop_reason": "completed",
                        # qa_passed / product_validated: orchestrator fills these;
                        # leave null here since phase_manager doesn't evaluate gates.
                        "qa_passed": None,
                        "product_validated": None,
                    }
                    metrics_path.write_text(
                        _json.dumps(_metrics, indent=2, default=str)
                    )
                    logger.info(
                        "[FEATURE-FOLDER] Wrote pipeline_metrics.json (phase_manager stub)"
                    )
                except Exception as _me:
                    logger.debug(
                        f"[FEATURE-FOLDER] Could not write pipeline_metrics.json: {_me}"
                    )

            logger.info(f"[FEATURE-FOLDER] Created {feature_dir}")
        except Exception as e:
            logger.warning(f"[FEATURE-FOLDER] Failed to populate feature folder: {e}")

    def get_workflow_status(self) -> Dict[str, Any]:
        """Get current workflow status.

        Returns:
            Dictionary with workflow status information
        """
        if not self.workflow_id:
            return {"error": "No active workflow"}

        session = self.db_manager.get_session()
        try:
            workflow = session.query(Workflow).filter_by(id=self.workflow_id).first()
            if not workflow:
                return {"error": "Workflow not found"}

            # Get phase statuses
            phases = (
                session.query(Phase)
                .filter_by(workflow_id=self.workflow_id)
                .order_by(Phase.order)
                .all()
            )

            phase_statuses = []
            for phase in phases:
                execution = (
                    session.query(PhaseExecution).filter_by(phase_id=phase.id).first()
                )

                task_stats = {
                    "total": session.query(Task).filter_by(phase_id=phase.id).count(),
                    "completed": session.query(Task)
                    .filter_by(phase_id=phase.id, status="done")
                    .count(),
                    "active": session.query(Task)
                    .filter_by(phase_id=phase.id)
                    .filter(Task.status.in_(["assigned", "in_progress"]))
                    .count(),
                    "failed": session.query(Task)
                    .filter_by(phase_id=phase.id, status="failed")
                    .count(),
                }

                phase_statuses.append(
                    {
                        "order": phase.order,
                        "name": phase.name,
                        "status": execution.status if execution else "pending",
                        "tasks": task_stats,
                    }
                )

            return {
                "workflow_id": self.workflow_id,
                "workflow_name": workflow.name,
                "workflow_status": workflow.status,
                "phases": phase_statuses,
            }

        finally:
            session.close()

    def get_workflow_config(self, workflow_id: str) -> PhasesConfig:
        """Get phases configuration for a workflow.

        Args:
            workflow_id: Workflow ID

        Returns:
            PhasesConfig with loaded configuration or defaults

        Raises:
            ValueError: If workflow not found
        """
        # Check cache first
        if workflow_id in self.phases_config_cache:
            return self.phases_config_cache[workflow_id]

        session = self.db_manager.get_session()
        try:
            workflow = session.query(Workflow).filter_by(id=workflow_id).first()
            if not workflow:
                raise ValueError(f"Workflow not found: {workflow_id}")

            # Load configuration from phases folder
            config = PhaseLoader.load_phases_config(workflow.phases_folder_path)

            # Cache the configuration
            self.phases_config_cache[workflow_id] = config

            logger.info(
                f"Loaded phases config for workflow {workflow_id}: has_result={config.has_result}"
            )
            return config

        finally:
            session.close()

    # ==================== Multi-Workflow Support Methods ====================

    def register_definition(
        self,
        definition_id: str,
        name: str,
        description: str = "",
        phases_config: List[Dict[str, Any]] = None,
        workflow_config: Dict[str, Any] = None,
    ) -> str:
        """Register a workflow definition.

        Args:
            definition_id: Unique ID for the definition (e.g., "prd-to-software")
            name: Human-readable name for the workflow
            description: Description of what this workflow does
            phases_config: List of phase definitions (serialized)
            workflow_config: Workflow configuration (has_result, result_criteria, etc.)

        Returns:
            The definition_id
        """
        session = self.db_manager.get_session()
        try:
            # Check if definition already exists
            existing = (
                session.query(DBWorkflowDefinition).filter_by(id=definition_id).first()
            )
            if existing:
                # Update existing definition
                existing.name = name
                existing.description = description
                existing.phases_config = phases_config or []
                existing.workflow_config = workflow_config or {}
                session.commit()
                logger.info(f"Updated workflow definition: {definition_id}")
            else:
                # Create new definition
                db_definition = DBWorkflowDefinition(
                    id=definition_id,
                    name=name,
                    description=description,
                    phases_config=phases_config or [],
                    workflow_config=workflow_config or {},
                )
                session.add(db_definition)
                session.commit()
                logger.info(f"Registered workflow definition: {definition_id}")

            # Cache in memory
            self.definitions[definition_id] = (
                session.query(DBWorkflowDefinition).filter_by(id=definition_id).first()
            )

            return definition_id

        except Exception as e:
            logger.error(f"Failed to register workflow definition: {e}")
            session.rollback()
            raise
        finally:
            session.close()

    def start_execution(
        self,
        definition_id: str,
        description: str,
        working_directory: str = None,
        launch_params: Dict[str, Any] = None,
        design_id: str = None,
    ) -> str:
        """Start a new workflow execution from a definition.

        Args:
            definition_id: ID of the workflow definition to execute
            description: Description of this specific execution (e.g., "Building URL Shortener")
            working_directory: Working directory for this execution
            launch_params: Parameters from UI launch form to substitute into phases

        Returns:
            workflow_id of the new execution
        """
        with self.db_manager.session_scope() as session:
            # Get the definition
            db_definition = (
                session.query(DBWorkflowDefinition).filter_by(id=definition_id).first()
            )
            if not db_definition:
                raise ValueError(f"Workflow definition not found: {definition_id}")

            # Generate unique workflow ID
            workflow_id = str(uuid.uuid4())
            
            # Resolve project_id from design if provided
            project_id = None
            if design_id:
                from src.core.database import AutopilotDesign
                design = session.query(AutopilotDesign).filter_by(id=design_id).first()
                if design:
                    project_id = design.project_id

            # Create workflow execution
            workflow = Workflow(
                id=workflow_id,
                name=db_definition.name,
                description=description,
                definition_id=definition_id,
                design_id=design_id,
                project_id=project_id,
                phases_folder_path=working_directory or ".",  # Store working dir
                working_directory=working_directory,
                launch_params=launch_params,  # Store launch params for reference
                status="active",
            )
            session.add(workflow)

            # Create phases from definition with parameter substitution
            phases_config = db_definition.phases_config or []
            first_phase_id = None

            for idx, phase_config in enumerate(phases_config):
                phase_id = str(uuid.uuid4())

                # Track first phase for initial task creation
                if idx == 0:
                    first_phase_id = phase_id

                # Helper to serialize lists/dicts as JSON strings for Text columns
                def serialize_for_text(value):
                    if value is None or value == "null":
                        return None
                    if isinstance(value, (list, dict)):
                        return json.dumps(value)
                    return value

                # Apply parameter substitution if launch_params provided
                phase_description = phase_config.get("description", "")
                phase_additional_notes = phase_config.get("additional_notes")
                phase_done_definitions = phase_config.get("done_definitions", [])
                phase_outputs = phase_config.get("outputs")
                phase_next_steps = phase_config.get("next_steps")

                if launch_params:
                    phase_description = substitute_params(
                        phase_description, launch_params
                    )
                    if phase_additional_notes:
                        phase_additional_notes = substitute_params(
                            phase_additional_notes, launch_params
                        )
                    if phase_done_definitions:
                        phase_done_definitions = substitute_params_in_list(
                            phase_done_definitions, launch_params
                        )
                    if phase_outputs:
                        if isinstance(phase_outputs, list):
                            phase_outputs = substitute_params_in_list(
                                phase_outputs, launch_params
                            )
                        elif isinstance(phase_outputs, str):
                            phase_outputs = substitute_params(
                                phase_outputs, launch_params
                            )
                    if phase_next_steps:
                        if isinstance(phase_next_steps, list):
                            phase_next_steps = substitute_params_in_list(
                                phase_next_steps, launch_params
                            )
                        elif isinstance(phase_next_steps, str):
                            phase_next_steps = substitute_params(
                                phase_next_steps, launch_params
                            )

                # Resolve working directory: substitute params, treat "." and "" as inherit
                phase_wd = phase_config.get("working_directory")
                if phase_wd and launch_params:
                    phase_wd = substitute_params(phase_wd, launch_params)
                if not phase_wd or phase_wd == ".":
                    phase_wd = working_directory

                phase = Phase(
                    id=phase_id,
                    workflow_id=workflow_id,
                    order=phase_config.get("order", idx + 1),
                    name=phase_config.get("name", f"Phase {idx + 1}"),
                    description=phase_description,
                    done_definitions=phase_done_definitions,
                    additional_notes=serialize_for_text(phase_additional_notes),
                    outputs=serialize_for_text(phase_outputs),
                    next_steps=serialize_for_text(phase_next_steps),
                    working_directory=phase_wd,
                    validation=serialize_for_text(phase_config.get("validation")),
                    # NOT wrapped in serialize_for_text: self_review is a JSON
                    # column and SQLAlchemy already serializes dict/list values
                    # for JSON columns on its own. Passing it through
                    # serialize_for_text would json.dumps() it into a string
                    # first, so a later `phase.self_review.get("enabled")` read
                    # would fail with AttributeError (str has no .get) --
                    # exactly the latent bug in the `validation` line above,
                    # which nothing has caught yet because no phase YAML sets
                    # `validation:` today (see docs/GAP_CHECK_SELF_LOOP_DESIGN.md).
                    self_review=phase_config.get("self_review"),
                    # Per-phase CLI configuration (optional - falls back to global defaults)
                    cli_tool=phase_config.get("cli_tool"),
                    cli_model=phase_config.get("cli_model"),
                    glm_api_token_env=phase_config.get("glm_api_token_env"),
                    thinking_level=phase_config.get("thinking_level"),
                )
                session.add(phase)

                # Create initial execution record
                execution = PhaseExecution(
                    id=str(uuid.uuid4()),
                    phase_id=phase_id,
                    workflow_execution_id=workflow_id,
                    status="pending",
                )
                session.add(execution)

            # Create BoardConfig if ticket tracking is enabled
            workflow_config_data = db_definition.workflow_config or {}
            if workflow_config_data.get("enable_tickets") and workflow_config_data.get(
                "board_config"
            ):
                from src.core.database import BoardConfig

                board_id = f"board-{str(uuid.uuid4())}"
                config = get_config()
                default_human_review = getattr(config, "default_human_review", False)
                default_approval_timeout = getattr(
                    config, "default_approval_timeout", 1800
                )
                board_config_data = workflow_config_data.get("board_config", {})

                board_config = BoardConfig(
                    id=board_id,
                    workflow_id=workflow_id,
                    name=f"{db_definition.name} Board",
                    columns=board_config_data.get("columns", []),
                    ticket_types=board_config_data.get("ticket_types", ["task"]),
                    default_ticket_type=board_config_data.get(
                        "default_ticket_type", "task"
                    ),
                    initial_status=board_config_data.get("initial_status", "backlog"),
                    auto_assign=board_config_data.get("auto_assign", False),
                    require_comments_on_status_change=board_config_data.get(
                        "require_comments_on_status_change", False
                    ),
                    allow_reopen=board_config_data.get("allow_reopen", True),
                    track_time=board_config_data.get("track_time", False),
                    ticket_human_review=board_config_data.get(
                        "ticket_human_review", default_human_review
                    ),
                    approval_timeout_seconds=board_config_data.get(
                        "approval_timeout_seconds", default_approval_timeout
                    ),
                )
                session.add(board_config)

            # Prepare initial task info if launch_template has phase_1_task_prompt
            # (actual task creation will be done by the API endpoint using the proper flow)
            initial_task_info = None
            launch_template = workflow_config_data.get("launch_template")
            if launch_template and first_phase_id:
                phase_1_task_prompt = launch_template.get("phase_1_task_prompt")
                if phase_1_task_prompt:
                    # Substitute launch params into the task prompt
                    if launch_params:
                        phase_1_task_prompt = substitute_params(
                            phase_1_task_prompt, launch_params
                        )

                    # Return task info for the API endpoint to create properly
                    initial_task_info = {
                        "task_description": phase_1_task_prompt,
                        "phase_id": "1",  # Phase order, not UUID
                        "phase_uuid": first_phase_id,  # real Phase.id, for the task-creation claim
                        "priority": "high",
                        "workflow_id": workflow_id,
                    }
                    logger.info(
                        f"Prepared Phase 1 task info for workflow {workflow_id}"
                    )

            # Track active execution
            self.active_executions[workflow_id] = definition_id

            # For backward compatibility, also set as the active workflow
            if not self.workflow_id:
                self.workflow_id = workflow_id

            # Start the first phase execution so the Monitor can track it
            if first_phase_id:
                self._start_phase(session, first_phase_id)

            logger.info(
                f"Started workflow execution: {workflow_id} (definition: {definition_id})"
            )

            # Return both workflow_id and initial task info
            return workflow_id, initial_task_info

    def get_workflow(self, workflow_id: str) -> Optional[Workflow]:
        """Get a specific workflow execution by ID.

        Args:
            workflow_id: The workflow execution ID

        Returns:
            Workflow object or None if not found
        """
        session = self.db_manager.get_session()
        try:
            workflow = (
                session.query(Workflow)
                .options(joinedload(Workflow.definition))
                .filter_by(id=workflow_id)
                .first()
            )
            if workflow:
                session.expunge(workflow)
            return workflow
        finally:
            session.close()

    def get_definition(self, definition_id: str) -> Optional[DBWorkflowDefinition]:
        """Get a workflow definition by ID.

        Args:
            definition_id: The definition ID

        Returns:
            WorkflowDefinition object or None if not found
        """
        # Check cache first
        if definition_id in self.definitions:
            return self.definitions[definition_id]

        session = self.db_manager.get_session()
        try:
            definition = (
                session.query(DBWorkflowDefinition).filter_by(id=definition_id).first()
            )
            if definition:
                self.definitions[definition_id] = definition
            return definition
        finally:
            session.close()

    def list_definitions(self) -> List[DBWorkflowDefinition]:
        """List all registered workflow definitions.

        Returns:
            List of WorkflowDefinition objects
        """
        session = self.db_manager.get_session()
        try:
            definitions = session.query(DBWorkflowDefinition).all()
            # Expunge objects so they can be accessed after session closes
            for defn in definitions:
                session.expunge(defn)
                self.definitions[defn.id] = defn
            return definitions
        finally:
            session.close()

    def list_active_executions(self, status: str = "all") -> List[Workflow]:
        """List all active workflow executions.

        Args:
            status: Filter by status ("all", "active", "completed", "paused", "failed")

        Returns:
            List of Workflow execution objects
        """
        session = self.db_manager.get_session()
        try:
            query = session.query(Workflow).options(joinedload(Workflow.definition))
            workflows = query.order_by(Workflow.created_at.desc()).all()
            # Expunge from session to allow access after session closes
            for w in workflows:
                session.expunge(w)
            return workflows
        finally:
            session.close()

    def get_phases_for_workflow(self, workflow_id: str) -> List[Phase]:
        """Get phases for a specific workflow execution.

        Args:
            workflow_id: The workflow execution ID

        Returns:
            List of Phase objects ordered by phase order
        """
        session = self.db_manager.get_session()
        try:
            return (
                session.query(Phase)
                .filter_by(workflow_id=workflow_id)
                .order_by(Phase.order)
                .all()
            )
        finally:
            session.close()

    def get_execution_stats(self, workflow_id: str) -> Dict[str, int]:
        """Get task statistics for a workflow execution.

        Args:
            workflow_id: The workflow execution ID

        Returns:
            Dictionary with task counts (active_tasks, total_tasks, done_tasks, failed_tasks)
        """
        session = self.db_manager.get_session()
        try:
            total = session.query(Task).filter_by(workflow_id=workflow_id).count()
            done = (
                session.query(Task)
                .filter_by(workflow_id=workflow_id, status="done")
                .count()
            )
            failed = (
                session.query(Task)
                .filter_by(workflow_id=workflow_id, status="failed")
                .count()
            )
            active = (
                session.query(Task)
                .filter(
                    Task.workflow_id == workflow_id,
                    Task.status.in_(["pending", "assigned", "in_progress"]),
                )
                .count()
            )

            return {
                "total_tasks": total,
                "done_tasks": done,
                "failed_tasks": failed,
                "active_tasks": active,
            }
        finally:
            session.close()

    def get_active_agents_count(self, workflow_id: str) -> int:
        """Get count of active agents for a workflow.

        Args:
            workflow_id: The workflow execution ID

        Returns:
            Number of active agents
        """
        from src.core.database import Agent

        session = self.db_manager.get_session()
        try:
            # Count agents working on tasks in this workflow
            return (
                session.query(Agent)
                .join(Task, Agent.current_task_id == Task.id)
                .filter(
                    Task.workflow_id == workflow_id,
                    Agent.status.in_(["working", "idle"]),
                )
                .count()
            )
        finally:
            session.close()

    def load_active_executions(self) -> None:
        """Load all active workflow executions into memory.

        Called on startup to restore state.
        """
        session = self.db_manager.get_session()
        try:
            # Load all active workflows
            workflows = (
                session.query(Workflow)
                .filter(Workflow.status.in_(["active", "paused"]))
                .all()
            )

            for workflow in workflows:
                if workflow.definition_id:
                    self.active_executions[workflow.id] = workflow.definition_id

            # Load all definitions
            definitions = session.query(DBWorkflowDefinition).all()
            for defn in definitions:
                self.definitions[defn.id] = defn

            logger.info(
                f"Loaded {len(self.active_executions)} active workflow executions"
            )
            logger.info(f"Loaded {len(self.definitions)} workflow definitions")

        finally:
            session.close()
