#!/usr/bin/env python3
"""Tests for src/workflow_engine/config_validator.py

Validates that the YAML config validator correctly catches structural errors,
missing fields, cross-reference issues, and naming inconsistencies in
workflow YAML configurations.
"""

import importlib
import sys
from pathlib import Path

import pytest
import yaml

sys.path.append(str(Path(__file__).parent.parent))

# Import directly to avoid src/autopilot/__init__.py pulling in heavy deps
# (orchestrator requires 'git' module which may not be installed in test env)
_config_validator = importlib.import_module("src.workflow_engine.config_validator")
validate_all_workflows = _config_validator.validate_all_workflows
validate_phase_file = _config_validator.validate_phase_file
validate_single_workflow = _config_validator.validate_single_workflow
validate_workflow_yaml = _config_validator.validate_workflow_yaml


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def valid_phase():
    """A minimal valid phase config."""
    return {
        "id": 1,
        "name": "test_phase",
        "description": "A test phase for validation.",
        "done_definitions": ["All tests pass"],
        "thinking_level": "low",
    }


@pytest.fixture
def valid_phase_2():
    """A second valid phase config."""
    return {
        "id": 2,
        "name": "second_phase",
        "description": "Second phase.",
        "done_definitions": ["Complete"],
    }


@pytest.fixture
def valid_workflow_cfg(valid_phase, valid_phase_2):
    """A minimal valid _workflow.yaml config."""
    return {
        "default_model": "test-model",
        "default_thinking_level": "low",
        "session_roles": {
            "test_phase": "tester",
            "second_phase": "reviewer",
        },
        "orchestrator": {
            "type": "evaluating",
            "max_phase_retries": 2,
            "max_total_gotos": 10,
            "evaluation_points": [
                {
                    "after_phase": "test_phase",
                    "evaluator": "heuristic",
                    "max_retries": 2,
                    "conditions": [
                        {"if": "score >= 0.6", "action": "continue", "reason": "OK"},
                        {"if": "score < 0.6", "action": "goto", "target": "second_phase", "reason": "Retry"},
                    ],
                },
            ],
        },
        "workflow": {
            "result_criteria": "All done",
            "on_result_found": "do_nothing",
            "board": {
                "columns": [
                    {"id": "backlog", "name": "Backlog", "order": 1},
                    {"id": "in-progress", "name": "In Progress", "order": 2},
                ],
            },
        },
        "launch_template": {
            "parameters": [
                {
                    "name": "design_document",
                    "label": "Design Doc",
                    "type": "text",
                    "required": True,
                    "description": "Path to design doc",
                },
            ],
        },
    }


@pytest.fixture
def valid_workflow_dir(tmp_path, valid_phase, valid_phase_2, valid_workflow_cfg):
    """Create a valid workflow directory with phase files."""
    wf_dir = tmp_path / "test_workflow"
    wf_dir.mkdir()

    # Write phase files
    (wf_dir / "01_test_phase.yaml").write_text(yaml.dump(valid_phase))
    (wf_dir / "02_second_phase.yaml").write_text(yaml.dump(valid_phase_2))

    # Write workflow config
    (wf_dir / "workflow.yaml").write_text(yaml.dump(valid_workflow_cfg))

    return wf_dir


@pytest.fixture
def seen_ids():
    return set()


@pytest.fixture
def seen_names():
    return set()


@pytest.fixture
def phase_names():
    return {"test_phase", "second_phase"}


# ── Phase file validation ─────────────────────────────────────────

