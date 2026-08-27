"""Tests for src/autopilot/orchestrator/speckit.py (REQ-01..REQ-15, REQ-21..23)."""

from pathlib import Path

import pytest

from src.autopilot.orchestrator.speckit import (
    SpecKitFeature,
    SpecKitSelectionError,
    _scan_one_repo,
    check_feature_readiness,
    discover_speckit_features_unregistered,
    resolve_feature_selection,
)


def _make_feature_dir(tmp_path: Path, name: str, files: dict) -> Path:
    d = tmp_path / "specs" / name
    d.mkdir(parents=True)
    for fname, content in files.items():
        (d / fname).write_text(content)
    return d


def test_single_feature_no_design_md_resolves_without_error(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec", "plan.md": "# plan"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    assert len(features) == 1
    resolved = resolve_feature_selection(features, feature_arg=None, repo_arg=None, design_md_present=False)
    assert resolved.number == "001"


def test_single_feature_with_design_md_raises_both_inputs_present(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    with pytest.raises(SpecKitSelectionError) as exc:
        resolve_feature_selection(features, feature_arg=None, repo_arg=None, design_md_present=True)
    assert exc.value.code == "BOTH_INPUTS_PRESENT"
    labels = {c.label() for c in exc.value.candidates}
    assert labels == {"001-x", "design.md"}


def test_two_features_no_selector_raises_multiple_features(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(tmp_path, "002-y", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    with pytest.raises(SpecKitSelectionError) as exc:
        resolve_feature_selection(features, feature_arg=None, repo_arg=None, design_md_present=False)
    assert exc.value.code == "MULTIPLE_FEATURES"
    assert len(exc.value.candidates) == 2


def test_bare_numeric_feature_arg_matches_exact_prefix_only(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(tmp_path, "0012-y", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    resolved = resolve_feature_selection(features, feature_arg="001", repo_arg=None, design_md_present=False)
    assert resolved.slug == "x"


def test_feature_arg_matching_two_repos_without_repo_raises_ambiguous_repo():
    backend = SpecKitFeature(
        dir_path=Path("/repo-a/specs/001-x"), number="001", slug="x",
        repo_id="repo-a", repo_label="backend", spec_path=Path("/repo-a/specs/001-x/spec.md"),
    )
    frontend = SpecKitFeature(
        dir_path=Path("/repo-b/specs/001-x"), number="001", slug="x",
        repo_id="repo-b", repo_label="frontend", spec_path=Path("/repo-b/specs/001-x/spec.md"),
    )
    with pytest.raises(SpecKitSelectionError) as exc:
        resolve_feature_selection([backend, frontend], feature_arg="001-x", repo_arg=None, design_md_present=False)
    assert exc.value.code == "AMBIGUOUS_REPO"
    assert len(exc.value.candidates) == 2


def test_malformed_specs_dir_never_counted_and_never_raises(tmp_path):
    (tmp_path / "specs" / "not-numbered").mkdir(parents=True)
    (tmp_path / "specs" / "001-no-spec").mkdir(parents=True)
    features = _scan_one_repo(tmp_path / "specs", None, None)
    assert features == []


def test_check_feature_readiness_reports_missing_plan_and_markers(tmp_path):
    d = _make_feature_dir(
        tmp_path, "001-x",
        {"spec.md": "Some text [NEEDS CLARIFICATION: what auth method?] more text"},
    )
    feature = SpecKitFeature(
        dir_path=d, number="001", slug="x", repo_id=None, repo_label=None, spec_path=d / "spec.md",
    )
    report = check_feature_readiness(feature)
    assert report.missing_files == ["plan.md", "tasks.md"]
    assert report.needs_clarification == ["what auth method?"]
