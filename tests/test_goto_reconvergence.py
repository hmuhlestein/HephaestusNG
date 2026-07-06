"""Integration test for goto-reconvergence (3c bug).

Verifies that after a GOTO back to an earlier phase, the workflow
advances through ALL later phases (not just the re-run phase) and
only completes after the final phase.
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import (
    Base,
    Phase,
    PhaseExecution,
    Workflow,
    WorkflowDefinition,
)


@pytest.fixture
def db_engine():
    """Create an in-memory SQLite database for testing."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Create a database session for testing."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def db_manager(db_session):
    """Create a mock DB manager that returns the test session."""
    manager = MagicMock()
    manager.get_session.return_value = db_session
    return manager


@pytest.fixture
def phase_ids(db_session):
    """Create a workflow with 4 phases and return phase IDs."""
    workflow_id = str(uuid.uuid4())
    definition_id = "test-goto-workflow"

    orchestrator_config = {
        "type": "evaluating",
        "max_phase_retries": 2,
        "max_total_gotos": 3,
        "evaluation_points": [
            {
                "after_phase": "phase_1",
                "evaluator": "heuristic",
                "conditions": [
                    {"if": "score >= 0.7", "action": "continue", "reason": "Passed"},
                ],
                "max_retries": 2,
            },
            {
                "after_phase": "phase_2",
                "evaluator": "heuristic",
                "conditions": [
                    {
                        "if": "score < 0.5",
                        "action": "goto",
                        "target": "phase_1",
                        "reason": "Failed",
                    },
                    {"if": "score >= 0.5", "action": "continue", "reason": "Passed"},
                ],
                "max_retries": 2,
            },
            {
                "after_phase": "phase_3",
                "evaluator": "heuristic",
                "conditions": [
                    {"if": "score >= 0.7", "action": "continue", "reason": "Passed"},
                ],
                "max_retries": 2,
            },
            {
                "after_phase": "phase_4",
                "evaluator": "heuristic",
                "conditions": [
                    {"if": "score >= 0.0", "action": "continue", "reason": "Done"},
                ],
                "max_retries": 0,
            },
        ],
    }

    definition = WorkflowDefinition(
        id=definition_id,
        name="Test Goto Workflow",
        phases_config=[
            {"name": "phase_1", "order": 1, "description": "Phase 1"},
            {"name": "phase_2", "order": 2, "description": "Phase 2"},
            {"name": "phase_3", "order": 3, "description": "Phase 3"},
            {"name": "phase_4", "order": 4, "description": "Phase 4"},
        ],
        orchestrator_config=orchestrator_config,
    )
    db_session.add(definition)

    workflow = Workflow(
        id=workflow_id,
        name="Test Goto Workflow",
        description="Test goto reconvergence",
        definition_id=definition_id,
        phases_folder_path="/tmp/test",
        working_directory="/tmp/test",
        status="active",
    )
    db_session.add(workflow)

    ids = []
    for i in range(1, 5):
        phase = Phase(
            id=str(uuid.uuid4()),
            workflow_id=workflow_id,
            order=i,
            name=f"phase_{i}",
            description=f"Phase {i}",
            done_definitions=[f"Phase {i} done"],
        )
        db_session.add(phase)
        ids.append(phase.id)

        execution = PhaseExecution(
            id=str(uuid.uuid4()),
            phase_id=phase.id,
            workflow_execution_id=workflow_id,
            status="pending",
        )
        db_session.add(execution)

    db_session.commit()
    return ids