class TestPhaseFileValidation:

    def test_valid_phase(self, valid_phase, seen_ids, seen_names):
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert errors == []
        assert 1 in seen_ids
        assert "test_phase" in seen_names

    def test_missing_id(self, valid_phase, seen_ids, seen_names):
        del valid_phase["id"]
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert len(errors) == 1
        assert "Missing required key: id" in errors[0]["message"]

    def test_missing_name(self, valid_phase, seen_ids, seen_names):
        del valid_phase["name"]
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert len(errors) == 1
        assert "Missing required key: name" in errors[0]["message"]

    def test_missing_description(self, valid_phase, seen_ids, seen_names):
        del valid_phase["description"]
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert len(errors) == 1
        assert "Missing required key: description" in errors[0]["message"]

    def test_id_not_integer(self, valid_phase, seen_ids, seen_names):
        valid_phase["id"] = "one"
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert len(errors) == 1
        assert "must be an integer" in errors[0]["message"]

    def test_id_float_rejected(self, valid_phase, seen_ids, seen_names):
        valid_phase["id"] = 1.5
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert len(errors) == 1
        assert "must be an integer" in errors[0]["message"]

    def test_duplicate_id(self, valid_phase, valid_phase_2, seen_ids, seen_names):
        validate_phase_file(valid_phase, "a.yaml", seen_ids, seen_names)
        valid_phase_2["id"] = 1  # Same ID
        errors = validate_phase_file(valid_phase_2, "b.yaml", seen_ids, seen_names)
        assert len(errors) == 1
        assert "Duplicate phase id: 1" in errors[0]["message"]

    def test_duplicate_name(self, valid_phase, valid_phase_2, seen_ids, seen_names):
        validate_phase_file(valid_phase, "a.yaml", seen_ids, seen_names)
        valid_phase_2["name"] = "test_phase"  # Same name
        errors = validate_phase_file(valid_phase_2, "b.yaml", seen_ids, seen_names)
        assert len(errors) == 1
        assert "Duplicate phase name: 'test_phase'" in errors[0]["message"]

    def test_empty_name(self, valid_phase, seen_ids, seen_names):
        valid_phase["name"] = ""
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert any("non-empty string" in e["message"] for e in errors)

    def test_whitespace_only_name(self, valid_phase, seen_ids, seen_names):
        valid_phase["name"] = "   "
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert any("non-empty string" in e["message"] for e in errors)

    def test_empty_description(self, valid_phase, seen_ids, seen_names):
        valid_phase["description"] = ""
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert any("description must be a non-empty string" in e["message"] for e in errors)

    def test_done_definitions_not_list(self, valid_phase, seen_ids, seen_names):
        valid_phase["done_definitions"] = "not a list"
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert any("done_definitions must be a list" in e["message"] for e in errors)

    def test_done_definitions_none_ok(self, valid_phase, seen_ids, seen_names):
        valid_phase["done_definitions"] = None
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert errors == []

    def test_valid_thinking_levels(self, valid_phase, seen_ids, seen_names):
        for level in ("low", "medium", "high"):
            valid_phase["thinking_level"] = level
            seen_ids.clear()
            seen_names.clear()
            errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
            assert errors == [], f"thinking_level '{level}' should be valid"

    def test_invalid_thinking_level(self, valid_phase, seen_ids, seen_names):
        valid_phase["thinking_level"] = "ultra"
        errors = validate_phase_file(valid_phase, "test.yaml", seen_ids, seen_names)
        assert any("Invalid thinking_level" in e["message"] for e in errors)

    def test_missing_required_keys_all(self, seen_ids, seen_names):
        errors = validate_phase_file({}, "test.yaml", seen_ids, seen_names)
        # Should report missing id (first required key checked)
        assert any("Missing required key: id" in e["message"] for e in errors)


# ── Workflow YAML validation ──────────────────────────────────────

