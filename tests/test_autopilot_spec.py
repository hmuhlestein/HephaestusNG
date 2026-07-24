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


def test_qa_failed_tests_reason_names_specific_tests(strict_spec):
    """Regression: the goto reason used to carry only a bare count
    ("failed_tests=1 > 0"), leaving development to guess which test and
    whether fixing it was even in scope. Observed live: a single
    pre-existing/stale test failure bounced the pipeline between
    qa_validation and development for 4+ cycles because development never
    knew which test to fix. failed_test_names (if populated) must reach
    the reason, plus explicit permission to fix pre-existing failures."""
    score, meta = S.score_qa(
        {
            "failed_tests": 1,
            "failed_test_names": ["test_anthropic_provider.py::test_stale_assertion"],
            "passed_tests": 999,
            "total_tests": 1000,
            "critical_issues": 0,
            "agent_score": 1.0,
        },
        strict_spec,
    )
    assert meta["band"] == "development"
    assert "test_anthropic_provider.py::test_stale_assertion" in meta["reason"]
    assert "pre-existing" in meta["reason"]

def test_qa_failed_tests_without_names_still_gets_permission_language(strict_spec):
    """failed_test_names is optional (agents may not populate it) -- the
    permission to fix pre-existing failures must still reach development
    even without specific names."""
    score, meta = S.score_qa(
        {
            "failed_tests": 1,
            "passed_tests": 9,
            "total_tests": 10,
            "critical_issues": 0,
            "agent_score": 1.0,
        },
        strict_spec,
    )
    assert meta["band"] == "development"
    assert "pre-existing" in meta["reason"]

def test_qa_pass_reason_has_no_permission_language(strict_spec):
    """A clean pass must not carry "you may fix failing tests" noise --
    there's nothing to fix."""
    score, meta = S.score_qa(
        {
            "failed_tests": 0,
            "passed_tests": 10,
            "total_tests": 10,
            "critical_issues": 0,
            "agent_score": 1.0,
        },
        strict_spec,
    )
    assert meta["band"] == "pass"
    assert "reason" not in meta


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


def test_product_missing_json_falls_back_to_report_text():
    # Same fail-safe pattern as score_adversarial_review/
    # score_architectural_review/score_feature_review: missing JSON is
    # always the failing band, report text is just attached as context --
    # never a route to a pass.
    score, meta = S.score_product_validation(
        None, S.DEFAULT_SPEC, report_text="## Verdict\nPASS, everything looks great"
    )
    assert 0.3 <= score < 0.7
    assert meta["result_missing"] is True
    assert "PASS, everything looks great" in meta["reason"]


def test_product_missing_json_no_report_text():
    score, meta = S.score_product_validation(None, S.DEFAULT_SPEC)
    assert 0.3 <= score < 0.7
    assert meta["reason"] == "no product_validation.json found"


def test_product_pass_with_minor_gaps_accepted_within_cap():
    score, meta = S.score_product_validation(
        {
            "verdict": "PASS_WITH_MINOR_GAPS",
            "unmet_requirements": ["cosmetic wording gap"],
            "agent_score": 0.9,
        },
        S.DEFAULT_SPEC,
    )
    assert score >= 0.7
    assert meta["band"] == "pass"


def test_product_pass_with_minor_gaps_exceeding_cap_is_overridden():
    # DEFAULT_SPEC's max_minor_unmet_requirements is 2 -- 3 unmet items
    # must fall through to the same hard floor as a plain PASS.
    score, meta = S.score_product_validation(
        {
            "verdict": "PASS_WITH_MINOR_GAPS",
            "unmet_requirements": ["gap 1", "gap 2", "gap 3"],
            "agent_score": 0.9,
        },
        S.DEFAULT_SPEC,
    )
    assert 0.3 <= score < 0.7
    assert "override" in meta["reason"]


