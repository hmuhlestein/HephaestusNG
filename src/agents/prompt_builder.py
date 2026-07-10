"""Building the initial task-assignment message sent to a freshly-created
agent.

Extracted from AgentManager, which mixed this pure string-formatting
concern in with tmux session lifecycle, DB persistence, and messaging —
see docs/SOLID_OO_REVIEW.md finding 3.1. AgentManager still exposes
_format_initial_message (tests call it directly on the AgentManager
instance) but delegates to an AgentPromptBuilder instance instead of
building the message itself.
"""

import logging

from src.autopilot.phases import SESSION_ROLES
from src.core.database import Task
from src.prompts.loader import (
    get_non_phase_agent_instructions,
    get_phase_agent_instructions,
    get_ticket_note,
    get_validator_prompt,
    get_workflow_result_criteria,
    get_writing_instructions,
)

logger = logging.getLogger(__name__)

# Shared writing instructions — loaded from YAML
WRITING_INSTRUCTIONS = get_writing_instructions()


class AgentPromptBuilder:
    """Formats the initial task-assignment message for a new agent."""

    def __init__(self, phase_manager=None):
        self.phase_manager = phase_manager

    def format_initial_message(
        self,
        task: Task,
        agent_id: str,
        branch_path: str = None,
        agent_type: str = "phase",
        enriched_data: dict = None,
    ) -> str:
        """Format the initial message to send to the agent.

        Args:
            task: Task to work on
            agent_id: Agent's ID
            branch_path: Path to the agent's worktree
            agent_type: Type of agent (phase, validator, result_validator)

        Returns:
            Formatted initial message
        """
        logger.info(
            f"🔍 PROMPT SIZE DEBUG: Starting to format initial message for {agent_type} agent {agent_id}"
        )

        # For validators and diagnostic agents, use specialized prompts from enriched_data
        if agent_type in ["result_validator", "validator", "diagnostic"]:
            logger.info(f"Using specialized prompt for {agent_type} agent {agent_id}")

            # The validation prompt should be passed in enriched_data by validator_agent.py
            if enriched_data and "validation_prompt" in enriched_data:
                validation_prompt = enriched_data["validation_prompt"]
                logger.info(
                    f"Found validation prompt in enriched_data for agent {agent_id}"
                )
                return validation_prompt
            else:
                logger.warning(
                    f"No specialized prompt found in enriched_data for {agent_type} agent {agent_id}"
                )
                return get_validator_prompt(agent_type)

        # Use the actual worktree path for the agent
        cwd_info = f"Working Directory: {branch_path}" if branch_path else ""

        # Get workflow information for context
        workflow_id = getattr(task, "workflow_id", None) or ""
        workflow_description = ""
        if workflow_id and self.phase_manager:
            try:
                workflow = self.phase_manager.get_workflow(workflow_id)
                if workflow:
                    workflow_description = workflow.description or ""
            except Exception as e:
                logger.warning(f"Could not get workflow description: {e}")

        base_message = f"""
=== TASK ASSIGNMENT ===
🔑 Your Agent ID: {agent_id}
   ⚠️  CRITICAL: Use this EXACT ID when calling MCP tools (hephaestus_update_task_status, hephaestus_create_task, etc.)
   ⚠️  DO NOT use 'agent-mcp' or any other placeholder - it will fail authorization!

📋 Task ID: {task.id}
🔄 Workflow ID: {workflow_id if workflow_id else "N/A (standalone task)"}
📁 {cwd_info}
"""

        logger.info(
            f"🔍 PROMPT SIZE DEBUG: Base message length: {len(base_message)} chars"
        )

        # Add phase information if available
        phase_context_section = ""
        resumed_session_warning = ""
        if hasattr(task, "phase_id") and task.phase_id:
            base_message += f"\nPhase ID: {task.phase_id}"

            # Add workflow description if available (ID already stated in the
            # header above — no need to repeat it here)
            if workflow_description:
                base_message += f"\n\n=== WORKFLOW CONTEXT ===\nWorkflow Description: {workflow_description}\n"

            logger.info(f"=== PHASE CONTEXT DEBUG for task {task.id} ===")
            logger.info(f"Task has phase_id: {task.phase_id}")

            # Try to get phase context if phase manager is available
            if self.phase_manager is not None:
                logger.info(f"Phase manager exists: {self.phase_manager}")
                logger.info(
                    f"Phase manager workflow_id: {getattr(self.phase_manager, 'workflow_id', 'NOT SET')}"
                )
                logger.debug(
                    f"Phase manager active_workflow: {getattr(self.phase_manager, 'active_workflow', 'NOT SET')}"
                )

                try:
                    logger.info(
                        f"Calling get_phase_context with phase_id: {task.phase_id}"
                    )
                    phase_ctx = self.phase_manager.get_phase_context(task.phase_id)
                    logger.debug(f"get_phase_context returned: {phase_ctx}")

                    if phase_ctx:
                        logger.info(
                            f"Phase context found! Phase name: {phase_ctx.phase.name}"
                        )
                        logger.info(
                            f"Phase context all_phases count: {len(phase_ctx.all_phases)}"
                        )
                        phase_context_section = "\n" + phase_ctx.to_prompt_context()
                        logger.info(
                            f"🔍 PROMPT SIZE DEBUG: Generated phase context section length: {len(phase_context_section)}"
                        )
                        logger.info(
                            f"Phase context section preview: {phase_context_section[:200]}..."
                        )

                        # Phases that share a session_role (e.g. architecture_design
                        # and architectural_review both map to "architect") resume
                        # the SAME pi session/conversation, on purpose, so the agent
                        # keeps its prior design context. But that means the agent's
                        # conversation history is full of an EARLIER, already-done
                        # task -- observed live: an agent resuming a shared session
                        # kept re-confirming and re-reporting on its old task instead
                        # of ever touching the new one, despite the new task_id being
                        # stated clearly above. Call this out explicitly rather than
                        # trusting the agent to infer it from a fresh task_id alone.
                        role = SESSION_ROLES.get(phase_ctx.phase.name)
                        if role and list(SESSION_ROLES.values()).count(role) > 1:
                            resumed_session_warning = f"""

⚠️  RESUMED SESSION — READ BEFORE DOING ANYTHING ELSE ⚠️
This session was previously used for an earlier phase on this same design
(shared "{role}" role, so you keep your prior context). That earlier task is
ALREADY COMPLETE. Do NOT call hephaestus_update_task_status, hephaestus_save_memory,
or any other tool referencing a task ID from your earlier conversation history.
Your ONLY current task is Task ID: {task.id} (stated above) — if you find
yourself about to act on a different task ID from memory, stop and re-read this."""
                    else:
                        logger.warning(
                            f"Phase context is None for phase_id: {task.phase_id}"
                        )

                except Exception as e:
                    logger.error(
                        f"Exception getting phase context for phase_id {task.phase_id}: {e}"
                    )
                    import traceback

                    logger.error(f"Full traceback: {traceback.format_exc()}")
            else:
                logger.warning(
                    f"Phase manager not available or is None: value={self.phase_manager}"
                )

            logger.info(
                f"🔍 PROMPT SIZE DEBUG: Final phase_context_section length: {len(phase_context_section)}"
            )
            logger.info("=== END PHASE CONTEXT DEBUG ===")
        else:
            logger.info(
                f"Task {task.id} has no phase_id: {getattr(task, 'phase_id', 'NO ATTRIBUTE')}"
            )

        base_message += resumed_session_warning

        base_message += f"""

TASK DESCRIPTION:
{task.enriched_description or task.raw_description}

COMPLETION CRITERIA:
{task.done_definition}"""

        # Add workflow result criteria if available
        result_criteria_section = ""
        ticket_note = ""
        if hasattr(task, "workflow_id") and task.workflow_id and self.phase_manager:
            try:
                workflow_config = self.phase_manager.get_workflow_config(
                    task.workflow_id
                )
                if workflow_config and getattr(workflow_config, "enable_tickets", False):
                    ticket_note = "\n" + get_ticket_note()
                if (
                    workflow_config
                    and hasattr(workflow_config, "result_criteria")
                    and workflow_config.result_criteria
                ):
                    result_criteria_section = "\n" + get_workflow_result_criteria(workflow_config.result_criteria)
            except Exception as e:
                logger.warning(f"Could not get workflow result criteria: {e}")

        base_message += result_criteria_section

        is_phase_agent = hasattr(task, "phase_id") and task.phase_id

        # Proactively inject open bug tickets into the development agent's
        # prompt, rather than only telling it to go call hephaestus_get_tickets
        # itself -- a pull-based instruction is compliance-dependent (the
        # agent has to remember to look), the same class of risk closed for
        # output artifacts by verify_output_artifact. Scoped to the
        # development phase, which is where verify_no_open_tickets enforces
        # this at task-completion time (task_completion_service.py) --
        # QA/security_review create these tickets and must not see them here.
        #
        # Only on a goto re-entry (task.action == "goto"): a first-time
        # development pass (action="continue" from architecture_design) has
        # no tickets of its own yet, and showing unrelated/stale tickets from
        # elsewhere would just be noise on a fresh build.
        open_tickets_section = ""
        if is_phase_agent and task.workflow_id and getattr(task, "action", None) == "goto":
            try:
                from src.core.database import Phase, Ticket, get_db

                with get_db() as db:
                    phase = db.query(Phase).filter_by(id=task.phase_id).first()
                    if phase and phase.name == "development":
                        open_tickets = (
                            db.query(Ticket)
                            .filter(
                                Ticket.workflow_id == task.workflow_id,
                                Ticket.ticket_type == "bug",
                                Ticket.is_resolved.is_(False),
                            )
                            .all()
                        )
                        if open_tickets:
                            lines = [
                                f"- {t.id}: {t.title} (priority={t.priority})\n"
                                f"  {(t.description or '')[:300]}"
                                for t in open_tickets
                            ]
                            open_tickets_section = (
                                "\n\n⚠️ OPEN BUG TICKETS FOR THIS WORKFLOW — fix and "
                                "resolve before marking done:\n"
                                + "\n".join(lines)
                                + "\n\nFor each: fix the underlying issue, then call "
                                'hephaestus_resolve_ticket(ticket_id="...", '
                                'resolution_comment="...", commit_sha="<sha>") to mark '
                                "it resolved. update_task_status(done) will be "
                                "REJECTED while any remain unresolved."
                            )
            except Exception as e:
                logger.warning(f"Could not check open tickets: {e}")

        base_message += open_tickets_section

        if is_phase_agent:
            # Compact instructions for workflow phase agents — keep context window lean
            base_message += "\n" + get_phase_agent_instructions(
                agent_id=agent_id,
                task_id=task.id,
                workflow_id=workflow_id if workflow_id else "N/A",
                phase_id=task.phase_id if hasattr(task, "phase_id") and task.phase_id else "N/A",
                ticket_note=ticket_note,
                phase_context_section=phase_context_section,
            )
        else:
            base_message += "\n" + get_non_phase_agent_instructions(
                agent_id=agent_id,
                task_id=task.id,
                phase_context_section=phase_context_section,
            )

        logger.info(
            f"🔍 PROMPT SIZE DEBUG: Message before adding phase context: {len(base_message)} chars"
        )

        # Phase context was already added earlier in the message building process, so don't add it again
        logger.info("🔍 PROMPT SIZE DEBUG: Skipping duplicate phase context addition")

        logger.info(
            f"🔍 PROMPT SIZE DEBUG: FINAL MESSAGE LENGTH: {len(base_message)} characters"
        )
        logger.info(
            f"🔍 PROMPT SIZE DEBUG: Phase context contributed: {len(phase_context_section)} chars ({len(phase_context_section) / len(base_message) * 100:.1f}% of total if only added once)"
        )

        return base_message
