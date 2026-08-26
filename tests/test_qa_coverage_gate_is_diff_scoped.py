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


# Follow-up regression: Hephaestus targets any language, and a diff with no
# code in a language this project has coverage tooling for (frontend-only
# on a Python-gated repo, docs-only, an unconfigured stack) has no real
# new/modified-code number to report. Agents were observed substituting the
# whole-repo pytest TOTAL line into coverage_percent just to satisfy the
# gate's "required, not optional" field -- exactly the whole-repo number
# this file's other tests exist to keep out of that field. Fixed by a
# coverage_not_applicable escape hatch. The prompt's own worked example
# (checked via `git diff --name-only origin/main...HEAD -- '*.py'`) is for
# Python specifically -- other stacks use their own equivalent check.
def test_bugfix_qa_validation_documents_coverage_not_applicable():
    prompt = _qa_validation_prompt("bugfix")
    assert "coverage_not_applicable" in prompt
    assert "'*.py'" in prompt


def test_autopilot_qa_validation_documents_coverage_not_applicable():
    prompt = _qa_validation_prompt("autopilot")
    assert "coverage_not_applicable" in prompt
    assert "'*.py'" in prompt


# Follow-up regression: the coverage_not_applicable check itself used a
# two-dot `git diff origin/main` (working tree vs. main's current tip),
# which on a long-lived branch/worktree pulls in every commit that landed
# on origin/main after this branch forked -- not just this branch's own
# changes. Observed live: a purely frontend diff on a branch several hours
# behind a fast-moving main falsely showed dozens of unrelated Python files
# as "changed" (including, absurdly, the very commit that added this
# check), so coverage_not_applicable never fired and the gate held a
# frontend-only feature to the Python coverage floor anyway. Three dots
# (git diff origin/main...HEAD, a merge-base diff) isolates only the
# branch's own changes -- verified against a real long-lived worktree in
# this repo, where the two-dot form returned 14 unrelated Python files and
# the three-dot form correctly returned none. diff-cover's own
# --compare-branch is unaffected -- its --diff-range-notation already
# defaults to '...'.
def test_bugfix_qa_validation_coverage_check_uses_merge_base_diff():
    prompt = _qa_validation_prompt("bugfix")
    assert "origin/main...HEAD -- '*.py'" in prompt
    assert "origin/main -- '*.py'" not in prompt.replace("origin/main...HEAD -- '*.py'", "")


def test_autopilot_qa_validation_coverage_check_uses_merge_base_diff():
    prompt = _qa_validation_prompt("autopilot")
    assert "origin/main...HEAD -- '*.py'" in prompt
    assert "origin/main -- '*.py'" not in prompt.replace("origin/main...HEAD -- '*.py'", "")
