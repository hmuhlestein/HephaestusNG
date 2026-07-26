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
    read_okf_report,
    score_adversarial_review,
    score_architectural_review,
    score_feature_review,
    score_product_validation,
    score_qa,
    score_scope_review,
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


class TestScoreScopeReview:
    def test_none_result(self):
        score, meta = score_scope_review(None)
        assert score == 0.4
        assert meta["result_missing"] is True

    def test_pass_no_drift(self):
        result = {"verdict": "PASS", "out_of_scope": [], "missing": []}
        score, meta = score_scope_review(result)
        assert score == 1.0
        assert meta["band"] == "pass"

    def test_drift_quotes_items_in_reason(self):
        """The goto handoff (_fire_phase_transition) reads meta["reason"] --
        must quote the actual out-of-scope/missing items, not just generic
        "Scope drift detected" boilerplate, or product_requirements has no
        idea what to fix on the return trip."""
        result = {
            "verdict": "FAIL",
            "out_of_scope": ["admin dashboard"],
            "missing": ["password reset flow"],
        }
        score, meta = score_scope_review(result)
        assert score < 0.6
        assert meta["band"] == "requirements"
        assert "admin dashboard" in meta["reason"]
        assert "password reset flow" in meta["reason"]

    def test_drift_with_no_itemized_details_falls_back_to_summary(self):
        """Regression: an agent can write verdict='FAIL' (or the analysis_
        summary.scope_drift_detected=True variant) using a schema this
        scorer doesn't itemize from (e.g. a nested "analysis": {...} shape
        with aggregate counts instead of flat out_of_scope/missing lists)
        -- out_of_scope/missing then default to [], and reason_parts ends
        up empty. Observed live: the resulting reason was a dangling
        "Scope drift detected — " with nothing after it, giving
        development zero information about what to actually fix. Must fall
        back to the agent's own free-text summary instead."""
        result = {
            "verdict": "FAIL",
            "summary": "Feature adds an admin dashboard not in the design doc.",
            "analysis": {"out_of_scope": 1, "missing_from_design": 0},
        }
        score, meta = score_scope_review(result)
        assert score < 0.6
        assert meta["reason"] == (
            "Scope drift detected — Feature adds an admin dashboard not in "
            "the design doc."
        )

    def test_drift_with_no_details_and_no_summary_is_still_not_a_dangling_reason(self):
        """Even with no itemized details AND no summary field at all, the
        reason must say something honest rather than trailing off after
        the em-dash with nothing following it."""
        result = {"verdict": "FAIL"}
        score, meta = score_scope_review(result)
        assert score < 0.6
        assert meta["reason"] == (
            "Scope drift detected — no specific out-of-scope/missing items "
            "reported by the agent"
        )


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
        # The goto handoff (_fire_phase_transition) reads meta["reason"] --
        # without it, development would only see the generic workflow.yaml
        # boilerplate ("QA failed, returning to development"), not which
        # specific violation triggered the goto.
        assert "failed_tests" in meta["reason"]

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
        assert "NEEDS_WORK" in meta["reason"]

    def test_pass_with_unmet_overrides(self):
        result = {"verdict": "PASS", "unmet_requirements": ["req1", "req2"]}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert score == 0.5  # unmet overrides verdict
        assert meta["band"] == "development"
        # The goto handoff (_fire_phase_transition) reads meta["reason"] --
        # must quote the actual unmet requirements, not just boilerplate.
        assert "req1" in meta["reason"]
        assert "req2" in meta["reason"]

    def test_unknown_verdict(self):
        result = {"verdict": "MAYBE", "unmet_requirements": []}
        score, meta = score_product_validation(result, DEFAULT_SPEC)
        assert score == 0.5  # conservative
        assert meta["band"] == "development"


def _okf(frontmatter_yaml: str, body: str = "# Report\n") -> str:
    return f"---\n{frontmatter_yaml}\n---\n\n{body}"


