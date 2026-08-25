"""Integration test for goto-reconvergence (3c bug).

Verifies that after a GOTO back to an earlier phase, the workflow
advances through ALL later phases (not just the re-run phase) and
only completes after the final phase.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import (
    Base,
    Phase,
    PhaseExecution,
    Task,
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

        # derive_workflow_status (called by PhaseManager._complete_workflow)
        # returns the current status unchanged with zero Task rows for the
        # workflow ("no tasks yet"), never reaching its own PhaseExecution
        # check -- this test simulates the whole pipeline via direct
        # PhaseExecution updates, with no Task rows, unlike a real run
        # where each phase's actual work always has one.
        db_session.add(
            Task(
                id=str(uuid.uuid4()),
                raw_description=f"Phase {i} work",
                done_definition=f"Phase {i} done",
                status="done",
                phase_id=phase.id,
                workflow_id=workflow_id,
            )
        )

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


def test_phase_retry_count_persists_across_fresh_phase_manager_instances(db_session, db_manager):
    """Sibling regression to total_gotos: WorkflowOrchestrator.phase_retry_counts
    is the same kind of in-memory-only dict, reset to {} whenever a fresh
    PhaseManager (hence a fresh, uncached orchestrator) is constructed --
    exactly what production does on nearly every mark_phase_complete call.
    Without persisting it on Phase.retry_count, eval_point.max_retries never
    actually capped a phase's RETRY budget."""
    from src.phases.phase_manager import PhaseManager

    workflow_id = str(uuid.uuid4())
    definition_id = "test-retry-workflow"

    orchestrator_config = {
        "type": "evaluating",
        "max_phase_retries": 5,
        "max_total_gotos": 10,
        "evaluation_points": [
            {
                "after_phase": "phase_1",
                "evaluator": "heuristic",
                "conditions": [
                    {
                        "if": "score < 0.5",
                        "action": "retry",
                        "reason": "Not good enough yet",
                    },
                    {"if": "score >= 0.5", "action": "continue", "reason": "Passed"},
                ],
                "max_retries": 2,
            },
        ],
    }

    definition = WorkflowDefinition(
        id=definition_id,
        name="Test Retry Workflow",
        phases_config=[{"name": "phase_1", "order": 1, "description": "Phase 1"}],
        orchestrator_config=orchestrator_config,
    )
    db_session.add(definition)

    workflow = Workflow(
        id=workflow_id,
        name="Test Retry Workflow",
        description="Test phase retry persistence",
        definition_id=definition_id,
        phases_folder_path="/tmp/test",
        working_directory="/tmp/test",
        status="active",
    )
    db_session.add(workflow)

    phase_id = str(uuid.uuid4())
    phase = Phase(
        id=phase_id,
        workflow_id=workflow_id,
        order=1,
        name="phase_1",
        description="Phase 1",
        done_definitions=["Phase 1 done"],
    )
    db_session.add(phase)
    execution = PhaseExecution(
        id=str(uuid.uuid4()),
        phase_id=phase_id,
        workflow_execution_id=workflow_id,
        status="in_progress",
    )
    db_session.add(execution)
    db_session.commit()

    actions = []
    for i in range(3):
        result = PhaseManager(db_manager).mark_phase_complete(
            phase_id, f"attempt #{i}", phase_output={"score": 0.2}
        )
        actions.append(result["action"])
        # RETRY resets the execution to pending -- re-mark in_progress for the next cycle
        db_session.query(PhaseExecution).filter_by(phase_id=phase_id).update(
            {"status": "in_progress"}
        )
        db_session.commit()

    assert actions == ["retry", "retry", "continue"], (
        f"expected the 3rd cycle to be forced to 'continue' once "
        f"max_retries=2 was exceeded, got {actions}"
    )

    phase_row = db_session.query(Phase).filter_by(id=phase_id).first()
    assert phase_row.retry_count == 2


