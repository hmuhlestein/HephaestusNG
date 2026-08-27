"""Tests for src.autopilot.orchestrator.engine_client's pure hashing helpers."""

import pytest

from src.autopilot.orchestrator.engine_client import directory_content_hash


def _make_feature_dir(tmp_path, spec_text="spec content", plan_text=None):
    feature_dir = tmp_path / "003-checkout-flow"
    feature_dir.mkdir()
    (feature_dir / "spec.md").write_text(spec_text)
    if plan_text is not None:
        (feature_dir / "plan.md").write_text(plan_text)
    return feature_dir


class TestDirectoryContentHash:
    def test_spec_only_hash_is_stable(self, tmp_path):
        feature_dir = _make_feature_dir(tmp_path)
        first = directory_content_hash(feature_dir)
        second = directory_content_hash(feature_dir)
        assert first == second

    def test_adding_plan_changes_hash(self, tmp_path):
        feature_dir = _make_feature_dir(tmp_path)
        before = directory_content_hash(feature_dir)
        (feature_dir / "plan.md").write_text("plan content")
        after = directory_content_hash(feature_dir)
        assert before != after

    def test_editing_plan_changes_hash(self, tmp_path):
        feature_dir = _make_feature_dir(tmp_path, plan_text="v1")
        before = directory_content_hash(feature_dir)
        (feature_dir / "plan.md").write_text("v2")
        after = directory_content_hash(feature_dir)
        assert before != after

    def test_missing_spec_raises_file_not_found(self, tmp_path):
        feature_dir = tmp_path / "003-checkout-flow"
        feature_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            directory_content_hash(feature_dir)

    def test_return_format_matches_file_hash_convention(self, tmp_path):
        feature_dir = _make_feature_dir(tmp_path)
        result = directory_content_hash(feature_dir)
        assert isinstance(result, str)
        assert len(result) == 16
        int(result, 16)  # hex
