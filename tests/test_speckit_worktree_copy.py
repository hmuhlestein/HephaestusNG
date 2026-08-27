"""Tests for copy_speckit_feature (REQ-03/FR-002a) and regression for
copy_design_document (Gotcha #3 in the architecture doc)."""

from pathlib import Path

import pytest

from src.autopilot.orchestrator.speckit import SpecKitFeature
from src.autopilot.orchestrator.state import DesignEntry
from src.autopilot.orchestrator.worktree_integration import (
    copy_design_document,
    copy_speckit_feature,
)
from src.core.constants import CONTEXT_DIR_NAME


def _make_feature(tmp_path: Path) -> SpecKitFeature:
    d = tmp_path / "specs" / "001-x"
    d.mkdir(parents=True)
    (d / "spec.md").write_text("# spec")
    (d / "plan.md").write_text("# plan")
    (d / "data-model.md").write_text("# model")
    contracts = d / "contracts"
    contracts.mkdir()
    (contracts / "api.yaml").write_text("openapi: 3.0.0")
    return SpecKitFeature(
        dir_path=d, number="001", slug="x", repo_id=None, repo_label=None,
        spec_path=d / "spec.md", plan_path=d / "plan.md",
        extra_files=[d / "data-model.md", contracts],
    )


def test_copies_all_files_regardless_of_which_exist(tmp_path):
    feature = _make_feature(tmp_path)
    feature_folder = tmp_path / "feature_folder"

    dest = copy_speckit_feature(feature.dir_path, feature_folder)

    assert dest == feature_folder / CONTEXT_DIR_NAME / "specs" / "001-x"
    assert (dest / "spec.md").read_text() == "# spec"
    assert (dest / "plan.md").read_text() == "# plan"
    assert (dest / "data-model.md").read_text() == "# model"
    assert (dest / "contracts" / "api.yaml").is_file()


def test_raises_file_not_found_if_source_vanished(tmp_path):
    feature = _make_feature(tmp_path)
    import shutil as _shutil

    _shutil.rmtree(feature.dir_path)

    with pytest.raises(FileNotFoundError):
        copy_speckit_feature(feature.dir_path, tmp_path / "feature_folder")


def test_does_not_break_existing_copy_design_document(tmp_path):
    design_file = tmp_path / "design.md"
    design_file.write_text("# design")
    entry = DesignEntry(path=design_file, name="design", content_hash="abc")
    feature_folder = tmp_path / "feature_folder"

    dest = copy_design_document(entry, feature_folder)

    assert dest == feature_folder / CONTEXT_DIR_NAME / "design.md"
    assert dest.read_text() == "# design"
