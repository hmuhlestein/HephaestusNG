"""Tests for spec gate firing on qa_validation completion.

This test verifies that:
1. The spec gate fires when qa_validation phase completes
2. The gate causes a GOTO when the score is low
3. Optional phases can fail without blocking the pipeline
"""

import pytest
import yaml


def _write_qa_report(docs_dir, qa_result):
    frontmatter = {"type": "qa_validation_result", **qa_result}
    text = "---\n" + yaml.safe_dump(frontmatter, sort_keys=False) + "---\n\n# QA Report\n"
    (docs_dir / "qa.md").write_text(text)


class TestSpecGateFiring:
    """Test that the spec gate fires correctly on qa_validation completion."""

    def test_spec_gate_fires_on_qa_completion(self, tmp_path):
        """Test that the spec gate fires when qa_validation phase completes."""
        from src.autopilot.spec import build_phase_output

        # Create a failing qa.md
        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        qa_result = {
            "failed_tests": 5,
            "passed_tests": 45,
            "pass_rate": 90.0,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        _write_qa_report(docs, qa_result)

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

        # Create a qa.md with critical issues (architecture band)
        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        qa_result = {
            "failed_tests": 0,
            "passed_tests": 50,
            "pass_rate": 100.0,
            "critical_issues": 3,  # Critical issues trigger architecture band
            "agent_score": 1.0,
        }
        _write_qa_report(docs, qa_result)

        # Build phase output
        phase_output = build_phase_output("qa_validation", tmp_path)

        # Verify the score is in architecture band (triggers GOTO to architecture)
        assert phase_output["score"] == 0.25
        assert phase_output["spec_gate"]["band"] == "architecture"

    def test_spec_gate_passes_on_good_result(self, tmp_path):
        """Test that a good result passes the gate."""
        from src.autopilot.spec import build_phase_output

        # Create a passing qa.md
        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        qa_result = {
            "failed_tests": 0,
            "passed_tests": 50,
            "pass_rate": 100.0,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        _write_qa_report(docs, qa_result)

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

        # Default optional phases should include forensics and git_expert
        assert "forensics_analysis" in OPTIONAL_PHASES
        assert "git_expert" in OPTIONAL_PHASES

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


class TestPerWorkflowDefinitionCacheIsolation:
    """Regression: load_phase_output_artifacts/load_optional_phases used to
    merge every workflow.yaml's overrides into ONE shared module-level
    dict/set (via .update() / reassignment), so a phase name declared
    differently by two unrelated workflow definitions would clobber each
    other -- whichever workflow got queried last for the process's entire
    remaining lifetime silently won for every other workflow's lookups too.
    Caching must be scoped per Workflow.definition_id."""

    @pytest.fixture
    def two_workflow_defs(self, tmp_path, monkeypatch):
        from src.core.database import DatabaseManager, Workflow, WorkflowDefinition

        db_path = tmp_path / "test.db"
        real_db = DatabaseManager(str(db_path))
        real_db.create_tables()
        monkeypatch.setattr(
            "src.core.database.DatabaseManager", lambda *a, **kw: real_db
        )

        workflows_dir = tmp_path / "workflows"
        (workflows_dir / "def_a").mkdir(parents=True)
        (workflows_dir / "def_a" / "workflow.yaml").write_text(
            "required_output:\n  qa_validation: a_result.json\n"
            "optional_phases:\n  - phase_x\n"
        )
        (workflows_dir / "def_b").mkdir(parents=True)
        (workflows_dir / "def_b" / "workflow.yaml").write_text(
            "required_output:\n  qa_validation: b_result.json\n"
            "optional_phases:\n  - phase_y\n"
        )
        # def_c declares no optional_phases override at all -- it must fall
        # back to the true OPTIONAL_PHASES default, not whatever another
        # workflow definition's override happened to leave behind.
        (workflows_dir / "def_c").mkdir(parents=True)
        (workflows_dir / "def_c" / "workflow.yaml").write_text(
            "required_output:\n  qa_validation: c_result.json\n"
        )
        monkeypatch.setattr("src.workflow_registry._WORKFLOWS_DIR", workflows_dir)

        session = real_db.get_session()
        for def_id in ("def_a", "def_b", "def_c"):
            session.add(WorkflowDefinition(id=def_id, name=def_id))
        session.add(
            Workflow(id="wf-a", name="A", phases_folder_path="/tmp", definition_id="def_a")
        )
        session.add(
            Workflow(id="wf-b", name="B", phases_folder_path="/tmp", definition_id="def_b")
        )
        session.add(
            Workflow(id="wf-c", name="C", phases_folder_path="/tmp", definition_id="def_c")
        )
        session.commit()
        session.close()

        return real_db

    def test_required_output_does_not_leak_across_definitions(self, two_workflow_defs):
        from src.autopilot.spec import load_phase_output_artifacts

        result_a = load_phase_output_artifacts("wf-a")
        result_b = load_phase_output_artifacts("wf-b")

        assert result_a["qa_validation"] == "a_result.json"
        assert result_b["qa_validation"] == "b_result.json"
        # Re-querying A after B must still see A's own override, not B's.
        assert load_phase_output_artifacts("wf-a")["qa_validation"] == "a_result.json"

    def test_optional_phases_does_not_leak_across_definitions(self, two_workflow_defs):
        from src.autopilot.spec import load_optional_phases

        result_a = load_optional_phases("wf-a")
        result_b = load_optional_phases("wf-b")

        assert result_a == {"phase_x"}
        assert result_b == {"phase_y"}
        assert load_optional_phases("wf-a") == {"phase_x"}

    def test_workflow_without_override_gets_true_default_not_a_leftover(
        self, two_workflow_defs
    ):
        """def_c declares no optional_phases key. Querying it after wf-a
        must NOT silently inherit wf-a's {"phase_x"} override -- it must see
        the real OPTIONAL_PHASES default."""
        from src.autopilot.spec import OPTIONAL_PHASES, load_optional_phases

        load_optional_phases("wf-a")
        result_c = load_optional_phases("wf-c")

        assert result_c == OPTIONAL_PHASES
        assert "phase_x" not in result_c


class TestOutputExistenceFloor:
    """Test that the output existence floor derives required files from
    each phase's own declared outputs: list rather than a hardcoded dict."""

    def test_phase_required_files_derived_from_yaml_outputs(self):
        from src.autopilot.spec import get_phase_required_files

        class _FakePhase:
            name = "qa_validation"
            outputs = ["qa.md", "qa_result.json"]

        assert get_phase_required_files(_FakePhase()) == [
            "qa.md",
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
            _FakePhase("adversarial_review", ["adversarial.md"])
        ) == ["adversarial.md"]
        assert get_phase_required_files(
            _FakePhase("security_review", ["security.md"])
        ) == ["security.md"]
