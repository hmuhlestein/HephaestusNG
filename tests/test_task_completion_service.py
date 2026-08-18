"""Unit tests for TaskCompletionService."""

from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.services.task_completion_service import TaskCompletionService


class TestParseForensicsRecommendations:
    """Tests for _parse_forensics_recommendations (pure function)."""

    def test_parse_standard_format(self):
        report = """# Forensics Report

## Recommendations for Future Pipeline Runs

### High Priority
1. **Add retry logic for API calls** - External APIs fail intermittently
2. **Improve error messages** - Users need clearer error context

### Medium Priority
3. **Add monitoring dashboards** - Better visibility

### Low Priority
4. **Update documentation** - Some sections are outdated
"""
        result = TaskCompletionService._parse_forensics_recommendations(report)
        assert len(result) == 4
        assert result[0]["title"] == "Add retry logic for API calls"
        assert result[0]["priority"] == "high"
        assert result[1]["priority"] == "high"
        assert result[2]["priority"] == "medium"
        assert result[3]["priority"] == "low"

    def test_parse_no_recommendations_section(self):
        report = "# Forensics Report\n\n## Analysis\n\nNo issues found."
        result = TaskCompletionService._parse_forensics_recommendations(report)
        assert result == []

    def test_parse_empty_recommendations(self):
        report = "## Recommendations\n\n### High Priority\n\n"
        result = TaskCompletionService._parse_forensics_recommendations(report)
        assert result == []

    def test_parse_default_priority_when_no_heading(self):
        report = "## Recommendations\n\n1. **First item** - description\n2. **Second item** - description\n"
        result = TaskCompletionService._parse_forensics_recommendations(report)
        assert len(result) == 2
        assert result[0]["priority"] == "medium"

    def test_parse_items_without_bold_title(self):
        report = "## Recommendations\n\n1. Fix the database connection pool\n"
        result = TaskCompletionService._parse_forensics_recommendations(report)
        assert len(result) == 0

    def test_parse_real_world_report(self):
        report = """# Forensics Analysis Report

## Recommendations for Future Pipeline Runs

### High Priority
1. **Fix database connection leak** - Connections not closed properly
2. **Add rate limiting** - API calls not rate-limited

### Medium Priority
3. **Implement structured logging** - Better debugging

### Low Priority
4. **Update README** - Outdated instructions
"""
        result = TaskCompletionService._parse_forensics_recommendations(report)
        assert len(result) == 4
        assert result[0]["priority"] == "high"
        assert result[2]["priority"] == "medium"
        assert result[3]["priority"] == "low"