class TestWorkflowYamlValidation:

    def test_valid_workflow(self, valid_workflow_cfg, phase_names):
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert errors == []

    def test_missing_orchestrator(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["orchestrator"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("Missing required top-level key: 'orchestrator'" in e["message"] for e in errors)

    def test_missing_workflow_section(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["workflow"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("Missing required top-level key: 'workflow'" in e["message"] for e in errors)

    def test_missing_launch_template(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["launch_template"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("Missing required top-level key: 'launch_template'" in e["message"] for e in errors)

    def test_orchestrator_not_dict(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"] = "not a dict"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("orchestrator must be a dict" in e["message"] for e in errors)

    def test_orchestrator_missing_type(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["orchestrator"]["type"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("orchestrator missing required key: 'type'" in e["message"] for e in errors)

    def test_invalid_orchestrator_type(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["type"] = "invalid_type"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("Invalid orchestrator type" in e["message"] for e in errors)

    def test_max_phase_retries_negative(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["max_phase_retries"] = -1
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("max_phase_retries must be a non-negative integer" in e["message"] for e in errors)

    def test_max_total_gotos_non_int(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["max_total_gotos"] = "ten"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("max_total_gotos must be a non-negative integer" in e["message"] for e in errors)

    def test_eval_point_references_unknown_phase(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["evaluation_points"][0]["after_phase"] = "nonexistent"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("references unknown phase: 'nonexistent'" in e["message"] for e in errors)

    def test_eval_point_duplicate_after_phase(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["evaluation_points"].append({
            "after_phase": "test_phase",  # Duplicate
            "evaluator": "heuristic",
            "conditions": [{"if": "score >= 0.0", "action": "continue"}],
        })
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("duplicates 'test_phase'" in e["message"] for e in errors)

    def test_eval_point_goto_unknown_target(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["evaluation_points"][0]["conditions"][1]["target"] = "nonexistent"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("references unknown phase: 'nonexistent'" in e["message"] for e in errors)

    def test_condition_invalid_action(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["evaluation_points"][0]["conditions"][0]["action"] = "skip"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("condition[0].action invalid: 'skip'" in e["message"] for e in errors)

    def test_condition_missing_action(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["orchestrator"]["evaluation_points"][0]["conditions"][0]["action"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("condition[0] missing required key: 'action'" in e["message"] for e in errors)

    def test_workflow_section_not_dict(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["workflow"] = "bad"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("workflow must be a dict" in e["message"] for e in errors)

    def test_workflow_missing_result_criteria(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["workflow"]["result_criteria"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("workflow missing required key: 'result_criteria'" in e["message"] for e in errors)

    def test_workflow_missing_on_result_found(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["workflow"]["on_result_found"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("workflow missing required key: 'on_result_found'" in e["message"] for e in errors)

    def test_launch_template_not_dict(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["launch_template"] = "bad"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("launch_template must be a dict" in e["message"] for e in errors)

    def test_launch_param_missing_name(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["launch_template"]["parameters"][0]["name"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("parameters[0] missing required key: 'name'" in e["message"] for e in errors)

    def test_launch_param_missing_required_field(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["launch_template"]["parameters"][0]["required"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("parameters[0] missing required key: 'required'" in e["message"] for e in errors)

    def test_duplicate_launch_param_name(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["launch_template"]["parameters"].append({
            "name": "design_document",  # Duplicate
            "label": "Duplicate",
            "type": "text",
            "required": True,
        })
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("Duplicate parameter name: 'design_document'" in e["message"] for e in errors)

    def test_board_column_missing_id(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["workflow"]["board"]["columns"][0]["id"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("missing required key: 'id'" in e["message"] for e in errors)

    def test_board_duplicate_column_id(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["workflow"]["board"]["columns"].append({
            "id": valid_workflow_cfg["workflow"]["board"]["columns"][0]["id"],
            "name": "Duplicate",
            "order": 99,
        })
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("Duplicate board column id" in e["message"] for e in errors)

    def test_eval_points_not_list(self, valid_workflow_cfg, phase_names):
        valid_workflow_cfg["orchestrator"]["evaluation_points"] = "not a list"
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("evaluation_points must be a list" in e["message"] for e in errors)

    def test_eval_point_missing_conditions(self, valid_workflow_cfg, phase_names):
        del valid_workflow_cfg["orchestrator"]["evaluation_points"][0]["conditions"]
        errors = validate_workflow_yaml(valid_workflow_cfg, "workflow.yaml", phase_names)
        assert any("evaluation_point[0] missing required key: 'conditions'" in e["message"] for e in errors)


# ── Single workflow directory validation ──────────────────────────

class TestSingleWorkflowValidation:

    def test_valid_workflow_dir(self, valid_workflow_dir):
        errors = validate_single_workflow(valid_workflow_dir)
        assert errors == []

    def test_missing_workflow_yaml(self, tmp_path):
        wf_dir = tmp_path / "empty_workflow"
        wf_dir.mkdir()
        errors = validate_single_workflow(wf_dir)
        assert any("File not found" in e["message"] for e in errors)

    def test_invalid_phase_yaml_syntax(self, tmp_path, valid_workflow_cfg):
        wf_dir = tmp_path / "bad_yaml"
        wf_dir.mkdir()
        (wf_dir / "workflow.yaml").write_text(yaml.dump(valid_workflow_cfg))
        (wf_dir / "01_bad.yaml").write_text("{{{{invalid yaml: [")
        errors = validate_single_workflow(wf_dir)
        assert any("YAML parse error" in e["message"] for e in errors)

    def test_invalid_workflow_yaml_syntax(self, tmp_path):
        wf_dir = tmp_path / "bad_wf"
        wf_dir.mkdir()
        (wf_dir / "workflow.yaml").write_text("{{{{invalid yaml: [")
        errors = validate_single_workflow(wf_dir)
        assert any("YAML parse error" in e["message"] for e in errors)

    def test_no_phase_files(self, tmp_path, valid_workflow_cfg):
        wf_dir = tmp_path / "no_phases"
        wf_dir.mkdir()
        (wf_dir / "workflow.yaml").write_text(yaml.dump(valid_workflow_cfg))
        errors = validate_single_workflow(wf_dir)
        assert any("No phase YAML files found" in e["message"] for e in errors)

    def test_duplicate_phase_ids(self, tmp_path, valid_workflow_cfg):
        wf_dir = tmp_path / "dup_ids"
        wf_dir.mkdir()
        phase = {"id": 1, "name": "phase_a", "description": "A"}
        (wf_dir / "workflow.yaml").write_text(yaml.dump(valid_workflow_cfg))
        (wf_dir / "01_a.yaml").write_text(yaml.dump(phase))
        (wf_dir / "02_b.yaml").write_text(yaml.dump(phase))  # Same ID
        errors = validate_single_workflow(wf_dir)
        assert any("Duplicate phase id" in e["message"] for e in errors)

    def test_phase_not_mapping(self, tmp_path, valid_workflow_cfg):
        wf_dir = tmp_path / "list_phase"
        wf_dir.mkdir()
        (wf_dir / "workflow.yaml").write_text(yaml.dump(valid_workflow_cfg))
        (wf_dir / "01_list.yaml").write_text(yaml.dump(["not", "a", "mapping"]))
        errors = validate_single_workflow(wf_dir)
        assert any("must contain a YAML mapping" in e["message"] for e in errors)


# ── Full scan validation ──────────────────────────────────────────

class TestValidateAllWorkflows:

    def test_valid_workflows_dir(self, valid_workflow_dir):
        # Parent of the workflow dir acts as config_dir
        errors = validate_all_workflows(valid_workflow_dir.parent)
        assert errors == []

    def test_nonexistent_dir(self, tmp_path):
        errors = validate_all_workflows(tmp_path / "nonexistent")
        assert len(errors) == 1
        assert "does not exist" in errors[0]["message"]

    def test_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        errors = validate_all_workflows(empty)
        assert len(errors) == 1
        assert "No workflow directories found" in errors[0]["message"]

    def test_mixed_valid_and_invalid(self, tmp_path, valid_workflow_cfg, valid_phase, valid_phase_2):
        # Valid workflow — include both phases so goto target resolves
        good = tmp_path / "good_workflow"
        good.mkdir()
        (good / "workflow.yaml").write_text(yaml.dump(valid_workflow_cfg))
        (good / "01_phase.yaml").write_text(yaml.dump(valid_phase))
        (good / "02_phase.yaml").write_text(yaml.dump(valid_phase_2))

        # Invalid workflow
        bad = tmp_path / "bad_workflow"
        bad.mkdir()
        bad_phase = {"id": 1, "name": "x", "description": "x"}
        (bad / "workflow.yaml").write_text(yaml.dump(valid_workflow_cfg))
        (bad / "01_phase.yaml").write_text(yaml.dump(bad_phase))
        (bad / "02_phase.yaml").write_text(yaml.dump(bad_phase))  # Duplicate ID

        errors = validate_all_workflows(tmp_path)
        # Should have errors from bad_workflow but not good_workflow
        bad_errors = [e for e in errors if "bad_workflow" in e["file"]]
        good_errors = [e for e in errors if "good_workflow" in e["file"]]
        assert len(bad_errors) > 0
        assert len(good_errors) == 0


# ── Integration: validate the actual project configs ──────────────

class TestRealProjectConfigs:
    """Validate the actual config files in the repo.

    These tests only run if the project config directory exists.
    """

    @pytest.fixture(autouse=True)
    def _skip_if_no_config(self):
        config_dir = Path(__file__).parent.parent / "config" / "workflows"
        if not config_dir.exists():
            pytest.skip("Project config directory not found")

    def test_autopilot_workflow_valid(self):
        """The autopilot workflow should have zero config errors."""
        config_dir = Path(__file__).parent.parent / "config" / "workflows"
        autopilot_dir = config_dir / "autopilot"
        if not autopilot_dir.exists():
            pytest.skip("autopilot workflow dir not found")

        errors = validate_single_workflow(autopilot_dir)
        real_errors = [e for e in errors if e["severity"] == "error"]
        if real_errors:
            msg = "\n".join(f"  [{e['severity']}] {e['file']}: {e['message']}" for e in real_errors)
            pytest.fail(f"Autopilot workflow has config errors:\n{msg}")

    def test_all_workflows_valid(self):
        """All workflows in config/workflows/ should pass validation."""
        config_dir = Path(__file__).parent.parent / "config" / "workflows"
        errors = validate_all_workflows(config_dir)
        real_errors = [e for e in errors if e["severity"] == "error"]
        if real_errors:
            msg = "\n".join(f"  [{e['severity']}] {e['file']}: {e['message']}" for e in real_errors)
            pytest.fail(f"Workflow config errors:\n{msg}")
