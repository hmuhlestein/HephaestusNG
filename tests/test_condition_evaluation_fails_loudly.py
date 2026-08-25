"""A gate condition that cannot be evaluated must not wave the phase through.

SOLID review 2.9. _check_condition returned False for three different
"cannot evaluate" cases -- variable not present, value not numeric, malformed
condition string -- which is indistinguishable from "condition not met". The
caller loops over conditions, and when none matches it defaults to CONTINUE.
So an unevaluable gate advanced the phase it existed to hold back.

That is not hypothetical: every condition in every shipped workflow tests
`score`, and the engine only binds `score` when it is not None. A phase that
produced no score therefore had *all* of its gates fail open at once, exactly
when scoring had already gone wrong.

Raising alone was not enough. phase_manager.mark_phase_complete wraps the
call in `except Exception` and used to return
{"action": "continue", "should_continue": True} -- turning the new exception
straight back into the silent pass it was meant to prevent. That handler now
escalates to arbitration instead, so the decision is made by an arbiter
rather than guessed at on a session that just rolled back.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.workflow_engine.orchestrator import (
    ConditionEvaluationError,
    OrchestrationAction,
    WorkflowOrchestrator,
)


@pytest.fixture
def orchestrator():
    # The condition logic under test reads nothing off config; a minimal
    # instance keeps these focused on _check_condition itself.
    return WorkflowOrchestrator(MagicMock())


def _condition(expr="score < 0.5", action="goto", target="development"):
    return {"if": expr, "action": action, "target": target}


class TestUnevaluableConditionsRaise:
    def test_missing_variable_raises_instead_of_reading_as_false(self, orchestrator):
        """The live case: the phase produced no score, so `score` is unbound."""
        with pytest.raises(ConditionEvaluationError) as exc:
            orchestrator._check_condition(_condition(), score=None, metadata={}, phase_output={})
        assert "score" in str(exc.value)

    def test_non_numeric_value_raises(self, orchestrator):
        with pytest.raises(ConditionEvaluationError):
            orchestrator._check_condition(
                _condition(), score=None, metadata={"score": "not-a-number"}, phase_output={}
            )

    def test_malformed_condition_raises(self, orchestrator):
        with pytest.raises(ConditionEvaluationError):
            orchestrator._check_condition(
                {"if": "score is definitely fine"}, score=0.9, metadata={}, phase_output={}
            )


class TestGenuineComparisonsStillWork:
    """The change must not make ordinary evaluation noisier -- a condition
    that legitimately does not hold still returns False, quietly."""

    def test_true_condition_is_true(self, orchestrator):
        assert orchestrator._check_condition(
            _condition("score < 0.5"), score=0.2, metadata={}, phase_output={}
        )

    def test_false_condition_is_false_not_an_error(self, orchestrator):
        assert not orchestrator._check_condition(
            _condition("score < 0.5"), score=0.9, metadata={}, phase_output={}
        )

    def test_bare_booleans_still_work(self, orchestrator):
        assert orchestrator._check_condition({"if": "true"}, None, {}, {})
        assert not orchestrator._check_condition({"if": "false"}, None, {}, {})

    def test_variable_from_phase_output_is_usable(self, orchestrator):
        assert orchestrator._check_condition(
            _condition("blocker_count > 0"),
            score=None,
            metadata={},
            phase_output={"blocker_count": 3},
        )


class TestFailOpenIsClosed:
    def test_a_score_less_phase_no_longer_silently_continues(self, orchestrator):
        """End to end through evaluate(): the gate must not resolve to
        CONTINUE just because the score was missing."""
        conditions = [_condition("score < 0.5", "goto", "development")]

        with pytest.raises(ConditionEvaluationError):
            orchestrator._evaluate_conditions(
                conditions=conditions, score=None, metadata={}, phase_output={}
            )

    def test_a_matching_gate_still_routes_normally(self, orchestrator):
        result = orchestrator._evaluate_conditions(
            conditions=[_condition("score < 0.5", "goto", "development")],
            score=0.1,
            metadata={},
            phase_output={},
        )
        assert result.action is OrchestrationAction.GOTO
        assert result.target_phase == "development"


class TestMarkPhaseCompleteEscalates:
    """The other half: the exception must not be converted back into a pass."""

    def test_failure_escalates_to_arbitration_not_continue(self):
        from src.phases.phase_manager import PhaseManager

        phase = MagicMock()
        phase.name = "adversarial_review"
        phase.workflow_id = "wf-1"

        first_query = MagicMock()
        first_query.filter_by.return_value.first.return_value = phase

        session = MagicMock()
        # Let the phase load (so the handler's name is bound), then fail on
        # the next query -- standing in for anything that can go wrong after
        # that point, including ConditionEvaluationError out of evaluate().
        session.query.side_effect = [
            first_query,
            ConditionEvaluationError("score missing"),
        ]

        db_manager = MagicMock()
        db_manager.get_session.return_value = session

        manager = PhaseManager.__new__(PhaseManager)
        manager.db_manager = db_manager
        manager.workflow_id = "wf-1"

        result = manager.mark_phase_complete("phase-1", "done")

        assert result["action"] == "arbitrate", (
            "a failed phase completion must escalate, not advance the phase"
        )
        assert result["target_phase_id"] == "phase-1"
        assert result["target_phase"] == "adversarial_review"
        assert "score missing" in result["reason"]
        session.rollback.assert_called_once()

    def test_the_handler_does_not_raise_before_the_phase_is_loaded(self):
        """phase_name_for_error is read by the handler, so it must be bound
        even when the very first query is what failed."""
        from src.phases.phase_manager import PhaseManager

        db_manager = MagicMock()
        session = MagicMock()
        db_manager.get_session.return_value = session
        session.query.side_effect = RuntimeError("db gone")

        manager = PhaseManager.__new__(PhaseManager)
        manager.db_manager = db_manager
        manager.workflow_id = "wf-1"

        result = manager.mark_phase_complete("phase-1", "done")

        assert result["action"] == "arbitrate"
        assert result["target_phase"] is None


class TestOrchestratorLoadFailureDoesNotGoSequential:
    """_get_orchestrator returns None for three legitimate answers -- no
    workflow, no orchestrator_config, sequential mode -- and
    mark_phase_complete treats None as sequential, which advances the phase
    past every gate. It also used to return None from `except Exception`,
    so a transient DB error while loading the config silently bypassed all
    evaluation: the same fail-open as the unevaluable-condition bug above.
    """

    def _manager(self, session):
        from src.phases.phase_manager import PhaseManager

        db_manager = MagicMock()
        db_manager.get_session.return_value = session
        manager = PhaseManager.__new__(PhaseManager)
        manager.db_manager = db_manager
        manager.workflow_id = "wf-1"
        manager._orchestrators = {}
        return manager

    def test_load_failure_raises_instead_of_reading_as_sequential(self):
        session = MagicMock()
        session.query.side_effect = RuntimeError("db gone")
        manager = self._manager(session)

        with pytest.raises(RuntimeError):
            manager._get_orchestrator(session, "wf-1")

    def test_a_genuinely_absent_config_still_returns_none(self):
        """The legitimate answers must keep working -- this is not "raise on
        everything", it is "stop conflating failure with absence"."""
        session = MagicMock()
        session.query.return_value.filter_by.return_value.first.return_value = None
        manager = self._manager(session)

        assert manager._get_orchestrator(session, "wf-1") is None

    def test_the_failure_reaches_arbitration_rather_than_advancing(self):
        """End to end: a config-load failure during mark_phase_complete must
        escalate, not fall through to sequential mode and advance."""
        from src.phases.phase_manager import PhaseManager

        phase = MagicMock()
        phase.name = "adversarial_review"
        phase.workflow_id = "wf-1"

        first_query = MagicMock()
        first_query.filter_by.return_value.first.return_value = phase

        session = MagicMock()
        session.query.side_effect = [first_query, RuntimeError("db gone")]

        db_manager = MagicMock()
        db_manager.get_session.return_value = session
        manager = PhaseManager.__new__(PhaseManager)
        manager.db_manager = db_manager
        manager.workflow_id = "wf-1"
        manager._orchestrators = {}

        result = manager.mark_phase_complete("phase-1", "done")

        assert result["action"] == "arbitrate"
        assert result["action"] != "continue"


