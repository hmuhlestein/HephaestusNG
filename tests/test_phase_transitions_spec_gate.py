"""Tests for fire_spec_gate_if_ready, relocated from
tests/test_task_completion_service.py per Phase 1b decomposition
(design_docs/phase_1b_decomposition.md section 4.4).
"""

from unittest.mock import patch

import pytest

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
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases", lambda: ("adversarial_review",)
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
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases", lambda: ("adversarial_review",)
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
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases", lambda: ("adversarial_review",)
            ), patch(
                "src.autopilot.spec.build_phase_output", return_value={"score": 0.9}
            ), patch(
                "src.autopilot.orchestrator.phase_transitions._create_phase_task"
            ) as mock_create_task:
                await fire_spec_gate_if_ready(session, task)

        mock_create_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_mark_phase_complete_is_offloaded_to_executor(self, gate_db, tmp_path):
        """Regression: mark_phase_complete can itself run an LLM evaluate()
        call and, on completing the whole workflow, cascade into
        _populate_feature_folder's recursive filesystem copies -- both
        blocking, called directly (unoffloaded) right after the already-
        offloaded build_phase_output call. Must go through run_in_executor
        like its neighbor."""
        from unittest.mock import AsyncMock, MagicMock

        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-adv").first()

            fake_loop = MagicMock()
            fake_loop.run_in_executor = AsyncMock(return_value={"action": "continue"})

            with patch(
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases", lambda: ("adversarial_review",)
            ), patch(
                "src.autopilot.spec.build_phase_output", return_value={"score": 0.9}
            ), patch(
                "asyncio.get_event_loop", return_value=fake_loop
            ):
                await fire_spec_gate_if_ready(session, task)

        # First run_in_executor call is build_phase_output (pre-existing);
        # the second must be mark_phase_complete.
        assert fake_loop.run_in_executor.call_count == 2
        second_call_args = fake_loop.run_in_executor.call_args_list[1].args
        executor_arg, func_arg = second_call_args[0], second_call_args[1]
        assert executor_arg is None
        assert func_arg.func.__name__ == "mark_phase_complete"
        assert func_arg.args[0] == "phase-adv"

    @pytest.mark.asyncio
    async def test_arbitration_task_completion_resolves_decision_not_the_gate(
        self, gate_db, tmp_path
    ):
        """Regression, observed live (workflow ca539a75): each arbitration
        task's OWN completion re-fired the generic phase gate here, which
        re-evaluated the phase against stale/consumed artifacts (score 0.4,
        "no challenge.md found" -- consume_gate_artifacts deletes them after
        every goto), hit the already-exhausted retry budget, and dispatched
        yet another arbitration agent to re-answer the question the
        completing one had JUST answered "continue" in
        arbitration_result.json: 3 consecutive arbitrations independently
        re-verifying the same already-fixed architecture.md, each spawning
        the next. An arbitration task completing must route to
        _maybe_resolve_arbitration (which acts on and consumes the
        decision) instead of re-running the phase gate."""
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-adv").first()
            task.created_by_agent_id = "arbitration"

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete"
            ) as mock_mark_complete, patch(
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases", lambda: ("adversarial_review",)
            ), patch(
                "src.autopilot.spec.build_phase_output"
            ) as mock_build_output, patch(
                "src.autopilot.orchestrator.phase_transitions._maybe_resolve_arbitration"
            ) as mock_resolve:
                await fire_spec_gate_if_ready(session, task)

        mock_resolve.assert_called_once()
        args, _ = mock_resolve.call_args
        assert args[0] == "wf-1"
        mock_mark_complete.assert_not_called()
        mock_build_output.assert_not_called()

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
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases", lambda: ("adversarial_review",)
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

    @pytest.mark.asyncio
    async def test_goto_never_resets_its_own_firing_phase_execution(self, gate_db, tmp_path):
        """Characterization: a goto must not re-reset the firing phase's
        own just-closed PhaseExecution row. mark_phase_complete closes it
        "completed" before returning -- the stale-execution reset must
        exclude it or the idempotency guard ("if execution.status ==
        completed: skip") is defeated on the next evaluation."""
        from src.autopilot.orchestrator.phase_transitions import reset_stale_executions_on_goto
        from src.core.database import PhaseExecution

        self._seed(gate_db, tmp_path)
        # Seed's PhaseExecution for phase-adv is "in_progress"; set it
        # to "completed" to simulate mark_phase_complete having just
        # closed it. Add a "completed" execution for the target phase.
        with gate_db.session_scope() as session:
            adv_exec = session.query(PhaseExecution).filter_by(phase_id="phase-adv").first()
            adv_exec.status = "completed"
            session.add(PhaseExecution(
                id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
                status="completed",
            ))

        # Fire the goto-reset targeting development (order 4).
        with gate_db.session_scope() as session:
            n = reset_stale_executions_on_goto(
                session, "wf-1", 4, exclude_phase_id="phase-adv",
            )

        # The firing phase's execution must still be "completed" --
        # the goto-reset must not have touched it.
        with gate_db.session_scope() as session:
            adv_exec = session.query(PhaseExecution).filter_by(phase_id="phase-adv").first()
            assert adv_exec.status == "completed"
            # The target phase's execution must be reset to "pending".
            dev_exec = session.query(PhaseExecution).filter_by(phase_id="phase-dev").first()
            assert dev_exec.status == "pending"

    @pytest.mark.asyncio
    async def test_goto_resets_phase_at_target_order(self, gate_db, tmp_path):
        """Characterization: a goto resets phases at OR after the target
        order, not just strictly between target and source. A phase at
        the target's own order (the target itself) must be reset if its
        execution was "completed" from a prior pass."""
        from src.autopilot.orchestrator.phase_transitions import reset_stale_executions_on_goto
        from src.core.database import PhaseExecution

        self._seed(gate_db, tmp_path)
        with gate_db.session_scope() as session:
            adv_exec = session.query(PhaseExecution).filter_by(phase_id="phase-adv").first()
            adv_exec.status = "completed"
            session.add(PhaseExecution(
                id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
                status="completed",
            ))

        # Fire the goto-reset targeting development (order 4).
        with gate_db.session_scope() as session:
            n = reset_stale_executions_on_goto(
                session, "wf-1", 4, exclude_phase_id="phase-adv",
            )

        # The target phase's execution must be reset to "pending".
        with gate_db.session_scope() as session:
            dev_exec = session.query(PhaseExecution).filter_by(phase_id="phase-dev").first()
            assert dev_exec.status == "pending"
            assert n >= 1

    @pytest.mark.asyncio
    async def test_goto_reset_does_not_clobber_a_phase_with_a_live_task(self, gate_db, tmp_path):
        """Root-cause regression: a REDUNDANT goto evaluation of the same
        already-handled completion (mark_phase_complete entered twice for
        one task completion) must not wipe a downstream phase's
        PhaseExecution while a real task is actively in_progress under it.

        Observed live: development had a task genuinely in_progress
        (dispatched, doing real work) when a second, redundant "goto
        development" from adversarial_review reset development's
        PhaseExecution.started_at to None mid-flight. started_at was later
        re-derived from an unrelated task, permanently excluding the real
        (later-completing) task from the cycle-scoped completion check --
        stalling the phase forever, since the pipeline never advanced past
        development even after that real task finished.
        """
        from src.autopilot.orchestrator.phase_transitions import reset_stale_executions_on_goto
        from src.core.database import PhaseExecution, Task

        self._seed(gate_db, tmp_path)
        with gate_db.session_scope() as session:
            adv_exec = session.query(PhaseExecution).filter_by(phase_id="phase-adv").first()
            adv_exec.status = "completed"
            session.add(PhaseExecution(
                id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
                status="in_progress",
            ))
            # A real task actively being worked on in development right now.
            session.add(
                Task(
                    id="task-dev-live", raw_description="r", done_definition="d",
                    status="in_progress", phase_id="phase-dev", workflow_id="wf-1",
                )
            )

        # A redundant goto-reset re-fires targeting development (order 4).
        with gate_db.session_scope() as session:
            n = reset_stale_executions_on_goto(
                session, "wf-1", 4, exclude_phase_id="phase-adv",
            )

        # development's execution must be left untouched -- it has a live
        # task, so it is not stale and must not be reset.
        with gate_db.session_scope() as session:
            dev_exec = session.query(PhaseExecution).filter_by(phase_id="phase-dev").first()
            assert dev_exec.status == "in_progress"
        assert n == 0

    @pytest.mark.asyncio
    async def test_goto_terminates_a_live_agent_in_a_strictly_later_phase(self, gate_db, tmp_path):
        """A goto rewinding past a still-running later phase must kill that
        phase's agent (it is validating/reviewing code about to be
        rewritten, and shares the feature worktree with the incoming
        target-phase agent) -- unlike the target phase's own live task,
        which is left alone by the test above.

        Observed live: a qa_validation agent (order 9) ran concurrently
        with a development agent (order 5) for ~an hour on workflow
        72ed4df8 after a goto-to-development.
        """
        from src.autopilot.orchestrator.phase_transitions import reset_stale_executions_on_goto
        from src.core.database import Agent, Phase, PhaseExecution, Task

        self._seed(gate_db, tmp_path)
        with gate_db.session_scope() as session:
            # Source phase (adversarial_review, order 6) just fired the goto.
            session.query(PhaseExecution).filter_by(phase_id="phase-adv").first().status = "completed"
            # A strictly-later phase than the target: qa_validation, order 9.
            session.add(Phase(
                id="phase-qa", workflow_id="wf-1", order=9,
                name="qa_validation", description="d", done_definitions=["x"],
            ))
            session.add(PhaseExecution(
                id="exec-qa", phase_id="phase-qa", workflow_execution_id="wf-1",
                status="in_progress",
            ))
            session.add(Agent(
                id="agent-qa", system_prompt="t", status="working",
                cli_type="pi", current_task_id="task-qa-live",
            ))
            session.add(Task(
                id="task-qa-live", raw_description="r", done_definition="d",
                status="in_progress", phase_id="phase-qa", workflow_id="wf-1",
                assigned_agent_id="agent-qa",
            ))

        # Goto from adversarial_review (order 6) back to development (order 4).
        with gate_db.session_scope() as session:
            reset_stale_executions_on_goto(
                session, "wf-1", 4, exclude_phase_id="phase-adv",
            )

        with gate_db.session_scope() as session:
            agent = session.query(Agent).filter_by(id="agent-qa").first()
            assert agent.status == "terminated"
            assert agent.current_task_id is None
            qa_task = session.query(Task).filter_by(id="task-qa-live").first()
            assert qa_task.status == "pending"
            assert qa_task.assigned_agent_id is None
            qa_exec = session.query(PhaseExecution).filter_by(phase_id="phase-qa").first()
            assert qa_exec.status == "pending"