def test_product_pass_with_minor_gaps_is_not_a_loose_substring_match():
    # Regression: the original implementation matched any verdict string
    # containing both "PASS" and "MINOR" as substrings, not this exact
    # value -- e.g. a verdict that happens to mention both words in an
    # unrelated sentence should NOT bypass the hard floor.
    score, meta = S.score_product_validation(
        {
            "verdict": "NEEDS_WORK - MINOR ISSUES SHOULD NOT PASS REVIEW",
            "unmet_requirements": ["FR-1 missing"],
            "agent_score": 0.5,
        },
        S.DEFAULT_SPEC,
    )
    assert 0.3 <= score < 0.7
    assert meta["band"] == "development"


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


class TestConsumeGateArtifacts:
    """Regression: after a gate's goto decision fired, the result files its
    score was computed from stayed on disk. The re-run agent's output floor
    only checks the report EXISTS (not that it's fresh), and the gate
    re-scores the same stale file -- observed live: development genuinely
    fixed all 4 BLOCKERs, but every adversarial_review re-run re-scored the
    pre-fix result.json (blocker_count=4) and looped the pipeline back to
    development, burning one goto per cycle."""

    def test_deletes_result_and_report_from_docs(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "adversarial_review_result.json").write_text("{}")
        (docs / "adversarial_review_report.md").write_text("# report")

        deleted = S.consume_gate_artifacts("adversarial_review", tmp_path)

        assert len(deleted) == 2
        assert not (docs / "adversarial_review_result.json").exists()
        assert not (docs / "adversarial_review_report.md").exists()

    def test_unknown_phase_is_a_noop(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "something.json").write_text("{}")

        assert S.consume_gate_artifacts("development", tmp_path) == []
        assert (docs / "something.json").exists()

    def test_missing_files_return_empty(self, tmp_path):
        assert S.consume_gate_artifacts("adversarial_review", tmp_path) == []

    def test_every_mapped_artifact_gets_consumed(self, tmp_path):
        """Guardrail: for each mapped phase, writing then consuming its
        artifacts must leave the gate scorer with nothing stale to read --
        including phases like feature_review that live under a
        GATE_RESULT_SUBDIR override instead of docs/."""
        docs = tmp_path / "docs"
        docs.mkdir()
        for phase_name, filenames in S.GATE_RESULT_ARTIFACTS.items():
            subdir = S.GATE_RESULT_SUBDIR.get(phase_name)
            target_dir = tmp_path / subdir if subdir else docs
            target_dir.mkdir(exist_ok=True)
            for filename in filenames:
                (target_dir / filename).write_text("{}")
            S.consume_gate_artifacts(phase_name, tmp_path)
            for filename in filenames:
                assert not (target_dir / filename).exists(), (phase_name, filename)

    def test_deletes_from_phase_scoped_subdirectory(self, tmp_path):
        """Agents are told to write to the one sanctioned docs/<phase_name>/
        subdirectory -- consume_gate_artifacts must find and delete stale
        results there too, not just flat docs/."""
        sub = tmp_path / "docs" / "adversarial_review"
        sub.mkdir(parents=True)
        (sub / "adversarial_review_result.json").write_text("{}")
        (sub / "adversarial_review_report.md").write_text("# report")

        deleted = S.consume_gate_artifacts("adversarial_review", tmp_path)

        assert len(deleted) == 2
        assert not (sub / "adversarial_review_result.json").exists()
        assert not (sub / "adversarial_review_report.md").exists()

    def test_does_not_delete_a_different_phases_subdirectory_file(self, tmp_path):
        """Regression: the old fallback searched EVERY subdirectory of
        docs/ for a same-named file and deleted the first match -- risking
        deletion of a DIFFERENT feature's (or phase's) still-needed result
        file. Consuming adversarial_review's artifacts must never touch a
        file sitting under an unrelated subdirectory."""
        docs = tmp_path / "docs"
        other = docs / "some_other_feature"
        other.mkdir(parents=True)
        (other / "adversarial_review_result.json").write_text("{}")

        deleted = S.consume_gate_artifacts("adversarial_review", tmp_path)

        assert deleted == []
        assert (other / "adversarial_review_result.json").exists()


