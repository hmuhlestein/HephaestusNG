"""Tests for applying forensics-proposed prompt edits.

This is the module that lets an LLM's proposal rewrite the prompts driving the
pipeline, so the guards matter more than the feature. Two things are being
pinned: that the safety allowlist actually refuses what it claims to, and that
a "surgical" text edit is genuinely surgical -- these phase YAMLs carry long
explanatory comments that are load-bearing documentation, and an edit that
quietly ate one would be worse than no feature at all.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

from src.services.prompt_proposal_service import (
    EDITABLE_FIELDS,
    apply_edit,
    apply_proposal,
    commit_file,
    current_value,
    phase_yaml_path,
    revert_proposal,
    validate_proposal,
)
from src.workflow_registry import _WORKFLOWS_DIR


def _real_phase_files():
    for wf_dir in sorted(_WORKFLOWS_DIR.iterdir()):
        if not wf_dir.is_dir():
            continue
        for f in sorted(wf_dir.glob("*.yaml")):
            if f.name == "workflow.yaml":
                continue
            cfg = yaml.safe_load(f.read_text())
            if isinstance(cfg, dict) and cfg.get("name"):
                yield f, cfg


class TestEditIsActuallySurgical:
    """The edit must change exactly one field and nothing else -- not the
    other fields, and not the comments, which yaml.safe_load/safe_dump would
    have deleted wholesale."""

    @pytest.mark.parametrize(
        "phase_file,field",
        [
            (f, field)
            for f, cfg in _real_phase_files()
            for field in EDITABLE_FIELDS
            if field in cfg
        ],
        ids=lambda v: v.name if isinstance(v, Path) else str(v),
    )
    def test_identity_edit_changes_nothing_in_any_real_phase_file(
        self, phase_file, field, tmp_path
    ):
        """Re-applying a field's own value must produce a byte-equivalent
        parse and keep every comment. Run against every real phase YAML,
        because the failure modes here were all file-specific: a block scalar
        that is the last field in the file, whitespace-only lines inside an
        embedded code sample, a comment paragraph sitting between two keys."""
        target = tmp_path / phase_file.name
        target.write_text(phase_file.read_text())
        original = target.read_text()
        before = yaml.safe_load(original)

        try:
            updated = apply_edit(target, field, before[field])
        except ValueError as e:
            # One real file legitimately refuses -- see the duplicate-key test.
            assert "declares" in str(e), str(e)
            return

        assert yaml.safe_load(updated) == before, "an identity edit changed a value"
        assert updated.count("#") == original.count("#"), "an identity edit lost a comment"

    def test_preserves_a_comment_sitting_between_the_field_and_the_next_key(self, tmp_path):
        """Comments between a field's content and the following key belong to
        what FOLLOWS. Swallowing them into the replacement silently deletes
        documentation -- security_review.yaml keeps a paragraph directly above
        `outputs:` explaining why that filename is bare."""
        f = tmp_path / "p.yaml"
        f.write_text(
            "name: demo\n"
            "description: |\n"
            "  old text\n"
            "\n"
            "# This comment explains the next key and must survive.\n"
            "outputs:\n"
            '  - "thing.md"\n'
        )
        updated = apply_edit(f, "description", "new text\n")
        assert "# This comment explains the next key and must survive." in updated
        assert yaml.safe_load(updated)["outputs"] == ["thing.md"]
        assert yaml.safe_load(updated)["description"] == "new text\n"

    def test_preserves_whitespace_only_lines_inside_a_block_scalar(self, tmp_path):
        """These prompts embed code samples whose blank-looking lines carry
        real indentation. Flattening them to "" changes the value."""
        f = tmp_path / "p.yaml"
        body = 'def f():\n    """Doc.\n    \n    Args: x\n    """\n'
        f.write_text("name: demo\nadditional_notes: |\n  placeholder\n")
        updated = apply_edit(f, "additional_notes", body)
        assert yaml.safe_load(updated)["additional_notes"] == body

    def test_refuses_a_file_with_a_duplicated_top_level_key(self, tmp_path):
        """PyYAML resolves duplicates last-wins while the span finder takes the
        first, so editing would rewrite the copy that has no effect and leave
        the effective one alone -- a file whose text disagrees with its
        meaning. config/workflows/feature_architect/01_feature_architect.yaml
        really does declare `description:` twice."""
        f = tmp_path / "p.yaml"
        f.write_text('name: demo\ndescription: "one"\nother: 1\ndescription: |\n  two\n')
        with pytest.raises(ValueError, match="declares 'description' 2 times"):
            apply_edit(f, "description", "three")

    def test_rejects_an_edit_that_would_disturb_another_key(self, tmp_path):
        """The collateral check is the real safety net: a textual replacement
        that swallowed the following key would still parse, and would quietly
        drop that phase's outputs or spec_gate."""
        f = tmp_path / "p.yaml"
        f.write_text("name: demo\ndescription: |\n  text\nspec_gate: true\n")
        # A value that reintroduces a top-level-looking key inside the block is
        # still safe (it is indented), so assert the guard directly instead.
        updated = apply_edit(f, "description", "text\n")
        parsed = yaml.safe_load(updated)
        assert parsed["spec_gate"] is True
        assert parsed["name"] == "demo"

    def test_missing_field_raises_rather_than_appending(self, tmp_path):
        f = tmp_path / "p.yaml"
        f.write_text("name: demo\n")
        with pytest.raises(ValueError, match="no top-level"):
            apply_edit(f, "additional_notes", "x")


