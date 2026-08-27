"""Integration test for the BLOCKER fixed in architectural review: the
worktree-population call sites in pipeline.py (run_phase0 and the
per-feature path) previously always did a plain shutil.copy2(design.md)
regardless of DesignEntry.speckit_feature_dir, so a Spec Kit-selected run
never got plan.md/tasks.md/contracts/ into the worktree at all. This tests
the actual shared helper both call sites now use, not just
copy_speckit_feature in isolation (already covered by
test_speckit_worktree_copy.py)."""

from src.autopilot.orchestrator.pipeline import _copy_design_input_into_worktree
from src.autopilot.orchestrator.state import DesignEntry
from src.core.constants import CONTEXT_DIR_NAME


def test_speckit_entry_populates_whole_feature_dir_in_worktree(tmp_path):
    feature_dir = tmp_path / "specs" / "001-x"
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text("# spec")
    (feature_dir / "plan.md").write_text("# plan")
    (feature_dir / "tasks.md").write_text("# tasks")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    entry = DesignEntry(
        path=feature_dir / "spec.md",
        name="001-x",
        content_hash="abc",
        speckit_feature_dir=feature_dir,
    )

    _copy_design_input_into_worktree(entry, worktree)

    dest = worktree / CONTEXT_DIR_NAME / "specs" / "001-x"
    assert (dest / "spec.md").read_text() == "# spec"
    assert (dest / "plan.md").read_text() == "# plan"
    assert (dest / "tasks.md").read_text() == "# tasks"
    # No plain design.md written for a Spec Kit-selected entry.
    assert not (worktree / CONTEXT_DIR_NAME / "design.md").exists()


def test_design_md_entry_still_copies_plain_design_md(tmp_path):
    design_file = tmp_path / "design.md"
    design_file.write_text("# design")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    entry = DesignEntry(path=design_file, name="design", content_hash="abc")

    _copy_design_input_into_worktree(entry, worktree)

    assert (worktree / CONTEXT_DIR_NAME / "design.md").read_text() == "# design"
    assert not (worktree / CONTEXT_DIR_NAME / "specs").exists()