class TestUnresolvableGotoEscalates:
    """A goto whose target does not exist must not advance the phase.

    Both handlers used to log a warning and call _advance_or_complete -- the
    opposite of the decision just made. They now escalate to arbitration,
    which is capped; once the arbiter has had its retries and neither has a
    pending decision nor genuinely passes, _trigger_arbitration fails the
    workflow. So the sequence is arbitrate, retry, then fail -- never a
    silent advance.
    """

    def _fixture(self):
        from src.phases.phase_manager import PhaseManager

        phase = MagicMock()
        phase.name = "adversarial_review"
        phase.id = "phase-1"
        phase.workflow_id = "wf-1"

        session = MagicMock()
        manager = PhaseManager.__new__(PhaseManager)
        manager.db_manager = MagicMock()
        manager._orchestrators = {}
        return manager, session, phase, MagicMock()

    def test_gate_decided_goto_with_a_bad_target_escalates(self):
        manager, session, phase, execution = self._fixture()
        evaluation = MagicMock()
        evaluation.target_phase = "no_such_phase"

        with (
            patch.object(type(manager), "_close_execution", create=True),
            patch("src.phases.phase_manager._reopen_phase_execution") as reopen,
            patch.object(
                type(manager), "_find_phase_by_name_or_order", return_value=None, create=True
            ),
            patch.object(type(manager), "_advance_or_complete", create=True) as advance,
        ):
            result = manager._handle_evaluation_goto(
                session, phase, execution, "done", evaluation
            )

        assert result["action"] == "arbitrate"
        advance.assert_not_called(), "must not advance past the phase it was told to leave"
        reopen.assert_called_once()
        assert "no_such_phase" in result["reason"]

    def test_arbiter_decided_goto_with_a_bad_target_fails_terminally(self):
        """The arbiter's own resolution being unexecutable fails rather than
        escalating: it is already past the arbitrator, and
        _resolve_arbitration_outcome (its caller) dispatches only
        continue/goto/retry -- returning "arbitrate" there would strand the
        phase with no task and no arbitration in flight."""
        manager, session, phase, execution = self._fixture()

        with (
            patch.object(type(manager), "_close_execution", create=True) as close,
            patch.object(type(manager), "_fail_workflow", create=True) as fail_wf,
            patch("src.phases.phase_manager._reopen_phase_execution") as reopen,
            patch.object(
                type(manager), "_find_phase_by_name_or_order", return_value=None, create=True
            ),
            patch.object(type(manager), "_advance_or_complete", create=True) as advance,
        ):
            result = manager._handle_force_goto(
                session, phase, execution, "done", "renamed_away", "arbiter said so"
            )

        assert result["action"] == "fail"
        assert result["should_continue"] is False
        advance.assert_not_called()
        reopen.assert_not_called(), "a terminal failure must not reopen the phase"
        fail_wf.assert_called_once()
        assert close.call_args.args[2] == "failed"
        assert "renamed_away" in result["reason"]

    def test_the_escalated_execution_is_reopened_in_progress_not_pending(self):
        """_advance_phases picks the next pending phase after the latest
        COMPLETED one, so a phase left completed (or reopened as pending)
        while awaiting arbitration gets raced past."""
        manager, session, phase, execution = self._fixture()
        evaluation = MagicMock()
        evaluation.target_phase = "gone"

        with (
            patch.object(type(manager), "_close_execution", create=True),
            patch("src.phases.phase_manager._reopen_phase_execution") as reopen,
            patch.object(
                type(manager), "_find_phase_by_name_or_order", return_value=None, create=True
            ),
            patch.object(type(manager), "_advance_or_complete", create=True),
        ):
            manager._handle_evaluation_goto(
                session, phase, execution, "done", evaluation
            )

        assert reopen.call_args.kwargs["status"] == "in_progress"

    def test_a_resolvable_goto_is_untouched(self):
        """The escalation must only fire when the target genuinely does not
        resolve -- normal gotos keep working."""
        manager, session, phase, execution = self._fixture()
        target = MagicMock()
        target.name = "development"
        target.order = 3

        with (
            patch.object(type(manager), "_close_execution", create=True),
            patch.object(type(manager), "_fail_workflow", create=True) as fail_wf,
            patch("src.phases.phase_manager._reopen_phase_execution") as reopen,
            patch.object(
                type(manager), "_find_phase_by_name_or_order", return_value=target, create=True
            ),
            patch("src.phases.phase_manager._reset_stale_executions_on_goto", return_value=0),
            patch.object(type(manager), "_consume_gate_artifacts_for", create=True),
            patch.object(type(manager), "_start_phase", create=True),
        ):
            result = manager._handle_force_goto(
                session, phase, execution, "done", "development", "arbiter"
            )

        assert result["action"] not in ("arbitrate", "fail")
        reopen.assert_not_called()
        fail_wf.assert_not_called()


