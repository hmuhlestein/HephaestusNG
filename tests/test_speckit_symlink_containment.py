"""Regression for ticket-84a86e68: _scan_one_repo (src/autopilot/orchestrator/
speckit.py) enumerated specs/ entries via entry.is_dir(), which follows
symlinks. A top-level symlink under specs/ (e.g. specs/999-x -> /etc or
-> ~/.ssh) was enumerated as a legitimate SpecKitFeature and later copied
wholesale into the agent's worktree by _copy_design_content's
shutil.copytree -- exposing out-of-tree filesystem content to phase
prompts. Requires filesystem write access to the project repo (same trust
boundary as writing arbitrary code Autopilot would build anyway); not
exploitable via --feature/--repo CLI arguments alone."""

import os

import pytest

from src.autopilot.orchestrator.speckit import _scan_one_repo


def _make_real_feature_dir(specs_root, name):
    d = specs_root / name
    d.mkdir(parents=True)
    (d / "spec.md").write_text("# spec")
    return d


def test_symlinked_top_level_entry_is_not_enumerated(tmp_path):
    specs_root = tmp_path / "project" / "specs"
    specs_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "spec.md").write_text("# not actually part of this project")

    try:
        os.symlink(outside, specs_root / "999-escape")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/filesystem")

    features = _scan_one_repo(specs_root, None, None)

    assert features == []


def test_symlinked_entry_alongside_real_feature_only_yields_the_real_one(tmp_path):
    specs_root = tmp_path / "project" / "specs"
    specs_root.mkdir(parents=True)
    _make_real_feature_dir(specs_root, "001-legit")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "spec.md").write_text("# leaked content")

    try:
        os.symlink(outside, specs_root / "999-escape")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform/filesystem")

    features = _scan_one_repo(specs_root, None, None)

    assert [f.number for f in features] == ["001"]
    assert all(f.slug != "escape" for f in features)
