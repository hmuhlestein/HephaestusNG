"""Regression: feature_review must never resume a prior review's session.

Found live (design des-c7b9534a7dfd): _resolve_session_id derives a
deterministic --resume session id from (project_id, design_slug,
phase_name, model) alone. Every feature_review task for the same design
hashed to the same id, so a goto-triggered SECOND review resumed the
FIRST review agent's already-finished Claude Code conversation instead of
starting fresh -- it echoed the first review's exact verdict (4 already-
FIXED blockers reported as still present, even the first agent's own id
showing up in the second agent's save_memory call), sending the pipeline
into a needless extra re-decomposition cycle. feature_review's own
instructions require independent, current-state verification each entry
("you are a fresh reviewer ... don't rely on memory"), which a resumed
session directly defeats.

Restart (the task's own crashed/orphaned agent continuing the SAME
in-progress task) is a different, legitimate case that must keep
resuming -- covered by the excluded_phases-omitted case below.
"""

import uuid

import pytest

from src.core.database import Task, Workflow


@pytest.fixture
def _task(db_manager):
    workflow_id = str(uuid.uuid4())
    task_id = str(uuid.uuid4())
    with db_manager.session_scope() as session:
        session.add(
            Workflow(
                id=workflow_id,
                name="w",
                phases_folder_path="/tmp",
                status="active",
                launch_params={
                    "project_id": "proj-1",
                    "design_slug": "des-abc123",
                },
            )
        )
        session.add(
            Task(
                id=task_id,
                workflow_id=workflow_id,
                raw_description="x",
                done_definition="x",
                status="pending",
            )
        )
    with db_manager.session_scope() as session:
        return session.query(Task).filter_by(id=task_id).first()


def _launch_pipeline(db_manager):
    from src.agents.launch_pipeline import LaunchPipeline

    fake_agent_manager = type("FakeAgentManager", (), {"db_manager": db_manager})()
    return LaunchPipeline(fake_agent_manager)


class TestFeatureReviewSessionExclusion:
    def test_feature_review_new_dispatch_gets_no_session_id(self, db_manager, _task):
        """The bug: without excluded_phases, this would return a
        deterministic id and the caller would pass --resume."""
        pipeline = _launch_pipeline(db_manager)
        session_id = pipeline._resolve_session_id(
            _task, "phase", "feature_review", "sonnet",
            excluded_types=(),
            excluded_phases=("feature_review",),
        )
        assert session_id == ""

    def test_other_phases_still_get_a_stable_session_id(self, db_manager, _task):
        """Not a blanket disable -- development (and any phase not
        explicitly excluded) must keep resuming, since that's the whole
        point of the session-id scheme for iterative phases."""
        pipeline = _launch_pipeline(db_manager)
        session_id = pipeline._resolve_session_id(
            _task, "phase", "development", "sonnet",
            excluded_types=(),
            excluded_phases=("feature_review",),
        )
        assert session_id != ""

    def test_restart_path_still_resumes_feature_review(self, db_manager, _task):
        """Restart (excluded_phases omitted, matching the real restart call
        site) continues the SAME interrupted task/session and must NOT be
        affected by the new-dispatch exclusion."""
        pipeline = _launch_pipeline(db_manager)
        session_id = pipeline._resolve_session_id(
            _task, "phase", "feature_review", "sonnet",
            excluded_types=(),
        )
        assert session_id != ""
