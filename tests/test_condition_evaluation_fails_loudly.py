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

from unittest.mock import MagicMock

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
