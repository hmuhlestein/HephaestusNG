"""Load and interpolate prompt templates from YAML configuration.

Extracts hardcoded prompts from prompt_builder.py, llm_client.py,
and llm_interface.py into config/prompts/system_prompts.yaml.
"""

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

# Cache for loaded prompts
_prompts_cache: Optional[Dict[str, Any]] = None

PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts"


def _load_prompts() -> Dict[str, Any]:
    """Load prompts from YAML file (cached)."""
    global _prompts_cache
    if _prompts_cache is not None:
        return _prompts_cache

    prompts_file = PROMPTS_DIR / "system_prompts.yaml"
    if not prompts_file.exists():
        logger.warning(f"Prompts file not found: {prompts_file}")
        return {}

    try:
        with open(prompts_file) as f:
            _prompts_cache = yaml.safe_load(f) or {}
        logger.info(f"Loaded prompts from {prompts_file}")
        return _prompts_cache
    except Exception as e:
        logger.error(f"Failed to load prompts: {e}")
        return {}


def get_prompt(key: str, variables: Optional[Dict[str, Any]] = None) -> str:
    """Get a prompt template and interpolate variables.

    Args:
        key: Dot-separated key (e.g., "writing_instructions" or "validator_prompt")
        variables: Dict of variables to interpolate into the template

    Returns:
        Interpolated prompt string
    """
    prompts = _load_prompts()

    # Support dot notation for nested keys
    value = prompts
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    if value is None:
        logger.warning(f"Prompt not found: {key}")
        return ""

    if not isinstance(value, str):
        logger.warning(f"Prompt is not a string: {key}")
        return str(value)

    # Interpolate variables if provided
    if variables:
        try:
            return value.format(**variables)
        except KeyError as e:
            logger.warning(f"Missing variable {e} in prompt {key}")
            return value
        except Exception as e:
            logger.error(f"Failed to interpolate prompt {key}: {e}")
            return value

    return value


def get_base_system_prompt(
    agent_id: str,
    task_id: str,
    memory_context: str,
    project_context: str,
) -> str:
    """Get the base system prompt with variables interpolated."""
    return get_prompt("base_system_prompt", {
        "agent_id": agent_id,
        "task_id": task_id,
        "memory_context": memory_context,
        "project_context": project_context,
    })


def get_phase_system_prompt(
    phase_name: Optional[str],
    agent_id: str,
    task_id: str,
    memory_context: str,
    project_context: str,
) -> Optional[str]:
    """Get a phase-specific system prompt, or None if this phase doesn't
    have one.

    Looked up by convention -- "{phase_name}_system_prompt" in
    system_prompts.yaml -- so a phase opts into a specialized prompt purely
    by that template existing (e.g. feature_architect_system_prompt for the
    feature_architect phase). Callers fall back to get_base_system_prompt
    (or their own equivalent) on None; no caller needs to know which phase
    names have a specialized prompt.
    """
    if not phase_name:
        return None
    key = f"{phase_name}_system_prompt"
    if key not in _load_prompts():
        return None
    return get_prompt(key, {
        "agent_id": agent_id,
        "task_id": task_id,
        "memory_context": memory_context,
        "project_context": project_context,
    })


def get_phase_agent_instructions(
    agent_id: str,
    task_id: str,
    workflow_id: str,
    phase_id: str,
    ticket_note: str = "",
    phase_context_section: str = "",
) -> str:
    """Get phase agent instructions with variables interpolated."""
    return get_prompt("phase_agent_instructions", {
        "agent_id": agent_id,
        "task_id": task_id,
        "workflow_id": workflow_id,
        "phase_id": phase_id,
        "ticket_note": ticket_note,
        "phase_context_section": phase_context_section,
    })


def get_phase_agent_resumed_instructions(
    agent_id: str,
    task_id: str,
    phase_context_section: str = "",
) -> str:
    """Get the condensed instructions for a genuinely resumed phase-agent
    session, with variables interpolated."""
    return get_prompt("phase_agent_resumed_instructions", {
        "agent_id": agent_id,
        "task_id": task_id,
        "phase_context_section": phase_context_section,
    })


def get_non_phase_agent_instructions(
    agent_id: str,
    task_id: str,
    phase_context_section: str = "",
) -> str:
    """Get non-phase agent instructions with variables interpolated."""
    return get_prompt("non_phase_agent_instructions", {
        "agent_id": agent_id,
        "task_id": task_id,
        "phase_context_section": phase_context_section,
    })


def get_workflow_result_criteria(result_criteria: str) -> str:
    """Get workflow result criteria with variables interpolated."""
    return get_prompt("workflow_result_criteria", {
        "result_criteria": result_criteria,
    })


def get_ticket_note() -> str:
    """Get the ticket tracking note."""
    return get_prompt("ticket_note")


def get_validator_prompt(validator_type: str = "validator") -> str:
    """Get validator agent prompt."""
    key_map = {
        "validator": "validator_prompt",
        "result_validator": "result_validator_prompt",
        "diagnostic": "diagnostic_prompt",
    }
    key = key_map.get(validator_type, "validator_prompt")
    return get_prompt(key)


def get_monitor_nudge(key: str, **variables: str) -> str:
    """Get a monitor nudge/steering message with variables interpolated.

    Args:
        key: Sub-key under monitor_nudges (e.g. "operation_aborted")
        variables: Values for the message's {placeholder} variables
    """
    return get_prompt(f"monitor_nudges.{key}", variables)


def reload_prompts():
    """Force reload prompts from YAML (for testing or hot-reload)."""
    global _prompts_cache
    _prompts_cache = None
    _load_prompts()