class TestUngatedPhaseAdvancesFromCompletion:
    """Regression: an ungated phase had no synchronous advancement at all.

    fire_spec_gate_if_ready returned immediately for any phase not in
    get_gated_phases(), so a phase like development -- which has no gate
    artifact to score -- could only ever be advanced by the background
    sweep. That sweep filters workflows to projects with is_active=True
    (to stop stale, constantly-retrying workflows from starving the
    projects in use), so a live pipeline whose project did not hold one of
    the max_concurrent_projects slots simply stopped.

    Observed live, workflow 72ed4df8: development completed at 00:25 with
    its work committed, ParentChat was not one of the two active projects,
    and the pipeline sat there for 8h52m. It advanced 8 seconds after the
    project was activated, on the very next sweep tick. Gated phases kept
    moving that whole time -- each one's completion dispatches the next
    through this path -- which is why the stall always landed on
    development.
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
                    id="phase-dev", workflow_id="wf-1", order=5,
                    name="development", description="d", done_definitions=["x"],
                )
            )
            session.add(
                Phase(
                    id="phase-adv", workflow_id="wf-1", order=6,
                    name="adversarial_review", description="d", done_definitions=["x"],
                )
            )
            session.add(
                PhaseExecution(
                    id="exec-dev", phase_id="phase-dev", workflow_execution_id="wf-1",
                    status="in_progress",
                )
            )
            session.add(
                Task(
                    id="task-dev", raw_description="r", done_definition="d",
                    status="done", phase_id="phase-dev", workflow_id="wf-1",
                )
            )

    @pytest.mark.asyncio
    async def test_an_ungated_phase_advances_when_its_last_task_completes(
        self, gate_db, tmp_path
    ):
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-dev").first()

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete",
                return_value={
                    "action": "goto",
                    "target_phase": "adversarial_review",
                    "target_phase_id": "phase-adv",
                    "reason": "back to review",
                },
            ) as mark_complete, patch(
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases",
                lambda: ("adversarial_review",),
            ), patch(
                "src.autopilot.orchestrator.phase_transitions._create_phase_task"
            ) as mock_create_task:
                mock_create_task.return_value = True
                await fire_spec_gate_if_ready(session, task)

        mark_complete.assert_called_once()
        mock_create_task.assert_called_once()
        assert mock_create_task.call_args[0][2] == "adversarial_review"

    @pytest.mark.asyncio
    async def test_an_ungated_phase_does_not_pay_for_gate_artifacts(
        self, gate_db, tmp_path
    ):
        """build_phase_output can run pytest for minutes. It returns {} for a
        phase with no gate, so the result cannot change -- don't pay for it."""
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            task = session.query(Task).filter_by(id="task-dev").first()

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete",
                return_value={"action": "continue"},
            ) as mark_complete, patch(
                "src.autopilot.orchestrator.phase_transitions.get_gated_phases",
                lambda: ("adversarial_review",),
            ), patch(
                "src.autopilot.spec.build_phase_output"
            ) as build_output, patch(
                "src.autopilot.orchestrator.phase_transitions._create_phase_task"
            ):
                await fire_spec_gate_if_ready(session, task)

        build_output.assert_not_called()
        assert mark_complete.call_args.kwargs["phase_output"] == {}

    @pytest.mark.asyncio
    async def test_a_phase_with_work_still_outstanding_is_left_alone(
        self, gate_db, tmp_path
    ):
        """Unchanged by the ungated path: the phase only fires once every one
        of its tasks is finished."""
        self._seed(gate_db, tmp_path)

        with gate_db.session_scope() as session:
            from src.core.database import Task

            session.add(
                Task(
                    id="task-dev-2", raw_description="r", done_definition="d",
                    status="in_progress", phase_id="phase-dev", workflow_id="wf-1",
                )
            )
            session.flush()
            task = session.query(Task).filter_by(id="task-dev").first()

            with patch(
                "src.phases.phase_manager.PhaseManager.mark_phase_complete"
            ) as mark_complete:
                await fire_spec_gate_if_ready(session, task)

        mark_complete.assert_not_called()
