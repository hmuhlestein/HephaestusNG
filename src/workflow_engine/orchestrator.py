"""
Workflow Orchestrator Engine

Evaluates phase outputs and decides flow control:
- Continue to next phase
- Retry current phase
- Goto a specific phase
- Fail the workflow

Configurable per workflow via orchestrator_config.
"""

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Grammar for condition strings like "score < 0.6": a variable name, a
# comparison operator, and a numeric threshold. Exported so
# config_validator.py can validate condition["if"] strings against the same
# grammar this module actually evaluates them with, instead of the two files
# silently drifting out of sync (SOLID review 2.9/2.10).
CONDITION_PATTERN = r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*(<=|>=|==|!=|<|>)\s*([0-9.]+)$"

_EPSILON = 0.0001

CONDITION_OPERATORS: Dict[str, Any] = {
    "<": lambda value, threshold: value < threshold,
    "<=": lambda value, threshold: value <= threshold,
    ">": lambda value, threshold: value > threshold,
    ">=": lambda value, threshold: value >= threshold,
    "==": lambda value, threshold: abs(value - threshold) < _EPSILON,
    "!=": lambda value, threshold: abs(value - threshold) >= _EPSILON,
}


def is_valid_condition_string(condition_str: str) -> bool:
    """True if condition_str is either a bare boolean or matches CONDITION_PATTERN.

    Shared by _check_condition (evaluation) and config_validator.py
    (startup-time validation) so both agree on what a valid condition looks
    like.
    """
    import re

    stripped = condition_str.strip()
    if stripped in ("true", "false"):
        return True
    return re.match(CONDITION_PATTERN, stripped) is not None


class OrchestrationAction(Enum):
    CONTINUE = "continue"  # Move to next phase
    RETRY = "retry"  # Retry current phase
    GOTO = "goto"  # Jump to a specific phase
    FAIL = "fail"  # Fail the workflow
    SKIP = "skip"  # Skip to next phase (same as continue but logged differently)
    ARBITRATE = (
        "arbitrate"  # Budget exhausted — spawn LLM arbitration agent before deciding
    )


@dataclass
class EvaluationResult:
    """Result of evaluating a phase output."""

    action: OrchestrationAction
    target_phase: Optional[str] = None  # Phase name or order for GOTO
    reason: str = ""
    score: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationPoint:
    """Configuration for evaluating after a specific phase."""

    after_phase: str  # Phase name or order
    evaluator: str  # Evaluator function name or "llm"
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    max_retries: int = 2
    timeout_seconds: int = 300
    on_budget_exhausted: str = "continue"  # "continue" or "arbitrate"
    # Opt-in cap on how many times THIS phase itself may re-run (distinct
    # from max_retries, which bounds the GOTO TARGET's retries, e.g.
    # development). None = uncapped (default for every phase that doesn't
    # set it). See _get_max_review_runs/_create_phase_task's cap-enforcement
    # block in src/autopilot/orchestrator.py.
    max_review_runs: Optional[int] = None