def test_start_next_phase_returns_true_for_completed(db_session, db_manager, phase_ids):
    """3c fix: _start_next_phase returns True for completed phases (not just pending)."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)

    # Set phase 2 to completed (simulating re-run scenario)
    db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[1]).update(
        {"status": "completed"}
    )
    db_session.commit()

    # _start_next_phase should return the started Phase even though phase 3
    # was already completed
    result = pm._start_next_phase(db_session, phase_ids[1])
    assert result is not None
    assert result.id == phase_ids[2]
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[2]).first().status
        == "in_progress"
    )


def test_start_next_phase_skipped_when_workflow_paused_by_review(
    db_session, db_manager, phase_ids
):
    """Regression, observed live: a review-paused workflow (every phase
    already "completed", paused_by="review" awaiting human approval) must
    not advance when a later completion event (e.g. a goto back from
    deploy) fires _start_next_phase again. "paused" alone is in the
    allowed-status set (a plain pause resumes later without losing its
    place), but paused_by must still block advancement regardless -- feature
    e6437c3f kept cycling qa_validation -> ... -> deploy every ~6 minutes
    for hours after being review-paused, because nothing here checked
    paused_by."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)

    db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[1]).update(
        {"status": "completed"}
    )
    workflow = db_session.query(Workflow).first()
    workflow.status = "paused"
    workflow.paused_by = "review"
    db_session.commit()

    result = pm._start_next_phase(db_session, phase_ids[1])

    assert result is None
    assert (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[2]).first().status
        == "pending"
    )


def test_start_next_phase_resets_task_creation_claim(db_session, db_manager, phase_ids):
    """Regression: task_creation_claimed_at is a one-time-per-cycle lock
    (see orchestrator.py's _claim_phase_task_creation), not permanent.
    Re-running a phase after goto reconvergence must clear a claim left
    over from the PREVIOUS cycle, or the self-heal task-creation checks
    would find it already set and never create a fresh task for the
    re-run."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)

    next_execution = (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[2]).first()
    )
    next_execution.status = "pending"
    next_execution.task_creation_claimed_at = datetime.utcnow()
    db_session.commit()

    pm._start_next_phase(db_session, phase_ids[1])

    db_session.refresh(next_execution)
    assert next_execution.status == "in_progress"
    assert next_execution.task_creation_claimed_at is None


def test_mark_phase_complete_target_phase_id_matches_started_phase(
    db_session, db_manager, phase_ids
):
    """Regression: mark_phase_complete's returned target_phase_id (what
    orchestrator.py's dispatch uses to create the next Task) must name the
    SAME phase that was actually flipped to in_progress. Previously
    _advance_or_complete_with_phase_info recomputed the target via a
    separate, order-only lookup that didn't know about action_target_phase,
    so it could report a different phase than the one _start_next_phase
    actually started -- orchestrator would then dispatch a task for the
    wrong phase while the right one sat in_progress with no task."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)
    pm.workflow_id = db_session.query(Workflow).first().id

    target_phase_name = db_session.query(Phase).filter_by(id=phase_ids[3]).first().name

    task = Task(
        id=str(uuid.uuid4()),
        phase_id=phase_ids[0],
        workflow_id=pm.workflow_id,
        raw_description="Fix per qa_validation findings",
        done_definition="done",
        status="done",
        action="goto",
        action_target_phase=target_phase_name,
        completed_at=datetime.utcnow(),
    )
    db_session.add(task)
    db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).update(
        {"status": "in_progress"}
    )
    db_session.commit()

    result = pm.mark_phase_complete(
        phase_ids[0], "P1 done", phase_output={"score": 0.8}
    )

    assert result["target_phase_id"] == phase_ids[3]
    actually_started = (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[3]).first()
    )
    assert actually_started.status == "in_progress"