class TestVerifyOutputArtifact:
    """Tests for verify_output_artifact method."""

    def test_returns_none_when_no_phase(self):
        task = Mock(phase_id=None)
        result = TaskCompletionService.verify_output_artifact(
            session=Mock(), task=task, phase=None
        )
        assert result is None

    def test_returns_none_when_no_required_files(self):
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        phase.name = "development"
        phase.name = "development"
        phase.name = "development"
        phase.name = "development"
        phase.name = "development"
        phase.name = "development"
        phase.name = "development"
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id=None)

        with patch("src.autopilot.spec.get_phase_required_files", return_value=[]):
            result = TaskCompletionService.verify_output_artifact(
                session=Mock(), task=task, phase=phase
            )
            assert result is None

    def test_passes_when_no_workflow_id_and_files_in_feature_dir(self):
        """Test passes when workflow_id is None but files exist in feature dir."""
        phase = Mock(name="development", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id=None, id="task-1")

        mock_session = Mock()

        with patch("src.autopilot.spec.get_phase_required_files", return_value=["docs/output.md"]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("src.autopilot.okf_markdown.read_okf", return_value=({"type": "test"}, "body")):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is None

    def test_rejects_when_workflow_has_no_working_directory(self):
        phase = Mock(name="development", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1", assigned_agent_id=None)

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = Mock(working_directory=None)
        # AgentWorktree recovery's own query chain (.join().filter().order_by().first())
        # is a separate auto-mocked branch from the plain .filter_by().first()
        # above -- must be pinned to None too, or its default (an
        # auto-generated Mock whose .worktree_path is itself a Mock, not a
        # real path string) reaches _Path(wt_record.worktree_path) and
        # raises a TypeError instead of exercising the "recovery also
        # failed" path this test means to assert.
        mock_session.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value = None

        with patch("src.autopilot.spec.get_phase_required_files", return_value=["docs/output.md"]):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is not None
            assert result["status"] == "failed"
            assert "system error" in result["message"].lower()

    def test_finds_output_in_phase_scoped_subdirectory(self, tmp_path):
        """Regression: agents are now told to write to the one sanctioned
        .hephaestus/<phase.name>/ subdirectory (see each gated phase's
        CRITICAL PATH RULE) -- this must be checked, not just flat
        .hephaestus/."""
        phase = Mock(name="qa_validation", id="phase-1")
        phase.name = "qa_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        sub = tmp_path / ".hephaestus" / "qa_validation"
        sub.mkdir(parents=True)
        (sub / "qa.md").write_text("---\ntype: qa_validation_result\n---\n\n# QA Report")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        with patch(
            "src.autopilot.spec.get_phase_required_files", return_value=["qa.md"]
        ):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is None

    def test_does_not_find_a_different_phases_subdirectory_output(self, tmp_path):
        """Regression: the old fallback searched EVERY subdirectory of
        .hephaestus/ for a same-named file -- a leftover file from a
        DIFFERENT feature or an earlier retry pass must not count as proof
        THIS phase's own agent produced its required output."""
        phase = Mock(name="qa_validation", id="phase-1")
        phase.name = "qa_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        other = tmp_path / ".hephaestus" / "some_other_feature"
        other.mkdir(parents=True)
        (other / "qa.md").write_text("# stale report from elsewhere")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        # Step 2's feature_dir fallback defaults to the REAL config's
        # project_root (cwd under pytest -- this actual repo, which has
        # real leftover .hephaestus/features/*/docs/qa.md files from
        # past pipeline runs) unless pinned at tmp_path -- without this the
        # test could pass for the wrong reason (finding an unrelated real
        # file) instead of proving the subdirectory-search removal.
        with patch(
            "src.autopilot.spec.get_phase_required_files", return_value=["qa.md"]
        ), patch("src.autopilot.spec.load_optional_phases", return_value=[]), patch(
            "src.core.simple_config.get_config",
            return_value=Mock(project_root=tmp_path),
        ):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is not None
            assert result["status"] == "failed"

    def test_rejects_when_output_file_missing(self):
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = Mock(working_directory="/nonexistent", project_id=None)

        with patch("src.autopilot.spec.get_phase_required_files", return_value=["docs/output.md"]), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("src.autopilot.spec.load_optional_phases", return_value=[]):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is not None
            assert result["status"] == "failed"

    def test_passes_when_output_file_exists(self):
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = Mock(working_directory="/path/to/project", project_id=None)

        with patch("src.autopilot.spec.get_phase_required_files", return_value=["docs/output.md"]), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("src.autopilot.okf_markdown.read_okf", return_value=({"type": "test"}, "body")):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is None

    def test_skips_verification_for_optional_phases(self):
        phase = Mock(name="optional_analysis", id="phase-1")
        phase.name = "optional_analysis"
        phase.name = "optional_analysis"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = Mock(working_directory="/path", project_id=None)

        with patch("src.autopilot.spec.get_phase_required_files", return_value=["docs/output.md"]), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("src.autopilot.spec.load_optional_phases", return_value=["optional_analysis"]):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is None

    def test_rejects_when_md_output_exists_but_has_no_valid_frontmatter(self, tmp_path):
        """Regression: a truncated/malformed write used to pass this floor
        (the file exists) and only surface much later, at gate-scoring
        time, as a confusing 'not found' -- read_okf's bare
        except-return-None makes a malformed file indistinguishable from a
        missing one downstream. This floor must catch it here instead."""
        phase = Mock(name="product_validation", id="phase-1")
        phase.name = "product_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        sub = tmp_path / ".hephaestus" / "product_validation"
        sub.mkdir(parents=True)
        (sub / "validation.md").write_text("no frontmatter block here")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        with patch(
            "src.autopilot.spec.get_phase_required_files",
            return_value=["validation.md"],
        ), patch("src.autopilot.spec.load_optional_phases", return_value=[]):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is not None
            assert result["status"] == "failed"
            assert "okf" in result["message"].lower()

    def test_passes_when_md_output_has_valid_frontmatter(self, tmp_path):
        phase = Mock(name="product_validation", id="phase-1")
        phase.name = "product_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        sub = tmp_path / ".hephaestus" / "product_validation"
        sub.mkdir(parents=True)
        (sub / "validation.md").write_text(
            "---\ntype: product_validation_result\nverdict: PASS\n---\n\n# Report"
        )

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        with patch(
            "src.autopilot.spec.get_phase_required_files",
            return_value=["validation.md"],
        ):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )
            assert result is None

    def test_uses_own_projects_feature_dir_not_global_singleton(self, tmp_path):
        """Regression: with two projects active simultaneously, the
        feature-dir fallback must search the task's OWN project's base_dir,
        not whichever project the process-wide config singleton currently
        points at (there's no longer only one "the active project")."""
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        workdir = tmp_path / "workdir"  # workflow's shared worktree -- no output here
        workdir.mkdir()

        project_dir = tmp_path / "project-a"
        feature_docs = project_dir / ".hephaestus" / "features" / "feat-1" / "docs"
        feature_docs.mkdir(parents=True)
        (feature_docs / "output.md").write_text("---\ntype: test\n---\n\ncontent")

        wf = Mock(working_directory=str(workdir), project_id="proj-a")
        project = Mock(base_dir=str(project_dir))

        def fake_query(model):
            q = Mock()
            if getattr(model, "__name__", "") == "Workflow":
                q.filter_by.return_value.first.return_value = wf
            elif getattr(model, "__name__", "") == "AutopilotProject":
                q.filter_by.return_value.first.return_value = project
            return q

        mock_session = Mock()
        mock_session.query.side_effect = fake_query

        # Simulates a DIFFERENT project being the one the global config
        # singleton points at -- proves the fix doesn't fall back to it.
        other_active_project = tmp_path / "some-other-active-project"
        other_active_project.mkdir()

        with patch(
            "src.autopilot.spec.get_phase_required_files", return_value=["output.md"]
        ), patch("src.autopilot.spec.load_optional_phases", return_value=[]), patch(
            "src.core.simple_config.get_config",
            return_value=Mock(project_root=other_active_project),
        ):
            result = TaskCompletionService.verify_output_artifact(
                session=mock_session, task=task, phase=phase
            )

        assert result is None


class TestVerifyOutputArtifactWorktreeRecovery:
    """Regression: when wf.working_directory is empty and recovery falls
    back to a worktree record, it must prefer the CURRENTLY completing
    task's own agent's worktree over the workflow's earliest one.

    "Earliest" was the right heuristic for the original transient-tracking-
    gap case (one shared worktree, briefly untracked), but breaks once a
    workflow has accumulated several genuinely DISCONNECTED isolated
    worktrees over multiple manual redo rounds (review-mode
    request_changes, a manual phase rerun) after its original worktree was
    already cleaned up post-merge -- each redo spawns its own fresh
    isolated worktree, and "earliest" then points at some unrelated one
    from a past round. Observed live: an architectural_review re-run after
    a development redo had its own genuinely-written review.md rejected as
    "missing" because verification checked a stale, disconnected worktree.
    """

    @pytest.fixture
    def real_db(self, tmp_path):
        from src.core.database import DatabaseManager as _DBM

        db = _DBM(str(tmp_path / "test.db"))
        db.create_tables()
        return db

    def test_prefers_completing_agents_own_worktree_over_earliest(self, real_db, tmp_path):
        from src.core.database import Agent, AgentWorktree, Task, Workflow

        # An earlier redo round's agent -- old, stale, and has nothing to
        # do with what THIS task's agent just wrote.
        stale_wt = tmp_path / "stale-worktree"
        stale_wt.mkdir()

        # This run's completing agent -- fresh worktree with the real output.
        fresh_wt = tmp_path / "fresh-worktree"
        (fresh_wt / ".hephaestus" / "architectural_review").mkdir(parents=True)
        (fresh_wt / ".hephaestus" / "architectural_review" / "review.md").write_text(
            "---\ntype: architectural_review_result\n---\n\n# Review"
        )

        with real_db.session_scope() as session:
            session.add(Workflow(
                id="wf-1", name="t", phases_folder_path="/tmp",
                status="active", working_directory=None,
            ))
            session.add(Agent(id="agent-old", system_prompt="p", status="terminated", cli_type="claude"))
            session.add(Task(
                id="task-old", workflow_id="wf-1", phase_id="phase-1",
                raw_description="d", done_definition="d", status="done",
                assigned_agent_id="agent-old",
            ))
            session.add(AgentWorktree(
                agent_id="agent-old", worktree_path=str(stale_wt), branch_name="b-old",
                parent_commit_sha="x", base_commit_sha="x",
            ))

            session.add(Agent(id="agent-new", system_prompt="p", status="working", cli_type="claude"))
            session.add(Task(
                id="task-new", workflow_id="wf-1", phase_id="phase-1",
                raw_description="d", done_definition="d", status="in_progress",
                assigned_agent_id="agent-new",
            ))
            session.add(AgentWorktree(
                agent_id="agent-new", worktree_path=str(fresh_wt), branch_name="b-new",
                parent_commit_sha="x", base_commit_sha="x",
            ))

        phase = Mock(name="architectural_review", id="phase-1")
        phase.name = "architectural_review"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-new", assigned_agent_id="agent-new")

        session = real_db.get_session()
        try:
            with patch(
                "src.autopilot.spec.get_phase_required_files", return_value=["review.md"]
            ), patch("src.autopilot.spec.load_optional_phases", return_value=[]):
                result = TaskCompletionService.verify_output_artifact(
                    session=session, task=task, phase=phase
                )
            assert result is None, f"expected pass via task-new's own fresh worktree, got: {result}"

            from src.core.database import Workflow as _Workflow
            wf = session.query(_Workflow).filter_by(id="wf-1").first()
            assert wf.working_directory == str(fresh_wt)
        finally:
            session.close()


class TestVerifyOutputSurvivedCommit:
    """Regression: verify_output_artifact found the declared output in the
    worktree BEFORE 'done' was accepted -- but a real incident showed the
    file could still be gone from the worktree by the time
    commit_and_link_ticket ran moments later (an agent whose actual last
    write landed outside its assigned worktree). Nothing re-checked after
    the commit, so a full report + code fix vanished with the task still
    showing "done" and zero commit in git history. This is the re-check
    that closes that gap."""

    def test_returns_none_when_no_phase(self):
        task = Mock(phase_id=None)
        result = TaskCompletionService.verify_output_survived_commit(
            session=Mock(), task=task, phase=None
        )
        assert result is None

    def test_returns_none_when_no_required_files(self):
        phase = Mock(name="development", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id="wf-1")

        with patch("src.autopilot.spec.get_phase_required_files", return_value=[]):
            result = TaskCompletionService.verify_output_survived_commit(
                session=Mock(), task=task, phase=phase
            )
            assert result is None

    def test_returns_none_when_no_workflow_id(self):
        phase = Mock(name="development", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id=None)

        with patch("src.autopilot.spec.get_phase_required_files", return_value=["docs/output.md"]):
            result = TaskCompletionService.verify_output_survived_commit(
                session=Mock(), task=task, phase=phase
            )
            assert result is None

    def test_passes_when_output_file_exists(self, tmp_path):
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1", status="done")

        (tmp_path / "output.md").write_text("content")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        with patch("src.autopilot.spec.get_phase_required_files", return_value=["output.md"]):
            result = TaskCompletionService.verify_output_survived_commit(
                session=mock_session, task=task, phase=phase
            )
            assert result is None
            assert task.status == "done"  # untouched

    def test_fails_and_marks_task_failed_when_output_missing(self, tmp_path):
        """The exact live incident: the pre-commit check saw the file (an
        earlier pass genuinely wrote it into the worktree), but by the time
        this runs -- right after commit_and_link_ticket -- it's gone."""
        phase = Mock(name="security_review", id="phase-1")
        phase.name = "security_review"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1", status="done")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path))  # empty -- no docs/ dir at all
        )

        with patch(
            "src.autopilot.spec.get_phase_required_files",
            return_value=["security.md"],
        ):
            result = TaskCompletionService.verify_output_survived_commit(
                session=mock_session, task=task, phase=phase
            )

        assert result is not None
        assert result["status"] == "failed"
        assert "security.md" in result["message"]
        assert task.status == "failed"
        assert task.failure_reason == result["message"]


