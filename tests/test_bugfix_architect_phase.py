"""Regression: the bugfix pipeline had no phase to do root-cause/impact
analysis before development started implementing -- development jumped
straight from the bug report to code. architect is a new, lightweight,
ADVISORY phase (not the heavyweight autopilot architecture_design phase
reused) that runs first: it explores the codebase and writes
architect.md, which development treats as an optional head start, not a
gate -- development still does its own independent investigation and
reproduction regardless. It's listed in optional_phases so a failure here
never blocks the pipeline, matching "advisory."
"""

from pathlib import Path

import yaml

from src.workflow_engine.config_validator import validate_single_workflow
from src.workflow_engine.yaml_loader import build_phase_list, load_workflow_from_dir

BUGFIX_DIR = Path(__file__).resolve().parent.parent / "config" / "workflows" / "bugfix"


def test_architect_is_the_first_bugfix_phase():
    cfg = load_workflow_from_dir(BUGFIX_DIR)
    phases = build_phase_list(cfg)
    names = [p.name for p in phases]
    assert names[0] == "architect"
    assert names[1] == "development"


def test_architect_phase_file_declares_its_output():
    architect_path = BUGFIX_DIR / "architect.yaml"
    assert architect_path.exists()
    with open(architect_path) as f:
        cfg = yaml.safe_load(f)
    assert cfg["name"] == "architect"
    assert "architect.md" in cfg["outputs"]


def test_architect_is_optional_so_it_cannot_block_the_pipeline():
    with open(BUGFIX_DIR / "workflow.yaml") as f:
        cfg = yaml.safe_load(f)
    assert "architect" in cfg["optional_phases"]


def test_development_lists_architect_md_as_an_optional_input():
    with open(BUGFIX_DIR / "workflow.yaml") as f:
        cfg = yaml.safe_load(f)
    assert "architect.md" in cfg["phase_inputs"]["development"]["optional"]


def test_architect_has_a_continue_only_evaluation_point():
    with open(BUGFIX_DIR / "workflow.yaml") as f:
        cfg = yaml.safe_load(f)
    after_phases = [e["after_phase"] for e in cfg["orchestrator"]["evaluation_points"]]
    assert "architect" in after_phases


def test_bugfix_workflow_config_still_validates_clean():
    errors = validate_single_workflow(BUGFIX_DIR)
    assert errors == []