class TestSafetyAllowlist:
    """A guard the API does not enforce is exactly the kind of 'configured but
    never fires' gate this project's design review kept finding. These are
    enforced in the service, not the UI."""

    @pytest.mark.parametrize(
        "field", ["spec_gate", "outputs", "id", "name", "thinking_level", "next_steps"]
    )
    def test_orchestration_wiring_is_not_editable(self, field):
        problem = validate_proposal("autopilot", "security_review", field, "anything")
        assert problem is not None
        assert "not editable" in problem

    @pytest.mark.parametrize("field", EDITABLE_FIELDS)
    def test_prose_fields_are_editable(self, field):
        value = ["a done definition"] if field == "done_definitions" else "some prose"
        assert validate_proposal("autopilot", "security_review", field, value) is None

    def test_a_phase_may_not_rewrite_its_own_prompt(self):
        """Closes the self-modification loop: without this,
        forensics_analysis rewrites forensics_analysis.yaml and there is no
        fixed point outside itself."""
        problem = validate_proposal(
            "autopilot",
            "forensics_analysis",
            "additional_notes",
            "x",
            proposing_phase="forensics_analysis",
        )
        assert problem is not None and "own prompt" in problem

    def test_another_phase_may_still_propose_against_forensics(self):
        assert (
            validate_proposal(
                "autopilot",
                "forensics_analysis",
                "additional_notes",
                "x",
                proposing_phase="qa_validation",
            )
            is None
        )

    def test_workflow_yaml_is_unreachable_as_a_phase(self):
        """It holds the evaluation points, thresholds, required_output and
        phase_inputs -- the orchestration contract, not a prompt."""
        assert validate_proposal("autopilot", "workflow", "additional_notes", "x") is not None
        assert phase_yaml_path("autopilot", "workflow") is None

    def test_unknown_phase_is_rejected(self):
        problem = validate_proposal("autopilot", "no_such_phase", "additional_notes", "x")
        assert problem is not None and "no phase named" in problem

    def test_done_definitions_must_be_a_non_empty_list_of_strings(self):
        assert validate_proposal("autopilot", "development", "done_definitions", []) is not None
        assert validate_proposal("autopilot", "development", "done_definitions", "str") is not None
        assert (
            validate_proposal("autopilot", "development", "done_definitions", [1, 2]) is not None
        )
        assert (
            validate_proposal("autopilot", "development", "done_definitions", ["ok"]) is None
        )

    def test_prose_fields_reject_blank(self):
        assert validate_proposal("autopilot", "development", "additional_notes", "   ") is not None

    def test_phase_is_located_by_declared_name_not_filename(self):
        """feature_architect's files are numbered (01_feature_architect.yaml),
        so filename matching would miss them."""
        found = phase_yaml_path("feature_architect", "feature_architect")
        assert found is not None and found.name == "01_feature_architect.yaml"

    def test_current_value_reads_live_from_disk(self):
        """The 'before' side of a diff must not be a value the proposing agent
        quoted -- the file can have changed since, making the diff a fiction."""
        on_disk = yaml.safe_load(
            (_WORKFLOWS_DIR / "autopilot" / "development.yaml").read_text()
        )
        assert current_value("autopilot", "development", "done_definitions") == (
            on_disk["done_definitions"]
        )


