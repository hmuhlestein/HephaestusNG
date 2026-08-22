"""Regression tests for task-id-suffixed gate artifacts.

Every gated phase's report now gets written under a filename suffixed
with the dispatching task's own first 8 hex chars (security.md ->
security-a1b2c3d4.md), not the bare declared name -- so a duplicate/
concurrent dispatch for what should be one job (a scheduling bug, not
hypothetical: the exact failure mode behind feature_review's session-
resume incident, where a second agent for "the same" review ran
alongside the first) writes to two DIFFERENT files instead of racing on
one shared path.

Design is additive: every existing bare-filename call/test keeps working
unchanged (checked first in resolve_declared_output_path, still checked
in read_okf_report after the suffixed name); the suffix scheme only
activates for callers that pass task_id, with a newest-suffixed-file
fallback for callers (build_phase_output's own two call sites) that
score a phase's CURRENT state without a specific task in mind.
"""

from pathlib import Path

import pytest

from src.autopilot.spec import (
    _newest_glob_match,
    _output_glob_pattern,
    consume_gate_artifacts,
    read_okf_report,
    resolve_declared_output_path,
    suffixed_output_name,
)


class TestSuffixHelpers:
    def test_suffixed_output_name(self):
        assert suffixed_output_name("security.md", "a1b2c3d4-...") == "security-a1b2c3d4.md"

    def test_suffixed_output_name_short_task_id(self):
        """A short task id (test fixtures, unlikely in production UUIDs)
        isn't truncated further than it already is."""
        assert suffixed_output_name("qa.md", "abc") == "qa-abc.md"

    def test_glob_pattern(self):
        assert _output_glob_pattern("security.md") == "security-*.md"

    def test_newest_glob_match_picks_most_recently_modified(self, tmp_path):
        older = tmp_path / "security-11111111.md"
        newer = tmp_path / "security-22222222.md"
        older.write_text("old")
        newer.write_text("new")
        import os
        import time

        older_time = time.time() - 100
        os.utime(older, (older_time, older_time))
        result = _newest_glob_match(tmp_path, "security.md")
        assert result == newer

    def test_newest_glob_match_none_when_nothing_matches(self, tmp_path):
        assert _newest_glob_match(tmp_path, "security.md") is None


class TestResolveDeclaredOutputPathSuffix:
    def test_finds_suffixed_file_when_task_id_given(self, tmp_path):
        d = tmp_path / ".hephaestus" / "security_review"
        d.mkdir(parents=True)
        (d / "security-a1b2c3d4.md").write_text("x")
        found = resolve_declared_output_path(
            str(tmp_path), "security_review", "security.md", task_id="a1b2c3d4-rest"
        )
        assert found == d / "security-a1b2c3d4.md"

    def test_bare_filename_still_found_without_task_id(self, tmp_path):
        """Backward compat: every existing caller/test that never passes
        task_id must keep finding a literally-named file."""
        d = tmp_path / ".hephaestus" / "qa_validation"
        d.mkdir(parents=True)
        (d / "qa.md").write_text("x")
        found = resolve_declared_output_path(str(tmp_path), "qa_validation", "qa.md")
        assert found == d / "qa.md"

    def test_wrong_tasks_suffix_does_not_match_a_different_task(self, tmp_path):
        """Pinning to a specific task_id must not fall through to some
        OTHER task's leftover suffixed file -- that would defeat the
        entire point (a duplicate dispatch's stray file satisfying a
        DIFFERENT task's own existence check)."""
        d = tmp_path / ".hephaestus" / "security_review"
        d.mkdir(parents=True)
        (d / "security-99999999.md").write_text("someone else's")
        found = resolve_declared_output_path(
            str(tmp_path), "security_review", "security.md", task_id="a1b2c3d4-rest"
        )
        assert found is None

    def test_falls_back_to_newest_suffixed_when_no_task_id(self, tmp_path):
        """A caller with no specific task in mind (none of
        resolve_declared_output_path's real callers currently lack one,
        but the fallback exists for robustness/future callers) still
        finds a suffixed file via the newest-match fallback."""
        d = tmp_path / ".hephaestus" / "security_review"
        d.mkdir(parents=True)
        (d / "security-a1b2c3d4.md").write_text("x")
        found = resolve_declared_output_path(str(tmp_path), "security_review", "security.md")
        assert found == d / "security-a1b2c3d4.md"


