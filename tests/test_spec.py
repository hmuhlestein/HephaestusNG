"""Tests for autopilot/spec.py — scoring, loading, phase output."""

import json

from src.autopilot.spec import (
    DEFAULT_SPEC,
    GATED_PHASES,
    PHASE_OUTPUT_ARTIFACTS,
    _clamp01,
    _pass_with_subjective,
    build_phase_output,
    load_spec,
    read_result,
    score_product_validation,
    score_qa,
)


class TestClamp01:
    def test_valid_values(self):
        assert _clamp01(0.5) == 0.5
        assert _clamp01(0.0) == 0.0
        assert _clamp01(1.0) == 1.0

    def test_clamps_above(self):
        assert _clamp01(2.0) == 1.0
        assert _clamp01(100) == 1.0

    def test_clamps_below(self):
        assert _clamp01(-1.0) == 0.0
        assert _clamp01(-100) == 0.0

    def test_non_numeric(self):
        assert _clamp01("hello") == 0.0
        assert _clamp01(None) == 0.0
        assert _clamp01([]) == 0.0

    def test_custom_default(self):
        assert _clamp01("x", default=0.5) == 0.5

    def test_string_number(self):
        assert _clamp01("0.7") == 0.7


class TestPassWithSubjective:
    def test_full_score(self):
        assert _pass_with_subjective(1.0) == 1.0

    def test_zero_score(self):
        assert _pass_with_subjective(0.0) == 0.7

    def test_half_score(self):
        result = _pass_with_subjective(0.5)
        assert 0.7 <= result <= 1.0

    def test_non_numeric(self):
        # _clamp01 defaults to 1.0 for non-numeric → 0.7 + 0.3*1.0 = 1.0
        assert _pass_with_subjective("bad") == 1.0


class TestLoadSpec:
    def test_default_spec(self, tmp_path):
        result = load_spec(tmp_path / "nonexistent.json")
        assert result == DEFAULT_SPEC

    def test_valid_spec(self, tmp_path):
        spec_file = tmp_path / "qa_spec.json"
        spec_file.write_text(json.dumps({"max_failed_tests": 5}))
        result = load_spec(spec_file)
        assert result["max_failed_tests"] == 5
        assert result["required_pass_rate"] == 100  # default preserved

    def test_corrupt_spec(self, tmp_path):
        spec_file = tmp_path / "qa_spec.json"
        spec_file.write_text("not json")
        result = load_spec(spec_file)
        assert result == DEFAULT_SPEC

    def test_partial_spec(self, tmp_path):
        spec_file = tmp_path / "qa_spec.json"
        spec_file.write_text(json.dumps({"max_critical_issues": 3, "extra_key": 42}))
        result = load_spec(spec_file)
        assert result["max_critical_issues"] == 3
        assert "extra_key" not in result