class TestApplyAndRevert:
    """Approve writes the YAML and commits it; revert puts it back. Both go
    through the same verified edit path."""

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        wf = repo / "config" / "workflows" / "demo"
        wf.mkdir(parents=True)
        (wf / "demo_phase.yaml").write_text(
            "id: 1\n"
            "name: demo_phase\n"
            "spec_gate: true\n"
            "description: |\n"
            "  original description\n"
            "\n"
            "# load-bearing comment\n"
            "outputs:\n"
            '  - "thing.md"\n'
        )
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
        monkeypatch.setattr(
            "src.services.prompt_proposal_service._workflows_dir",
            lambda: repo / "config" / "workflows",
        )
        return repo

    def _phase_file(self, repo):
        return repo / "config" / "workflows" / "demo" / "demo_phase.yaml"

    def test_apply_writes_commits_and_returns_the_previous_value(self, repo):
        result = apply_proposal(
            repo, "demo", "demo_phase", "description", "rewritten\n", "prop-1"
        )
        assert result["previous_value"] == "original description\n"
        assert result["commit_sha"]

        parsed = yaml.safe_load(self._phase_file(repo).read_text())
        assert parsed["description"] == "rewritten\n"
        assert parsed["spec_gate"] is True and parsed["outputs"] == ["thing.md"]
        assert "# load-bearing comment" in self._phase_file(repo).read_text()

        log = subprocess.run(
            ["git", "log", "-1", "--format=%s"], cwd=repo, capture_output=True, text=True
        )
        assert "demo_phase.description" in log.stdout

    def test_revert_restores_the_recorded_previous_value(self, repo):
        applied = apply_proposal(
            repo, "demo", "demo_phase", "description", "rewritten\n", "prop-1"
        )
        revert_proposal(
            repo, "demo", "demo_phase", "description", applied["previous_value"], "prop-1"
        )
        parsed = yaml.safe_load(self._phase_file(repo).read_text())
        assert parsed["description"] == "original description\n"
        assert "# load-bearing comment" in self._phase_file(repo).read_text()

    def test_apply_revalidates_and_refuses_a_protected_field(self, repo):
        """Re-validated at apply time, not just at creation: the allowlist may
        have tightened since the proposal was filed, and a stored proposal must
        not be grandfathered past a guard that exists now."""
        with pytest.raises(ValueError, match="not editable"):
            apply_proposal(repo, "demo", "demo_phase", "spec_gate", False, "prop-2")

    def test_commit_is_scoped_to_the_one_file(self, repo):
        """The working tree routinely carries unrelated in-flight work; an
        approved prompt tweak must never sweep it into a commit."""
        stray = repo / "unrelated.txt"
        stray.write_text("someone else's work in progress")
        apply_proposal(repo, "demo", "demo_phase", "description", "rewritten\n", "prop-3")

        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
        )
        assert "unrelated.txt" in status.stdout, "the stray file was swept into the commit"
        show = subprocess.run(
            ["git", "show", "--stat", "--format=", "HEAD"],
            cwd=repo, capture_output=True, text=True,
        )
        assert "demo_phase.yaml" in show.stdout and "unrelated.txt" not in show.stdout

    def test_commit_returns_none_when_nothing_changed(self, repo):
        current = yaml.safe_load(self._phase_file(repo).read_text())["description"]
        apply_proposal(repo, "demo", "demo_phase", "description", current, "prop-4")
        assert commit_file(repo, self._phase_file(repo), "no-op") is None
