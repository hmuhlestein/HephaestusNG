"""Tests for the consolidated worktree-copy helper (REQ-08)."""

from pathlib import Path

import pytest

from src.autopilot.orchestrator.state import DesignEntry
from src.autopilot.orchestrator.worktree_integration import (
    copy_design_document,
    copy_design_source,
)


def _file_entry(tmp_path) -> DesignEntry:
    tmp_path.mkdir(parents=True, exist_ok=True)
    design_path = tmp_path / "design.md"
    design_path.write_text("design content")
    return DesignEntry(path=design_path, name="design", content_hash="abc")


def _dir_entry(tmp_path) -> DesignEntry:
    feature_dir = tmp_path / "specs" / "003-checkout-flow"
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text("spec content")
    (feature_dir / "plan.md").write_text("plan content")
    (feature_dir / "contracts").mkdir()
    (feature_dir / "contracts" / "api.yaml").write_text("openapi: 3.0.0")
    return DesignEntry(path=feature_dir, name="003-checkout-flow", content_hash="def", source_dir=feature_dir)


class TestCopyDesignSourceFileSourced:
    def test_copies_file_to_default_filename(self, tmp_path):
        entry = _file_entry(tmp_path / "src")
        heph_dir = tmp_path / "dest" / ".hephaestus"
        dest = copy_design_source(entry, heph_dir)
        assert dest == heph_dir / "design.md"
        assert dest.read_text() == "design content"

    def test_missing_file_raises(self, tmp_path):
        entry = DesignEntry(path=tmp_path / "missing.md", name="x", content_hash="x")
        with pytest.raises(FileNotFoundError):
            copy_design_source(entry, tmp_path / "dest")


class TestCopyDesignSourceDirectorySourced:
    def test_copies_entire_tree(self, tmp_path):
        entry = _dir_entry(tmp_path / "src")
        heph_dir = tmp_path / "dest" / ".hephaestus"
        dest = copy_design_source(entry, heph_dir)
        assert dest == heph_dir / "specs" / "003-checkout-flow"
        assert (dest / "spec.md").read_text() == "spec content"
        assert (dest / "plan.md").read_text() == "plan content"
        assert (dest / "contracts" / "api.yaml").read_text() == "openapi: 3.0.0"

    def test_rerun_replaces_stale_files(self, tmp_path):
        entry = _dir_entry(tmp_path / "src")
        heph_dir = tmp_path / "dest" / ".hephaestus"
        dest = copy_design_source(entry, heph_dir)
        stale = dest / "stale.md"
        stale.write_text("should be removed")

        copy_design_source(entry, heph_dir)
        assert not stale.exists()
        assert (dest / "spec.md").exists()

    def test_missing_source_dir_raises(self, tmp_path):
        missing = tmp_path / "nope"
        entry = DesignEntry(path=missing, name="x", content_hash="x", source_dir=missing)
        with pytest.raises(FileNotFoundError):
            copy_design_source(entry, tmp_path / "dest")


class TestCopyDesignDocumentWrapper:
    def test_file_sourced_preserved_exactly(self, tmp_path):
        entry = _file_entry(tmp_path / "src")
        feature_folder = tmp_path / "feature"
        dest = copy_design_document(entry, feature_folder)
        assert dest == feature_folder / ".hephaestus" / "design.md"
        assert dest.read_text() == "design content"

    def test_directory_sourced_copies_full_tree(self, tmp_path):
        entry = _dir_entry(tmp_path / "src")
        feature_folder = tmp_path / "feature"
        dest = copy_design_document(entry, feature_folder)
        assert dest == feature_folder / ".hephaestus" / "specs" / "003-checkout-flow"
        assert (dest / "spec.md").exists()
        assert (dest / "contracts" / "api.yaml").exists()