class TestVerifyGateResultSchema:
    """Regression: a QA agent wrote its own nested JSON shape instead of
    the documented flat schema. verify_output_artifact only checks the
    declared file EXISTS -- it passed, since qa_result.json was there --
    but score_qa's field reads all silently defaulted to "everything
    passed" against that shape, including critical_issues and
    requirements_met, which nothing else independently re-verifies."""

    def test_returns_none_for_non_gated_phase(self, tmp_path):
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")
        result = TaskCompletionService.verify_gate_result_schema(
            session=Mock(), task=task, phase=phase
        )
        assert result is None

    def test_rejects_the_live_incompatible_qa_shape(self, tmp_path):
        phase = Mock(name="qa_validation", id="phase-1")
        phase.name = "qa_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        sub = tmp_path / ".hephaestus" / "qa_validation"
        sub.mkdir(parents=True)
        (sub / "qa.md").write_text(
            "---\n"
            "overall_status: PASS\n"
            "test_results:\n"
            "  main_suite:\n"
            "    total: 1410\n"
            "    passed: 1410\n"
            "---\n\n# QA Report"
        )

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        result = TaskCompletionService.verify_gate_result_schema(
            session=mock_session, task=task, phase=phase
        )

        assert result is not None
        assert result["status"] == "failed"
        assert "qa_validation" in result["message"]
        assert task.status == "failed"
        assert task.failure_reason == result["message"]

    def test_passes_the_documented_qa_shape(self, tmp_path):
        phase = Mock(name="qa_validation", id="phase-1")
        phase.name = "qa_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        sub = tmp_path / ".hephaestus" / "qa_validation"
        sub.mkdir(parents=True)
        (sub / "qa.md").write_text(
            "---\n"
            "type: qa_validation\n"
            "failed_tests: 0\n"
            "passed_tests: 1410\n"
            "critical_issues: 0\n"
            "---\n\n# QA Report"
        )

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        result = TaskCompletionService.verify_gate_result_schema(
            session=mock_session, task=task, phase=phase
        )
        assert result is None

    def test_returns_none_when_file_missing(self, tmp_path):
        """A missing result file is verify_output_artifact's job -- this
        floor only fires once a file exists but has the wrong shape."""
        phase = Mock(name="qa_validation", id="phase-1")
        phase.name = "qa_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        result = TaskCompletionService.verify_gate_result_schema(
            session=mock_session, task=task, phase=phase
        )
        assert result is None

    def test_reads_feature_reviews_hephaestus_subdir(self, tmp_path):
        """feature_review's result lives under .hephaestus/feature_review/,
        not docs/ -- the schema floor must read from the same subdir
        build_phase_output actually uses, or it would always see 'missing'
        and never fire."""
        phase = Mock(name="feature_review", id="phase-1")
        phase.name = "feature_review"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        internal_dir = tmp_path / ".hephaestus" / "feature_review"
        internal_dir.mkdir(parents=True)
        (internal_dir / "feature_review.md").write_text(
            "---\ntype: feature_review_result\nsummary: no counts here\n---\n\n# Report"
        )

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        result = TaskCompletionService.verify_gate_result_schema(
            session=mock_session, task=task, phase=phase
        )
        assert result is not None
        assert "feature_review" in result["message"]

    def test_reads_feature_reviews_legacy_flat_location_as_fallback(self, tmp_path):
        """TEMPORARY (Phase 2 §4.9 follow-up): an in-flight Phase 0 run
        started before the normalization may still be writing to the old
        flat .hephaestus/review.md -- checked as a fallback so it doesn't
        get rejected as unreadable just for predating the rename."""
        phase = Mock(name="feature_review", id="phase-1")
        phase.name = "feature_review"
        task = Mock(phase_id="phase-1", workflow_id="wf-1", id="task-1")

        internal_dir = tmp_path / ".hephaestus"
        internal_dir.mkdir()
        (internal_dir / "review.md").write_text(
            "---\ntype: feature_review_result\nblocker_count: 0\nfix_count: 0\n---\n\n# Report"
        )

        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = (
            Mock(working_directory=str(tmp_path), project_id=None)
        )

        result = TaskCompletionService.verify_gate_result_schema(
            session=mock_session, task=task, phase=phase
        )
        assert result is None