class TestReadOkfReport:
    def test_reads_from_docs(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "qa_report.md").write_text(_okf("passed: true"))
        result, body = read_okf_report(tmp_path, "qa_report.md")
        assert result == {"passed": True}
        assert body == "# Report\n"

    def test_reads_from_root(self, tmp_path):
        (tmp_path / "qa_report.md").write_text(_okf("passed: true"))
        result, _ = read_okf_report(tmp_path, "qa_report.md")
        assert result == {"passed": True}

    def test_missing_file(self, tmp_path):
        result, body = read_okf_report(tmp_path, "nonexistent.md")
        assert result is None
        assert body is None

    def test_no_frontmatter_block(self, tmp_path):
        (tmp_path / "qa_report.md").write_text("not okf, just text")
        result, body = read_okf_report(tmp_path, "qa_report.md")
        assert result is None
        assert body is None

    def test_reads_from_phase_scoped_subdirectory(self, tmp_path):
        """Regression: agents are now told to write to the one sanctioned
        docs/<phase_name>/ subdirectory (see each gated phase's CRITICAL
        PATH RULE) -- this must be checked, not just flat docs/."""
        sub = tmp_path / "docs" / "qa_validation"
        sub.mkdir(parents=True)
        (sub / "qa_report.md").write_text(_okf("passed: true"))
        result, _ = read_okf_report(tmp_path, "qa_report.md", phase_name="qa_validation")
        assert result == {"passed": True}

    def test_phase_scoped_subdirectory_wins_over_stale_flat_docs_file(self, tmp_path):
        """The phase-scoped location is the canonical one an agent was
        actually told to use -- it must win over a stale file sitting flat
        in docs/ from an earlier attempt, not the other way around."""
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "qa_report.md").write_text(_okf("passed: false\nstale: true"))
        sub = docs / "qa_validation"
        sub.mkdir()
        (sub / "qa_report.md").write_text(_okf("passed: true\nstale: false"))

        result, _ = read_okf_report(tmp_path, "qa_report.md", phase_name="qa_validation")
        assert result == {"passed": True, "stale": False}

    def test_no_phase_name_does_not_search_other_subdirectories(self, tmp_path):
        """Regression: the old fallback searched EVERY subdirectory of
        docs/ for a same-named file, which could silently pick up a
        DIFFERENT feature's (or an unrelated phase's) file. Without a
        phase_name, only flat docs/ and root are checked -- no guessing."""
        docs = tmp_path / "docs"
        docs.mkdir()
        other = docs / "some_other_feature"
        other.mkdir()
        (other / "qa_report.md").write_text(_okf("passed: true"))

        result, _ = read_okf_report(tmp_path, "qa_report.md")
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
        (docs / "qa_report.md").write_text(_okf(
            "type: qa_validation_result\n"
            "failed_tests: 0\n"
            "passed_tests: 50\n"
            "pass_rate: 100.0\n"
            "critical_issues: 0"
        ))
        result = build_phase_output("qa_validation", tmp_path)
        assert result["score"] >= 0.7

    def test_product_validation_no_result(self, tmp_path):
        result = build_phase_output("product_validation", tmp_path)
        assert "score" in result

    def test_custom_spec(self, tmp_path):
        result = build_phase_output("development", tmp_path, spec={"custom": True})
        assert result == {}

    def test_adversarial_review_no_result(self, tmp_path):
        result = build_phase_output("adversarial_review", tmp_path)
        assert result["score"] == 0.4  # no result → conservative fallback

    def test_adversarial_review_with_blockers(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "adversarial_review_report.md").write_text(_okf(
            "type: adversarial_review_result\n"
            "blocker_count: 6\n"
            "warning_count: 6\n"
            "nit_count: 5"
        ))
        result = build_phase_output("adversarial_review", tmp_path)
        assert result["score"] < 0.6

    def test_architectural_review_with_blockers(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "architectural_review_report.md").write_text(_okf(
            "type: architectural_review_result\n"
            "blocker_count: 2\n"
            "fix_count: 0\n"
            "defer_count: 0"
        ))
        result = build_phase_output("architectural_review", tmp_path)
        assert result["score"] < 0.6

    def test_feature_review_no_result(self, tmp_path):
        result = build_phase_output("feature_review", tmp_path)
        assert result["score"] == 0.4  # no result → conservative fallback

    def test_feature_review_with_fix_quotes_full_report_for_handoff(self, tmp_path):
        """The goto/retry handoff back to Feature Architect
        (_fire_phase_transition -> _create_phase_task's "WHY YOU'RE HERE:"
        text) reads result["spec_gate"]["reason"] verbatim -- this is the
        same mechanism architectural_review/adversarial_review gotos use, so
        a FIX-only feature review (which now also routes back, unlike those
        two) must quote its full report here too, not just a count.

        Written to .hephaestus/, not docs/ -- feature_review's real output
        location, matching its sibling Feature Architect (Phase 0 artifacts
        are internal orchestration state, not a git-tracked deliverable)."""
        from src.core.constants import CONTEXT_DIR_NAME

        heph_dir = tmp_path / CONTEXT_DIR_NAME
        heph_dir.mkdir()
        (heph_dir / "feature_review_report.md").write_text(_okf(
            "type: feature_review_result\n"
            "blocker_count: 0\n"
            "fix_count: 1\n"
            "defer_count: 0",
            body="# Feature Review Report\n\n### [FIX] Ownership overlap",
        ))
        result = build_phase_output("feature_review", tmp_path)
        assert result["score"] < 0.3
        assert "Ownership overlap" in result["spec_gate"]["reason"]


