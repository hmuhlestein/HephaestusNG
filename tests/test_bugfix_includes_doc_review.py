"""Regression: bugfix/workflow.yaml's execution_order dropped doc_review
along with product_requirements/architecture_design/architectural_review/
product_validation -- but doc_review is the SOLE producer of
feature_report.html (see its own STEP 4), which the feature review modal
displays. Every bugfix-typed feature shipped with no report at all.

doc_review's own design rationale (docs/BUGFIX_WORKFLOW_TYPE_DESIGN.md,
now removed) explicitly anticipated this: "kept out of the default list
but easy to re-add if that's wrong in practice." Re-added by request.
"""

from pathlib import Path

import yaml

from src.workflow_engine.config_validator import validate_single_workflow
from src.workflow_engine.yaml_loader import build_phase_list, load_workflow_from_dir

BUGFIX_DIR = Path(__file__).resolve().parent.parent / "config" / "workflows" / "bugfix"


def test_doc_review_is_in_bugfix_execution_order():
    with open(BUGFIX_DIR / "workflow.yaml") as f:
        cfg = yaml.safe_load(f)
    assert 11 in cfg["execution_order"]  # doc_review.yaml's id


def test_doc_review_phase_file_exists_and_produces_the_report():
    doc_review_path = BUGFIX_DIR / "doc_review.yaml"
    assert doc_review_path.exists()
    with open(doc_review_path) as f:
        cfg = yaml.safe_load(f)
    assert cfg["id"] == 11
    assert "feature_report.html" in cfg["outputs"]


def test_doc_review_has_an_evaluation_point():
    with open(BUGFIX_DIR / "workflow.yaml") as f:
        cfg = yaml.safe_load(f)
    after_phases = [e["after_phase"] for e in cfg["orchestrator"]["evaluation_points"]]
    assert "doc_review" in after_phases


def test_bugfix_workflow_loads_with_doc_review_in_order():
    cfg = load_workflow_from_dir(BUGFIX_DIR)
    phases = build_phase_list(cfg)
    names = [p.name for p in phases]
    assert names == [
        "development",
        "adversarial_review",
        "security_review",
        "qa_validation",
        "doc_review",
        "git_expert",
        "deploy",
    ]


def test_bugfix_workflow_config_validates_clean():
    errors = validate_single_workflow(BUGFIX_DIR)
    assert errors == []
