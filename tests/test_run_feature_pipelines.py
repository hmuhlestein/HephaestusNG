"""Tests for run_feature_pipelines' dependency-respecting layer execution.

Regression: observed live in this repo's own self-hosted pipeline --
"AI Generation Service" (depends_on: auth-fraud, credit-system) was
dispatched while "Credit Management System" (credit-system) was still
genuinely active, not completed. Root cause: run_single_workflow's
per-call poll loop returned "timeout" for credit-system (its 2-hour
wall-clock budget resets on every resume -- see run_single_workflow's
start_time -- and this repo restarts frequently while self-hosting), and
the outer for-loop over execution_groups advanced to the next dependency
layer regardless of that non-terminal result.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.autopilot.orchestrator import run_feature_pipelines

from src.autopilot.orchestrator.state import DesignEntry, FeatureRunStatus


@pytest.fixture
def mock_logger():
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    return logger


@pytest.fixture
def design_entry(tmp_path):
    return DesignEntry(
        path=tmp_path / "design.md",
        name="Backend Design",
        content_hash="abc123",
        db_id="des-123",
        project_path=tmp_path,
    )


def _features_json():
    return {
        "features": [
            {"id": "auth-fraud", "name": "Auth & Fraud", "depends_on": [], "execution": "sequential"},
            {"id": "credit-system", "name": "Credit System", "depends_on": ["auth-fraud"], "execution": "parallel"},
            {"id": "ai-generation", "name": "AI Generation", "depends_on": ["auth-fraud", "credit-system"], "execution": "sequential"},
        ]
    }


class TestRunFeaturePipelinesHaltsOnNonTerminalStatus:
    def test_timeout_on_a_dependency_blocks_its_dependent(
        self, mock_logger, design_entry, tmp_path
    ):
        """The exact live scenario: credit-system's own poll loop times out
        (still genuinely in progress, not resolved) -- ai-generation, which
        depends on it, must not be dispatched this walk."""
        call_order = []

        def fake_run_one_feature(sdk, design_entry, feat, designs_folder, project_path, logger, state, max_iterations, project_id):
            feat_id = feat["id"]
            call_order.append(feat_id)
            if feat_id == "auth-fraud":
                return FeatureRunStatus.COMPLETED
            if feat_id == "credit-system":
                return FeatureRunStatus.TIMEOUT
            return FeatureRunStatus.COMPLETED  # ai-generation -- should never actually be called

        with patch("src.autopilot.orchestrator.pipeline._run_one_feature", side_effect=fake_run_one_feature):
            run_feature_pipelines(
                sdk=MagicMock(),
                design_entry=design_entry,
                features_json=_features_json(),
                designs_folder=tmp_path,
                project_path=tmp_path,
                logger=mock_logger,
            )

        assert call_order == ["auth-fraud", "credit-system"]
        assert "ai-generation" not in call_order

    def test_interrupted_on_a_dependency_blocks_its_dependent(
        self, mock_logger, design_entry, tmp_path
    ):
        call_order = []

        def fake_run_one_feature(sdk, design_entry, feat, designs_folder, project_path, logger, state, max_iterations, project_id):
            feat_id = feat["id"]
            call_order.append(feat_id)
            if feat_id == "credit-system":
                return FeatureRunStatus.INTERRUPTED
            return FeatureRunStatus.COMPLETED

        with patch("src.autopilot.orchestrator.pipeline._run_one_feature", side_effect=fake_run_one_feature):
            run_feature_pipelines(
                sdk=MagicMock(),
                design_entry=design_entry,
                features_json=_features_json(),
                designs_folder=tmp_path,
                project_path=tmp_path,
                logger=mock_logger,
            )

        assert call_order == ["auth-fraud", "credit-system"]
        assert "ai-generation" not in call_order

    def test_failed_dependency_still_lets_dependent_run(
        self, mock_logger, design_entry, tmp_path
    ):
        """Regression: this must stay unchanged -- a genuinely resolved
        "failed" status (unlike "interrupted"/"timeout") does not block
        dependents, per the pre-existing, deliberate design (see the
        comment above the loop in run_feature_pipelines)."""
        call_order = []

        def fake_run_one_feature(sdk, design_entry, feat, designs_folder, project_path, logger, state, max_iterations, project_id):
            feat_id = feat["id"]
            call_order.append(feat_id)
            if feat_id == "credit-system":
                return FeatureRunStatus.FAILED
            return FeatureRunStatus.COMPLETED

        with patch("src.autopilot.orchestrator.pipeline._run_one_feature", side_effect=fake_run_one_feature):
            run_feature_pipelines(
                sdk=MagicMock(),
                design_entry=design_entry,
                features_json=_features_json(),
                designs_folder=tmp_path,
                project_path=tmp_path,
                logger=mock_logger,
            )

        assert call_order == ["auth-fraud", "credit-system", "ai-generation"]

    def test_timeout_in_a_parallel_group_blocks_later_layers(
        self, mock_logger, design_entry, tmp_path
    ):
        """The non-terminal check must also apply to the ThreadPoolExecutor
        (multiple-features-in-one-layer) branch, not just the single-feature
        branch."""
        features_json = {
            "features": [
                {"id": "svc-a", "name": "Service A", "depends_on": [], "execution": "parallel"},
                {"id": "svc-b", "name": "Service B", "depends_on": [], "execution": "parallel"},
                {"id": "svc-c", "name": "Service C", "depends_on": ["svc-a", "svc-b"], "execution": "sequential"},
            ]
        }
        call_order = []

        def fake_run_one_feature(sdk, design_entry, feat, designs_folder, project_path, logger, state, max_iterations, project_id):
            feat_id = feat["id"]
            call_order.append(feat_id)
            if feat_id == "svc-b":
                return FeatureRunStatus.TIMEOUT
            return FeatureRunStatus.COMPLETED

        with patch("src.autopilot.orchestrator.pipeline._run_one_feature", side_effect=fake_run_one_feature):
            run_feature_pipelines(
                sdk=MagicMock(),
                design_entry=design_entry,
                features_json=features_json,
                designs_folder=tmp_path,
                project_path=tmp_path,
                logger=mock_logger,
            )

        assert "svc-c" not in call_order
        assert set(call_order) == {"svc-a", "svc-b"}