class TestScoreAdversarialReview:
    def test_none_result(self):
        score, meta = score_adversarial_review(None)
        assert score == 0.4
        assert meta["result_missing"] is True

    def test_none_result_with_report_text_still_quotes_report(self):
        """Regression: the JSON can be missing (agent forgot/failed to
        write it) while the markdown report still exists with real
        findings -- don't discard those findings just because the JSON
        didn't get written."""
        report = "# Adversarial Review Report\n\n### [BLOCKER] Connection leak"
        score, meta = score_adversarial_review(None, report_text=report)
        assert meta["result_missing"] is True
        assert report in meta["reason"]

    def test_blocker_routes_to_development(self):
        score, meta = score_adversarial_review(
            {"blocker_count": 6, "warning_count": 6, "nit_count": 5}
        )
        assert score < 0.6
        assert meta["band"] == "development"
        assert meta["blocker_count"] == 6

    def test_warnings_only_still_passes(self):
        score, meta = score_adversarial_review(
            {"blocker_count": 0, "warning_count": 3, "nit_count": 0}
        )
        assert score >= 0.6
        assert meta["band"] == "pass"

    def test_clean(self):
        score, meta = score_adversarial_review(
            {"blocker_count": 0, "warning_count": 0, "nit_count": 0}
        )
        assert score >= 0.6
        assert meta["band"] == "pass"

    def test_blocker_with_report_text_quotes_full_report(self):
        report = "# Adversarial Review Report\n\n### [BLOCKER] Connection leak\n- File: src/foo.py:42"
        score, meta = score_adversarial_review(
            {"blocker_count": 1, "warning_count": 0, "nit_count": 0},
            report_text=report,
        )
        assert score < 0.6
        assert report in meta["reason"]

    def test_blocker_without_report_text_falls_back_to_count(self):
        score, meta = score_adversarial_review(
            {"blocker_count": 1, "warning_count": 0, "nit_count": 0}
        )
        assert score < 0.6
        assert "1 BLOCKER" in meta["reason"]


