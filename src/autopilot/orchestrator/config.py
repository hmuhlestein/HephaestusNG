"""Config-backed timeout/retry-budget getters, each with a hardcoded
fallback if config lookup fails. Extracted from orchestrator/__init__.py
(SOLID review: that module had grown to 3411 lines mixing the actual
pipeline-execution flow with unrelated config-getter/human-escalation/
agent-registration helpers -- see docs/SOLID_OO_REVIEW_UPDATE_2026-08-19.md).
"""


def _get_workflow_timeout() -> int:
    """Get workflow timeout from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().workflow_timeout_seconds
    except Exception:
        return 7200  # 2 hours default


def _get_phase0_timeout() -> int:
    """Get Phase 0 timeout from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().phase0_timeout_seconds
    except Exception:
        return 3600  # 1 hour default


def _get_paused_workflow_retry_cooldown_seconds() -> int:
    """Get the exhausted-retry-pause cooldown from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().paused_workflow_retry_cooldown_seconds
    except Exception:
        return 300  # 5 min default


def _get_paused_workflow_max_retry_cycles() -> int:
    """Get the exhausted-retry-pause retry cycle cap from config, with fallback to default."""
    try:
        from src.core.simple_config import get_config

        return get_config().paused_workflow_max_retry_cycles
    except Exception:
        return 10  # default
