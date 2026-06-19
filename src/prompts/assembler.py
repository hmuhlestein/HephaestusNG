"""Prompt assembler for phase-based agent prompts.

This module is the single source of truth for how phase fields are
assembled into the system prompt and user prompt that agents receive.
The frontend has a TypeScript port (``frontend/src/lib/promptAssember.ts``)
that must produce identical output given identical inputs.

Usage::

    from src.prompts.assembler import PromptAssembler

    assembler = PromptAssembler(
        phase_description="You are a QA tester...",
        done_definitions=["Plan created", "Targets identified"],
        additional_notes="Focus on API",
        outputs="test_plan.md",
        next_steps="Proceed to implementation",
    )

    # Render with context variables
    result = assembler.render(
        variables={"project_name": "my-app", "phase_number": 1, "phase_name": "Test Planning"},
    )

    # With task overrides
    result = assembler.render(
        variables={...},
        task_system_prompt="You are a senior engineer...",
        task_user_prompt="Create the test plan...",
    )
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set


# ── Template variable detection ─────────────────────────────────────────────

_VAR_RE = re.compile(r"\{(\w+)\}")


def detect_variables(text: str) -> Set[str]:
    """Return all ``{var_name}`` tokens found in *text*."""
    return set(_VAR_RE.findall(text))


def substitute_variables(text: str, variables: Dict[str, str]) -> str:
    """Replace ``{var_name}`` tokens.  Missing variables are left as-is."""
    for name, value in variables.items():
        text = text.replace(f"{{{name}}}", str(value))
    return text


# ── Preview result ──────────────────────────────────────────────────────────

@dataclass
class RenderedPrompt:
    """Output of ``PromptAssembler.render()``."""

    system_prompt: str
    user_prompt: str
    variables_used: List[str] = field(default_factory=list)
    variables_missing: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# ── Assembler ───────────────────────────────────────────────────────────────

class PromptAssembler:
    """Assemble phase fields into the prompts agents receive.

    Parameters
    ----------
    phase_description:
        The ``description`` field from the phase.  This becomes the root
        of the system prompt.
    done_definitions:
        List of criteria that must be met for the phase to be considered
        complete.  Rendered as a bulleted list in the user prompt.
    additional_notes:
        Free-form notes (optional).
    outputs:
        Expected outputs / deliverables (optional).
    next_steps:
        Instructions for transitioning to the next phase (optional).
    working_directory:
        Default working directory for agents in this phase.
    cli_tool:
        CLI tool name (for context, not prompt generation).
    cli_model:
        CLI model name (for context, not prompt generation).
    phase_order:
        Phase order number (1-based).
    phase_name:
        Human-readable phase name.
    """

    def __init__(
        self,
        phase_description: str = "",
        done_definitions: Optional[List[str]] = None,
        additional_notes: Optional[str] = None,
        outputs: Optional[str] = None,
        next_steps: Optional[str] = None,
        working_directory: Optional[str] = None,
        cli_tool: Optional[str] = None,
        cli_model: Optional[str] = None,
        phase_order: Optional[int] = None,
        phase_name: Optional[str] = None,
    ):
        self.phase_description = phase_description or ""
        self.done_definitions = done_definitions or []
        self.additional_notes = additional_notes
        self.outputs = outputs
        self.next_steps = next_steps
        self.working_directory = working_directory
        self.cli_tool = cli_tool
        self.cli_model = cli_model
        self.phase_order = phase_order
        self.phase_name = phase_name

    # ── Public API ──────────────────────────────────────────────────────

    def render(
        self,
        variables: Optional[Dict[str, str]] = None,
        all_phases: Optional[List[Dict[str, Any]]] = None,
        task_system_prompt: Optional[str] = None,
        task_user_prompt: Optional[str] = None,
        task_description: Optional[str] = None,
        task_done_definition: Optional[str] = None,
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        phase_id: Optional[str] = None,
        memories: Optional[List[Dict[str, Any]]] = None,
        project_context: str = "",
    ) -> RenderedPrompt:
        """Build the full prompt pair.

        Parameters
        ----------
        variables:
            Template variables to substitute into all text fields.
        all_phases:
            All phases in the workflow (for cross-phase context).
        task_system_prompt:
            Per-task override for system prompt.  If set, *replaces*
            the phase description entirely.
        task_user_prompt:
            Per-task override for user prompt.  If set, *replaces*
            the phase-assembled user prompt.
        task_description:
            Task ``raw_description`` or ``enriched_description``.
        task_done_definition:
            Task-level ``done_definition``.
        agent_id:
            Current agent ID (for IDs section).
        task_id:
            Current task ID (for IDs section).
        memories:
            Pre-loaded memory list.
        project_context:
            Project context string.
        """
        variables = variables or {}

        # Resolve all phase fields through variable substitution
        description = self._resolve(self.phase_description, variables)
        done_defs = [self._resolve(d, variables) for d in self.done_definitions]
        notes = self._resolve(self.additional_notes, variables) if self.additional_notes else None
        outputs_text = self._resolve(self.outputs, variables) if self.outputs else None
        next_steps_text = self._resolve(self.next_steps, variables) if self.next_steps else None

        # Build system prompt
        system_prompt = self._build_system_prompt(
            description=description,
            task_description=task_description,
            task_done_definition=task_done_definition,
            project_context=project_context,
            memories=memories or [],
            agent_id=agent_id,
            task_id=task_id,
            phase_id=phase_id,
        )

        # Build user prompt
        user_prompt = self._build_user_prompt(
            description=description,
            done_definitions=done_defs,
            additional_notes=notes,
            outputs=outputs_text,
            next_steps=next_steps_text,
            all_phases=all_phases,
            agent_id=agent_id,
            task_id=task_id,
        )

        # Apply task overrides (full replacement, not partial merge)
        if task_system_prompt:
            system_prompt = task_system_prompt
        if task_user_prompt:
            user_prompt = task_user_prompt

        # Track variable usage
        all_text = self.phase_description + " ".join(self.done_definitions) + (self.additional_notes or "") + (self.outputs or "") + (self.next_steps or "")
        defined_vars = set(variables.keys())
        requested_vars = detect_variables(all_text)
        variables_used = sorted(requested_vars & defined_vars)
        variables_missing = sorted(requested_vars - defined_vars)

        warnings = []
        if variables_missing:
            warnings.append(
                f"Variables referenced in prompt but not defined: {', '.join(variables_missing)}"
            )

        return RenderedPrompt(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            variables_used=variables_used,
            variables_missing=variables_missing,
            warnings=warnings,
        )

    # ── Diff helpers ────────────────────────────────────────────────────

    def diff(
        self,
        other: "PromptAssembler",
        variables: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Return a structured diff between two assembler states."""
        variables = variables or {}
        result: Dict[str, Any] = {"field_changes": {}, "added_lines": 0, "removed_lines": 0, "changed_fields": []}

        fields_to_compare = {
            "description": (self.phase_description, other.phase_description),
            "done_definitions": (self.done_definitions, other.done_definitions),
            "additional_notes": (self.additional_notes, other.additional_notes),
            "outputs": (self.outputs, other.outputs),
            "next_steps": (self.next_steps, other.next_steps),
        }

        for fname, (old_val, new_val) in fields_to_compare.items():
            old_str = str(old_val) if old_val is not None else ""
            new_str = str(new_val) if new_val is not None else ""
            if old_str != new_str:
                result["changed_fields"].append(fname)
                result["field_changes"][fname] = {"from": old_str, "to": new_str}

        # Unified diff from rendered output
        old_rendered = self.render(variables)
        new_rendered = other.render(variables)
        old_lines = old_rendered.system_prompt.splitlines()
        new_lines = new_rendered.system_prompt.splitlines()
        result["added_lines"] = max(0, len(new_lines) - len(old_lines))
        result["removed_lines"] = max(0, len(old_lines) - len(new_lines))

        return result

    # ── Private helpers ─────────────────────────────────────────────────

    def _resolve(self, text: str, variables: Dict[str, str]) -> str:
        return substitute_variables(text, variables)

    def _build_system_prompt(
        self,
        description: str,
        task_description: Optional[str],
        task_done_definition: Optional[str],
        project_context: str,
        memories: List[Dict[str, Any]],
        agent_id: Optional[str] = None,
        task_id: Optional[str] = None,
        phase_id: Optional[str] = None,
    ) -> str:
        """Build the system prompt the LLM receives.

        Mirrors the structure in ``LLMInterface.generate_agent_prompt``
        but uses the phase description as the root.
        """
        memory_context = "\n".join([
            f"- {mem.get('content', '')[:200]}"
            for mem in memories[:10]
        ]) if memories else "(no memories loaded)"

        task_desc = task_description or "(no task description)"
        task_done = task_done_definition or "Complete the assigned task"

        # Phase description becomes the agent identity
        identity = description or "You are an AI agent in the Hephaestus orchestration system."

        return f"""{identity}

═══ TASK ═══
{task_desc}

COMPLETION CRITERIA:
{task_done}

═══ PRE-LOADED CONTEXT ═══
Top 10 relevant memories (use search_memory for more):
{memory_context}

PROJECT:
{project_context or '(no project context loaded)'}

═══ AVAILABLE TOOLS ═══

Hephaestus MCP (task management):
• create_task - Create sub-tasks (MUST set parent_task_id="{task_id or 'unknown'}")
• update_task_status - Mark done/failed when complete (REQUIRED)
• save_memory - Save discoveries for other agents
• spawn_agent - Spawn a specialized Hephaestus subagent (see below)

Qdrant MCP (memory search):
• search_memory - Search agent memories semantically
  Use when: encountering errors, needing implementation details, finding related work
  Example: "qdrant-find 'PostgreSQL connection timeout solutions'"
  Note: Pre-loaded context covers most needs; search for specifics

═══ SUBAGENT SPAWNING ═══
To delegate specialized work, use spawn_agent:

• spawn_agent(agent_name="hephaestus-development", task="implement X", workflow_id="your-workflow-id")
• spawn_agent(agent_name="hephaestus-architecture-design", task="design Y", workflow_id="your-workflow-id")
• spawn_agent(agent_name="hephaestus-adversarial-review", task="review Z", workflow_id="your-workflow-id")

Available agents: hephaestus-product-requirements, hephaestus-architecture-design,
hephaestus-development, hephaestus-adversarial-review, hephaestus-doc-review,
hephaestus-security-review, hephaestus-qa-validation, hephaestus-product-validation,
hephaestus-git-commit-push, hephaestus-forensics-analysis

Note: workflow_id is required for task creation. Use your current workflow_id.

═══ WORKFLOW ═══
1. Work on your task using pre-loaded context
2. Use qdrant-find if you need specific information (errors, patterns, implementations)
3. Save important discoveries via save_memory (error fixes, decisions, warnings)
4. Spawn subagents for specialized work (architecture, development, review, etc.)
5. Call update_task_status when done (status='done') or failed (status='failed')

IDs: Agent={agent_id or 'unknown'} | Task={task_id or 'unknown'} | Phase={phase_id or 'unknown'}"""

    def _build_user_prompt(
        self,
        description: str,
        done_definitions: List[str],
        additional_notes: Optional[str],
        outputs: Optional[str],
        next_steps: Optional[str],
        all_phases: Optional[List[Dict[str, Any]]],
        agent_id: Optional[str],
        task_id: Optional[str],
    ) -> str:
        """Build the user prompt with phase context.

        Mirrors ``PhaseContext.to_prompt_context()`` from
        ``src/phases/models.py``.
        """
        parts: List[str] = []

        parts.append("## WORKFLOW PHASE INFORMATION\n")

        if self.phase_name and self.phase_order:
            parts.append(f"### Current Phase: {self.phase_name} (Phase {self.phase_order})\n")
        parts.append(f"**Description:**\n{description}\n")

        if done_definitions:
            parts.append("**Completion Criteria:**")
            for criterion in done_definitions:
                parts.append(f"- {criterion}")
            parts.append("")

        if additional_notes:
            parts.append(f"**Additional Notes:**\n{additional_notes}\n")

        if outputs:
            parts.append(f"**Expected Outputs:**\n{outputs}\n")

        if next_steps:
            parts.append(f"**Next Steps:**\n{next_steps}\n")

        # Cross-phase context
        if all_phases:
            parts.append("### All Workflow Phases:\n")
            for phase in all_phases:
                order = phase.get("order", 0)
                name = phase.get("name", "Unknown")
                if self.phase_order:
                    if order < self.phase_order:
                        indicator = "✓"
                    elif order == self.phase_order:
                        indicator = "→"
                    else:
                        indicator = "○"
                else:
                    indicator = "•"
                parts.append(f"{indicator} Phase {order}: {name}")
            parts.append("")

            # Detailed cross-phase info
            parts.append("### Phase Details for Cross-Phase Task Creation:\n")
            for phase in all_phases:
                order = phase.get("order", 0)
                if self.phase_order and order != self.phase_order:
                    name = phase.get("name", "Unknown")
                    desc = phase.get("description", "No description")
                    phase_outputs = phase.get("outputs", "Not specified")
                    phase_done_defs = phase.get("done_definitions", [])

                    parts.append(f"**Phase {order}: {name}**")
                    parts.append(f"- Purpose: {desc[:200]}{'...' if len(desc) > 200 else ''}")
                    parts.append(f"- Key Outputs: {phase_outputs}")
                    if phase_done_defs:
                        parts.append("- Main Goals:")
                        for criterion in phase_done_defs[:3]:
                            parts.append(f"  • {criterion}")
                    parts.append("")

        if self.phase_order:
            parts.append("### Creating Tasks for Different Phases:")
            parts.append("When creating tasks, ALWAYS specify the phase number: phase=1, phase=2, etc.\n")
            parts.append("**Phase Assignment Guidelines:**")
            if all_phases:
                for phase in all_phases:
                    order = phase.get("order", 0)
                    name = phase.get("name", "Unknown")
                    desc = phase.get("description", "")[:150]
                    parts.append(f"- **Phase {order}** ({name}): {desc}...")
            parts.append("")
            parts.append(f"**Important:** You're currently in Phase {self.phase_order}. You can create tasks for:")
            parts.append(f"- Your own phase (phase={self.phase_order}) for parallel work")
            parts.append(f"- Earlier phases (phase < {self.phase_order}) if you discover gaps")
            parts.append(f"- Later phases (phase > {self.phase_order}) for future work")

        return "\n".join(parts)

    # ── Serialisation helpers ───────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialise assembler state to a dict (for JSON storage)."""
        return {
            "description": self.phase_description,
            "done_definitions": self.done_definitions,
            "additional_notes": self.additional_notes,
            "outputs": self.outputs,
            "next_steps": self.next_steps,
            "working_directory": self.working_directory,
            "cli_tool": self.cli_tool,
            "cli_model": self.cli_model,
            "phase_order": self.phase_order,
            "phase_name": self.phase_name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PromptAssembler":
        """Deserialise from dict."""
        return cls(
            phase_description=data.get("description", ""),
            done_definitions=data.get("done_definitions", []),
            additional_notes=data.get("additional_notes"),
            outputs=data.get("outputs"),
            next_steps=data.get("next_steps"),
            working_directory=data.get("working_directory"),
            cli_tool=data.get("cli_tool"),
            cli_model=data.get("cli_model"),
            phase_order=data.get("phase_order"),
            phase_name=data.get("phase_name"),
        )


# ── Convenience functions ───────────────────────────────────────────────────

def assemble_phase_prompt(
    phase_id: str,
    variables: Optional[Dict[str, str]] = None,
    task_system_prompt: Optional[str] = None,
    task_user_prompt: Optional[str] = None,
    db_manager=None,
) -> RenderedPrompt:
    """Assemble a prompt for a phase by loading it from the database.

    This is the primary entry point for the preview endpoint.
    """
    from src.core.database import DatabaseManager, Phase, TaskPromptOverride

    if db_manager is None:
        db_manager = DatabaseManager("hephaestus.db")
    with db_manager.get_session() as session:
        phase = session.query(Phase).filter_by(id=phase_id).first()
        if not phase:
            raise ValueError(f"Phase {phase_id} not found")

        assembler = PromptAssembler(
            phase_description=phase.description,
            done_definitions=phase.done_definitions or [],
            additional_notes=phase.additional_notes,
            outputs=phase.outputs,
            next_steps=phase.next_steps,
            working_directory=phase.working_directory,
            cli_tool=phase.cli_tool,
            cli_model=phase.cli_model,
            phase_order=phase.order,
            phase_name=phase.name,
        )

        # Get all phases for cross-phase context
        all_phases = session.query(Phase).filter_by(workflow_id=phase.workflow_id).order_by(Phase.order).all()
        phases_list = [
            {
                "order": p.order,
                "name": p.name,
                "description": p.description,
                "done_definitions": p.done_definitions or [],
                "outputs": p.outputs,
            }
            for p in all_phases
        ]

    return assembler.render(
        variables=variables,
        all_phases=phases_list,
        task_system_prompt=task_system_prompt,
        task_user_prompt=task_user_prompt,
    )


def assemble_task_prompt(
    task_id: str,
    variables: Optional[Dict[str, str]] = None,
    db_manager=None,
) -> RenderedPrompt:
    """Assemble a prompt for a specific task, applying overrides.

    This is used at agent creation time.
    """
    from src.core.database import DatabaseManager, Task, Phase, TaskPromptOverride

    if db_manager is None:
        db_manager = DatabaseManager("hephaestus.db")
    with db_manager.get_session() as session:
        task = session.query(Task).filter_by(id=task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} not found")

        # Get phase
        phase = None
        if task.phase_id:
            if task.phase_id.isdigit():
                phase = session.query(Phase).filter_by(order=int(task.phase_id)).first()
            else:
                phase = session.query(Phase).filter_by(id=task.phase_id).first()

        # Get overrides
        override = session.query(TaskPromptOverride).filter_by(task_id=task_id).first()

        assembler = PromptAssembler(
            phase_description=phase.description if phase else "",
            done_definitions=phase.done_definitions if phase else [],
            additional_notes=phase.additional_notes if phase else None,
            outputs=phase.outputs if phase else None,
            next_steps=phase.next_steps if phase else None,
            working_directory=phase.working_directory if phase else None,
            cli_tool=phase.cli_tool if phase else None,
            cli_model=phase.cli_model if phase else None,
            phase_order=phase.order if phase else None,
            phase_name=phase.name if phase else None,
        )

        # Get all phases for cross-phase context
        all_phases = None
        if phase:
            all_phases_list = session.query(Phase).filter_by(workflow_id=phase.workflow_id).order_by(Phase.order).all()
            all_phases = [
                {"order": p.order, "name": p.name, "description": p.description, "done_definitions": p.done_definitions or [], "outputs": p.outputs}
                for p in all_phases_list
            ]

    return assembler.render(
        variables=variables,
        all_phases=all_phases,
        task_system_prompt=override.system_prompt if override else None,
        task_user_prompt=override.user_prompt if override else None,
        task_description=task.enriched_description or task.raw_description,
        task_done_definition=task.done_definition,
        agent_id=task.assigned_agent_id,
        task_id=task.id,
        phase_id=task.phase_id,
    )
