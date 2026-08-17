"""Tests for fire_spec_gate_if_ready, relocated from
tests/test_task_completion_service.py per Phase 1b decomposition
(design_docs/phase_1b_decomposition.md section 4.4).
"""

import pytest
from unittest.mock import patch

from src.autopilot.orchestrator.phase_transitions import fire_spec_gate_if_ready


class TestFireSpecGateIfReadyGoto:
    """Regression: fire_spec_gate_if_ready's synchronous "gate fired from
    completion path" decides a GOTO (e.g. adversarial_review finding
    BLOCKER findings routes back to development) via mark_phase_complete,
    but mark_phase_complete only closes the CURRENT phase's execution and
    returns the decision -- creating the target phase's task was always a
    separate step (_fire_phase_transition's job, normally invoked by the
    background sweep). Since this synchronous path already closes the
    phase as "completed", the background sweep's _case_in_progress_complete
    never fires for it either (it only looks at "in_progress" phases) --
    so nothing ever created the goto task, and _case_completed_with_
    successor just marched forward to the next pending phase by order,
    silently skipping the goto target. Observed live: an adversarial_review
    gate found 4 BLOCKER findings and decided "GOTO development", but the
    pipeline proceeded straight to security_review with the blockers never
    addressed.
    """

    @pytest.fixture
    def gate_db(self, tmp_path, monkeypatch):
        from src.core.database import DatabaseManager

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        db = DatabaseManager(str(db_path))
        db.create_tables()
        return db

    def _seed(self, db, working_directory):
        from src.core.database import Phase, PhaseExecution, Task, Workflow

        with db.session_scope() as session:
            session.add(
                Workflow(
                    id="wf-1", name="t", phases_folder_path="/tmp",
                    working_directory=str(working_directory), status="active",
                )
            )
            session.add(
                Phase(
                    id="phase-adv", workflow_id="wf-1", order=6,
                    name="adversarial_review", description="d", done_definitions=["x"],
                )
            )
            session.add(
                Phase(
                    id="phase-dev", workflow_id="wf-1", order=4,
                    name="development", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-adv", phase_id="phase-adv", workflow_execution_id="wf-1",
                    status="in_progress",
                )
            )
            session.add(
                Task(
                    id="task-adv", raw_description="r", done_definition="d",
                    status="done", phase_id="phase-adv", workflow_id="wf-1",
                )
            )

    @pytest.mark.asyncio
    async def test_goto_creates_task_at_target_phase(self, gate_db, tmp_path):
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-adv").first()

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete",
                return_value={
                    "action": "goto",
                    "target_phase": "development",
                    "target_phase_id": "phase-dev",
                    "reason": "Runtime failure modes found, returning to development to fix",
                    "metadata": {
                        "spec_gate": {
                            "reason": "4 BLOCKER(s) found — returning to development"
                        }
                    },
                },
            ), patch(
                "src.autopilot.spec.GATED_PHASES", ("adversarial_review",)
            ), patch(
                "src.autopilot.spec.build_phase_output", return_value={"score": 0.4}
            ), patch(
                "src.autopilot.orchestrator.phase_transitions._create_phase_task"
            ) as mock_create_task:
                mock_create_task.return_value = True
                await fire_spec_gate_if_ready(session, task)

        mock_create_task.assert_called_once()
        args, kwargs = mock_create_task.call_args
        assert args[0] == "wf-1"
        assert args[1] == "phase-dev"
        assert args[2] == "development"
        assert args[3] == "goto"
        assert kwargs["feedback"] == "4 BLOCKER(s) found — returning to development"

    @pytest.mark.asyncio
    async def test_result_missing_prefers_completing_tasks_own_notes(self, gate_db, tmp_path):
        """Regression, observed live: a "result_missing" gate reason ("no
        adversarial_review_result.json found") only means build_phase_
        output's file read came up empty at this exact evaluation instant
        -- not that the agent didn't do the work. An adversarial_review
        agent's own completion_notes described 3 concrete BLOCKERs it had
        genuinely found, but the corrective development task's "WHY YOU'RE
        HERE" reason ended up as the generic missing-file message instead,
        because this path always preferred the gate's own reason
        unconditionally. The completing task's own completion_notes, when
        present, is a strictly more accurate signal and must win."""
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-adv").first()
            task.completion_notes = (
                "Adversarial review found 3 BLOCKERs: B-1 ..., B-2 ..., B-3 ..."
            )

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete",
                return_value={
                    "action": "goto",
                    "target_phase": "development",
                    "target_phase_id": "phase-dev",
                    "reason": "no adversarial_review_result.json found",
                    "metadata": {
                        "spec_gate": {
                            "reason": "no adversarial_review_result.json found",
                            "result_missing": True,
                        }
                    },
                },
            ), patch(
                "src.autopilot.spec.GATED_PHASES", ("adversarial_review",)
            ), patch(
                "src.autopilot.spec.build_phase_output", return_value={"score": 0.4}
            ), patch(
                "src.autopilot.orchestrator.phase_transitions._create_phase_task"
            ) as mock_create_task:
                mock_create_task.return_value = True
                await fire_spec_gate_if_ready(session, task)

        _, kwargs = mock_create_task.call_args
        assert kwargs["feedback"] == (
            "Adversarial review found 3 BLOCKERs: B-1 ..., B-2 ..., B-3 ..."
        )

    @pytest.mark.asyncio
    async def test_continue_does_not_create_a_task(self, gate_db, tmp_path):
        """The 'continue' branch must not be affected by this fix -- no
        target phase to create a task for."""
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-adv").first()

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete",
                return_value={"action": "continue"},
            ), patch(
                "src.autopilot.spec.GATED_PHASES", ("adversarial_review",)
            ), patch(
                "src.autopilot.spec.build_phase_output", return_value={"score": 0.9}
            ), patch(
                "src.autopilot.orchestrator.phase_transitions._create_phase_task"
            ) as mock_create_task:
                await fire_spec_gate_if_ready(session, task)

        mock_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_arbitrate_triggers_arbitration(self, gate_db, tmp_path):
        """Regression: this synchronous "gate fired from completion path"
        checked action in ("already_completed", "goto", "continue") and
        silently fell through for anything else -- "arbitrate" was never
        handled. mark_phase_complete's own evaluate() call already
        incremented total_gotos and logged the "[ARBITRATE] ... requesting
        LLM arbitration" warning as a side effect of merely being called,
        so every completion of a phase stuck needing arbitration re-hit
        this leak: total_gotos climbed and the warning re-logged, but
        _trigger_arbitration (the thing that actually spawns a capped
        arbitration agent, or fails the workflow past the cap) was never
        invoked. Observed live: 1100+ occurrences over ~30 hours on one
        workflow, zero arbitration tasks ever created."""
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-adv").first()

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete",
                return_value={
                    "action": "arbitrate",
                    "target_phase": "adversarial_review",
                    "target_phase_id": "phase-adv",
                    "reason": "GOTO limit exceeded (4/3), arbitration requested",
                },
            ), patch(
                "src.autopilot.spec.GATED_PHASES", ("adversarial_review",)
            ), patch(
                "src.autopilot.spec.build_phase_output", return_value={"score": 0.4}
            ), patch(
                "src.autopilot.orchestrator.phase_transitions._trigger_arbitration"
            ) as mock_arbitrate:
                mock_arbitrate.return_value = True
                await fire_spec_gate_if_ready(session, task)

        mock_arbitrate.assert_called_once()
        args, _ = mock_arbitrate.call_args
        assert args[0] == "wf-1"
        assert args[1] == "phase-adv"
        assert args[2] == "adversarial_review"
        assert "GOTO limit exceeded" in args[3]
