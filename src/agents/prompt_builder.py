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

from src.core.database import Task

logger = logging.getLogger(__name__)


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
                # Fallback message
                if agent_type == "result_validator":
                    return "You are a result validator agent. Please check the task details for validation instructions."
                elif agent_type == "diagnostic":
                    return "You are a diagnostic agent. Please analyze the workflow state and create tasks to progress toward the goal."
                else:
                    return "You are a task validator agent. Please check the task details for validation instructions."

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
                    # Ticket tracking makes hephaestus_create_task reject any
                    # call with no ticket_id ("MCP agents MUST provide
                    # ticket_id"). Without this note, every agent discovers
                    # that the hard way on its first subtask-creation
                    # attempt, wasting a full round trip every time —
                    # observed live during smoke testing.
                    ticket_note = (
                        "\n  Ticket tracking is ON for this workflow — hephaestus_create_task "
                        "REQUIRES ticket_id. Call hephaestus_create_ticket(...) first, then pass "
                        "its id as ticket_id here."
                    )
                if (
                    workflow_config
                    and hasattr(workflow_config, "result_criteria")
                    and workflow_config.result_criteria
                ):
                    result_criteria_section = f"""

**WORKFLOW-LEVEL GOAL** (Ultimate objective for all phases):
{workflow_config.result_criteria}

This is the final deliverable this entire workflow is working toward. All phases and tasks should contribute to achieving this goal.

NOTE: Having a workflow-level goal does NOT mean you skip hephaestus_update_task_status. You must still mark your individual task as done when you complete it. The workflow result submission is ONLY for when someone achieves the final goal."""
            except Exception as e:
                logger.warning(f"Could not get workflow result criteria: {e}")

        base_message += result_criteria_section

        is_phase_agent = hasattr(task, "phase_id") and task.phase_id

        if is_phase_agent:
            # Compact instructions for workflow phase agents — keep context window lean
            base_message += f"""

INSTRUCTIONS (always pass agent_id="{agent_id}"; pass workflow_id="{workflow_id if workflow_id else "N/A"}" only for tools that accept it — save_memory and validate_my_agent_id do NOT take workflow_id, don't pass it to those):
- Complete the task described above
- hephaestus_update_task_status(task_id="{task.id}", agent_id="{agent_id}", status="done") — REQUIRED when done
- hephaestus_update_task_status(task_id="{task.id}", agent_id="{agent_id}", status="failed", failure_reason="...") — on unrecoverable error
- hephaestus_save_memory(content="...", agent_id="{agent_id}", memory_type="<type>"): save as you go, not just at end
  types: error_fix | discovery | decision | learning | warning | codebase_knowledge
- hephaestus_search_memory(query="..."): search before reinventing
- hephaestus_create_task(task_description="...", done_definition="...", phase_id="{task.phase_id if hasattr(task, "phase_id") and task.phase_id else "N/A"}", workflow_id="{workflow_id if workflow_id else "N/A"}"): create SUBTASKS within YOUR OWN current phase only.
  This tool does NOT take agent_id — omit it here even though other tools need it.
  Do NOT use this to create the next pipeline phase's task — the orchestrator creates that
  automatically, with the correct phase name and required output, once you mark this task done.
  Manually guessing a future phase number here has caused tasks to be created under the wrong
  phase (e.g. full implementation work filed under an architecture-design phase) — never do this.{ticket_note}
{phase_context_section}
Begin now.
"""
        else:
            base_message += f"""

IMPORTANT INSTRUCTIONS:
1. Complete all the requirements listed in the COMPLETION CRITERIA above

2. MCP tools (always use agent_id="{agent_id}"):
   - hephaestus_update_task_status: Mark your task as done (task_id: {task.id})
   - hephaestus_save_memory: Save discoveries for other agents
   - hephaestus_search_memory: Search past memories
   - hephaestus_create_task: Create sub-tasks
   - hephaestus_get_tasks: Check status of other tasks
   - hephaestus_broadcast_message: Send message to ALL active agents
   - hephaestus_send_message: Send direct message to a SPECIFIC agent

**Agent Communication**:
- hephaestus_broadcast_message: Use when all agents need to know something
- hephaestus_send_message: Use for direct agent-to-agent coordination
  (use hephaestus_get_agent_status first to find agent IDs)

3. **TASK COMPLETION** (REQUIRED):
   hephaestus_update_task_status(task_id="{task.id}", agent_id="{agent_id}", status="done", ...)

4. **WORKFLOW RESULT** (only if you solved the ENTIRE workflow):
   hephaestus_submit_result(markdown_file_path="...", agent_id="{agent_id}", explanation="...", evidence=[...])

5. On unrecoverable failure:
   hephaestus_update_task_status(task_id="{task.id}", agent_id="{agent_id}", status="failed", failure_reason="...")

6. Memory — save liberally throughout (not just at end):
   Types: error_fix, discovery, decision, learning, warning, codebase_knowledge
   Search before reinventing: hephaestus_search_memory(query="specific topic")
{phase_context_section}

Begin working on your task now.

REMEMBER:
- Task done → hephaestus_update_task_status(task_id="{task.id}", agent_id="{agent_id}", status="done") [ALWAYS required]
- Entire workflow solved → also hephaestus_submit_result(agent_id="{agent_id}", ...) [separate action]
"""

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