def test_goto_reconvergence(db_session, db_manager, phase_ids):
    """Test goto reconvergence: phase_1 -> phase_2 (fail) -> goto phase_1 -> continue through all."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)
    pm.workflow_id = db_session.query(Workflow).first().id

    def simulate_monitor_action(result):
        """Simulate what the Monitor does after mark_phase_complete."""
        if result["should_continue"] and result["target_phase_id"]:
            db_session.query(PhaseExecution).filter_by(
                phase_id=result["target_phase_id"]
            ).update({"status": "in_progress", "started_at": datetime.utcnow()})
            db_session.commit()

    # Start phase 1
    db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).update(
        {"status": "in_progress", "started_at": datetime.utcnow()}
    )
    db_session.commit()

    # Complete phase 1 -> CONTINUE to phase_2
    result = pm.mark_phase_complete(
        phase_ids[0], "P1 done", phase_output={"score": 0.8}
    )
    assert result["action"] == "continue"
    assert result["target_phase_id"] == phase_ids[1]
    simulate_monitor_action(result)
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).first().status
        == "completed"
    )
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[1]).first().status
        == "in_progress"
    )

    # Complete phase 2 with LOW score -> GOTO phase_1
    result = pm.mark_phase_complete(
        phase_ids[1], "P2 failed", phase_output={"score": 0.3}
    )
    assert result["action"] == "goto"
    assert result["target_phase_id"] == phase_ids[0]
    simulate_monitor_action(result)
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).first().status
        == "in_progress"
    )

    # Re-complete phase 1 -> CONTINUE to phase_2
    result = pm.mark_phase_complete(
        phase_ids[0], "P1 re-done", phase_output={"score": 0.9}
    )
    assert result["action"] == "continue"
    assert result["target_phase_id"] == phase_ids[1]
    simulate_monitor_action(result)
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[1]).first().status
        == "in_progress"
    )

    # Complete phase 2 with HIGH score -> CONTINUE to phase_3
    result = pm.mark_phase_complete(
        phase_ids[1], "P2 passed", phase_output={"score": 0.8}
    )
    assert result["target_phase_id"] == phase_ids[2]
    simulate_monitor_action(result)

    # Complete phase 3 -> CONTINUE to phase_4
    result = pm.mark_phase_complete(
        phase_ids[2], "P3 done", phase_output={"score": 0.8}
    )
    assert result["target_phase_id"] == phase_ids[3]
    simulate_monitor_action(result)

    # Complete phase 4 -> workflow complete
    result = pm.mark_phase_complete(
        phase_ids[3], "P4 done", phase_output={"score": 1.0}
    )
    assert result["should_continue"] is False
    assert db_session.query(Workflow).first().status == "completed"


def test_goto_does_not_skip_phases(db_session, db_manager, phase_ids):
    """3c regression: after goto, workflow must advance through ALL later phases."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)
    pm.workflow_id = db_session.query(Workflow).first().id

    def simulate_monitor(result):
        if result.get("should_continue") and result.get("target_phase_id"):
            db_session.query(PhaseExecution).filter_by(
                phase_id=result["target_phase_id"]
            ).update({"status": "in_progress", "started_at": datetime.utcnow()})
            db_session.commit()

    # Start and complete phase 1
    db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).update(
        {"status": "in_progress"}
    )
    db_session.commit()
    simulate_monitor(
        pm.mark_phase_complete(phase_ids[0], "P1 done", phase_output={"score": 0.8})
    )

    # Complete phase 2 with goto back to phase 1
    simulate_monitor(
        pm.mark_phase_complete(phase_ids[1], "P2 failed", phase_output={"score": 0.3})
    )
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).first().status
        == "in_progress"
    )

    # Re-complete phase 1
    simulate_monitor(
        pm.mark_phase_complete(phase_ids[0], "P1 re-done", phase_output={"score": 0.9})
    )

    # Re-complete phase 2 (now passes)
    simulate_monitor(
        pm.mark_phase_complete(phase_ids[1], "P2 passed", phase_output={"score": 0.8})
    )

    # Complete phase 3
    simulate_monitor(
        pm.mark_phase_complete(phase_ids[2], "P3 done", phase_output={"score": 0.8})
    )

    # Complete phase 4 -> workflow should complete (NOT skip phases 3-4)
    result = pm.mark_phase_complete(
        phase_ids[3], "P4 done", phase_output={"score": 1.0}
    )
    assert result["should_continue"] is False

    # Verify ALL phases are completed
    for pid in phase_ids:
        assert (
            db_session.query(PhaseExecution).filter_by(phase_id=pid).first().status
            == "completed"
        )
    assert db_session.query(Workflow).first().status == "completed"


def test_total_gotos_persists_across_fresh_phase_manager_instances(
    db_session, db_manager, phase_ids
):
    """Regression: production creates a brand-new PhaseManager() (hence an
    uncached WorkflowOrchestrator) on nearly every mark_phase_complete call
    -- task_completion_service.py's fire_spec_gate_if_ready and
    autopilot/orchestrator.py's periodic sweep both do this. Since
    WorkflowOrchestrator.total_gotos was in-memory only, it silently reset to
    0 every time, so max_total_gotos (3, per phase_ids' orchestrator_config)
    never actually fired and a failing gate could goto-loop forever.

    Drives the phase_1 <-> phase_2 goto cycle with a FRESH PhaseManager
    instance each call (as production actually does) and asserts the 4th
    goto attempt is forced to 'continue' instead of looping indefinitely.
    """
    from src.phases.phase_manager import PhaseManager

    def simulate_monitor(result):
        if result.get("should_continue") and result.get("target_phase_id"):
            db_session.query(PhaseExecution).filter_by(
                phase_id=result["target_phase_id"]
            ).update({"status": "in_progress", "started_at": datetime.utcnow()})
            db_session.commit()

    db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).update(
        {"status": "in_progress"}
    )
    db_session.commit()

    # Complete phase 1 with a fresh PhaseManager (as every real call site does)
    simulate_monitor(
        PhaseManager(db_manager).mark_phase_complete(
            phase_ids[0], "P1 done", phase_output={"score": 0.8}
        )
    )

    actions = []
    for i in range(4):
        result = PhaseManager(db_manager).mark_phase_complete(
            phase_ids[1], f"P2 failed #{i}", phase_output={"score": 0.3}
        )
        actions.append(result["action"])
        simulate_monitor(result)
        if result["action"] == "goto":
            # Re-complete phase 1 (fresh instance again) to set up the next cycle
            simulate_monitor(
                PhaseManager(db_manager).mark_phase_complete(
                    phase_ids[0], f"P1 re-done #{i}", phase_output={"score": 0.9}
                )
            )

    assert actions == ["goto", "goto", "goto", "continue"], (
        f"expected the 4th cycle to be forced to 'continue' once "
        f"max_total_gotos=3 was exceeded, got {actions}"
    )

    workflow = db_session.query(Workflow).first()
    assert workflow.total_gotos == 4


def test_start_next_phase_returns_true_for_completed(db_session, db_manager, phase_ids):
    """3c fix: _start_next_phase returns True for completed phases (not just pending)."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)

    # Set phase 2 to completed (simulating re-run scenario)
    db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[1]).update(
        {"status": "completed"}
    )
    db_session.commit()

    # _start_next_phase should return True even though phase 3 was already completed
    result = pm._start_next_phase(db_session, phase_ids[1])
    assert result is True
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[2]).first().status
        == "in_progress"
    )
