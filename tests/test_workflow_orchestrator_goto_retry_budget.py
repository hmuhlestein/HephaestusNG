"""Regression: a "goto" decision never counted toward an eval_point's
max_retries budget, only literal "retry" actions did. An eval_point whose
condition can only ever resolve to "goto" or "continue" (e.g. scope_review,
which always sends things back to product_requirements on a low score, never
"retries" itself) therefore had NO local budget at all -- the per-phase
max_retries/on_budget_exhausted=arbitrate gate could never fire for it,
no matter how many times the same two phases cycled on an identical
disagreement. The only thing that could ever stop it was the workflow-wide
max_total_gotos, shared across every phase pair in the whole pipeline and
far too coarse to catch two specific agents stuck disagreeing quickly.

Observed live: scope_review and product_requirements cycled on an identical
disagreement for 12+ rounds with total_gotos climbing toward the workflow-
wide budget, no local cap catching it sooner.

goto_counts_as_retry (opt-in per eval_point, default False) fixes this.
"""

from src.workflow_engine.orchestrator import (
    EvaluationPoint,
    OrchestrationAction,
    OrchestratorConfig,
    WorkflowOrchestrator,
)


def _scope_review_config(goto_counts_as_retry: bool, max_retries: int = 4) -> OrchestratorConfig:
    return OrchestratorConfig(
        type="evaluating",
        max_total_gotos=30,
        evaluation_points=[
            EvaluationPoint(
                after_phase="scope_review",
                evaluator="heuristic",
                max_retries=max_retries,
                on_budget_exhausted="arbitrate",
                goto_counts_as_retry=goto_counts_as_retry,
                conditions=[
                    {"if": "score < 0.5", "action": "goto", "target": "product_requirements", "reason": "drift"},
                    {"if": "score >= 0.5", "action": "continue", "reason": "ok"},
                ],
            )
        ],
    )


class TestGotoCountsAsRetryOptIn:
    def test_default_off_goto_never_touches_phase_retry_counts(self):
        """Sanity check the fix isn't overbroad: with the flag left at its
        default (False), repeated goto must behave exactly as before --
        phase_retry_counts stays untouched, and only the workflow-wide
        total_gotos budget (here effectively unlimited at 30) governs."""
        orchestrator = WorkflowOrchestrator(_scope_review_config(goto_counts_as_retry=False))

        for _ in range(10):
            result = orchestrator.evaluate(
                phase_name="scope_review",
                phase_output={"score": 0.2},
                phase_history=[],
            )
            assert result.action == OrchestrationAction.GOTO

        assert orchestrator.phase_retry_counts.get("scope_review", 0) == 0
        assert orchestrator.total_gotos == 10

    def test_enabled_arbitrates_after_max_retries_gotos(self):
        """With the flag on and max_retries=4, the 5th consecutive goto
        decision for the same phase must arbitrate instead of gotoing
        again -- independent of total_gotos, which is nowhere near its own
        (much larger) workflow-wide limit."""
        orchestrator = WorkflowOrchestrator(_scope_review_config(goto_counts_as_retry=True, max_retries=4))

        actions = []
        for _ in range(5):
            result = orchestrator.evaluate(
                phase_name="scope_review",
                phase_output={"score": 0.2},
                phase_history=[],
            )
            actions.append(result.action)

        assert actions == [
            OrchestrationAction.GOTO,
            OrchestrationAction.GOTO,
            OrchestrationAction.GOTO,
            OrchestrationAction.GOTO,
            OrchestrationAction.ARBITRATE,
        ]
        # total_gotos must still be tracked too (the workflow-wide signal
        # is independent of, not replaced by, the per-phase one) -- 4
        # real gotos landed before the 5th call short-circuited to
        # arbitrate without evaluating conditions again.
        assert orchestrator.total_gotos == 4

    def test_enabled_does_not_arbitrate_on_a_genuine_pass(self):
        """The cap must only fire from repeated goto/retry -- a phase that
        eventually passes must still continue normally, at any point in
        the cycle, not get stuck permanently primed to arbitrate."""
        orchestrator = WorkflowOrchestrator(_scope_review_config(goto_counts_as_retry=True, max_retries=4))

        for _ in range(2):
            result = orchestrator.evaluate(
                phase_name="scope_review",
                phase_output={"score": 0.2},
                phase_history=[],
            )
            assert result.action == OrchestrationAction.GOTO

        result = orchestrator.evaluate(
            phase_name="scope_review",
            phase_output={"score": 0.9},
            phase_history=[],
        )
        assert result.action == OrchestrationAction.CONTINUE


class TestOrchestratorConfigFromDictParsesGotoCountsAsRetry:
    def test_defaults_to_false_when_absent(self):
        config = OrchestratorConfig.from_dict({
            "type": "evaluating",
            "evaluation_points": [
                {"after_phase": "scope_review", "evaluator": "heuristic", "conditions": []},
            ],
        })
        assert config.evaluation_points[0].goto_counts_as_retry is False

    def test_reads_true_when_set(self):
        config = OrchestratorConfig.from_dict({
            "type": "evaluating",
            "evaluation_points": [
                {
                    "after_phase": "scope_review",
                    "evaluator": "heuristic",
                    "conditions": [],
                    "goto_counts_as_retry": True,
                },
            ],
        })
        assert config.evaluation_points[0].goto_counts_as_retry is True