class TestScoreArchitecturalReview:
    def test_none_result(self):
        score, meta = score_architectural_review(None)
        assert score == 0.4
        assert meta["result_missing"] is True

    def test_none_result_with_report_text_still_quotes_report(self):
        report = "# Architectural Review Report\n\n### [BLOCKER] Interface contract violated"
        score, meta = score_architectural_review(None, report_text=report)
        assert meta["result_missing"] is True
        assert report in meta["reason"]

    def test_blocker_routes_to_development(self):
        score, meta = score_architectural_review(
            {"blocker_count": 2, "fix_count": 1, "defer_count": 0}
        )
        assert score < 0.6
        assert meta["band"] == "development"
        assert meta["blocker_count"] == 2

    def test_fix_only_still_passes(self):
        score, meta = score_architectural_review(
            {"blocker_count": 0, "fix_count": 2, "defer_count": 0}
        )
        assert score >= 0.6
        assert meta["band"] == "pass"

    def test_clean(self):
        score, meta = score_architectural_review(
            {"blocker_count": 0, "fix_count": 0, "defer_count": 0}
        )
        assert score >= 0.6
        assert meta["band"] == "pass"

    def test_blocker_with_report_text_quotes_full_report(self):
        report = "# Architectural Review Report\n\n### [BLOCKER] Interface contract violated"
        score, meta = score_architectural_review(
            {"blocker_count": 1, "fix_count": 0, "defer_count": 0},
            report_text=report,
        )
        assert score < 0.6
        assert report in meta["reason"]


class TestScoreFeatureReview:
    def test_none_result(self):
        score, meta = score_feature_review(None)
        assert score == 0.4
        assert meta["result_missing"] is True

    def test_none_result_with_report_text_still_quotes_report(self):
        report = "# Feature Review Report\n\n### [BLOCKER] Missing auth handling"
        score, meta = score_feature_review(None, report_text=report)
        assert meta["result_missing"] is True
        assert report in meta["reason"]

    def test_blocker_routes_back_to_feature_architect(self):
        score, meta = score_feature_review(
            {"blocker_count": 1, "fix_count": 0, "defer_count": 0}
        )
        assert score < 0.3
        assert meta["band"] == "feature_architect"

    def test_fix_only_also_routes_back(self):
        """Unlike architectural/adversarial review, a FIX-only feature
        review still routes back — Phase 0 has no later phase to catch an
        unaddressed FIX the way development/QA do downstream."""
        score, meta = score_feature_review(
            {"blocker_count": 0, "fix_count": 1, "defer_count": 0}
        )
        assert score < 0.3
        assert meta["band"] == "feature_architect"

    def test_clean_passes(self):
        score, meta = score_feature_review(
            {"blocker_count": 0, "fix_count": 0, "defer_count": 0}
        )
        assert score >= 0.3
        assert meta["band"] == "pass"

    def test_fix_with_report_text_quotes_full_report(self):
        report = "# Feature Review Report\n\n### [FIX] Ownership overlap"
        score, meta = score_feature_review(
            {"blocker_count": 0, "fix_count": 1, "defer_count": 0},
            report_text=report,
        )
        assert score < 0.3
        assert report in meta["reason"]


class TestConstants:
    def test_gated_phases(self):
        assert "qa_validation" in GATED_PHASES
        assert "product_validation" in GATED_PHASES
        assert "architectural_review" in GATED_PHASES
        assert "adversarial_review" in GATED_PHASES
        assert "feature_review" in GATED_PHASES
        assert "development" not in GATED_PHASES

    def test_phase_artifacts(self):
        """PHASE_OUTPUT_ARTIFACTS now only holds workflow.yaml required_output
        overrides (e.g. Phase 0) -- per-phase artifacts are derived from each
        phase's own YAML outputs: list, see get_phase_required_files."""
        assert PHASE_OUTPUT_ARTIFACTS == {}

    def test_default_spec_keys(self):
        assert "max_failed_tests" in DEFAULT_SPEC
        assert "required_pass_rate" in DEFAULT_SPEC