class TestScoreQA:
    def test_none_result(self):
        score, meta = score_qa(None, DEFAULT_SPEC)
        assert score == 0.5
        assert meta["result_missing"] is True

    def test_passing_result(self):
        result = {
            "failed_tests": 0,
            "passed_tests": 50,
            "total_tests": 50,
            "pass_rate": 100.0,
            "critical_issues": 0,
            "requirements_met": 7,
            "requirements_total": 7,
            "agent_score": 1.0,
        }
        score, meta = score_qa(result, DEFAULT_SPEC)
        assert score >= 0.7
        assert meta["band"] == "pass"
        assert meta["violations"] == []

    def test_critical_issues(self):
        result = {
            "failed_tests": 0,
            "passed_tests": 50,
            "critical_issues": 3,
            "agent_score": 1.0,
        }
        score, meta = score_qa(result, DEFAULT_SPEC)
        assert score == 0.25  # architecture band
        assert meta["band"] == "architecture"

    def test_failed_tests_violation(self):
        result = {
            "failed_tests": 5,
            "passed_tests": 45,
            "pass_rate": 90.0,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        score, meta = score_qa(result, DEFAULT_SPEC)
        assert score == 0.5  # development band
        assert meta["band"] == "development"
        assert any("failed_tests" in v for v in meta["violations"])

    def test_low_pass_rate(self):
        result = {
            "failed_tests": 0,
            "passed_tests": 70,
            "total_tests": 100,
            "pass_rate": 70.0,  # below 100%
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        score, meta = score_qa(result, DEFAULT_SPEC)
        assert score == 0.5  # development band
        assert any("pass_rate" in v for v in meta["violations"])

    def test_subjective_blend(self):
        result = {
            "failed_tests": 0,
            "passed_tests": 50,
            "pass_rate": 100.0,
            "critical_issues": 0,
            "agent_score": 0.5,
        }
        score, meta = score_qa(result, DEFAULT_SPEC)
        assert 0.7 <= score <= 1.0
        assert meta["band"] == "pass"

    def test_computed_pass_rate(self):
        result = {"passed_tests": 9, "failed_tests": 1}
        score, meta = score_qa(result, DEFAULT_SPEC)
        # pass_rate = 9/10*100 = 90% < 100%
        assert meta["pass_rate"] == 90.0

    def test_requirements_violation(self):
        result = {
            "passed_tests": 50,
            "requirements_met": 3,
            "requirements_total": 10,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        score, meta = score_qa(result, DEFAULT_SPEC)
        assert score == 0.5  # development band
        assert meta["requirements_met_rate"] == 30.0

    def test_malformed_agent_report_adopts_independent_verification(
        self, tmp_path, monkeypatch
    ):
        """Agent ignores the documented top-level schema (writes its own
        nested report shape instead of failed_tests/passed_tests/total_tests/
        pass_rate) -> total computes to 0, which independent verification
        must not read as '0 tests, 0% pass' when the real suite passed."""
        from src.autopilot import spec as spec_module

        (tmp_path / "test_x.py").write_text(
            "def test_a():\n    assert True\ndef test_b():\n    assert True\n"
        )

        def fake_verification(working_directory, timeout_seconds=300):
            return {
                "failed": 0,
                "passed": 2,
                "total": 2,
                "pass_rate": 100.0,
                "source": "independent_verification",
            }

        monkeypatch.setattr(
            spec_module, "run_independent_test_verification", fake_verification
        )

        result = {
            "overall_status": "PASS",
            "test_results": {"unit_tests": {"total": 2, "passed": 2, "failed": 0}},
        }
        score, meta = score_qa(
            result, DEFAULT_SPEC, working_directory=str(tmp_path)
        )
        assert meta["band"] == "pass"
        assert meta["pass_rate"] == 100.0
        assert meta["failed_tests"] == 0
        assert meta["violations"] == []

    def test_independent_verification_still_overrides_worse_claim(
        self, tmp_path, monkeypatch
    ):
        """Regression: when the agent DOES populate the schema but claims
        better results than independent verification finds, the independent
        (worse) result must still win — this is the original Enhancement 1
        behavior and must not be disturbed by the total==0 fallback."""
        from src.autopilot import spec as spec_module

        def fake_verification(working_directory, timeout_seconds=300):
            return {
                "failed": 3,
                "passed": 7,
                "total": 10,
                "pass_rate": 70.0,
                "source": "independent_verification",
            }

        monkeypatch.setattr(
            spec_module, "run_independent_test_verification", fake_verification
        )

        result = {
            "failed_tests": 0,
            "passed_tests": 10,
            "total_tests": 10,
            "pass_rate": 100.0,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        score, meta = score_qa(result, DEFAULT_SPEC, working_directory=str(tmp_path))
        assert meta["band"] == "development"
        assert meta["failed_tests"] == 3
        assert meta["pass_rate"] == 70.0


class TestScoreProductValidation:
    def test_none_result(self):
        score, meta = score_product_validation(None, DEFAULT_SPEC)
        assert score == 0.5
        assert meta["result_missing"] is True

    def test_pass_no_unmet(self):
        result = {"verdict": "PASS", "unmet_requirements": [], "agent_score": 1.0}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert score >= 0.7
        assert meta["band"] == "pass"

    def test_architecture_verdict(self):
        result = {"verdict": "ARCHITECTURE", "unmet_requirements": []}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert score == 0.25
        assert meta["band"] == "architecture"

    def test_arch_in_verdict(self):
        result = {"verdict": "NEEDS ARCHITECTURE WORK", "unmet_requirements": []}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert meta["band"] == "architecture"

    def test_needs_work(self):
        result = {"verdict": "NEEDS_WORK", "unmet_requirements": []}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert score == 0.5
        assert meta["band"] == "development"

    def test_pass_with_unmet_overrides(self):
        result = {"verdict": "PASS", "unmet_requirements": ["req1", "req2"]}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert score == 0.5  # unmet overrides verdict
        assert meta["band"] == "development"

    def test_unknown_verdict(self):
        result = {"verdict": "MAYBE", "unmet_requirements": []}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert score == 0.5  # conservative
        assert meta["band"] == "development"


class TestReadResult:
    def test_reads_from_docs(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "qa_result.json").write_text('{"passed": true}')
        result = read_result(tmp_path, "qa_result.json")
        assert result == {"passed": True}

    def test_reads_from_root(self, tmp_path):
        (tmp_path / "qa_result.json").write_text('{"passed": true}')
        result = read_result(tmp_path, "qa_result.json")
        assert result == {"passed": True}

    def test_missing_file(self, tmp_path):
        result = read_result(tmp_path, "nonexistent.json")
        assert result is None

    def test_corrupt_json(self, tmp_path):
        (tmp_path / "qa_result.json").write_text("not json")
        result = read_result(tmp_path, "qa_result.json")
        assert result is None


class TestBuildPhaseOutput:
    def test_non_gated_phase(self, tmp_path):
        result = build_phase_output("development", tmp_path)
        assert result == {}

    def test_qa_validation_no_result(self, tmp_path):
        result = build_phase_output("qa_validation", tmp_path)
        assert "score" in result
        assert result["score"] == 0.5  # no result → default dev band

    def test_qa_validation_with_result(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "qa_result.json").write_text(
            json.dumps(
                {
                    "failed_tests": 0,
                    "passed_tests": 50,
                    "pass_rate": 100.0,
                    "critical_issues": 0,
                }
            )
        )
        result = build_phase_output("qa_validation", tmp_path)
        assert result["score"] >= 0.7

    def test_product_validation_no_result(self, tmp_path):
        result = build_phase_output("product_validation", tmp_path)
        assert "score" in result

    def test_custom_spec(self, tmp_path):
        result = build_phase_output("development", tmp_path, spec={"custom": True})
        assert result == {}


class TestConstants:
    def test_gated_phases(self):
        assert "qa_validation" in GATED_PHASES
        assert "product_validation" in GATED_PHASES
        assert "development" not in GATED_PHASES

    def test_phase_artifacts(self):
        assert PHASE_OUTPUT_ARTIFACTS["qa_validation"] == "qa_result.json"
        assert PHASE_OUTPUT_ARTIFACTS["architecture_design"] == "architecture.md"

    def test_default_spec_keys(self):
        assert "max_failed_tests" in DEFAULT_SPEC
        assert "required_pass_rate" in DEFAULT_SPEC
