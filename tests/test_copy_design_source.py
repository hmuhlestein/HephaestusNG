"""Tests for the consolidated worktree-copy helper (REQ-08)."""

import pytest

from src.autopilot.orchestrator.state import DesignEntry
from src.autopilot.orchestrator.worktree_integration import (
    copy_design_document,
    copy_design_source,
    copy_design_source_path,
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

    def test_stale_file_at_destination_path_is_replaced_not_a_crash(self, tmp_path):
        """Adversarial review WARNING: a plain file left at the exact
        destination path (partial prior run, manual touch, naming
        collision) used to make shutil.rmtree raise NotADirectoryError.
        """
        entry = _dir_entry(tmp_path / "src")
        heph_dir = tmp_path / "dest" / ".hephaestus"
        stale_path = heph_dir / "specs" / "003-checkout-flow"
        stale_path.parent.mkdir(parents=True)
        stale_path.write_text("not a directory")

        dest = copy_design_source(entry, heph_dir)

        assert dest == stale_path
        assert dest.is_dir()
        assert (dest / "spec.md").read_text() == "spec content"


class TestCopyDesignSourcePath:
    """The path-only public entry point run_single_workflow uses when it
    only has launch_params["design_document"], not a DesignEntry."""

    def test_file_case(self, tmp_path):
        (tmp_path / "src").mkdir()
        design_path = tmp_path / "src" / "design.md"
        design_path.write_text("hello")
        dest = copy_design_source_path(design_path, tmp_path / "dest", "design.md", is_directory=False)
        assert dest == tmp_path / "dest" / "design.md"
        assert dest.read_text() == "hello"

    def test_directory_case(self, tmp_path):
        feature_dir = tmp_path / "specs" / "004-x"
        feature_dir.mkdir(parents=True)
        (feature_dir / "spec.md").write_text("spec")
        dest = copy_design_source_path(feature_dir, tmp_path / "dest", "design.md", is_directory=True)
        assert dest == tmp_path / "dest" / "specs" / "004-x"
        assert (dest / "spec.md").read_text() == "spec"

    def test_directory_case_preserves_symlinks_does_not_follow_them(self, tmp_path):
        """Security-review finding: a Spec Kit feature directory is
        git-tracked and editable by anyone with repo write access. A
        symlink inside it pointing outside the repo (e.g. to a
        credentials file readable by the Hephaestus process user) must
        not have its target's actual content copied into the
        destination -- the symlink itself must be preserved instead.
        """
        outside_secret = tmp_path / "outside_secret.txt"
        outside_secret.write_text("super secret content")

        feature_dir = tmp_path / "specs" / "005-symlink"
        feature_dir.mkdir(parents=True)
        (feature_dir / "spec.md").write_text("spec")
        (feature_dir / "linked-secret.txt").symlink_to(outside_secret)

        dest = copy_design_source_path(feature_dir, tmp_path / "dest", "design.md", is_directory=True)

        copied_link = dest / "linked-secret.txt"
        assert copied_link.is_symlink()
        # The link target is preserved as a path, not dereferenced into a
        # plain file at copy time.
        assert copied_link.readlink() == outside_secret


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