@dataclass
class OrchestratorConfig:
    """Configuration for workflow orchestration."""

    type: str = "sequential"  # "sequential" or "evaluating"
    evaluation_points: List[EvaluationPoint] = field(default_factory=list)
    max_phase_retries: int = 2
    max_total_gotos: int = 10  # Safety limit on total GOTO operations per workflow
    fail_on_stuck: bool = True
    stuck_timeout_seconds: int = 1800  # 30 minutes

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrchestratorConfig":
        """Create config from dictionary (e.g., from JSON)."""
        if not data:
            return cls()

        eval_points = []
        for ep in data.get("evaluation_points", []):
            eval_points.append(
                EvaluationPoint(
                    after_phase=ep.get("after_phase", ""),
                    evaluator=ep.get("evaluator", "llm"),
                    conditions=ep.get("conditions", []),
                    max_retries=ep.get("max_retries", 2),
                    timeout_seconds=ep.get("timeout_seconds", 300),
                    on_budget_exhausted=ep.get("on_budget_exhausted", "continue"),
                    max_review_runs=ep.get("max_review_runs"),
                )
            )

        return cls(
            type=data.get("type", "sequential"),
            evaluation_points=eval_points,
            max_phase_retries=data.get("max_phase_retries", 2),
            max_total_gotos=data.get("max_total_gotos", 10),
            fail_on_stuck=data.get("fail_on_stuck", True),
            stuck_timeout_seconds=data.get("stuck_timeout_seconds", 1800),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "evaluation_points": [
                {
                    "after_phase": ep.after_phase,
                    "evaluator": ep.evaluator,
                    "conditions": ep.conditions,
                    "max_retries": ep.max_retries,
                    "timeout_seconds": ep.timeout_seconds,
                    "max_review_runs": ep.max_review_runs,
                }
                for ep in self.evaluation_points
            ],
            "max_phase_retries": self.max_phase_retries,
            "max_total_gotos": self.max_total_gotos,
            "fail_on_stuck": self.fail_on_stuck,
            "stuck_timeout_seconds": self.stuck_timeout_seconds,
        }


