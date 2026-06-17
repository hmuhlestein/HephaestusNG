"""
Workflow Orchestrator Engine

Evaluates phase outputs and decides flow control:
- Continue to next phase
- Retry current phase
- Goto a specific phase
- Fail the workflow

Configurable per workflow via orchestrator_config.
"""

import logging
import json
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class OrchestrationAction(Enum):
    CONTINUE = "continue"          # Move to next phase
    RETRY = "retry"                # Retry current phase
    GOTO = "goto"                  # Jump to a specific phase
    FAIL = "fail"                  # Fail the workflow
    SKIP = "skip"                  # Skip to next phase (same as continue but logged differently)


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
    evaluator: str    # Evaluator function name or "llm"
    conditions: List[Dict[str, Any]] = field(default_factory=list)
    max_retries: int = 2
    timeout_seconds: int = 300


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
    def from_dict(cls, data: Dict[str, Any]) -> 'OrchestratorConfig':
        """Create config from dictionary (e.g., from JSON)."""
        if not data:
            return cls()

        eval_points = []
        for ep in data.get("evaluation_points", []):
            eval_points.append(EvaluationPoint(
                after_phase=ep.get("after_phase", ""),
                evaluator=ep.get("evaluator", "llm"),
                conditions=ep.get("conditions", []),
                max_retries=ep.get("max_retries", 2),
                timeout_seconds=ep.get("timeout_seconds", 300),
            ))

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

    def __init__(self, config: OrchestratorConfig):
        self.config = config
        self.phase_retry_counts: Dict[str, int] = {}
        self.total_gotos: int = 0  # Track total GOTO operations
        self.evaluation_history: List[Dict[str, Any]] = []

    def evaluate(
        self,
        phase_name: str,
        phase_output: Dict[str, Any],
        phase_history: List[Dict[str, Any]],
        llm_evaluator=None
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
                reason="Sequential mode - always continue"
            )

        # Find evaluation point for this phase
        eval_point = self._find_evaluation_point(phase_name)
        if not eval_point:
            return EvaluationResult(
                action=OrchestrationAction.CONTINUE,
                reason=f"No evaluation point configured for {phase_name}"
            )

        # Check retry count
        current_retries = self.phase_retry_counts.get(phase_name, 0)
        if current_retries >= eval_point.max_retries:
            logger.info(f"Max retries ({eval_point.max_retries}) reached for {phase_name}")
            return EvaluationResult(
                action=OrchestrationAction.CONTINUE,
                reason=f"Max retries reached for {phase_name}, continuing"
            )

        # Evaluate using configured evaluator
        score = None
        evaluation_metadata = {}

        if eval_point.evaluator == "llm" and llm_evaluator:
            score, evaluation_metadata = llm_evaluator(
                phase_name=phase_name,
                phase_output=phase_output,
                conditions=eval_point.conditions
            )
        elif eval_point.evaluator == "heuristic":
            score, evaluation_metadata = self._heuristic_evaluate(
                phase_name, phase_output, eval_point.conditions
            )
        else:
            # Default: check phase_output for common success indicators
            score, evaluation_metadata = self._default_evaluate(phase_output)

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
                logger.warning(
                    f"GOTO limit exceeded ({self.total_gotos}/{self.config.max_total_gotos}). "
                    f"Forcing continue to prevent infinite loop."
                )
                action = EvaluationResult(
                    action=OrchestrationAction.CONTINUE,
                    reason=f"GOTO limit exceeded ({self.total_gotos}/{self.config.max_total_gotos}), forcing continue",
                    score=score,
                    metadata=action.metadata
                )

        # Log evaluation
        self.evaluation_history.append({
            "phase": phase_name,
            "score": score,
            "action": action.action.value,
            "reason": action.reason,
            "metadata": evaluation_metadata,
        })

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
        
        This is a best-effort lookup based on common naming patterns.
        Returns 0 if unable to determine order.
        """
        # Common phase name patterns
        name_to_order = {
            "product_requirements": 1,
            "architecture_design": 2,
            "architecture": 2,
            "development": 3,
            "adversarial_review": 4,
            "doc_review": 5,
            "security_review": 6,
            "qa_validation": 7,
            "product_validation": 8,
            "git_commit_push": 9,
            "forensics_analysis": 10,
        }
        
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
        conditions: List[Dict[str, Any]]
    ) -> Tuple[float, Dict[str, Any]]:
        """Simple heuristic evaluation based on output content."""
        score = 0.5  # Default neutral score
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
        self,
        phase_output: Dict[str, Any]
    ) -> Tuple[float, Dict[str, Any]]:
        """Default evaluation - check for basic success/failure."""
        score = 0.5
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
        phase_output: Dict[str, Any]
    ) -> EvaluationResult:
        """Evaluate conditions and return action."""
        if not conditions:
            # No conditions - default to continue
            return EvaluationResult(
                action=OrchestrationAction.CONTINUE,
                score=score,
                reason="No conditions configured"
            )

        for condition in conditions:
            if self._check_condition(condition, score, metadata, phase_output):
                action_str = condition.get("action", "continue")
                target = condition.get("target")
                reason = condition.get("reason", f"Condition matched: {condition.get('if', 'true')}")

                try:
                    action = OrchestrationAction(action_str)
                except ValueError:
                    action = OrchestrationAction.CONTINUE

                return EvaluationResult(
                    action=action,
                    target_phase=target,
                    reason=reason,
                    score=score,
                    metadata=metadata
                )

        # No condition matched - default to continue
        return EvaluationResult(
            action=OrchestrationAction.CONTINUE,
            score=score,
            reason="No conditions matched"
        )

    def _check_condition(
        self,
        condition: Dict[str, Any],
        score: Optional[float],
        metadata: Dict[str, Any],
        phase_output: Dict[str, Any]
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
            # Pattern: variable_name <operator> number
            # Supported operators: <, <=, >, >=, ==, !=
            pattern = r'^([a-zA-Z_][a-zA-Z0-9_]*)\s*(<|<=|>|>=|==|!=)\s*([0-9.]+)$'
            match = re.match(pattern, condition_str.strip())

            if match:
                var_name = match.group(1)
                operator = match.group(2)
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

                # Evaluate comparison
                if operator == "<":
                    return var_value < threshold
                elif operator == "<=":
                    return var_value <= threshold
                elif operator == ">":
                    return var_value > threshold
                elif operator == ">=":
                    return var_value >= threshold
                elif operator == "==":
                    return abs(var_value - threshold) < 0.0001
                elif operator == "!=":
                    return abs(var_value - threshold) >= 0.0001
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


# Built-in evaluators

def llm_evaluator(
    phase_name: str,
    phase_output: Dict[str, Any],
    conditions: List[Dict[str, Any]],
    llm_client=None
) -> Tuple[float, Dict[str, Any]]:
    """
    LLM-based evaluator that uses AI to assess phase output.

    Args:
        phase_name: Name of the phase
        phase_output: Output from the phase
        conditions: Conditions to evaluate against
        llm_client: LLM client for evaluation

    Returns:
        Tuple of (score, metadata)
    """
    if not llm_client:
        # Fallback to heuristic
        return WorkflowOrchestrator(OrchestratorConfig())._heuristic_evaluate(
            phase_name, phase_output, conditions
        )

    prompt = f"""Evaluate the output of phase '{phase_name}' and provide a score from 0.0 to 1.0.

Phase Output:
{json.dumps(phase_output, indent=2)[:2000]}

Evaluation Criteria:
{json.dumps(conditions, indent=2)}

Respond with JSON:
{{
    "score": <0.0 to 1.0>,
    "reason": "<brief explanation>",
    "issues": ["<list of issues if any>"],
    "suggestions": ["<list of suggestions if any>"]
}}"""

    try:
        response = llm_client.complete(prompt)
        result = json.loads(response)
        return result.get("score", 0.5), {
            "reason": result.get("reason", ""),
            "issues": result.get("issues", []),
            "suggestions": result.get("suggestions", []),
        }
    except Exception as e:
        logger.error(f"LLM evaluation failed: {e}")
        return 0.5, {"error": str(e)}
