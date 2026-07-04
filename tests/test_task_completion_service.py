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


def _seed(db, tmp_path, phase_name, phase_id=None):
    """Seed a Workflow + Phase + Task, return (session, task)."""
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

    def test_docs_path_still_works_for_existing_phases(self, db, tmp_path, monkeypatch):
        """Regression: adding the .hephaestus/ candidate must not break the
        existing docs/<file> search used by every other gated phase."""
        from src.autopilot import spec

        monkeypatch.setitem(spec.PHASE_OUTPUT_ARTIFACTS, "qa_validation", "qa_result.json")

        session, task = _seed(db, tmp_path, "qa_validation")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "qa_result.json").write_text("{}")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None  # found via docs/ path, unaffected by the new candidate
        session.close()

    def test_worktree_root_path_still_works(self, db, tmp_path, monkeypatch):
        """Regression: the worktree-root <file> search path (no docs/ prefix)
        must still work after adding the .hephaestus/ candidate."""
        from src.autopilot import spec

        monkeypatch.setitem(spec.PHASE_OUTPUT_ARTIFACTS, "architecture_design", "architecture.md")

        session, task = _seed(db, tmp_path, "architecture_design")
        (tmp_path / "architecture.md").write_text("# arch")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None
        session.close()

    def test_no_declared_output_for_phase_returns_none(self, db, tmp_path):
        """Phases with no PHASE_OUTPUT_ARTIFACTS entry get no enforcement."""
        session, task = _seed(db, tmp_path, "some_undeclared_phase")

        result = TaskCompletionService.verify_output_artifact(session, task)

        assert result is None
        session.close()