def test_start_next_phase_honors_action_target_phase_skipping_intermediates(
    db_session, db_manager, phase_ids
):
    """Regression: a task's own action_target_phase (recorded when it was
    created because an earlier phase goto'd back to it) must be honored on
    that task's own completion -- resuming directly at the recorded target,
    not by walking forward one intermediate phase at a time.

    E.g. qa_validation (phase_4) finds an issue and goto's back to
    development (phase_1) with action_target_phase="phase_4" -- once
    development's fix is done, the pipeline must jump straight back to
    phase_4, not re-run phase_2/phase_3 (architectural_review/
    adversarial_review) from scratch."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)
    pm.workflow_id = db_session.query(Workflow).first().id

    target_phase_name = db_session.query(Phase).filter_by(id=phase_ids[3]).first().name

    task = Task(
        id=str(uuid.uuid4()),
        phase_id=phase_ids[0],
        workflow_id=pm.workflow_id,
        raw_description="Fix per qa_validation findings",
        done_definition="done",
        status="done",
        action="goto",
        action_target_phase=target_phase_name,
        completed_at=datetime.utcnow(),
    )
    db_session.add(task)
    db_session.commit()

    next_phase = pm._start_next_phase(db_session, phase_ids[0])

    assert next_phase is not None
    assert next_phase.id == phase_ids[3]

    # Intermediate phases must not be STARTED -- but they are recorded as
    # "skipped" rather than left "pending". phase_manager marks them
    # deliberately ("skipped over by the jump ... instead of leaving it
    # 'pending' forever"): a PhaseExecution stuck at "pending" is never
    # completed by anything, so every downstream all-phases-done check waits
    # on it indefinitely. "skipped" is terminal and honest about what
    # happened; the requirement this test exists for is that they were not
    # started, which "skipped" satisfies.
    for pid in (phase_ids[1], phase_ids[2]):
        execution = db_session.query(PhaseExecution).filter_by(phase_id=pid).first()
        assert execution.status == "skipped"
        assert execution.started_at is None, "skipped-over phase must never start"

    target_execution = (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[3]).first()
    )
    assert target_execution.status == "in_progress"


def test_start_next_phase_ignores_goto_tag_from_a_prior_cycle(
    db_session, db_manager, phase_ids
):
    """Regression: a phase revisited via goto reuses the same phase_id
    across cycles. A synthetic completion (e.g. _cap_out_review_phase's
    review-run cap) closes the phase via mark_phase_complete WITHOUT ever
    creating a new Task row for this cycle -- so "the last done task" for
    that phase_id can be one from cycles ago, carrying a goto tag that has
    nothing to do with this pass. Observed live: qa_validation hit its
    review-run cap, scored a clean synthetic pass, but the only "done"
    task on record was a goto/development tag from 2 days earlier -- the
    pipeline resumed at development instead of advancing to the next
    phase by order, even though nothing THIS cycle sent it back."""
    from src.phases.phase_manager import PhaseManager

    pm = PhaseManager(db_manager)
    pm.workflow_id = db_session.query(Workflow).first().id

    target_phase_name = db_session.query(Phase).filter_by(id=phase_ids[3]).first().name

    # An old task from a PRIOR cycle, carrying a real goto tag.
    stale_task = Task(
        id=str(uuid.uuid4()),
        phase_id=phase_ids[0],
        workflow_id=pm.workflow_id,
        raw_description="Fix per qa_validation findings (old cycle)",
        done_definition="done",
        status="done",
        action="goto",
        action_target_phase=target_phase_name,
        created_at=datetime.utcnow() - timedelta(hours=48),
        completed_at=datetime.utcnow() - timedelta(hours=48),
    )
    db_session.add(stale_task)

    # This cycle's own execution started AFTER that stale task -- no new
    # task was created this time (a synthetic completion), so the stale
    # task must not be picked up as "this cycle's" goto tag.
    execution = (
        db_session.query(PhaseExecution).filter_by(phase_id=phase_ids[0]).first()
    )
    execution.started_at = datetime.utcnow() - timedelta(minutes=1)
    db_session.commit()

    next_phase = pm._start_next_phase(db_session, phase_ids[0])

    assert next_phase is not None
    assert next_phase.id == phase_ids[1]  # next by order, not phase_ids[3]