class TestReadOkfReportSuffix:
    def _write_okf(self, path: Path, blocker_count: int):
        path.write_text(
            f"---\ntype: security_review_report\ncritical_count: {blocker_count}\n---\n\nBody text.\n"
        )

    def test_exact_task_suffix_preferred_over_bare_name(self, tmp_path):
        d = tmp_path / ".hephaestus" / "security_review"
        d.mkdir(parents=True)
        self._write_okf(d / "security.md", 5)
        self._write_okf(d / "security-a1b2c3d4.md", 0)
        result, _ = read_okf_report(
            tmp_path, "security.md", phase_name="security_review", task_id="a1b2c3d4-rest"
        )
        assert result["critical_count"] == 0

    def test_bare_name_still_works_without_task_id(self, tmp_path):
        d = tmp_path / ".hephaestus" / "qa_validation"
        d.mkdir(parents=True)
        self._write_okf(d / "qa.md", 1)
        result, _ = read_okf_report(tmp_path, "qa.md", phase_name="qa_validation")
        assert result["critical_count"] == 1

    def test_duplicate_dispatch_two_suffixed_files_picks_newest(self, tmp_path):
        """The scenario this whole change exists for: two agents somehow
        dispatched for "the same" phase each write their own suffixed
        file. A caller with no task_id (scoring the phase's current
        state, not one task's output) must not crash or pick arbitrarily
        -- it takes the most recently written one."""
        import os
        import time

        d = tmp_path / ".hephaestus" / "security_review"
        d.mkdir(parents=True)
        self._write_okf(d / "security-11111111.md", 4)
        self._write_okf(d / "security-22222222.md", 0)
        older_time = time.time() - 100
        os.utime(d / "security-11111111.md", (older_time, older_time))
        result, _ = read_okf_report(tmp_path, "security.md", phase_name="security_review")
        assert result["critical_count"] == 0


class TestConsumeGateArtifactsSuffix:
    def test_deletes_all_suffixed_variants_not_just_one(self, tmp_path):
        """A leftover from an earlier duplicate/retry attempt must not
        survive consumption -- read_okf_report's own newest-match fallback
        would otherwise pick it right back up next run, resurrecting the
        exact stale-result goto loop this function exists to prevent."""
        d = tmp_path / ".hephaestus" / "security_review"
        d.mkdir(parents=True)
        (d / "security-11111111.md").write_text("x")
        (d / "security-22222222.md").write_text("y")
        (d / "security.md").write_text("z")

        deleted = consume_gate_artifacts("security_review", str(tmp_path))

        assert not (d / "security-11111111.md").exists()
        assert not (d / "security-22222222.md").exists()
        assert not (d / "security.md").exists()
        assert len(deleted) == 3

    def test_does_not_wildcard_delete_unrelated_files_at_worktree_root(self, tmp_path):
        """The worktree root is the actual project source tree, not an
        exclusively-Hephaestus location -- a real, unrelated committed
        project file (a genuine "qa-notes.md" with nothing to do with this
        gate) that happens to match the glob pattern must survive. Only
        .hephaestus/<phase_name>/ is swept by wildcard; the worktree root
        is only ever touched by the pre-existing EXACT bare-name check."""
        d = tmp_path / ".hephaestus" / "qa_validation"
        d.mkdir(parents=True)
        (d / "qa-a1b2c3d4.md").write_text("the real gate artifact")
        real_project_file = tmp_path / "qa-notes.md"
        real_project_file.write_text("a real project doc, unrelated to this gate")

        deleted = consume_gate_artifacts("qa_validation", str(tmp_path))

        assert not (d / "qa-a1b2c3d4.md").exists()
        assert real_project_file.exists()
        assert real_project_file.read_text() == "a real project doc, unrelated to this gate"
        assert str(real_project_file) not in deleted

    def test_read_okf_report_glob_fallback_ignores_worktree_root(self, tmp_path):
        """Same worktree-root exclusion on the read side -- a project file
        matching the pattern at the worktree root must not be mistaken for
        this phase's gate result."""
        real_project_file = tmp_path / "qa-notes.md"
        real_project_file.write_text("not a gate result, no frontmatter")

        result, _ = read_okf_report(tmp_path, "qa.md", phase_name="qa_validation")

        assert result is None
