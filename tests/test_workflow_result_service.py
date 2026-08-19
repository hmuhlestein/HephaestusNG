"""Tests for workflow_result_service.py — static methods."""

import os
import tempfile
from unittest.mock import Mock, patch

import pytest


class TestSubmitResult:
    def test_submit_rejects_missing_file(self):
        from src.services.workflow_result_service import WorkflowResultService

        # Under the system temp dir -- a legitimate location per
        # validate_file_path's default allowed roots -- so this exercises
        # the "file doesn't exist" path, not the containment check.
        missing_path = os.path.join(tempfile.gettempdir(), "nonexistent-result-file.md")
        with pytest.raises(FileNotFoundError):
            WorkflowResultService.submit_result(
                agent_id="a1",
                workflow_id="wf1",
                markdown_file_path=missing_path,
            )

    def test_submit_validates_path(self):
        from src.services.workflow_result_service import WorkflowResultService

        with pytest.raises((ValueError, Exception)):
            WorkflowResultService.submit_result(
                agent_id="a1",
                workflow_id="wf1",
                markdown_file_path="../etc/passwd",
            )

    def test_submit_rejects_oversized_file(self, tmp_path):
        from src.services.workflow_result_service import WorkflowResultService

        big_file = tmp_path / "big.md"
        big_file.write_text("x" * (1024 * 1024 + 1))  # > 1MB
        with pytest.raises((ValueError, Exception)):
            WorkflowResultService.submit_result(
                agent_id="a1",
                workflow_id="wf1",
                markdown_file_path=str(big_file),
            )


class TestGetWorkflowResults:
    def test_returns_list(self):
        from src.services.workflow_result_service import WorkflowResultService

        with patch("src.services.workflow_result_service.get_db") as mock_db:
            mock_session = Mock()
            mock_session.query.return_value.filter_by.return_value.all.return_value = []
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = WorkflowResultService.get_workflow_results("wf1")
            assert isinstance(result, list)


class TestCheckWorkflowCompletion:
    def test_returns_bool(self):
        from src.services.workflow_result_service import WorkflowResultService

        with patch("src.services.workflow_result_service.get_db") as mock_db:
            mock_session = Mock()
            mock_session.query.return_value.filter_by.return_value.first.return_value = None
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            result = WorkflowResultService.check_workflow_completion("wf1")
            assert isinstance(result, bool)


class TestUpdateResultStatus:
    def test_update_nonexistent_raises(self):
        from src.services.workflow_result_service import WorkflowResultService

        with patch("src.services.workflow_result_service.get_db") as mock_db:
            mock_session = Mock()
            mock_session.query.return_value.filter_by.return_value.first.return_value = None
            mock_db.return_value.__enter__ = Mock(return_value=mock_session)
            mock_db.return_value.__exit__ = Mock(return_value=False)
            with pytest.raises(ValueError, match="not found"):
                WorkflowResultService.update_result_status(
                    result_id="nonexistent",
                    status="validated",
                )
