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
        """PHASE_OUTPUT_ARTIFACTS now holds only workflow.yaml required_output
        overrides (e.g. Phase 0's Feature Architect); per-phase artifacts are
        derived from each phase's own YAML outputs: list at verification time
        (see get_phase_required_files / TestOutputExistenceFloor below)."""
        from src.autopilot.spec import (
            PHASE_OUTPUT_ARTIFACTS,
            load_phase_output_artifacts,
        )

        result = load_phase_output_artifacts(None)
        assert result is PHASE_OUTPUT_ARTIFACTS


class TestOutputExistenceFloor:
    """Test that the output existence floor derives required files from
    each phase's own declared outputs: list rather than a hardcoded dict."""

    def test_phase_required_files_derived_from_yaml_outputs(self):
        from src.autopilot.spec import get_phase_required_files

        class _FakePhase:
            name = "qa_validation"
            outputs = ["qa_report.md", "qa_result.json"]

        assert get_phase_required_files(_FakePhase()) == [
            "qa_report.md",
            "qa_result.json",
        ]

    def test_non_file_descriptive_outputs_filtered_out(self):
        from src.autopilot.spec import get_phase_required_files

        class _FakePhase:
            name = "development"
            outputs = ["source code in project path"]

        assert get_phase_required_files(_FakePhase()) == []

    def test_previously_uncovered_phases_now_get_required_files(self):
        """The systemic fix: adversarial_review/security_review (and any
        other phase with a declared outputs: file) previously had zero
        enforcement -- only 4 phases were in a hardcoded dict."""
        from src.autopilot.spec import get_phase_required_files

        class _FakePhase:
            def __init__(self, name, outputs):
                self.name = name
                self.outputs = outputs

        assert get_phase_required_files(
            _FakePhase("adversarial_review", ["adversarial_review_report.md"])
        ) == ["adversarial_review_report.md"]
        assert get_phase_required_files(
            _FakePhase("security_review", ["security_report.md"])
        ) == ["security_report.md"]