class TestValidateGateResultSchema:
    """Regression: a QA agent wrote its own nested JSON shape
    ({"overall_status": ..., "test_results": {"main_suite": {...}}})
    instead of the documented flat schema. score_qa's field reads all
    silently defaulted to "everything passed" -- including critical_issues
    and requirements_met, which nothing else independently re-verifies the
    way the pytest re-run catches a wrong pass/fail count."""

    def test_accepts_the_documented_qa_schema(self):
        result = {
            "failed_tests": 0,
            "passed_tests": 1410,
            "total_tests": 1410,
            "critical_issues": 0,
        }
        assert S.validate_gate_result_schema("qa_validation", result) is None

    def test_rejects_the_live_incompatible_qa_shape(self):
        result = {
            "overall_status": "PASS",
            "test_results": {"main_suite": {"total": 1410, "passed": 1410}},
            "requirements_compliance": {"FR-1": "PASS"},
        }
        error = S.validate_gate_result_schema("qa_validation", result)
        assert error is not None
        assert "qa_validation" in error

    def test_accepts_only_one_required_key_present(self):
        """Any one of the required keys is enough -- doesn't demand all of
        them, just evidence the agent used the right schema shape."""
        assert S.validate_gate_result_schema(
            "qa_validation", {"critical_issues": 2}
        ) is None

    def test_scope_review_accepts_documented_nested_variant(self):
        """score_scope_review itself normalizes {"scope_review": {...}} --
        the schema check must not reject a shape the scorer already
        tolerates."""
        assert S.validate_gate_result_schema(
            "scope_review", {"scope_review": {"verdict": "PASS"}}
        ) is None

    def test_architectural_review_rejects_missing_blocker_count(self):
        error = S.validate_gate_result_schema(
            "architectural_review", {"summary": "looks fine"}
        )
        assert error is not None

    def test_unmapped_phase_is_a_noop(self):
        assert S.validate_gate_result_schema("development", {"anything": True}) is None

    def test_none_result_is_a_noop(self):
        """A missing/unparseable file is verify_output_artifact's job --
        each score_* already has its own result_missing band for None."""
        assert S.validate_gate_result_schema("qa_validation", None) is None

    def test_every_gated_phase_has_a_required_keys_mapping(self):
        """Guardrail: every phase in GATED_PHASES should have a schema
        check, or a newly-added gated phase silently skips this floor."""
        for phase_name in S.GATED_PHASES:
            assert phase_name in S.GATE_RESULT_REQUIRED_KEYS, phase_name


# ── max_review_runs + review-findings history ──────────────────────────
# Closes the review-fix-review loop a forensics_analysis report found
# (architectural_review ran 19 times, adversarial_review 14 times on one
# feature): opt-in per-phase cap + a persisted findings history so a
# re-run's fresh agent session can verify prior findings instead of
# re-reviewing from scratch. See _create_phase_task's cap/injection block
# and _cap_out_review_phase.


class TestGetMaxReviewRuns:
    def test_returns_none_without_workflow_id(self):
        assert S.get_max_review_runs(None, "architectural_review") is None

    def test_reads_configured_value_from_the_real_autopilot_workflow(self, db_manager):
        """Integration-flavored on purpose: reads config/workflows/autopilot/
        workflow.yaml for real, proving the eval_point's max_review_runs
        key actually round-trips through this lookup, not just a mock."""
        from src.core.database import Workflow

        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-mrr-1",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    definition_id="autopilot",
                )
            )

        assert S.get_max_review_runs("wf-mrr-1", "architectural_review") == 4
        assert S.get_max_review_runs("wf-mrr-1", "adversarial_review") == 4

    def test_returns_none_for_a_phase_that_did_not_opt_in(self, db_manager):
        from src.core.database import Workflow

        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-mrr-2",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    definition_id="autopilot",
                )
            )

        assert S.get_max_review_runs("wf-mrr-2", "development") is None

    def test_returns_none_for_unknown_definition_id(self, db_manager):
        from src.core.database import Workflow

        with db_manager.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-mrr-3",
                    name="t",
                    phases_folder_path="/tmp",
                    status="active",
                    definition_id="does-not-exist",
                )
            )

        assert S.get_max_review_runs("wf-mrr-3", "architectural_review") is None


