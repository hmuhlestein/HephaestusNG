"""Service for spawning and managing result validator agents."""

import logging
from typing import Any, Dict, Optional

from src.core.database import DatabaseManager, WorkflowResult
from src.phases.phase_manager import PhaseManager

logger = logging.getLogger(__name__)


class ResultValidatorService:
    """Service for validating workflow results."""

    def __init__(self, db_manager: DatabaseManager, phase_manager: PhaseManager):
        """Initialize the result validator service.

        Args:
            db_manager: Database manager instance
            phase_manager: Phase manager for accessing workflow configuration
        """
        self.db_manager = db_manager
        self.phase_manager = phase_manager

    def should_spawn_validator(self, workflow_id: str) -> tuple[bool, Optional[str]]:
        """
        Check if a validator should be spawned for the workflow.

        Args:
            workflow_id: ID of the workflow

        Returns:
            Tuple of (should_spawn, criteria) where criteria is None if no validation needed
        """
        try:
            config = self.phase_manager.get_workflow_config(workflow_id)

            if not config.has_result:
                logger.info(f"Workflow {workflow_id} does not expect results")
                return False, None

            if not config.result_criteria:
                logger.info(f"Workflow {workflow_id} has no validation criteria")
                return False, None

            logger.info(f"Workflow {workflow_id} requires validation with criteria")
            return True, config.result_criteria

        except Exception as e:
            logger.error(f"Error checking workflow config: {e}")
            return False, None

    def process_validation_outcome(
        self,
        result_id: str,
        passed: bool,
        feedback: str,
        evidence: Optional[Dict[str, Any]] = None,
        validator_agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Process the outcome of result validation.

        Args:
            result_id: ID of the result that was validated
            passed: Whether validation passed
            feedback: Validation feedback
            evidence: Validation evidence
            validator_agent_id: ID of the validator agent

        Returns:
            Dictionary with processing outcome and next actions

        Raises:
            ValueError: If result not found
        """
        with self.db_manager.session_scope() as session:
            result = session.query(WorkflowResult).filter_by(id=result_id).first()
            if not result:
                raise ValueError(f"Result not found: {result_id}")

            # Update result status
            from src.services.workflow_result_service import WorkflowResultService

            result_info = WorkflowResultService.update_result_status(
                result_id=result_id,
                status="validated" if passed else "rejected",
                feedback=feedback,
                evidence=evidence,
                validator_agent_id=validator_agent_id,
                db=session,
            )

            # If validation passed, check workflow termination action
            next_actions = []
            if passed:
                try:
                    config = self.phase_manager.get_workflow_config(result.workflow_id)

                    if config.on_result_found == "stop_all":
                        next_actions.append("terminate_workflow")
                        logger.info(
                            f"Workflow {result.workflow_id} will be terminated due to validated result"
                        )
                    elif config.on_result_found == "do_nothing":
                        next_actions.append("continue_workflow")
                        logger.info(
                            f"Workflow {result.workflow_id} will continue after validated result"
                        )

                except Exception as e:
                    logger.error(f"Error checking workflow termination action: {e}")
                    next_actions.append("continue_workflow")  # Default to continue

            return {
                "result_validation": result_info,
                "validation_passed": passed,
                "next_actions": next_actions,
                "workflow_id": result.workflow_id,
            }
