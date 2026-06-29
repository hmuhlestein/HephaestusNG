"""Tests for spec gate firing on qa_validation completion.

This test verifies that:
1. The spec gate fires when qa_validation phase completes
2. The gate causes a GOTO when the score is low
3. Optional phases can fail without blocking the pipeline
"""

import json


class TestSpecGateFiring:
    """Test that the spec gate fires correctly on qa_validation completion."""

    def test_spec_gate_fires_on_qa_completion(self, tmp_path):
        """Test that the spec gate fires when qa_validation phase completes."""
        from src.autopilot.spec import build_phase_output

        # Create a failing qa_result.json
        docs = tmp_path / "docs"
        docs.mkdir()
        qa_result = {
            "failed_tests": 5,
            "passed_tests": 45,
            "pass_rate": 90.0,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        (docs / "qa_result.json").write_text(json.dumps(qa_result))

        # Build phase output
        phase_output = build_phase_output("qa_validation", tmp_path)

        # Verify the gate fired and returned a score
        assert "score" in phase_output
        assert phase_output["score"] == 0.5  # development band due to failed tests
        assert phase_output["spec_gate"]["gate"] == "qa"
        assert "violations" in phase_output["spec_gate"]

    def test_spec_gate_causes_goto_on_low_score(self, tmp_path):
        """Test that a low score causes a GOTO action."""
        from src.autopilot.spec import build_phase_output

        # Create a qa_result.json with critical issues (architecture band)
        docs = tmp_path / "docs"
        docs.mkdir()
        qa_result = {
            "failed_tests": 0,
            "passed_tests": 50,
            "pass_rate": 100.0,
            "critical_issues": 3,  # Critical issues trigger architecture band
            "agent_score": 1.0,
        }
        (docs / "qa_result.json").write_text(json.dumps(qa_result))

        # Build phase output
        phase_output = build_phase_output("qa_validation", tmp_path)

        # Verify the score is in architecture band (triggers GOTO to architecture)
        assert phase_output["score"] == 0.25
        assert phase_output["spec_gate"]["band"] == "architecture"

    def test_spec_gate_passes_on_good_result(self, tmp_path):
        """Test that a good result passes the gate."""
        from src.autopilot.spec import build_phase_output

        # Create a passing qa_result.json
        docs = tmp_path / "docs"
        docs.mkdir()
        qa_result = {
            "failed_tests": 0,
            "passed_tests": 50,
            "pass_rate": 100.0,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        (docs / "qa_result.json").write_text(json.dumps(qa_result))

        # Build phase output
        phase_output = build_phase_output("qa_validation", tmp_path)

        # Verify the score passes the gate
        assert phase_output["score"] >= 0.7
        assert phase_output["spec_gate"]["band"] == "pass"


class TestOptionalPhases:
    """Test that optional phases can fail without blocking the pipeline."""

    def test_optional_phases_loaded(self):
        """Test that optional phases are loaded from configuration."""
        from src.autopilot.spec import OPTIONAL_PHASES, load_optional_phases

        # Default optional phases should include forensics and git_commit_push
        assert "forensics_analysis" in OPTIONAL_PHASES
        assert "git_commit_push" in OPTIONAL_PHASES

        # Test loading with None workflow_id (returns defaults)
        result = load_optional_phases(None)
        assert "forensics_analysis" in result

    def test_required_output_loaded(self):
        """Test that required output artifacts are loaded."""
        from src.autopilot.spec import (
            PHASE_OUTPUT_ARTIFACTS,
            load_phase_output_artifacts,
        )

        # Default required output should include qa_validation
        assert "qa_validation" in PHASE_OUTPUT_ARTIFACTS
        assert PHASE_OUTPUT_ARTIFACTS["qa_validation"] == "qa_result.json"

        # Test loading with None workflow_id (returns defaults)
        result = load_phase_output_artifacts(None)
        assert "qa_validation" in result


class TestOutputExistenceFloor:
    """Test that the output existence floor works correctly."""

    def test_phase_output_artifacts_defined(self):
        """Test that PHASE_OUTPUT_ARTIFACTS is properly defined."""
        from src.autopilot.spec import PHASE_OUTPUT_ARTIFACTS

        # Verify key phases have required outputs
        assert "architecture_design" in PHASE_OUTPUT_ARTIFACTS
        assert "scope_review" in PHASE_OUTPUT_ARTIFACTS
        assert "qa_validation" in PHASE_OUTPUT_ARTIFACTS
        assert "product_validation" in PHASE_OUTPUT_ARTIFACTS

    def test_optional_phases_not_in_required_output(self):
        """Test that optional phases are not in required output."""
        from src.autopilot.spec import OPTIONAL_PHASES, PHASE_OUTPUT_ARTIFACTS

        # Optional phases should not have required outputs
        for phase in OPTIONAL_PHASES:
            assert phase not in PHASE_OUTPUT_ARTIFACTS
