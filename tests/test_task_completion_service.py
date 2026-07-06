"""Tests for TaskCompletionService.verify_output_artifact — the
declared-output-artifact hard floor extracted from update_task_status.

Covers the .hephaestus/ search path added to support Phase 0's Feature
Architect (see docs/LOOP_ENGINEERING_REVIEW.md's Phase 0 "bolt-on" finding).
"""

import uuid

import pytest

from src.core.database import DatabaseManager, Phase, Task, Workflow
from src.services.task_completion_service import TaskCompletionService


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


def _seed(db, tmp_path, phase_name, phase_id=None, outputs=None):
    """Seed a Workflow + Phase + Task, return (session, task).

    outputs, if given, is JSON-serialized before insert to match the real
    production write path (Phase.outputs is a Text column; see
    phase_manager.py's serialize_for_text)."""
    import json

    session = db.get_session()
    workflow_id = f"wf-{uuid.uuid4().hex[:8]}"
    phase_id = phase_id or f"phase-{uuid.uuid4().hex[:8]}"
    task_id = f"task-{uuid.uuid4().hex[:8]}"

    session.add(
        Workflow(
            id=workflow_id,
            name="t",
            phases_folder_path="/tmp",
            status="active",
            definition_id="autopilot-phase0",
            working_directory=str(tmp_path),
        )
    )
    session.add(
        Phase(
            id=phase_id,
            workflow_id=workflow_id,
            order=1,
            name=phase_name,
            description="d",
            done_definitions=["done"],
            outputs=json.dumps(outputs) if outputs is not None else None,
        )
    )
    session.add(
        Task(
            id=task_id,
            raw_description="raw",
            done_definition="done",
            status="in_progress",
            workflow_id=workflow_id,
            phase_id=phase_id,
        )
    )
    session.commit()

    task = session.query(Task).filter_by(id=task_id).first()
    return session, task


class TestVerifyOutputArtifactHephaestusPath:
    def test_finds_artifact_in_hephaestus_dir(self, db, tmp_path, monkeypatch):
        from src.autopilot import spec

        monkeypatch.setitem(spec.PHASE_OUTPUT_ARTIFACTS, "Feature Architect", "features.json")

        session, task = _seed(db, tmp_path, "Feature Architect")
        (tmp_path / ".hephaestus").mkdir()
        (tmp_path / ".hephaestus" / "features.json").write_text("{}")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None  # found -> no rejection
        session.close()

    def test_rejects_when_hephaestus_artifact_missing(self, db, tmp_path, monkeypatch):
        from src.autopilot import spec

        monkeypatch.setitem(spec.PHASE_OUTPUT_ARTIFACTS, "Feature Architect", "features.json")

        session, task = _seed(db, tmp_path, "Feature Architect")
        # .hephaestus/features.json deliberately not written

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is not None
        assert result["status"] == "failed"
        assert "features.json" in result["message"]

        refreshed = session.query(Task).filter_by(id=task.id).first()
        assert refreshed.status == "failed"
        session.close()

    def test_docs_path_still_works_for_existing_phases(self, db, tmp_path):
        """Regression: adding the .hephaestus/ candidate must not break the
        existing docs/<file> search used by every other gated phase. Output
        is now derived from the phase's own declared outputs (Phase.outputs),
        not a hardcoded dict."""
        session, task = _seed(
            db, tmp_path, "qa_validation", outputs=["qa_report.md", "qa_result.json"]
        )
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "qa_result.json").write_text("{}")
        (tmp_path / "docs" / "qa_report.md").write_text("# qa")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None  # found via docs/ path, unaffected by the new candidate
        session.close()

    def test_worktree_root_path_still_works(self, db, tmp_path):
        """Regression: the worktree-root <file> search path (no docs/ prefix)
        must still work after adding the .hephaestus/ candidate."""
        session, task = _seed(
            db, tmp_path, "architecture_design", outputs=["architecture.md"]
        )
        (tmp_path / "architecture.md").write_text("# arch")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None
        session.close()

    def test_no_declared_output_for_phase_returns_none(self, db, tmp_path):
        """Phases with no declared outputs (or only non-file, descriptive
        deliverables) get no enforcement."""
        session, task = _seed(db, tmp_path, "some_undeclared_phase")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None
        session.close()

    def test_non_file_descriptive_outputs_are_ignored(self, db, tmp_path):
        """development/git_commit_push declare deliverables like 'source
        code in project path' that aren't checkable files — must not be
        treated as a missing artifact."""
        session, task = _seed(
            db, tmp_path, "development", outputs=["source code in project path"]
        )

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None
        session.close()

    def test_previously_unenforced_phase_now_gets_hard_floor(self, db, tmp_path):
        """The systemic fix: adversarial_review/security_review (and any
        other phase with a declared outputs: file) previously had zero
        enforcement — only 4 phases were in a hardcoded dict. A real smoke
        run merged successfully despite adversarial_review_report.md and
        security_report.md both being missing. Now derived straight from
        the phase's own YAML outputs, so this can't happen silently."""
        session, task = _seed(
            db, tmp_path, "adversarial_review", outputs=["adversarial_review_report.md"]
        )
        # adversarial_review_report.md deliberately not written

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is not None
        assert result["status"] == "failed"
        assert "adversarial_review_report.md" in result["message"]
        session.close()

    def test_multiple_declared_outputs_all_required(self, db, tmp_path):
        """qa_validation declares two files (qa_report.md, qa_result.json).
        Writing only one must still fail — every declared file is required."""
        session, task = _seed(
            db, tmp_path, "qa_validation", outputs=["qa_report.md", "qa_result.json"]
        )
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "qa_result.json").write_text("{}")
        # qa_report.md deliberately not written

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is not None
        assert "qa_report.md" in result["message"]
        assert "qa_result.json" not in result["message"]
        session.close()

    def test_placeholder_path_segments_are_not_enforced(self, db, tmp_path):
        """Phase 0's outputs: list includes '.hephaestus/features/<id>/scope.md'
        — a templated path with no concrete '<id>', not a real filename to
        check. Only the concrete features.json entry should be enforced."""
        session, task = _seed(
            db,
            tmp_path,
            "Feature Architect",
            outputs=[".hephaestus/features.json", ".hephaestus/features/<id>/scope.md"],
        )
        (tmp_path / ".hephaestus").mkdir()
        (tmp_path / ".hephaestus" / "features.json").write_text("{}")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None
        session.close()