class WorkflowOrchestrator:
    """
    Evaluates phase outputs and decides flow control.

    Usage:
        config = OrchestratorConfig.from_dict(workflow.orchestrator_config)
        orchestrator = WorkflowOrchestrator(config)

        # After phase completes
        result = orchestrator.evaluate(
            phase_name="architecture_design",
            phase_output={"score": 0.8, "tests_passed": True},
            phase_history=[{"phase": "product_requirements", "status": "completed"}]
        )

        if result.action == OrchestrationAction.GOTO:
            # Jump to specified phase
            goto_phase(result.target_phase)
    """

    # Best-effort fallback for _phase_name_to_order when no phase_order_map
    # was provided (see __init__) -- the autopilot pipeline's phase names,
    # matching each phase's own `id:` field in config/workflows/autopilot/
    # (NOT workflow.yaml's session_roles dict order, which lists
    # git_commit_push before forensics_analysis but isn't itself load-
    # bearing for execution order -- a prior fix trusted that ordering and
    # got this pair backwards).
    _LEGACY_NAME_TO_ORDER: Dict[str, int] = {
        "product_requirements": 1,
        "scope_review": 2,
        "architecture_design": 3,
        "architecture": 3,
        "development": 4,
        "architectural_review": 5,
        "adversarial_review": 6,
        "security_review": 7,
        "qa_validation": 8,
        "product_validation": 9,
        "doc_review": 10,
        "forensics_analysis": 11,
        "git_commit_push": 12,
        "deploy": 13,
        "tech_debt_requirements": 14,
    }

    def __init__(
        self,
        config: OrchestratorConfig,
        phase_order_map: Optional[Dict[str, int]] = None,
    ):
        self.config = config
        self.phase_retry_counts: Dict[str, int] = {}
        self.total_gotos: int = 0  # Track total GOTO operations
        self.evaluation_history: List[Dict[str, Any]] = []
        # Phase.name -> Phase.order for the actual workflow this orchestrator
        # was built for (see PhaseManager._get_orchestrator) -- the
        # authoritative source for _phase_name_to_order. Without this, the
        # only option was a hand-maintained dict of one caller's phase
        # vocabulary baked into this supposedly generic, config-driven
        # engine (SOLID review 2.11) -- which is exactly how it drifted out
        # of sync with the real phase ids in the first place. Falls back to
        # _LEGACY_NAME_TO_ORDER only when unavailable (e.g. a orchestrator
        # constructed directly, without going through PhaseManager).
        self.phase_order_map: Dict[str, int] = phase_order_map or {}

    def evaluate(
        self,
        phase_name: str,
        phase_output: Dict[str, Any],
        phase_history: List[Dict[str, Any]],
        llm_evaluator=None,
    ) -> EvaluationResult:
        """
        Evaluate a completed phase and decide what to do next.

        Args:
            phase_name: Name of the completed phase
            phase_output: Output from the phase
            phase_history: History of all phases run so far
            llm_evaluator: Optional LLM function for evaluation

        Returns:
            EvaluationResult with action to take
        """
        # If sequential mode, always continue
        if self.config.type == "sequential":
            return EvaluationResult(
                action=OrchestrationAction.CONTINUE,
                reason="Sequential mode - always continue",
            )

        # Find evaluation point for this phase
        eval_point = self._find_evaluation_point(phase_name)
        if not eval_point:
            return EvaluationResult(
                action=OrchestrationAction.CONTINUE,
                reason=f"No evaluation point configured for {phase_name}",
            )

        # Check retry count
        current_retries = self.phase_retry_counts.get(phase_name, 0)
        if current_retries >= eval_point.max_retries:
            logger.info(
                f"Max retries ({eval_point.max_retries}) reached for {phase_name}"
            )
            if eval_point.on_budget_exhausted == "arbitrate":
                logger.warning(
                    f"[ARBITRATE] Budget exhausted for {phase_name} after {current_retries} retries — "
                    f"requesting LLM arbitration instead of forcing continue"
                )
                return EvaluationResult(
                    action=OrchestrationAction.ARBITRATE,
                    reason=f"Retry budget exhausted ({current_retries}/{eval_point.max_retries}) for {phase_name}; arbitration requested",
                    metadata={
                        "phase": phase_name,
                        "retries": current_retries,
                        "max_retries": eval_point.max_retries,
                    },
                )
            return EvaluationResult(
                action=OrchestrationAction.CONTINUE,
                reason=f"Max retries reached for {phase_name}, continuing",
            )

        # Evaluate using configured evaluator
        score = None
        evaluation_metadata = {}

        if eval_point.evaluator == "llm" and llm_evaluator:
            score, evaluation_metadata = llm_evaluator(
                phase_name=phase_name,
                phase_output=phase_output,
                conditions=eval_point.conditions,
            )
        elif eval_point.evaluator == "heuristic":
            score, evaluation_metadata = self._heuristic_evaluate(
                phase_name, phase_output, eval_point.conditions
            )
        else:
            # Default: check phase_output for common success indicators
            score, evaluation_metadata = self._default_evaluate(phase_output)

        # Pass through spec_gate from phase_output so feedback reasons
        # survive to the task description on goto/retry
        if "spec_gate" in phase_output and "spec_gate" not in evaluation_metadata:
            evaluation_metadata["spec_gate"] = phase_output["spec_gate"]

        # Evaluate conditions
        action = self._evaluate_conditions(
            eval_point.conditions, score, evaluation_metadata, phase_output
        )

        # Track retries
        if action.action == OrchestrationAction.RETRY:
            self.phase_retry_counts[phase_name] = current_retries + 1
            action.metadata["retry_count"] = self.phase_retry_counts[phase_name]
            action.metadata["max_retries"] = eval_point.max_retries

        # Track and limit GOTOs
        if action.action == OrchestrationAction.GOTO:
            self.total_gotos += 1
            action.metadata["total_gotos"] = self.total_gotos
            action.metadata["max_total_gotos"] = self.config.max_total_gotos

            # Check if we've exceeded the GOTO limit
            if self.total_gotos > self.config.max_total_gotos:
                if eval_point and eval_point.on_budget_exhausted == "arbitrate":
                    logger.warning(
                        f"[ARBITRATE] Total GOTO limit exceeded ({self.total_gotos}/{self.config.max_total_gotos}) "
                        f"for {phase_name} — requesting LLM arbitration."
                    )
                    action = EvaluationResult(
                        action=OrchestrationAction.ARBITRATE,
                        reason=f"GOTO limit exceeded ({self.total_gotos}/{self.config.max_total_gotos}), arbitration requested",
                        score=score,
                        metadata={**action.metadata, "phase": phase_name},
                    )
                else:
                    logger.warning(
                        f"GOTO limit exceeded ({self.total_gotos}/{self.config.max_total_gotos}). "
                        f"Forcing continue to prevent infinite loop."
                    )
                    action = EvaluationResult(
                        action=OrchestrationAction.CONTINUE,
                        reason=f"GOTO limit exceeded ({self.total_gotos}/{self.config.max_total_gotos}), forcing continue",
                        score=score,
                        metadata=action.metadata,
                    )

        # Log evaluation
        self.evaluation_history.append(
            {
                "phase": phase_name,
                "score": score,
                "action": action.action.value,
                "reason": action.reason,
                "metadata": evaluation_metadata,
            }
        )

        logger.info(
            f"Orchestrator evaluation for {phase_name}: "
            f"action={action.action.value}, score={score}, reason={action.reason}, "
            f"gotos={self.total_gotos}/{self.config.max_total_gotos}"
        )

        return action

    def _find_evaluation_point(self, phase_name: str) -> Optional[EvaluationPoint]:
        """Find evaluation point for a phase (by name or order)."""
        for ep in self.config.evaluation_points:
            if ep.after_phase == phase_name:
                return ep
            # Also match by order (e.g., "1" matches first phase)
            try:
                if int(ep.after_phase) == self._phase_name_to_order(phase_name):
                    return ep
            except (ValueError, TypeError):
                pass
        return None

    def _phase_name_to_order(self, phase_name: str) -> int:
        """Convert phase name to order number.

        Prefers self.phase_order_map (the real Phase.order values for the
        workflow this orchestrator was built for -- see __init__). Falls
        back to _LEGACY_NAME_TO_ORDER, a best-effort guess based on the
        autopilot pipeline's phase names, only when phase_order_map wasn't
        provided. Returns 0 if unable to determine order either way.
        """
        if phase_name in self.phase_order_map:
            return self.phase_order_map[phase_name]

        name_to_order = self._LEGACY_NAME_TO_ORDER

        # Try exact match
        if phase_name in name_to_order:
            return name_to_order[phase_name]

        # Try partial match
        for key, order in name_to_order.items():
            if key in phase_name or phase_name in key:
                return order

        return 0

    def _heuristic_evaluate(
        self,
        phase_name: str,
        phase_output: Dict[str, Any],
        conditions: List[Dict[str, Any]],
    ) -> Tuple[float, Dict[str, Any]]:
        """Simple heuristic evaluation based on output content."""
        # Start at 0.75 (passing), not 0.5 (neutral). At 0.5 every non-gated
        # phase with empty phase_output={} tripped the score<0.6 retry band and
        # burned max_retries re-runs before continuing — same issue that was
        # fixed in _default_evaluate. Only drop below the retry threshold when
        # there are explicit failure signals.
        score = 0.75
        metadata = {}

        # Check for common success indicators
        output_str = json.dumps(phase_output).lower()

        # Success indicators
        success_keywords = ["passed", "success", "completed", "valid", "approved"]
        failure_keywords = ["failed", "error", "invalid", "rejected", "issues"]

        success_count = sum(1 for kw in success_keywords if kw in output_str)
        failure_count = sum(1 for kw in failure_keywords if kw in output_str)

        if success_count + failure_count > 0:
            score = success_count / (success_count + failure_count)

        # Check for test results
        if "tests_passed" in phase_output:
            if phase_output["tests_passed"]:
                score = max(score, 0.8)
            else:
                score = min(score, 0.3)

        # Check for explicit score
        if "score" in phase_output:
            score = float(phase_output["score"])

        metadata["success_keywords"] = success_count
        metadata["failure_keywords"] = failure_count

        return score, metadata

    def _default_evaluate(
        self, phase_output: Dict[str, Any]
    ) -> Tuple[float, Dict[str, Any]]:
        """Default evaluation - check for basic success/failure.

        Reaching evaluation means the phase's tasks COMPLETED. With no explicit
        failure/error signal and no real evaluator, that's a pass — so the baseline is
        a passing score, not 0.5. (At 0.5 every non-gated phase tripped the
        `score < 0.6 -> retry` band and burned max_retries re-runs before continuing.)
        Real failures still drop it: status=failed -> 0.1 (goto), errors -> -0.3
        (which lands in the retry band), and gated phases supply a real score that
        overrides this default.
        """
        score = 0.75  # completed, no failure signal => pass
        metadata = {}

        # Check if phase reported success
        if phase_output.get("status") == "completed":
            score = 0.9
        elif phase_output.get("status") == "failed":
            score = 0.1

        # Check for errors
        if phase_output.get("errors"):
            score = max(0.1, score - 0.3)

        return score, metadata

    def _evaluate_conditions(
        self,
        conditions: List[Dict[str, Any]],
        score: Optional[float],
        metadata: Dict[str, Any],
        phase_output: Dict[str, Any],
    ) -> EvaluationResult:
        """Evaluate conditions and return action."""
        if not conditions:
            # No conditions - default to continue
            return EvaluationResult(
                action=OrchestrationAction.CONTINUE,
                score=score,
                reason="No conditions configured",
            )

        for condition in conditions:
            if self._check_condition(condition, score, metadata, phase_output):
                action_str = condition.get("action", "continue")
                target = condition.get("target")
                reason = condition.get(
                    "reason", f"Condition matched: {condition.get('if', 'true')}"
                )

                try:
                    action = OrchestrationAction(action_str)
                except ValueError:
                    action = OrchestrationAction.CONTINUE

                return EvaluationResult(
                    action=action,
                    target_phase=target,
                    reason=reason,
                    score=score,
                    metadata=metadata,
                )

        # No condition matched - default to continue
        return EvaluationResult(
            action=OrchestrationAction.CONTINUE,
            score=score,
            reason="No conditions matched",
        )

    def _check_condition(
        self,
        condition: Dict[str, Any],
        score: Optional[float],
        metadata: Dict[str, Any],
        phase_output: Dict[str, Any],
    ) -> bool:
        """Check if a condition is met using safe evaluation."""
        import re

        condition_str = condition.get("if", "true")

        # Build variable map for safe substitution
        variables = {}
        if score is not None:
            variables["score"] = score
        for key, value in metadata.items():
            if isinstance(value, (int, float, str, bool)):
                variables[key] = value
        for key, value in phase_output.items():
            if isinstance(value, (int, float, str, bool)):
                variables[key] = value

        # Safe evaluation: only allow comparison operators and numbers
        try:
            match = re.match(CONDITION_PATTERN, condition_str.strip())

            if match:
                var_name = match.group(1)
                op = match.group(2)
                threshold = float(match.group(3))

                # Get variable value
                var_value = variables.get(var_name)
                if var_value is None:
                    logger.warning(f"Variable '{var_name}' not found in conditions")
                    return False

                # Convert to float for comparison
                try:
                    var_value = float(var_value)
                except (ValueError, TypeError):
                    logger.warning(f"Cannot convert '{var_value}' to float")
                    return False

                return CONDITION_OPERATORS[op](var_value, threshold)
            else:
                # Try simple boolean evaluation
                if condition_str.strip() == "true":
                    return True
                elif condition_str.strip() == "false":
                    return False
                else:
                    logger.warning(f"Invalid condition format: {condition_str}")
                    return False

        except Exception as e:
            logger.warning(f"Failed to evaluate condition '{condition_str}': {e}")
            return False

    def get_retry_count(self, phase_name: str) -> int:
        """Get current retry count for a phase."""
        return self.phase_retry_counts.get(phase_name, 0)

    def reset_retries(self, phase_name: str = None):
        """Reset retry counts."""
        if phase_name:
            self.phase_retry_counts.pop(phase_name, None)
        else:
            self.phase_retry_counts.clear()

    def get_evaluation_history(self) -> List[Dict[str, Any]]:
        """Get history of all evaluations."""
        return self.evaluation_history.copy()