class TestReviewFindingsHistory:
    def test_empty_history_by_default(self, db_manager):
        assert S.get_review_findings_history("wf-no-history", "architectural_review") == []

    def test_record_then_read_back(self, db_manager):
        S.record_review_finding(
            "wf-history-1", "architectural_review", blocker_count=2, summary="B-1, B-2"
        )
        history = S.get_review_findings_history("wf-history-1", "architectural_review")
        assert len(history) == 1
        assert history[0]["run_number"] == 1
        assert history[0]["blocker_count"] == 2
        assert history[0]["summary"] == "B-1, B-2"

    def test_appends_across_multiple_runs(self, db_manager):
        S.record_review_finding("wf-history-2", "adversarial_review", 3, "B-1, B-2, B-3")
        S.record_review_finding("wf-history-2", "adversarial_review", 1, "B-2 still open")
        history = S.get_review_findings_history("wf-history-2", "adversarial_review")
        assert [h["run_number"] for h in history] == [1, 2]
        assert history[1]["blocker_count"] == 1

    def test_history_is_isolated_per_phase(self, db_manager):
        S.record_review_finding("wf-history-3", "architectural_review", 1, "arch finding")
        S.record_review_finding("wf-history-3", "adversarial_review", 1, "adv finding")
        assert len(S.get_review_findings_history("wf-history-3", "architectural_review")) == 1
        assert len(S.get_review_findings_history("wf-history-3", "adversarial_review")) == 1

    def test_summary_is_truncated(self, db_manager):
        S.record_review_finding(
            "wf-history-4", "architectural_review", 1, "x" * 1000
        )
        history = S.get_review_findings_history("wf-history-4", "architectural_review")
        assert len(history[0]["summary"]) == 500


class TestSyntheticCleanResult:
    """Regression: _cap_out_review_phase writes this result for a phase
    that hit its max_review_runs cap, so the gate's own scorer lets the
    pipeline continue instead of looping forever. Each gated phase's
    scorer reads a DIFFERENT schema -- a single blocker_count-only shape
    written for every phase is wrong for qa_validation/product_validation/
    scope_review, and reads as the WORST possible score there (e.g.
    score_qa sees total_tests=0 -> pass_rate=0%), which is worse than not
    capping at all. Observed live: qa_validation's cap-out wrote
    {"blocker_count": 0}, scored 0% pass rate, and immediately goto'd back
    to development -- burning through max_total_gotos faster than an
    uncapped loop would have. Each case below asserts the synthetic result
    actually clears the REAL scorer, not just that it has the right keys.
    """

    def test_qa_validation_result_scores_a_clean_pass(self):
        result = S.synthetic_clean_result("qa_validation", run_count=5)
        score, meta = S.score_qa(result, S.DEFAULT_SPEC)
        assert score >= S._PASS_FLOOR
        assert meta["violations"] == []

    def test_product_validation_result_scores_a_clean_pass(self):
        result = S.synthetic_clean_result("product_validation", run_count=5)
        score, meta = S.score_product_validation(result, S.DEFAULT_SPEC)
        assert score >= S._PASS_FLOOR
        assert meta["band"] == "pass"

    def test_scope_review_result_scores_a_clean_pass(self):
        result = S.synthetic_clean_result("scope_review", run_count=5)
        score, meta = S.score_scope_review(result)
        assert score >= S._PASS_FLOOR
        assert meta["band"] == "pass"

    def test_architectural_review_result_scores_a_clean_pass(self):
        result = S.synthetic_clean_result("architectural_review", run_count=5)
        score, meta = S.score_architectural_review(result)
        assert score >= S._PASS_FLOOR

    def test_adversarial_review_result_scores_a_clean_pass(self):
        result = S.synthetic_clean_result("adversarial_review", run_count=5)
        score, meta = S.score_adversarial_review(result)
        assert score >= S._PASS_FLOOR

    def test_every_result_records_capped_metadata(self):
        for phase_name in (
            "qa_validation", "product_validation", "scope_review", "architectural_review",
        ):
            result = S.synthetic_clean_result(phase_name, run_count=7)
            assert result["capped"] is True
            assert result["capped_after_runs"] == 7


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-p", "no:libtmux"])
