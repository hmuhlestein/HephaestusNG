"""Tests for autopilot/spec.py — scoring, loading, phase output."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from src.autopilot.spec import (
    DEFAULT_SPEC,
    PHASE_OUTPUT_ARTIFACTS,
    _clamp01,
    _pass_with_subjective,
    _select_relevant_test_files,
    build_phase_output,
    gate_finding_count,
    get_gated_phases,
    load_spec,
    read_okf_report,
    run_independent_test_verification,
    score_adversarial_review,
    score_architectural_review,
    score_design_review,
    score_feature_review,
    score_product_validation,
    score_qa,
    score_scope_review,
    score_security_review,
)

# get_gated_phases() is lru_cache'd and lazily computed (moved off module
# import, see its own docstring) -- bound once here so every existing
# `GATED_PHASES` usage below (a bare name, checked with plain `in`/`not in`)
# keeps working unchanged.
GATED_PHASES = get_gated_phases()


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

    def test_pass_with_benign_listed_items_is_still_a_pass(self):
        """Regression: scope_review.yaml tells the agent to PASS with benign
        items still listed for transparency (an implementation detail the
        design doc implies, e.g.) and reserve FAIL for real scope drift.
        Requiring both lists empty for a PASS score overrode that judgment
        call and forced an unwinnable goto loop: product_requirements keeps
        regenerating the same reasonable inference, scope_review keeps
        re-approving it with verdict=PASS, the scorer kept re-failing it
        anyway because the list wasn't empty. Observed live: 162 gotos and
        all 3 arbitration attempts burned on exactly this pattern."""
        result = {
            "verdict": "PASS",
            "out_of_scope": ["free_signup transaction type (consistent with design's own example)"],
            "missing": ["maxBalance: 500 constraint (minor, enforceable via validation)"],
        }
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
            "coverage_percent": 85,
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
            "coverage_percent": 85,
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
            "coverage_percent": 85,
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

    def test_independent_verification_timeout_comes_from_config(
        self, tmp_path, monkeypatch
    ):
        """Regression: the independent-verification timeout used to be a
        hardcoded 300s default with no way to raise it for a target project
        whose test suite runs close to that ceiling -- violates this
        project's no-hardcoded-timeouts convention (CLAUDE.md). It must now
        come from hephaestus_config.yaml's autopilot.
        independent_test_timeout_seconds, not the function's own default."""
        from unittest.mock import MagicMock

        from src.autopilot import spec as spec_module

        received = {}

        def fake_verification(working_directory, timeout_seconds=300):
            received["timeout_seconds"] = timeout_seconds
            return None

        monkeypatch.setattr(
            spec_module, "run_independent_test_verification", fake_verification
        )
        fake_config = MagicMock()
        fake_config.autopilot.independent_test_timeout_seconds = 900
        monkeypatch.setattr(
            "src.core.simple_config.get_config", lambda: fake_config
        )

        result = {
            "failed_tests": 0,
            "passed_tests": 10,
            "total_tests": 10,
            "pass_rate": 100.0,
            "critical_issues": 0,
            "agent_score": 1.0,
        }
        score_qa(result, DEFAULT_SPEC, working_directory=str(tmp_path))

        assert received["timeout_seconds"] == 900


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
    def test_reads_from_root(self, tmp_path):
        (tmp_path / "qa.md").write_text(_okf("passed: true"))
        result, _ = read_okf_report(tmp_path, "qa.md")
        assert result == {"passed": True}

    def test_missing_file(self, tmp_path):
        result, body = read_okf_report(tmp_path, "nonexistent.md")
        assert result is None
        assert body is None

    def test_no_frontmatter_block(self, tmp_path):
        (tmp_path / "qa.md").write_text("not okf, just text")
        result, body = read_okf_report(tmp_path, "qa.md")
        assert result is None
        assert body is None

    def test_reads_from_phase_scoped_subdirectory(self, tmp_path):
        """Regression: agents are now told to write to the one sanctioned
        .hephaestus/<phase_name>/ subdirectory (see each gated phase's
        CRITICAL PATH RULE) -- this must be checked before the root
        fallback."""
        sub = tmp_path / ".hephaestus" / "qa_validation"
        sub.mkdir(parents=True)
        (sub / "qa.md").write_text(_okf("passed: true"))
        result, _ = read_okf_report(tmp_path, "qa.md", phase_name="qa_validation")
        assert result == {"passed": True}

    def test_phase_scoped_subdirectory_wins_over_stale_root_file(self, tmp_path):
        """The phase-scoped location is the canonical one an agent was
        actually told to use -- it must win over a stale file sitting at
        the project root from an earlier attempt, not the other way
        around."""
        (tmp_path / "qa.md").write_text(_okf("passed: false\nstale: true"))
        sub = tmp_path / ".hephaestus" / "qa_validation"
        sub.mkdir(parents=True)
        (sub / "qa.md").write_text(_okf("passed: true\nstale: false"))

        result, _ = read_okf_report(tmp_path, "qa.md", phase_name="qa_validation")
        assert result == {"passed": True, "stale": False}

    def test_no_phase_name_does_not_search_other_subdirectories(self, tmp_path):
        """Regression: without a phase_name, only the project root is
        checked -- no guessing across .hephaestus/ subdirectories that
        could silently pick up a DIFFERENT feature's (or an unrelated
        phase's) file."""
        docs = tmp_path / ".hephaestus"
        docs.mkdir()
        other = docs / "some_other_feature"
        other.mkdir()
        (other / "qa.md").write_text(_okf("passed: true"))

        result, _ = read_okf_report(tmp_path, "qa.md")
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
        docs = tmp_path / ".hephaestus" / "qa_validation"
        docs.mkdir(parents=True)
        (docs / "qa.md").write_text(_okf(
            "type: qa_validation_result\n"
            "failed_tests: 0\n"
            "passed_tests: 50\n"
            "pass_rate: 100.0\n"
            "critical_issues: 0\n"
            "coverage_percent: 85"
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
        docs = tmp_path / ".hephaestus" / "adversarial_review"
        docs.mkdir(parents=True)
        (docs / "adversarial.md").write_text(_okf(
            "type: adversarial_review_result\n"
            "blocker_count: 6\n"
            "warning_count: 6\n"
            "nit_count: 5"
        ))
        result = build_phase_output("adversarial_review", tmp_path)
        assert result["score"] < 0.6
        assert result["spec_gate"]["blocker_count"] == 6
        assert "result_missing" not in result["spec_gate"]

    def test_adversarial_review_warnings_unchanged_from_history_passes(self, tmp_path, db_manager):
        """End-to-end wiring: build_phase_output looks up the workflow's own
        review-findings history and passes prior_warning_count through to
        score_adversarial_review, so a re-run with the same warning_count as
        last time passes instead of looping back to development forever."""
        from src.autopilot.spec import record_review_finding

        record_review_finding(
            "wf-bpo-adv-1", "adversarial_review", blocker_count=0,
            summary="2 pre-existing, out-of-scope warnings", warning_count=2,
        )
        docs = tmp_path / ".hephaestus" / "adversarial_review"
        docs.mkdir(parents=True)
        (docs / "adversarial.md").write_text(_okf(
            "type: adversarial_review_result\n"
            "blocker_count: 0\n"
            "warning_count: 2\n"
            "nit_count: 1"
        ))
        result = build_phase_output(
            "adversarial_review", tmp_path, workflow_id="wf-bpo-adv-1"
        )
        assert result["score"] >= 0.7
        assert result["spec_gate"]["band"] == "pass"

    def test_design_review_warnings_unchanged_from_history_passes(self, tmp_path, db_manager):
        """ticket-14029d38: same end-to-end wiring as
        test_adversarial_review_warnings_unchanged_from_history_passes,
        for design_review's challenge.md gate."""
        from src.autopilot.spec import record_review_finding

        record_review_finding(
            "wf-bpo-design-1", "design_review", blocker_count=0,
            summary="2 pre-existing, deferred-to-qa_validation warnings", warning_count=2,
        )
        docs = tmp_path / ".hephaestus" / "design_review"
        docs.mkdir(parents=True)
        (docs / "challenge.md").write_text(_okf(
            "type: design_review_result\n"
            "blocker_count: 0\n"
            "warning_count: 2\n"
            "nit_count: 2"
        ))
        result = build_phase_output(
            "design_review", tmp_path, workflow_id="wf-bpo-design-1"
        )
        assert result["score"] >= 0.6
        assert result["spec_gate"]["band"] == "pass"

    def test_architectural_review_with_blockers(self, tmp_path):
        docs = tmp_path / ".hephaestus" / "architectural_review"
        docs.mkdir(parents=True)
        (docs / "review.md").write_text(_okf(
            "type: architectural_review_result\n"
            "blocker_count: 2\n"
            "fix_count: 0\n"
            "defer_count: 0"
        ))
        result = build_phase_output("architectural_review", tmp_path)
        assert result["score"] < 0.6
        assert result["spec_gate"]["blocker_count"] == 2
        assert "result_missing" not in result["spec_gate"]

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

        Written to .hephaestus/feature_review/, not docs/ -- the same
        .hephaestus/<phase_name>/ convention every other gated phase uses
        (Phase 2 §4.9 follow-up normalized feature_review off its old
        flat-.hephaestus/, name-colliding-with-architectural_review's-own-
        review.md exception)."""
        heph_dir = tmp_path / ".hephaestus" / "feature_review"
        heph_dir.mkdir(parents=True)
        (heph_dir / "feature_review.md").write_text(_okf(
            "type: feature_review_result\n"
            "blocker_count: 0\n"
            "fix_count: 1\n"
            "defer_count: 0",
            body="# Feature Review Report\n\n### [FIX] Ownership overlap",
        ))
        result = build_phase_output("feature_review", tmp_path)
        assert result["score"] < 0.3
        assert "Ownership overlap" in result["spec_gate"]["reason"]


class TestSelectRelevantTestFiles:
    """Regression: run_independent_test_verification used to run pytest
    with no path restriction at all -- this project's own TESTING.md says
    "Do not run the full test suite... it's slow" and "may take several
    minutes" (222 files). Every qa_validation completion hit that full run
    unconditionally; when the target project IS this repo, that routinely
    exceeded the gate's own timeout and stalled phase advancement for the
    full window. _select_relevant_test_files scopes to what the feature
    branch actually changed instead."""

    def _run(self, repo, *args):
        import subprocess

        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    def _init_repo(self, tmp_path):
        repo = tmp_path
        self._run(repo, "init", "-b", "main")
        self._run(repo, "config", "user.email", "test@test.com")
        self._run(repo, "config", "user.name", "Test")
        return repo

    def test_maps_changed_source_file_to_its_test(self, tmp_path, monkeypatch):
        repo = self._init_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "foo.py").write_text("def foo(): return 1\n")
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_foo.py").write_text("def test_foo(): assert True\n")
        self._run(repo, "add", "-A")
        self._run(repo, "commit", "-m", "base")
        self._run(repo, "checkout", "-b", "feature")
        (repo / "src" / "foo.py").write_text("def foo(): return 2\n")
        self._run(repo, "add", "-A")
        self._run(repo, "commit", "-m", "change foo")

        fake_config = MagicMock()
        fake_config.git.base_branch = "main"
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: fake_config)

        result = _select_relevant_test_files(str(repo))

        assert result == ["tests/test_foo.py"]

    def test_includes_changed_test_file_directly(self, tmp_path, monkeypatch):
        repo = self._init_repo(tmp_path)
        tests_dir = repo / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_bar.py").write_text("def test_bar(): assert True\n")
        self._run(repo, "add", "-A")
        self._run(repo, "commit", "-m", "base")
        self._run(repo, "checkout", "-b", "feature")
        (tests_dir / "test_bar.py").write_text("def test_bar(): assert 1 == 1\n")
        self._run(repo, "add", "-A")
        self._run(repo, "commit", "-m", "change test_bar")

        fake_config = MagicMock()
        fake_config.git.base_branch = "main"
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: fake_config)

        result = _select_relevant_test_files(str(repo))

        assert result == ["tests/test_bar.py"]

    def test_returns_none_when_no_changed_file_maps_to_a_test(
        self, tmp_path, monkeypatch
    ):
        repo = self._init_repo(tmp_path)
        (repo / "README.md").write_text("hello\n")
        self._run(repo, "add", "-A")
        self._run(repo, "commit", "-m", "base")
        self._run(repo, "checkout", "-b", "feature")
        (repo / "README.md").write_text("hello world\n")
        self._run(repo, "add", "-A")
        self._run(repo, "commit", "-m", "change readme")

        fake_config = MagicMock()
        fake_config.git.base_branch = "main"
        monkeypatch.setattr("src.core.simple_config.get_config", lambda: fake_config)

        assert _select_relevant_test_files(str(repo)) is None

    def test_returns_none_outside_a_git_repo(self, tmp_path):
        assert _select_relevant_test_files(str(tmp_path)) is None


class TestRunIndependentTestVerificationScoping:
    def test_skips_pytest_entirely_when_nothing_maps(self, tmp_path, monkeypatch):
        """The whole point of scoping: when no changed file maps to a
        test, don't fall back to running the entire suite -- skip
        verification and let the caller fall back to the agent's report."""
        from src.autopilot import spec as spec_module

        monkeypatch.setattr(
            spec_module, "_select_relevant_test_files", lambda wd: None
        )

        def fail_if_called(*a, **k):
            raise AssertionError("pytest should not have been invoked")

        monkeypatch.setattr("subprocess.run", fail_if_called)

        result = run_independent_test_verification(str(tmp_path))

        assert result is None

    def test_runs_pytest_scoped_to_the_resolved_files_only(
        self, tmp_path, monkeypatch
    ):
        from src.autopilot import spec as spec_module

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_scoped.py").write_text(
            "def test_a(): assert True\ndef test_b(): assert True\n"
        )
        # An unrelated test file that must NOT run -- if pytest were
        # invoked unscoped, this failure would show up in the summary.
        (tmp_path / "tests" / "test_unrelated.py").write_text(
            "def test_fails(): assert False\n"
        )
        monkeypatch.setattr(
            spec_module,
            "_select_relevant_test_files",
            lambda wd: ["tests/test_scoped.py"],
        )

        result = run_independent_test_verification(str(tmp_path))

        assert result is not None
        assert result["failed"] == 0
        assert result["passed"] == 2


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
        assert score < 0.6
        assert meta["band"] == "development"

    def test_warnings_unchanged_from_prior_run_passes(self):
        """The exact loop observed live: the same pre-existing, out-of-scope
        warnings recur every run because development has nothing new to fix.
        Without prior_warning_count, this routed to development forever."""
        score, meta = score_adversarial_review(
            {"blocker_count": 0, "warning_count": 2, "nit_count": 1},
            prior_warning_count=2,
        )
        assert score >= 0.7
        assert meta["band"] == "pass"
        assert meta["warning_count"] == 2

    def test_warnings_fewer_than_prior_run_passes(self):
        score, meta = score_adversarial_review(
            {"blocker_count": 0, "warning_count": 1, "nit_count": 0},
            prior_warning_count=2,
        )
        assert score >= 0.7
        assert meta["band"] == "pass"

    def test_new_warning_beyond_prior_run_still_routes_to_development(self):
        """A HIGHER warning_count than last run is real signal something
        changed -- still worth another development pass, unlike the
        unchanged case above."""
        score, meta = score_adversarial_review(
            {"blocker_count": 0, "warning_count": 3, "nit_count": 0},
            prior_warning_count=2,
        )
        assert score < 0.6
        assert meta["band"] == "development"

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


class TestGateFindingCount:
    """record_review_finding read result["blocker_count"] unconditionally,
    so every gated phase that doesn't use that key recorded 0 findings no
    matter what it found -- and the prior-findings block injected into the
    next run's task description announced "0 finding(s)" above a summary
    describing real ones. Each phase's report speaks its own vocabulary."""

    def test_blocker_style_reviews(self):
        for phase in ("adversarial_review", "architectural_review", "design_review"):
            assert gate_finding_count(phase, {"blocker_count": 3}) == 3

    def test_security_review_uses_unresolved_count(self):
        # Found-but-fixed must NOT be recorded as an unresolved finding --
        # the same inverted polarity score_security_review reads.
        assert gate_finding_count(
            "security_review", {"critical_count": 6, "high_count": 2, "unresolved_count": 0}
        ) == 0
        assert gate_finding_count("security_review", {"unresolved_count": 2}) == 2

    def test_qa_validation_sums_failures_and_critical_issues(self):
        assert gate_finding_count(
            "qa_validation", {"failed_tests": 3, "critical_issues": 2}
        ) == 5

    def test_product_validation_counts_unmet_requirements(self):
        assert gate_finding_count(
            "product_validation", {"unmet_requirements": ["REQ-1", "REQ-2"]}
        ) == 2
        assert gate_finding_count("product_validation", {"unmet_requirements": None}) == 0

    def test_none_result_and_missing_keys_are_zero_not_a_crash(self):
        assert gate_finding_count("security_review", None) == 0
        assert gate_finding_count("qa_validation", {}) == 0
        assert gate_finding_count("scope_review", {"verdict": "FAIL"}) == 0


class TestScoreSecurityReview:
    """security_review carried a full set of workflow.yaml conditions
    (score < 0.3 -> architecture_design, score < 0.7 -> development) but
    never declared `spec_gate: true`, so build_phase_output returned {} for
    it and the heuristic evaluator's fixed 0.75 baseline continued past the
    gate every time -- a security review reporting unfixed critical
    vulnerabilities advanced straight to QA. Same dead-gate bug as
    adversarial_review/architectural_review, which were fixed earlier;
    security_review and doc_review were left behind.

    Note the inverted polarity vs every other review scorer: this phase
    FIXES what it finds, so finding a lot is not a failure -- only
    unresolved_count is."""

    def test_none_result(self):
        score, meta = score_security_review(None)
        assert score == 0.4
        assert meta["result_missing"] is True

    def test_none_result_with_report_text_still_quotes_report(self):
        report = "# Security Review Report\n\n### SQL injection at api/users.py:42"
        score, meta = score_security_review(None, report_text=report)
        assert meta["result_missing"] is True
        assert report in meta["reason"]

    def test_many_found_but_all_fixed_is_a_clean_pass(self):
        """The distinguishing case: a review that found six criticals and
        fixed all six passes. Scoring on found-counts would have punished
        the phase for doing its job."""
        score, meta = score_security_review(
            {"critical_count": 6, "high_count": 2, "unresolved_count": 0}
        )
        assert score >= 0.7
        assert meta["band"] == "pass"

    def test_unresolved_routes_to_development(self):
        score, meta = score_security_review(
            {"critical_count": 3, "high_count": 1, "unresolved_count": 2}
        )
        # Below security_review's 0.7 continue bar, but at or above the 0.3
        # architecture_design bar -- code-level fix, not a redesign.
        assert 0.3 <= score < 0.7
        assert meta["band"] == "development"
        assert meta["unresolved_count"] == 2

    def test_unresolved_with_report_text_quotes_full_report(self):
        """A developer agent with no other context needs the actual
        file:line references, not a count."""
        report = "# Security Review Report\n\n### Auth bypass at api/session.py:88\n- Fix: verify signature"
        score, meta = score_security_review(
            {"critical_count": 1, "high_count": 0, "unresolved_count": 1},
            report_text=report,
        )
        assert report in meta["reason"]

    def test_clean_report_with_nothing_found(self):
        score, meta = score_security_review(
            {"critical_count": 0, "high_count": 0, "unresolved_count": 0}
        )
        assert score >= 0.7
        assert meta["reason"] == "clean"

    def test_missing_counts_default_to_clean_not_crash(self):
        """A report with the right type but no counts is caught upstream by
        validate_gate_result_schema, not here -- this must not raise."""
        score, meta = score_security_review({"type": "security_review_report"})
        assert score >= 0.7


def _reachable_scores(spec):
    """Every (score, band) each gated phase's scorer can actually emit, for
    the representative inputs its own documented schema allows. The band is
    the scorer's OWN label ("pass" / "development" / "architecture"), so
    callers don't have to guess which anchor constant a phase uses -- they
    don't all use the same ones (scope_review emits 0.2/1.0 and never
    touches _DEV at all)."""
    from src.autopilot.spec import (
        score_adversarial_review,
        score_architectural_review,
        score_design_review,
        score_product_validation,
        score_qa,
        score_scope_review,
        score_security_review,
    )

    def add(bucket, pair):
        score, meta = pair
        out[bucket].add((score, meta.get("band")))

    out = {k: set() for k in (
        "scope_review", "design_review", "adversarial_review",
        "architectural_review", "security_review", "qa_validation",
        "product_validation",
    )}
    for v in ("PASS", "FAIL"):
        add("scope_review", score_scope_review({"type": "scope_review", "verdict": v}))
    for bc in (0, 1, 4):
        for second in (0, 2):
            add("design_review",
                score_design_review({"blocker_count": bc, "warning_count": second}))
            add("adversarial_review",
                score_adversarial_review({"blocker_count": bc, "warning_count": second}))
            add("architectural_review",
                score_architectural_review({"blocker_count": bc, "fix_count": second}))
    for u in (0, 1, 5):
        add("security_review",
            score_security_review({"unresolved_count": u, "critical_count": u, "high_count": 0}))
    for failed, total in ((0, 10), (1, 10), (10, 10)):
        for ci in (0, 3):
            for met in (10, 5):
                add("qa_validation", score_qa({
                    "type": "qa_validation", "passed_tests": total - failed,
                    "failed_tests": failed, "total_tests": total,
                    "pass_rate": (total - failed) / total * 100, "critical_issues": ci,
                    "requirements_met": met, "requirements_total": 10,
                }, spec, working_directory=None))
    for verdict in ("PASS", "PASS_WITH_MINOR_GAPS", "FAIL", "ARCHITECTURE", ""):
        for unmet in ([], ["a"], ["a", "b", "c"]):
            for agent_score in (0.0, 1.0):
                add("product_validation", score_product_validation({
                    "type": "product_validation", "verdict": verdict,
                    "unmet_requirements": unmet, "agent_score": agent_score,
                }, spec))
    return out


class TestInputManifest:
    """Phase prompts have always named their inputs in prose
    ("requirements.md (from Artifacts Path) - REQ-XX requirements to
    implement"). Prose tells an agent WHY it wants a file and nothing about
    whether the file is there -- an input a goto rewound, or that
    consume_gate_artifacts deleted after a gate decision, or that an optional
    phase never produced, reads exactly like one sitting on disk. The
    manifest resolves them at dispatch so the agent stops guessing.

    Consumer-side counterpart to verify_output_artifact: outputs have been
    existence-checked at completion for a while; inputs never were."""

    _PRODUCERS = {
        "architecture.md": ["architecture_design"],
        "requirements.md": ["product_requirements"],
        "adversarial.md": ["adversarial_review"],
        "security.md": ["security_review"],
        "challenge.md": ["design_review"],
        "review.md": ["architectural_review"],
    }

    def _manifest(self, phase, wd, declared):
        from unittest.mock import patch

        import src.autopilot.spec as sp

        with patch.object(sp, "load_phase_inputs", return_value=declared), patch.object(
            sp, "input_producer_phases", side_effect=lambda w, f: self._PRODUCERS.get(f, [])
        ):
            return sp.build_input_manifest("wf-1", phase, str(wd))

    @staticmethod
    def _seed(tmp_path, rel_paths):
        for rel in rel_paths:
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")

    def test_resolves_a_producer_subdirectory_not_just_the_flat_location(self, tmp_path):
        self._seed(tmp_path, [".hephaestus/architecture_design/architecture.md"])
        out = self._manifest(
            "development", tmp_path, {"development": {"required": ["architecture.md"]}}
        )
        assert "./.hephaestus/architecture_design/architecture.md" in out

    def test_manifest_mandates_reading_and_resolving_not_just_citing(self, tmp_path):
        """Regression: a phase claimed a document's content was already
        satisfied while only citing it in passing prose, without actually
        resolving every item in it into its own output. The manifest's own
        header must say so explicitly, not just tell the agent where files
        are."""
        self._seed(tmp_path, [".hephaestus/architecture_design/architecture.md"])
        out = self._manifest(
            "development", tmp_path, {"development": {"required": ["architecture.md"]}}
        )
        assert "MUST actually read AND resolve" in out
        assert "not just cite" in out

    def test_present_line_omits_the_bare_declared_name(self, tmp_path):
        """The bare declared name (e.g. "challenge.md") never exists on
        disk under that name -- every producing phase writes its report
        under a task-id-suffixed filename -- so pairing "[present]
        challenge.md -> .../challenge-de343171.md" read as pointing at an
        old/different file, not this codebase's normal per-task naming.
        Only the resolved path is shown now."""
        self._seed(tmp_path, [".hephaestus/architecture_design/architecture.md"])
        out = self._manifest(
            "development", tmp_path, {"development": {"required": ["architecture.md"]}}
        )
        assert "architecture.md  ->" not in out
        assert "[present]" not in out

    def test_marks_a_missing_required_input_and_explains_why(self, tmp_path):
        self._seed(tmp_path, [".hephaestus/requirements.md"])
        out = self._manifest(
            "development",
            tmp_path,
            {"development": {"required": ["architecture.md", "requirements.md"]}},
        )
        assert "[MISSING]  architecture.md  (required)" in out
        assert "./.hephaestus/requirements.md" in out
        # The agent is told what to do about it rather than left to invent.
        assert "rewound by a goto" in out
        assert "invent their contents" in out

    def test_missing_optional_input_does_not_raise_the_note(self, tmp_path):
        self._seed(tmp_path, [".hephaestus/requirements.md"])
        out = self._manifest(
            "qa_validation",
            tmp_path,
            {"qa_validation": {"required": ["requirements.md"], "optional": ["security.md"]}},
        )
        assert "[MISSING]  security.md  (optional)" in out
        assert "normally available to this phase" not in out

    def test_empty_string_when_the_phase_declares_no_inputs(self, tmp_path):
        assert self._manifest("git_expert", tmp_path, {}) == ""

    def test_empty_string_without_a_working_directory(self):
        from unittest.mock import patch

        import src.autopilot.spec as sp

        with patch.object(
            sp, "load_phase_inputs", return_value={"development": {"required": ["x.md"]}}
        ):
            assert sp.build_input_manifest("wf-1", "development", None) == ""

    def test_every_declared_input_has_a_real_producer_in_this_workflow(self):
        """A declared input nothing produces is a typo that would show up as a
        permanently MISSING line in every run's manifest."""
        import yaml

        from src.autopilot.spec import _extract_declared_files
        from src.workflow_registry import _WORKFLOWS_DIR

        wf_dir = _WORKFLOWS_DIR / "autopilot"
        cfg = yaml.safe_load((wf_dir / "workflow.yaml").read_text())
        produced = set()
        for entry in (cfg.get("required_output") or {}).values():
            for e in entry if isinstance(entry, list) else [entry]:
                produced.add(Path(e).name)
        for phase_file in wf_dir.glob("*.yaml"):
            if phase_file.name == "workflow.yaml":
                continue
            pc = yaml.safe_load(phase_file.read_text()) or {}
            for e in _extract_declared_files(pc.get("outputs")):
                produced.add(Path(e).name)
        # Seeded into the worktree by WorktreeManager's context_files rather
        # than produced by a phase.
        produced |= {"spec.md", "context.md", "qa_spec.json"}

        for phase, declared in (cfg.get("phase_inputs") or {}).items():
            for kind in ("required", "optional"):
                for filename in declared.get(kind) or []:
                    assert filename in produced, (
                        f"{phase} declares input {filename!r}, which no phase in "
                        f"this workflow produces and nothing seeds"
                    )

    def test_no_phase_declares_its_own_output_as_an_input(self):
        """A phase cannot consume what it has not produced yet, so such a
        declaration renders a spurious [MISSING] line on every run. The
        producer test above does not catch this: the file IS produced by
        something in the workflow -- that something is just this same phase."""
        import yaml

        from src.autopilot.spec import _extract_declared_files
        from src.workflow_registry import _WORKFLOWS_DIR

        wf_dir = _WORKFLOWS_DIR / "autopilot"
        cfg = yaml.safe_load((wf_dir / "workflow.yaml").read_text())
        own_outputs = {}
        for phase_file in wf_dir.glob("*.yaml"):
            if phase_file.name == "workflow.yaml":
                continue
            pc = yaml.safe_load(phase_file.read_text()) or {}
            if pc.get("name"):
                own_outputs[pc["name"]] = {
                    Path(e).name for e in _extract_declared_files(pc.get("outputs"))
                }
        for phase, declared in (cfg.get("phase_inputs") or {}).items():
            for kind in ("required", "optional"):
                for filename in declared.get(kind) or []:
                    assert filename not in own_outputs.get(phase, set()), (
                        f"{phase} declares its own output {filename!r} as an input"
                    )

    def test_declared_input_phases_all_exist(self):
        import yaml

        from src.workflow_registry import _WORKFLOWS_DIR

        wf_dir = _WORKFLOWS_DIR / "autopilot"
        cfg = yaml.safe_load((wf_dir / "workflow.yaml").read_text())
        real = {
            (yaml.safe_load(f.read_text()) or {}).get("name")
            for f in wf_dir.glob("*.yaml")
            if f.name != "workflow.yaml"
        }
        for phase in (cfg.get("phase_inputs") or {}):
            assert phase in real, f"phase_inputs names {phase!r}, which is not a phase"


class TestPipelineDocMatchesTheWorkflow:
    """docs/autopilot.md carries a phase-by-phase input/output reference table.
    It is the first place an operator looks when a phase is rejected for a
    missing output, so a table naming files that are not produced sends them
    hunting for something that does not exist.

    It had drifted badly before these tests existed: every filename in it was
    the pre-rename alias (requirements_analysis.md rather than requirements.md,
    qa_report.md rather than qa.md, and so on -- 33 references), and two rows
    omitted declared outputs entirely. Nothing noticed, because prose has no
    way to disagree with code out loud."""

    @staticmethod
    def _doc_rows():
        import re

        doc = Path(__file__).resolve().parents[1] / "docs" / "autopilot.md"
        text = doc.read_text()
        return {
            int(n): (inputs, outputs)
            for n, inputs, outputs in re.findall(
                r"^\| *(\d+) *\| Feature \| (.+?) \| (.+?) \|$", text, re.M
            )
        }, text

    @staticmethod
    def _declared_outputs():
        import yaml

        from src.autopilot.spec import _extract_declared_files
        from src.workflow_registry import _WORKFLOWS_DIR

        out = {}
        for f in sorted((_WORKFLOWS_DIR / "autopilot").glob("*.yaml")):
            if f.name == "workflow.yaml":
                continue
            cfg = yaml.safe_load(f.read_text()) or {}
            if cfg.get("id"):
                out[cfg["id"]] = (
                    cfg["name"],
                    {Path(e).name for e in _extract_declared_files(cfg.get("outputs"))},
                )
        return out

    def test_table_lists_every_declared_output(self):
        import re

        rows, _ = self._doc_rows()
        for phase_id, (name, declared) in self._declared_outputs().items():
            if phase_id not in rows:
                continue
            listed = set(re.findall(r"[\w.]+\.(?:md|html|json)", rows[phase_id][1]))
            missing = declared - listed
            assert not missing, (
                f"docs/autopilot.md's phase {phase_id} ({name}) row omits {sorted(missing)}, "
                "which verify_output_artifact will reject the phase for not producing"
            )

    def test_documented_feature_states_match_the_db_constraint(self):
        """docs/autopilot.md enumerates the Feature states an operator can see
        in the UI. Feature.status carries a CHECK constraint naming exactly
        which are writable, so the two can be compared -- and had diverged:
        `paused` is reachable (pausing a workflow cascades to its Feature, see
        engine_client's cascade_to_feature) but was undocumented, so anyone
        seeing a paused feature found nothing explaining it."""
        import re

        from src.core.database import Feature

        constraints = Feature.__table__.columns["status"].constraints
        allowed = set()
        for c in constraints:
            allowed |= set(re.findall(r"'(\w+)'", str(getattr(c, "sqltext", ""))))
        assert allowed, "Feature.status lost its CHECK constraint"

        _, text = self._doc_rows()
        block = text.split("**Feature states:**")[1].split("\n\n")[0]
        documented = set(re.findall(r"^- `(\w+)`:", block, re.M))

        assert allowed == documented, (
            f"Feature.status permits {sorted(allowed)} but docs/autopilot.md "
            f"documents {sorted(documented)}"
        )

    def test_doc_uses_current_filenames_not_pre_rename_aliases(self):
        """OUTPUT_NAME_ALIASES exists so a report written under an old name
        still resolves. It is a compatibility shim, not a name the docs should
        be teaching."""
        from src.autopilot.spec import OUTPUT_NAME_ALIASES

        _, text = self._doc_rows()
        stale = sorted(old for old in OUTPUT_NAME_ALIASES.values() if old in text)
        assert not stale, (
            f"docs/autopilot.md still names pre-rename files {stale}; "
            "an operator following it will look for files that are never written"
        )


class TestThresholdBandsAreCoherent:
    """workflow.yaml's continue thresholds are band separators, not quality
    bars -- see its own THRESHOLD RATIONALE comment. These tests pin the
    claims that comment makes, so it cannot quietly rot into a lie the way
    security_review's and doc_review's own conditions did.

    The whole lesson of that bug: a threshold nothing can ever cross reads
    exactly like an enforced one."""

    @staticmethod
    def _continue_bars():
        import yaml

        from src.workflow_registry import _WORKFLOWS_DIR

        cfg = yaml.safe_load((_WORKFLOWS_DIR / "autopilot" / "workflow.yaml").read_text())
        bars = {}
        for ep in cfg["orchestrator"]["evaluation_points"]:
            for cond in ep["conditions"]:
                if cond["action"] == "continue" and ">=" in cond["if"]:
                    bars[ep["after_phase"]] = float(cond["if"].split(">=")[1].strip().strip('"'))
        return bars

    def test_no_gated_phase_can_score_into_the_0_6_to_0_7_gap(self):
        """This is what makes the 0.6-vs-0.7 spread cosmetic. If a scorer
        ever starts emitting into this gap, the two values stop being
        interchangeable and the rationale comment needs rewriting."""
        from src.autopilot.spec import load_spec

        for phase, scores in _reachable_scores(load_spec()).items():
            in_gap = sorted(v for v, _band in scores if 0.6 <= v < 0.7)
            assert not in_gap, (
                f"{phase} can now score {in_gap}, inside the 0.6-0.7 gap that "
                "workflow.yaml's THRESHOLD RATIONALE says is empty"
            )

    def test_swapping_0_6_and_0_7_changes_no_outcome(self):
        """The direct statement of the claim: these two bars are
        interchangeable for every reachable score."""
        from src.autopilot.spec import load_spec

        reachable = _reachable_scores(load_spec())
        for phase, bar in self._continue_bars().items():
            if bar not in (0.6, 0.7):
                continue
            other = 0.7 if bar == 0.6 else 0.6
            for score, _band in reachable.get(phase, ()):
                assert (score >= bar) == (score >= other), (
                    f"{phase} score {score} is decided differently by "
                    f"{bar} vs {other} -- the bars are no longer interchangeable"
                )

    def test_no_failing_result_can_clear_its_own_gate(self):
        """The one property that actually matters, and the only one worth
        enforcing: for each gated phase, every score its scorer labels
        anything other than "pass" must fall BELOW that phase's continue bar.

        Stated per-phase against the scorer's own band label rather than
        against a shared constant, because the phases do not share anchors --
        scope_review emits 0.2/1.0 and never produces _DEV at all, so a
        blanket `bar > _DEV` assertion would flag its perfectly correct 0.5
        bar. Getting that wrong in the obvious direction is how a gate ends
        up enforcing nothing."""
        from src.autopilot.spec import load_spec

        reachable = _reachable_scores(load_spec())
        bars = self._continue_bars()
        for phase, scores in reachable.items():
            bar = bars[phase]
            for score, band in scores:
                if band == "pass":
                    assert score >= bar, (
                        f"{phase}: a clean result scores {score}, below its own "
                        f"continue bar {bar} -- the gate can never be passed"
                    )
                else:
                    assert score < bar, (
                        f"{phase}: a {band!r} result scores {score}, at or above "
                        f"its continue bar {bar} -- it would pass the gate"
                    )

    def test_architecture_band_reachability_is_as_documented(self):
        """Three phases can reach `score < 0.3`; the blocker-count scorers
        cannot, and workflow.yaml says so rather than pretending otherwise."""
        from src.autopilot.spec import load_spec

        reachable = _reachable_scores(load_spec())
        for phase in ("scope_review", "qa_validation", "product_validation"):
            assert any(v < 0.3 for v, _b in reachable[phase]), f"{phase} should reach the arch band"
        for phase in (
            "design_review", "adversarial_review", "architectural_review", "security_review",
        ):
            assert not any(v < 0.3 for v, _b in reachable[phase]), (
                f"{phase} now reaches the arch band -- workflow.yaml documents it as "
                "unreachable for the blocker-count scorers; update that comment"
            )


class TestResolveDeclaredOutputSubdirPrefixed:
    """Phase.outputs is snapshotted into the DB when a workflow is created
    and never re-read from YAML, but get_gated_phases() reads from YAML
    fresh (lru_cache'd per process). So the moment security_review became
    a gated phase, every workflow ALREADY IN FLIGHT kept its old
    "security_review/security.md"
    declaration -- and resolve_declared_output_path's flat-.hephaestus/
    exclusion (which applies to gated phases) rejected it, reporting a
    report sitting in exactly the right place as missing. Correcting the
    YAML fixes new workflows; only this fixes the ones already running.

    The exclusion is about the FLAT location, so it must test the path the
    candidate actually produces, not the phase's gated-ness alone."""

    def test_subdir_prefixed_name_resolves_for_a_gated_phase(self, tmp_path):
        from src.autopilot.spec import resolve_declared_output_path

        report = tmp_path / ".hephaestus" / "security_review" / "security.md"
        report.parent.mkdir(parents=True)
        report.write_text("---\ntype: security_review_report\n---\nbody\n")

        # Both the stale in-flight declaration and the corrected one must
        # land on the same file.
        assert (
            resolve_declared_output_path(
                str(tmp_path), "security_review", "security_review/security.md"
            )
            == report
        )
        assert (
            resolve_declared_output_path(str(tmp_path), "security_review", "security.md")
            == report
        )

    def test_genuinely_flat_report_is_still_rejected_for_a_gated_phase(self, tmp_path):
        """The bug the exclusion exists to prevent must stay prevented: a
        gated phase's report at flat .hephaestus/qa.md passes an existence
        check but then scores as "no report", since read_okf_report only
        looks in .hephaestus/<phase_name>/ and the worktree root."""
        from src.autopilot.spec import read_okf_report, resolve_declared_output_path

        flat = tmp_path / ".hephaestus" / "qa.md"
        flat.parent.mkdir(parents=True)
        flat.write_text("---\ntype: qa_validation\n---\nbody\n")

        assert resolve_declared_output_path(str(tmp_path), "qa_validation", "qa.md") is None
        # The two must agree -- that agreement is the whole point.
        assert read_okf_report(str(tmp_path), "qa.md", phase_name="qa_validation") == (
            None,
            None,
        )

    def test_flat_report_still_accepted_for_a_non_gated_phase(self, tmp_path):
        from src.autopilot.spec import resolve_declared_output_path

        flat = tmp_path / ".hephaestus" / "docs.md"
        flat.parent.mkdir(parents=True)
        flat.write_text("---\ntype: doc_review_report\n---\nbody\n")

        assert (
            resolve_declared_output_path(str(tmp_path), "doc_review", "docs.md") == flat
        )


class TestSecurityReviewClassificationSteps:
    """STEP 1 gates which later steps apply, by NUMBER. Those numbers drifted
    once already: the ash scan was inserted at position 2 and the skip lists
    were never renumbered, so STATELESS_LIBRARY was told to "SKIP Steps 2, 3,
    6" (2 = the mandatory ash scan, 3 = read security requirements) and to
    "Run Step 4 only if the library handles PII or writes files" (4 = auth;
    PII and file writes are 6). Harmless while nothing enforced the ash
    section -- a hard rejection once verify_output_artifact's check went
    live.

    These assertions pin the step NUMBERS the classification block names to
    the step TITLES it means, so inserting or reordering a step breaks here
    instead of silently mis-routing a security review."""

    @staticmethod
    def _notes_and_titles():
        import re

        import yaml

        from src.workflow_registry import _WORKFLOWS_DIR

        cfg = yaml.safe_load(
            (_WORKFLOWS_DIR / "autopilot" / "security_review.yaml").read_text()
        )
        notes = cfg["additional_notes"]
        titles = {
            int(n): title.strip()
            for n, title in re.findall(r"^\s*## STEP (\d+):\s*(.+)$", notes, re.M)
        }
        return notes, titles

    def test_the_skipped_step_is_the_auth_step(self):
        _notes, titles = self._notes_and_titles()
        assert "AUTHENTICATION" in titles[4].upper()

    def test_the_conditional_step_is_the_data_handling_step(self):
        """"Run Step 6 only if the library handles PII or writes files" only
        makes sense if 6 is DATA HANDLING."""
        _notes, titles = self._notes_and_titles()
        assert "DATA HANDLING" in titles[6].upper()

    def test_step_2_is_the_ash_scan_and_is_never_declared_skippable(self):
        notes, titles = self._notes_and_titles()
        assert "AUTOMATED SCAN" in titles[2].upper()

        classification = notes.split("## STEP 1:")[1].split("## STEP 2:")[0]
        # No classification may tell the agent to skip the ash step: the
        # report is hard-floor rejected without its section.
        assert "SKIP Step 2" not in classification
        assert "SKIP Steps 2" not in classification

    def test_every_step_number_the_classification_names_exists(self):
        import re

        notes, titles = self._notes_and_titles()
        classification = notes.split("## STEP 1:")[1].split("## STEP 2:")[0]
        referenced = {
            int(n)
            for chunk in re.findall(r"Steps? ([\d, and-]+)", classification)
            for n in re.findall(r"\d+", chunk)
        }
        assert referenced, "classification block names no steps at all"
        assert referenced <= set(titles), (
            f"classification references steps {sorted(referenced - set(titles))} "
            f"that do not exist (steps are {sorted(titles)})"
        )


class TestSecurityReviewGateWiring:
    """The scorer only matters if the phase is actually wired as a gate --
    that wiring is what was missing, not the scoring logic."""

    def test_declared_output_resolves_now_that_the_phase_is_gated(self, tmp_path):
        """Regression: security_review.yaml used to declare its output as
        "security_review/security.md" -- the only gated phase with a
        subdir-prefixed name. That form resolved ONLY via
        resolve_declared_output_path's flat-.hephaestus/ candidate, which is
        deliberately skipped for gated phases. Making this phase gated
        therefore broke verify_output_artifact for it: the report sat in the
        right place and was reported missing, rejecting every completion."""
        import yaml

        from src.autopilot.spec import (
            _extract_declared_files,
            resolve_declared_output_path,
        )
        from src.workflow_registry import _WORKFLOWS_DIR

        cfg = yaml.safe_load(
            (_WORKFLOWS_DIR / "autopilot" / "security_review.yaml").read_text()
        )
        declared = _extract_declared_files(cfg["outputs"])
        assert declared == ["security.md"]

        report = tmp_path / ".hephaestus" / "security_review" / "security.md"
        report.parent.mkdir(parents=True)
        report.write_text("---\ntype: security_review_report\n---\nbody\n")
        assert (
            resolve_declared_output_path(str(tmp_path), "security_review", declared[0])
            == report
        )

    def test_declared_output_matches_the_ash_scan_content_check(self):
        """verify_output_artifact gates the MANDATORY ash-scan section on
        `declared_output == "security.md"`. With the old subdir-prefixed
        declaration that comparison never matched, so the check silently
        never ran on any security review."""
        import yaml

        from src.autopilot.spec import _extract_declared_files
        from src.workflow_registry import _WORKFLOWS_DIR

        cfg = yaml.safe_load(
            (_WORKFLOWS_DIR / "autopilot" / "security_review.yaml").read_text()
        )
        assert "security.md" in _extract_declared_files(cfg["outputs"])

    def test_security_review_is_a_gated_phase(self):
        assert "security_review" in GATED_PHASES

    def test_gate_artifact_and_type_are_registered(self):
        from src.autopilot.spec import (
            GATE_RESULT_ARTIFACTS,
            expected_gate_result_type,
        )

        assert GATE_RESULT_ARTIFACTS["security_review"] == ("security.md",)
        # security_review.yaml documents `type: security_review_report`, not
        # the bare phase name the other gated phases use.
        assert expected_gate_result_type("security_review") == "security_review_report"

    def test_synthetic_clean_result_uses_this_scorers_schema(self):
        """_cap_out_review_phase's synthetic pass must be readable by THIS
        phase's scorer -- a blocker_count-only shape would score clean here
        by accident rather than by construction, the same bug that made
        qa_validation's cap-out read as a 0% pass rate."""
        from src.autopilot.spec import (
            synthetic_clean_result,
            validate_gate_result_schema,
        )

        result = synthetic_clean_result("security_review", 4)
        assert result["unresolved_count"] == 0
        assert validate_gate_result_schema("security_review", result) is None
        assert score_security_review(result)[0] >= 0.7

    def test_schema_validation_rejects_a_wrong_shaped_report(self):
        from src.autopilot.spec import validate_gate_result_schema

        problem = validate_gate_result_schema(
            "security_review",
            {"type": "security_review_report", "posture": "STRONG"},
        )
        assert problem is not None
        assert "unresolved_count" in problem


class TestScoreDesignReview:
    """design_review (the pre-development adversarial challenge of
    architecture.md) -- unlike score_adversarial_review/
    score_architectural_review, a WARNING-only report must ALSO route back
    to architecture_design, not pass: there's no development phase yet for
    a WARNING to be deferred to, and looping architecture_design again is
    far cheaper than finding the same gap after code exists."""

    def test_none_result(self):
        score, meta = score_design_review(None)
        assert score == 0.4
        assert meta["result_missing"] is True

    def test_none_result_with_report_text_still_quotes_report(self):
        report = "# Architecture Challenge Report\n\n### [BLOCKER] Race condition in cache write"
        score, meta = score_design_review(None, report_text=report)
        assert meta["result_missing"] is True
        assert report in meta["reason"]

    def test_blocker_routes_to_architecture_design(self):
        score, meta = score_design_review(
            {"blocker_count": 2, "warning_count": 1, "nit_count": 0}
        )
        assert score < 0.6
        assert meta["band"] == "architecture_design"
        assert meta["blocker_count"] == 2

    def test_warnings_only_also_routes_back(self):
        """The key difference from score_adversarial_review: a WARNING-only
        report does NOT pass here -- on its FIRST occurrence (no prior run
        to compare against)."""
        score, meta = score_design_review(
            {"blocker_count": 0, "warning_count": 2, "nit_count": 0}
        )
        assert score < 0.6
        assert meta["band"] == "architecture_design"

    def test_warnings_unchanged_from_prior_run_passes(self):
        """ticket-14029d38: architecture_design was re-dispatched 4x with an
        identical "2 WARNING/2 NIT" report even after the requested fixes
        were applied and verified -- design_review routed back on every
        single run because a WARNING-only report always did, regardless of
        whether it was the same already-acknowledged warning or a new one.
        Same fix and rationale as score_adversarial_review's
        prior_warning_count."""
        score, meta = score_design_review(
            {"blocker_count": 0, "warning_count": 2, "nit_count": 2},
            prior_warning_count=2,
        )
        assert score >= 0.6
        assert meta["band"] == "pass"
        assert meta["warning_count"] == 2

    def test_warnings_fewer_than_prior_run_passes(self):
        score, meta = score_design_review(
            {"blocker_count": 0, "warning_count": 1, "nit_count": 0},
            prior_warning_count=2,
        )
        assert score >= 0.6
        assert meta["band"] == "pass"

    def test_new_warning_beyond_prior_run_still_routes_back(self):
        """A HIGHER warning_count than last run is real signal something
        changed -- still worth another architecture_design pass, unlike the
        unchanged case above."""
        score, meta = score_design_review(
            {"blocker_count": 0, "warning_count": 3, "nit_count": 0},
            prior_warning_count=2,
        )
        assert score < 0.6
        assert meta["band"] == "architecture_design"

    def test_clean(self):
        score, meta = score_design_review(
            {"blocker_count": 0, "warning_count": 0, "nit_count": 0}
        )
        assert score >= 0.6
        assert meta["band"] == "pass"

    def test_blocker_with_report_text_quotes_full_report(self):
        report = "# Architecture Challenge Report\n\n### [BLOCKER] Unhandled REQ-04\n- Section: Components"
        score, meta = score_design_review(
            {"blocker_count": 1, "warning_count": 0, "nit_count": 0},
            report_text=report,
        )
        assert score < 0.6
        assert report in meta["reason"]

    def test_blocker_without_report_text_falls_back_to_count(self):
        score, meta = score_design_review(
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
        assert score < 0.6
        assert meta["band"] == "development"

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
