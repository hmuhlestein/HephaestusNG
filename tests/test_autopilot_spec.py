#!/usr/bin/env python3
"""Tests for the hybrid completion gate (src/autopilot/spec.py, design §9.1)."""

import json
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))

import pytest

from src.autopilot import spec as S


@pytest.fixture
def strict_spec():
    return dict(S.DEFAULT_SPEC)  # 0 failed, 0 critical, 100% pass, 100% reqs


# ── score bands map onto the evaluation-point thresholds ──────────────


def test_qa_pass_continues(strict_spec):
    score, meta = S.score_qa(
        {
            "failed_tests": 0,
            "passed_tests": 10,
            "total_tests": 10,
            "critical_issues": 0,
            "requirements_total": 5,
            "requirements_met": 5,
            "agent_score": 1.0,
        },
        strict_spec,
    )
    assert score >= 0.7  # continue
    assert meta["band"] == "pass"


def test_qa_subjective_blend_below_perfect(strict_spec):
    # floors pass, mediocre subjective score -> still >=0.7 but not 1.0
    score, _ = S.score_qa(
        {
            "failed_tests": 0,
            "passed_tests": 1,
            "total_tests": 1,
            "critical_issues": 0,
            "agent_score": 0.0,
        },
        strict_spec,
    )
    assert score == pytest.approx(0.7)


def test_qa_failed_tests_goto_development(strict_spec):
    score, meta = S.score_qa(
        {
            "failed_tests": 2,
            "passed_tests": 8,
            "total_tests": 10,
            "critical_issues": 0,
            "agent_score": 1.0,
        },
        strict_spec,
    )
    assert 0.3 <= score < 0.7  # goto development
    assert meta["band"] == "development"
    assert any("failed_tests" in v for v in meta["violations"])


def test_qa_critical_issue_goto_architecture(strict_spec):
    score, meta = S.score_qa(
        {
            "failed_tests": 0,
            "passed_tests": 10,
            "total_tests": 10,
            "critical_issues": 1,
            "agent_score": 1.0,
        },
        strict_spec,
    )
    assert score < 0.3  # goto architecture
    assert meta["band"] == "architecture"


def test_qa_low_pass_rate_goto_development(strict_spec):
    score, _ = S.score_qa(
        {"passed_tests": 7, "total_tests": 10, "failed_tests": 3, "critical_issues": 0},
        strict_spec,
    )
    assert 0.3 <= score < 0.7


def test_qa_missing_result_is_neutral_development():
    score, meta = S.score_qa(None, S.DEFAULT_SPEC)
    assert 0.3 <= score < 0.7
    assert meta["result_missing"] is True


# ── spec relaxation changes the verdict ───────────────────────────────


def test_relaxed_spec_allows_some_failures():
    relaxed = {
        "max_failed_tests": 5,
        "max_critical_issues": 0,
        "required_pass_rate": 50,
        "min_requirements_met_rate": 50,
    }
    score, meta = S.score_qa(
        {
            "failed_tests": 3,
            "passed_tests": 7,
            "total_tests": 10,
            "critical_issues": 0,
            "agent_score": 1.0,
        },
        relaxed,
    )
    assert score >= 0.7  # 3 failures within budget, 70% > 50% floor
    assert meta["band"] == "pass"


# ── product validation: hard floor overrides optimistic verdict ───────


def test_product_pass_continues():
    score, meta = S.score_product_validation(
        {"verdict": "PASS", "unmet_requirements": [], "agent_score": 1.0},
        S.DEFAULT_SPEC,
    )
    assert score >= 0.7
    assert meta["band"] == "pass"


def test_product_pass_with_unmet_reqs_is_overridden():
    # Agent says PASS but lists unmet requirements -> floor forces development
    score, meta = S.score_product_validation(
        {"verdict": "PASS", "unmet_requirements": ["FR-3 missing"], "agent_score": 1.0},
        S.DEFAULT_SPEC,
    )
    assert 0.3 <= score < 0.7
    assert "override" in meta["reason"]


def test_product_architecture_verdict_goto_architecture():
    score, _ = S.score_product_validation(
        {"verdict": "ARCHITECTURE", "unmet_requirements": []}, S.DEFAULT_SPEC
    )
    assert score < 0.3


def test_product_needs_work_goto_development():
    score, _ = S.score_product_validation(
        {"verdict": "NEEDS_WORK", "unmet_requirements": []}, S.DEFAULT_SPEC
    )
    assert 0.3 <= score < 0.7


# ── build_phase_output: the engine seam ───────────────────────────────


def test_non_gated_phase_returns_empty():
    assert S.build_phase_output("development", "/tmp") == {}


def test_build_phase_output_reads_docs_dir(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "qa_result.json").write_text(
        json.dumps(
            {
                "failed_tests": 0,
                "passed_tests": 5,
                "total_tests": 5,
                "critical_issues": 0,
                "agent_score": 1.0,
            }
        )
    )
    out = S.build_phase_output("qa_validation", tmp_path, spec=dict(S.DEFAULT_SPEC))
    assert out["score"] >= 0.7
    assert out["spec_gate"]["gate"] == "qa"


def test_build_phase_output_root_fallback(tmp_path):
    (tmp_path / "product_validation.json").write_text(
        json.dumps({"verdict": "NEEDS_WORK", "unmet_requirements": ["x"]})
    )
    out = S.build_phase_output(
        "product_validation", tmp_path, spec=dict(S.DEFAULT_SPEC)
    )
    assert 0.3 <= out["score"] < 0.7


def test_load_spec_merges_over_defaults(tmp_path):
    p = tmp_path / "qa_spec.json"
    p.write_text(json.dumps({"max_failed_tests": 3}))
    spec = S.load_spec(p)
    assert spec["max_failed_tests"] == 3
    assert spec["required_pass_rate"] == 100  # default preserved


def test_load_spec_missing_file_is_defaults(tmp_path):
    assert S.load_spec(tmp_path / "nope.json") == S.DEFAULT_SPEC


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:libtmux"])
