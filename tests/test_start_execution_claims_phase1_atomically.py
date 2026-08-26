"""Regression: PhaseManager.start_execution() used to create phase 1's
PhaseExecution row (status=pending, task_creation_claimed_at=NULL) and
COMMIT it, then return control to workflow_execution_routes.py, which only
THEN claimed phase 1's task-creation right in a separate, later call. In
the window between that commit and the later claim, phase 1 was visible to
any reader as pending/unclaimed -- and the orchestrator's periodic sweep
(_case_start_first_phase / _case_in_progress_no_tasks in
phase_transitions.py), polling independently, could win the claim first
and create phase 1's task itself.

Observed live: tasks de0c5972 (created by workflow_execution_routes.py's
own /start_workflow_execution step) and 8ac50aa3 (created by the
orchestrator sweep via _create_phase_task, created_by_agent_id
"orchestrator-*"), both for the same brand-new phase 1, ~15s apart --
after ALL of the sweep's own internal TOCTOU hardening (13dfea19,
22916abd, e6092c50) had already landed, because none of those closed
this particular window.

Fixed by claiming phase 1's task-creation right atomically inside
start_execution's own transaction, at the same time its PhaseExecution
row is created -- so the row is never visible to any reader in an
unclaimed state.
"""

import pytest

from src.core.database import DatabaseManager, PhaseExecution
from src.phases.phase_manager import PhaseManager


@pytest.fixture
def phase_manager(tmp_path):
    db_manager = DatabaseManager(str(tmp_path / "test.db"))
    db_manager.create_tables()
    return PhaseManager(db_manager)


def test_phase1_claim_is_already_held_when_start_execution_returns(phase_manager):
    phase_manager.register_definition(
        definition_id="claim-test",
        name="Claim Test",
        phases_config=[
            {"order": 1, "name": "Phase 1", "description": "First", "done_definitions": []},
            {"order": 2, "name": "Phase 2", "description": "Second", "done_definitions": []},
        ],
        workflow_config={
            "launch_template": {"phase_1_task_prompt": "Do the first thing"},
        },
    )

    workflow_id, initial_task_info = phase_manager.start_execution(
        definition_id="claim-test",
        description="Test",
    )

    assert initial_task_info is not None
    phase_uuid = initial_task_info["phase_uuid"]

    session = phase_manager.db_manager.get_session()
    try:
        execution = session.query(PhaseExecution).filter_by(phase_id=phase_uuid).first()
        assert execution.task_creation_claimed_at is not None, (
            "phase 1's task-creation claim must already be held the moment "
            "start_execution returns -- otherwise a concurrent orchestrator "
            "sweep tick can win it first and create a duplicate task"
        )
    finally:
        session.close()


def test_no_claim_taken_when_no_phase1_task_prompt(phase_manager):
    """A workflow definition with no launch_template.phase_1_task_prompt has
    nothing racing to create phase 1's task via that path -- pre-claiming
    here regardless would leave the claim permanently held (nothing would
    ever release it), blocking the orchestrator's own self-heal forever."""
    phase_manager.register_definition(
        definition_id="no-claim-test",
        name="No Claim Test",
        phases_config=[
            {"order": 1, "name": "Phase 1", "description": "First", "done_definitions": []},
        ],
    )

    workflow_id, initial_task_info = phase_manager.start_execution(
        definition_id="no-claim-test",
        description="Test",
    )

    assert initial_task_info is None

    session = phase_manager.db_manager.get_session()
    try:
        execution = session.query(PhaseExecution).filter_by(
            workflow_execution_id=workflow_id
        ).first()
        assert execution.task_creation_claimed_at is None
    finally:
        session.close()
