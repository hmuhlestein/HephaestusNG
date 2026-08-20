"""Regression: ResultValidatorService.process_validation_outcome opened its
own session via self.db_manager.session_scope(), then called
WorkflowResultService.update_result_status without passing that session
through -- update_result_status opened a SECOND, independent get_db()
session and committed separately. A failure in process_validation_outcome
after that call (e.g. phase_manager.get_workflow_config raising) couldn't
roll back the already-committed status write, so a result could end up
"validated" in the DB even though the outcome-processing call that was
supposedly doing that atomically had failed."""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.services.result_validator_service import ResultValidatorService
from src.services.workflow_result_service import WorkflowResultService


def test_process_validation_outcome_passes_its_session_to_update_result_status():
    mock_result = MagicMock(id="result-1", workflow_id="workflow-1")
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = mock_result

    db_manager = MagicMock()

    @contextmanager
    def fake_session_scope():
        yield session

    db_manager.session_scope = fake_session_scope

    phase_manager = MagicMock()
    phase_manager.get_workflow_config.return_value = MagicMock(on_result_found="do_nothing")

    service = ResultValidatorService(db_manager=db_manager, phase_manager=phase_manager)

    with patch.object(WorkflowResultService, "update_result_status") as mock_update:
        mock_update.return_value = {
            "result_id": "result-1",
            "status": "validated",
            "validation_feedback": "ok",
            "validated_at": "2026-01-01T00:00:00Z",
            "validated_by": "validator-1",
        }

        service.process_validation_outcome(
            result_id="result-1",
            passed=True,
            feedback="ok",
        )

    mock_update.assert_called_once()
    assert mock_update.call_args.kwargs["db"] is session