class TestEscalationReasonIsCarriedThrough:
    """The reason must survive to somewhere a human reads it.

    _escalate_unresolvable_goto's reason travels:
        result["reason"]
          -> _fire_phase_transition's `reason = result.get("reason") or ...`
          -> _trigger_arbitration(reason=...)
          -> on cap exhaustion, Workflow.status_reason
          -> /api/workflow-executions (status_reason) -> the dashboard.

    Without the first hop the chain silently falls back to the generic
    "exhausted its retry budget", and the operator never learns a goto
    target was misspelled.
    """

    def test_fire_phase_transition_forwards_the_reason_to_arbitration(self):
        # "development" deliberately: _fire_phase_transition runs a gate
        # pre-check (real DB + filesystem) for anything in GATED_PHASES
        # before reaching the action dispatch under test, so a gated phase
        # name here never gets far enough to call _trigger_arbitration.
        import src.autopilot.orchestrator.phase_transitions as pt

        captured = {}

        def fake_trigger(workflow_id, phase_id, phase_name, reason, logger):
            captured["reason"] = reason
            return True

        result = {
            "action": "arbitrate",
            "target_phase": "development",
            "target_phase_id": "phase-1",
            "should_continue": True,
            "reason": "gate evaluation chose goto 'no_such_phase', which names no phase",
        }

        pm = MagicMock()
        pm.mark_phase_complete.return_value = result

        with (
            patch.object(pt, "_trigger_arbitration", side_effect=fake_trigger),
            patch.object(pt, "PhaseManager", return_value=pm),
            patch.object(pt, "get_default_db_manager", MagicMock()),
        ):
            pt._fire_phase_transition("wf-1", "phase-1", "development", MagicMock())

        assert "no_such_phase" in captured.get("reason", ""), (
            "the specific reason must reach arbitration, not be replaced by the "
            "generic retry-budget message"
        )

    def test_the_generic_fallback_still_applies_when_no_reason_is_given(self):
        import src.autopilot.orchestrator.phase_transitions as pt

        captured = {}

        def fake_trigger(workflow_id, phase_id, phase_name, reason, logger):
            captured["reason"] = reason
            return True

        pm = MagicMock()
        pm.mark_phase_complete.return_value = {
            "action": "arbitrate",
            "target_phase": "development",
            "target_phase_id": "phase-1",
            "should_continue": True,
        }

        with (
            patch.object(pt, "_trigger_arbitration", side_effect=fake_trigger),
            patch.object(pt, "PhaseManager", return_value=pm),
            patch.object(pt, "get_default_db_manager", MagicMock()),
        ):
            pt._fire_phase_transition("wf-1", "phase-1", "development", MagicMock())

        assert captured.get("reason")