SAMPLE_REPORT = """# Hephaestus Forensics Report

## Executive Summary

Some summary text.

## Recommendations for Future Pipeline Runs

### High Priority

1. **Add .gitignore creation to Development phase** - Prevents security review from having to fix this basic setup issue

2. **Clarify MCP tool parameter requirements in prompts** - Reduces retry cycles from parameter confusion

### Medium Priority

3. **Add design scope guidance to adversarial review prompt** - Helps classify findings as in-scope vs out-of-scope

### Low Priority

4. **Add explicit path examples in prompts** - Helps agents resolve file paths correctly

---

## Conclusion

Some conclusion text that must NOT be parsed as a recommendation.
"""


class TestParseForensicsRecommendations:
    """Regression coverage for a real gap found via smoke testing: an agent
    wrote a genuinely thorough forensics_report.md with 7 concrete
    recommendations but never called hephaestus_create_ticket once, despite
    "Tickets created for actionable findings" being a mandated completion
    criterion. Auto-create tickets from the report itself instead of
    trusting the agent to remember."""

    def test_extracts_all_recommendations_with_correct_priority(self):
        recs = TaskCompletionService._parse_forensics_recommendations(SAMPLE_REPORT)

        assert len(recs) == 4
        assert recs[0]["title"] == "Add .gitignore creation to Development phase"
        assert recs[0]["priority"] == "high"
        assert "Prevents security review" in recs[0]["description"]
        assert recs[1]["priority"] == "high"
        assert recs[2]["priority"] == "medium"
        assert recs[3]["priority"] == "low"

    def test_does_not_parse_content_outside_recommendations_section(self):
        recs = TaskCompletionService._parse_forensics_recommendations(SAMPLE_REPORT)
        titles = [r["title"] for r in recs]
        assert not any("Conclusion" in t for t in titles)

    def test_no_recommendations_section_returns_empty(self):
        report = "# Report\n\n## Executive Summary\n\nNo recommendations here.\n"
        recs = TaskCompletionService._parse_forensics_recommendations(report)
        assert recs == []

    def test_flat_list_with_no_priority_headings_defaults_to_medium(self):
        report = (
            "## Recommendations for Future Pipeline Runs\n\n"
            "1. **Do the thing** - because reasons\n"
            "2. **Do another thing** - also reasons\n"
        )
        recs = TaskCompletionService._parse_forensics_recommendations(report)
        assert len(recs) == 2
        assert all(r["priority"] == "medium" for r in recs)


class TestCreateTicketsFromForensicsReport:
    @pytest.mark.asyncio
    async def test_creates_ticket_per_recommendation(self, db, tmp_path, monkeypatch):
        session, task = _seed(db, tmp_path, "forensics_analysis")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "forensics_report.md").write_text(SAMPLE_REPORT)

        created_calls = []

        async def fake_create_ticket(**kwargs):
            created_calls.append(kwargs)
            return {"id": f"ticket-{len(created_calls)}"}

        monkeypatch.setattr(
            "src.services.ticket_service.TicketService.create_ticket",
            fake_create_ticket,
        )

        count = await TaskCompletionService.create_tickets_from_forensics_report(
            session, task
        )

        assert count == 4
        assert len(created_calls) == 4
        assert created_calls[0]["priority"] == "high"
        assert created_calls[0]["ticket_type"] == "improvement"
        assert created_calls[0]["workflow_id"] == task.workflow_id
        session.close()

    @pytest.mark.asyncio
    async def test_non_forensics_phase_creates_nothing(self, db, tmp_path):
        session, task = _seed(db, tmp_path, "qa_validation")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "forensics_report.md").write_text(SAMPLE_REPORT)

        count = await TaskCompletionService.create_tickets_from_forensics_report(
            session, task
        )

        assert count == 0
        session.close()

    @pytest.mark.asyncio
    async def test_missing_report_file_returns_zero(self, db, tmp_path):
        session, task = _seed(db, tmp_path, "forensics_analysis")
        # docs/forensics_report.md deliberately not written

        count = await TaskCompletionService.create_tickets_from_forensics_report(
            session, task
        )

        assert count == 0
        session.close()

    @pytest.mark.asyncio
    async def test_individual_ticket_failure_does_not_block_others(
        self, db, tmp_path, monkeypatch
    ):
        session, task = _seed(db, tmp_path, "forensics_analysis")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "forensics_report.md").write_text(SAMPLE_REPORT)

        calls = []

        async def flaky_create_ticket(**kwargs):
            calls.append(kwargs)
            if len(calls) == 2:
                raise ValueError("Board configuration not found for workflow")
            return {"id": f"ticket-{len(calls)}"}

        monkeypatch.setattr(
            "src.services.ticket_service.TicketService.create_ticket",
            flaky_create_ticket,
        )

        count = await TaskCompletionService.create_tickets_from_forensics_report(
            session, task
        )

        assert count == 3  # 4 total, 1 failed
        assert len(calls) == 4  # still attempted all of them
        session.close()
