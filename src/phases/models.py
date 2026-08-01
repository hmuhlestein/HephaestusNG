"""Data models for phase system."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from src.sdk.models import Phase


def validate_cli_tool(cli_tool: Optional[str]) -> bool:
    """Validate that cli_tool is a recognized CLI agent type.

    Args:
        cli_tool: The CLI tool name to validate (or None for default)

    Returns:
        True if valid

    Raises:
        ValueError: If cli_tool is not in the valid list
    """
    if cli_tool is None:
        return True  # None is valid (uses default from global config)

    # Import here to avoid circular dependency
    from src.interfaces.cli_interface import CLI_AGENTS

    valid_tools = list(CLI_AGENTS.keys())

    if cli_tool not in valid_tools:
        raise ValueError(
            f"Invalid cli_tool '{cli_tool}'. Must be one of: {', '.join(valid_tools)}"
        )

    return True


class PhaseContext(BaseModel):
    """Context information for a phase during execution."""

    phase_id: str = Field(..., description="Phase ID in database")
    workflow_id: str = Field(..., description="Workflow ID in database")
    phase: Phase = Field(..., description="Phase definition")
    all_phases: List[Phase] = Field(..., description="All phases in workflow")
    current_status: str = Field(
        default="pending", description="Current execution status"
    )
    active_tasks: int = Field(
        default=0, description="Number of active tasks in this phase"
    )
    completed_tasks: int = Field(
        default=0, description="Number of completed tasks in this phase"
    )

    model_config = {"arbitrary_types_allowed": True}

    def to_prompt_context(self) -> str:
        """Generate context string for agent prompts."""
        current = self.phase
        context = (
            f"## PHASE: {current.name} (Phase {current.id} of {len(self.all_phases)})\n"
        )

        if current.outputs:
            if isinstance(current.outputs, list):
                outputs_str = ", ".join(current.outputs)
            else:
                outputs_str = current.outputs
            context += f"Outputs: {outputs_str}\n"

        context += "\nPipeline (use phase=N when creating tasks):\n"
        for phase in self.all_phases:
            if phase.id > current.id:
                continue
            status_indicator = "✓" if phase.id < current.id else "→"
            desc_short = (
                phase.description[:80].split("\n")[0] if phase.description else ""
            )
            context += (
                f"  {status_indicator} Phase {phase.id}: {phase.name} — {desc_short}\n"
            )

        return context


class PhasesConfig(BaseModel):
    """Configuration for workflow result handling and ticket tracking from phases_config.yaml."""

    has_result: bool = Field(
        default=False,
        description="Whether this workflow expects a definitive result/solution",
    )
    result_criteria: Optional[str] = Field(
        default=None,
        description="Clear criteria that submitted results must meet for validation",
    )
    on_result_found: Literal["stop_all", "do_nothing"] = Field(
        default="do_nothing",
        description="Action to take when a valid result is found and validated",
    )
    enable_tickets: bool = Field(
        default=False,
        description="Whether Kanban board ticket tracking is enabled for this workflow",
    )
    board_config: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Kanban board configuration (columns, ticket types, etc.)",
    )

    @field_validator("result_criteria")
    @classmethod
    def validate_result_criteria(cls, v: Optional[str], info) -> Optional[str]:
        """Validate that result_criteria is provided when has_result is True."""
        has_result = info.data.get("has_result", False)
        if has_result and not v:
            raise ValueError("result_criteria must be provided when has_result is True")
        return v

    @classmethod
    def from_yaml_content(cls, content: Dict[str, Any]) -> "PhasesConfig":
        """Create PhasesConfig from YAML content.

        Args:
            content: Parsed YAML content from phases_config.yaml

        Returns:
            PhasesConfig instance with defaults for missing fields
        """
        return cls(
            has_result=content.get("has_result", False),
            result_criteria=content.get("result_criteria"),
            on_result_found=content.get("on_result_found", "do_nothing"),
            enable_tickets=content.get("enable_tickets", False),
            board_config=content.get("board_config"),
        )
