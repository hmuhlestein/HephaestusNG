"""Regression: qa_validation.yaml (both workflows) instructed the agent to
report coverage_percent from `pytest --cov=src`'s whole-repo `TOTAL` line,
which score_qa (src/autopilot/spec.py) then held to an 80% floor. Since
most of this repo sits well below 80% overall coverage (.coveragerc's own
fail_under=20 documents that), the qa_validation gate effectively failed
every run regardless of how well-tested the actual changes were -- the
80% floor was never reachable against whole-repo coverage.

A separate, already-built mechanism (scripts/check_coverage.py, diff-cover,
hephaestus_config.yaml's testing.new_code_coverage_floor) already measures
coverage of only new/modified lines vs. origin/main, but was never wired
into the qa_validation phase's actual prompts. Fixed by having qa_validation
report diff-cover's new-code coverage number instead of the whole-repo
TOTAL line -- score_qa's comparison logic (coverage_percent >= 80) is
otherwise unchanged; only what gets measured and reported changed.
"""

from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "config" / "workflows"


def _qa_validation_prompt(workflow_name: str) -> str:
    with open(WORKFLOWS_DIR / workflow_name / "qa_validation.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg["additional_notes"]


def test_bugfix_qa_validation_measures_diff_scoped_coverage():
    prompt = _qa_validation_prompt("bugfix")
    assert "diff-cover" in prompt
    assert "compare-branch=origin/main" in prompt
    assert "whole-repo" in prompt.lower()


def test_autopilot_qa_validation_measures_diff_scoped_coverage():
    prompt = _qa_validation_prompt("autopilot")
    assert "diff-cover" in prompt
    assert "compare-branch=origin/main" in prompt
    assert "whole-repo" in prompt.lower()