class TestVerifyNoOpenTickets:
    """Tests for verify_no_open_tickets method."""

    def test_returns_none_for_non_development_phase(self):
        phase = Mock(name="qa_validation", id="phase-1")
        phase.name = "qa_validation"
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        result = TaskCompletionService.verify_no_open_tickets(
            session=Mock(), task=task, phase=phase
        )
        assert result is None

    def test_returns_none_when_no_open_tickets(self):
        phase = Mock(name="development", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.all.return_value = []

        result = TaskCompletionService.verify_no_open_tickets(
            session=mock_session, task=task, phase=phase
        )
        assert result is None

    def test_rejects_when_open_tickets_exist(self):
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        mock_ticket = Mock(id="ticket-abc123def", title="Database connection leak")
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.all.return_value = [mock_ticket]

        result = TaskCompletionService.verify_no_open_tickets(
            session=mock_session, task=task, phase=phase
        )
        assert result is not None
        assert result["status"] == "failed"
        assert "1 open bug ticket" in result["message"]

    def test_failure_message_includes_the_full_ticket_id(self):
        """Regression: the message shows the id truncated to 8 chars
        ("ticket-a" from "ticket-abc123def"), which reads as a plausible
        complete id since real ids already start with "ticket-" -- but
        it isn't a real, resolvable id. This message instructs the agent
        to call change_ticket_status/resolve_ticket with it directly.
        Observed live: an agent tried to resolve a ticket using exactly
        this kind of truncated-looking id and got "Ticket not found"."""
        phase = Mock(name="development", id="phase-1")
        phase.name = "development"
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        mock_ticket = Mock(id="ticket-abc123def-4567-89ab", title="Database connection leak")
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.all.return_value = [mock_ticket]

        result = TaskCompletionService.verify_no_open_tickets(
            session=mock_session, task=task, phase=phase
        )

        assert "ticket-abc123def-4567-89ab" in result["message"]
        assert task.failure_reason is not None
        assert "ticket-abc123def-4567-89ab" in task.failure_reason

    def test_returns_none_when_no_workflow_id(self):
        phase = Mock(name="development", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id=None)
        result = TaskCompletionService.verify_no_open_tickets(
            session=Mock(), task=task, phase=phase
        )
        assert result is None

    def test_rejects_at_git_commit_push_too(self):
        """The final-phase check that closes the gap where security_review's
        tickets never get resolved: a run that never routes back to
        development would otherwise reach git_commit_push with open tickets
        and ship anyway."""
        phase = Mock(name="git_commit_push", id="phase-1")
        phase.name = "git_commit_push"
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        mock_ticket = Mock(id="ticket-abc123def", title="SQL injection in search")
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.all.return_value = [mock_ticket]

        result = TaskCompletionService.verify_no_open_tickets(
            session=mock_session, task=task, phase=phase
        )
        assert result is not None
        assert result["status"] == "failed"
        assert "1 open bug ticket" in result["message"]
        # git_commit_push can't fix code itself -- message should say so,
        # not tell this agent to "fix the underlying issue".
        assert "route back to development" in result["message"]

    def test_returns_none_for_git_commit_push_with_no_open_tickets(self):
        phase = Mock(name="git_commit_push", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        mock_session = Mock()
        mock_session.query.return_value.filter.return_value.all.return_value = []

        result = TaskCompletionService.verify_no_open_tickets(
            session=mock_session, task=task, phase=phase
        )
        assert result is None


class TestRecordLearnings:
    """Tests for record_learnings method."""

    @pytest.mark.asyncio
    async def test_stores_learnings(self):
        mock_session = Mock()
        mock_llm_provider = Mock()
        mock_llm_provider.generate_embedding = AsyncMock(return_value=[0.1] * 384)
        mock_vector_store = Mock()
        mock_vector_store.store_memory = AsyncMock()

        with patch("src.core.app_context.get_app_state") as mock_get_state:
            mock_state = Mock()
            mock_state.llm_provider = mock_llm_provider
            mock_state.vector_store = mock_vector_store
            mock_get_state.return_value = mock_state

            await TaskCompletionService.record_learnings(
                session=mock_session,
                agent_id="agent-1",
                task_id="task-1",
                key_learnings=["Use connection pooling", "Add retry logic"],
                code_changes=["src/db/pool.py", "src/api/retry.py"],
            )

            assert mock_llm_provider.generate_embedding.call_count == 2
            assert mock_vector_store.store_memory.call_count == 2
            assert mock_session.add.call_count == 2

    @pytest.mark.asyncio
    async def test_empty_learnings(self):
        mock_session = Mock()
        mock_llm_provider = Mock()
        mock_vector_store = Mock()

        with patch("src.core.app_context.get_app_state") as mock_get_state:
            mock_state = Mock()
            mock_state.llm_provider = mock_llm_provider
            mock_state.vector_store = mock_vector_store
            mock_get_state.return_value = mock_state

            await TaskCompletionService.record_learnings(
                session=mock_session,
                agent_id="agent-1",
                task_id="task-1",
                key_learnings=[],
                code_changes=[],
            )

            assert mock_llm_provider.generate_embedding.call_count == 0
            assert mock_session.add.call_count == 0


class TestCreateTicketsFromForensicsReport:
    """Tests for create_tickets_from_forensics_report method."""

    @pytest.mark.asyncio
    async def test_returns_zero_for_non_forensics_phase(self):
        phase = Mock(name="development", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = phase

        result = await TaskCompletionService.create_tickets_from_forensics_report(
            session=mock_session, task=task
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_working_directory(self):
        phase = Mock(name="forensics_analysis", id="phase-1")
        phase.name = "forensics_analysis"
        phase.name = "forensics_analysis"
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        wf = Mock(working_directory=None)
        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = wf

        result = await TaskCompletionService.create_tickets_from_forensics_report(
            session=mock_session, task=task
        )
        assert result == 0

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_report_file(self):
        phase = Mock(name="forensics_analysis", id="phase-1")
        task = Mock(phase_id="phase-1", workflow_id="wf-1")
        wf = Mock(working_directory="/path/to/project")
        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.side_effect = [phase, wf]

        with patch("pathlib.Path.exists", return_value=False):
            result = await TaskCompletionService.create_tickets_from_forensics_report(
                session=mock_session, task=task
            )
            assert result == 0


class TestCommitAndLinkTicket:
    """Tests for commit_and_link_ticket method."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_workflow(self):
        task = Mock(workflow_id=None, phase_id=None)
        with patch("src.core.app_context.get_app_state") as mock_state:
            mock_state.return_value = Mock(branch_manager=None)
            result = await TaskCompletionService.commit_and_link_ticket(
                session=Mock(), agent_id="agent-1", task=task, summary="test"
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_dirty_files(self):
        task = Mock(workflow_id="wf-1", phase_id="phase-1", id="task-1", ticket_id=None)
        wf = Mock(working_directory="/path/to/project")
        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = wf

        mock_repo = Mock()
        mock_repo.is_dirty.return_value = False
        mock_repo.untracked_files = []

        with patch("git.Repo", return_value=mock_repo), \
             patch("src.core.app_context.get_app_state") as mock_state:
            mock_state.return_value = Mock()
            result = await TaskCompletionService.commit_and_link_ticket(
                session=mock_session, agent_id="agent-1", task=task, summary="test"
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_sha_when_committed(self):
        task = Mock(workflow_id="wf-1", phase_id="phase-1", id="task-1", ticket_id=None)
        wf = Mock(working_directory="/path/to/project")
        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = wf

        mock_repo = Mock()
        mock_repo.is_dirty.return_value = True
        mock_repo.untracked_files = []
        mock_repo.head.commit.hexsha = "abc123" * 7

        with patch("git.Repo", return_value=mock_repo), \
             patch("src.core.app_context.get_app_state") as mock_state, \
             patch("pathlib.Path.is_dir", return_value=True):
            mock_state.return_value = Mock()
            result = await TaskCompletionService.commit_and_link_ticket(
                session=mock_session, agent_id="agent-1", task=task, summary="Fixed the bug"
            )
            assert result == "abc123" * 7

    @pytest.mark.asyncio
    async def test_links_ticket_when_present(self):
        task = Mock(workflow_id="wf-1", phase_id="phase-1", id="task-1", ticket_id="t-1")
        wf = Mock(working_directory="/path/to/project")
        mock_session = Mock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = wf

        mock_repo = Mock()
        mock_repo.is_dirty.return_value = True
        mock_repo.untracked_files = []
        mock_repo.head.commit.hexsha = "abc123" * 7

        with patch("git.Repo", return_value=mock_repo), \
             patch("src.core.app_context.get_app_state") as mock_state, \
             patch("src.services.ticket_service.TicketService") as mock_ticket_svc, \
             patch("pathlib.Path.is_dir", return_value=True):
            mock_state.return_value = Mock()
            mock_ticket_svc.link_commit = AsyncMock()

            result = await TaskCompletionService.commit_and_link_ticket(
                session=mock_session, agent_id="agent-1", task=task, summary="Fixed the bug"
            )
            assert result is not None
            mock_ticket_svc.link_commit.assert_called_once()
