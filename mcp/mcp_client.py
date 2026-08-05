#!/usr/bin/env python3
"""
Claude-compatible MCP Client for Hephaestus
This client connects to the Hephaestus server running on port 8300. Despite
the filename, it's a generic MCP server usable by any compatible CLI agent
(pi, Claude Code, Codex, etc.) via the shared ~/.config/mcp/mcp.json config
-- not exclusive to Claude.
"""

import atexit
import logging
import os
from pathlib import Path

import httpx
from fastmcp import FastMCP

# This process has no logging of its own -- if it dies (OOM kill, an
# uncaught exception outside a tool's own try/except, the stdio pipe to
# the parent CLI breaking), there is currently zero trace of it anywhere.
# One instance of this process runs per live agent (spawned fresh by the
# CLI per ~/.config/mcp/mcp.json), so the log file is scoped by PID/agent
# to avoid concurrent instances interleaving into one file.
_LOG_DIR = Path(os.path.expanduser("~/.hephaestus/logs/mcp_client"))
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_agent_id_for_log = os.environ.get("HEPHAESTUS_AGENT_ID", "")
_log_name = f"{_agent_id_for_log[:8]}_{os.getpid()}.log" if _agent_id_for_log else f"pid{os.getpid()}.log"
logger = logging.getLogger("hephaestus.mcp_client")
logger.setLevel(logging.INFO)
_file_handler = logging.FileHandler(_LOG_DIR / _log_name)
_file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(_file_handler)

logger.info(
    f"mcp_client.py starting: pid={os.getpid()} "
    f"agent_id={os.environ.get('HEPHAESTUS_AGENT_ID', '<unset>')} "
    f"task_id={os.environ.get('HEPHAESTUS_TASK_ID', '<unset>')} "
    f"workflow_id={os.environ.get('HEPHAESTUS_WORKFLOW_ID', '<unset>')}"
)
atexit.register(lambda: logger.info(f"mcp_client.py exiting: pid={os.getpid()}"))

# Initialize MCP client
mcp = FastMCP("hephaestus-client")

# Hephaestus server URL. Use 127.0.0.1, NOT localhost — on macOS localhost
# resolves to IPv6 ::1 first, but the server binds IPv4 (0.0.0.0), so
# `localhost:8300` fails ("Cannot connect") while 127.0.0.1 works. This broke
# every MCP tool call (agents couldn't update_task_status → phases never
# completed). Env-overridable for non-local deployments.
HEPHAESTUS_URL = os.environ.get("HEPHAESTUS_URL", "http://127.0.0.1:8300")
DEFAULT_AGENT_ID = "main-session-agent"


@mcp.tool(name="health_check")
def health_check() -> str:
    """Check if Hephaestus server is running"""
    try:
        import requests

        response = requests.get(f"{HEPHAESTUS_URL}/health", timeout=5)
        if response.status_code == 200:
            return "✅ Hephaestus server is healthy and running on port 8300"
        else:
            return f"⚠️ Server responded with status {response.status_code}"
    except Exception as e:
        return f"❌ Cannot connect to Hephaestus server: {str(e)}"


@mcp.tool(name="create_task")
async def create_task(
    description: str,
    done_definition: str,
    agent_id: str = None,
    workflow_id: str = None,
    phase_id: str = None,
    priority: str = "medium",
    cwd: str = None,
    ticket_id: str = None,
) -> str:
    """Create a new task in Hephaestus.

    Args:
        description: What needs to be done
        done_definition: Clear criteria for completion
        agent_id: Your agent ID (REQUIRED - found in your initial prompt under "Your Agent ID:").
            Falls back to this process's own agent identity if omitted.
        workflow_id: Your workflow ID (REQUIRED - found in your initial prompt under "Your Workflow ID:").
            Falls back to this process's own workflow if omitted.
        phase_id: Phase order number for the task (REQUIRED). Use YOUR OWN phase's number — the
            same one from your task prompt — to create a SUBTASK within your current phase.
            Falls back to this process's own phase if omitted.
        priority: Task priority (low/medium/high)
        cwd: Current working directory for the task (optional)
        ticket_id: Associated ticket ID (OPTIONAL for SDK/root tasks, REQUIRED when ticket tracking is enabled for MCP agents)

    CRITICAL: You MUST provide agent_id, workflow_id, AND phase_id for every task.
    - agent_id: ALWAYS use YOUR agent ID from the prompt header (looks like "6a062184-e189-4d8d-8376-89da987b9996").
      NEVER use placeholder values like 'agent-mcp' - they will cause authorization failures.
    - workflow_id: ALWAYS use YOUR workflow ID from the prompt header (looks like "a1b2c3d4-e5f6-7890-abcd-ef1234567890").
    - phase_id: REQUIRED — pass YOUR OWN current phase number (same one shown in your task prompt).

    DO NOT use this tool to create the NEXT pipeline phase's task, and do not guess a phase
    number based on what you assume comes "next" (e.g. "scope review is phase 2, so phase 3
    must be implementation"). The pipeline's phase order and names are workflow-specific and
    not a fixed universal sequence — guessing has previously caused full implementation work
    to be filed under an architecture-design phase, corrupting the pipeline. The orchestrator
    automatically creates the correct next-phase task, with the correct name and required
    output, once you mark your own task done. Only use this tool for subtasks within your
    OWN phase.

    IMPORTANT FOR TICKET TRACKING:
    - When ticket tracking is active, MCP agents MUST provide ticket_id
    - SDK tasks (root/beginning tasks created by main-session-agent) may omit ticket_id as they ARE the ticket creators
    - Use create_ticket() first to get a ticket_id, then pass it here when creating tasks

    Omitting phase_id will cause workflow coordination issues.
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    workflow_id = workflow_id or os.environ.get("HEPHAESTUS_WORKFLOW_ID")
    phase_id = phase_id or os.environ.get("HEPHAESTUS_PHASE_ID")
    if not agent_id:
        return "❌ Error creating task: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    if not workflow_id:
        return "❌ Error creating task: workflow_id was not provided and HEPHAESTUS_WORKFLOW_ID is not set in the environment"
    if not phase_id:
        return "❌ Error creating task: phase_id was not provided and HEPHAESTUS_PHASE_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            request_data = {
                "task_description": description,
                "done_definition": done_definition,
                "ai_agent_id": agent_id,
                "workflow_id": workflow_id,
                "priority": priority,
                "phase_id": str(phase_id),
            }

            # Add optional fields if provided
            if cwd:
                request_data["cwd"] = cwd
            if ticket_id:
                request_data["ticket_id"] = ticket_id

            response = await client.post(
                f"{HEPHAESTUS_URL}/create_task",
                json=request_data,
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                cwd_info = f"\nWorking Directory: {cwd}" if cwd else ""
                return f"""✅ Task created successfully!
Task ID: {result.get("task_id", "unknown")}
Assigned to: {result.get("assigned_agent_id", "unknown")}
Status: {result.get("status", "unknown")}{cwd_info}
Description: {result.get("enriched_description", description)[:100]}..."""
            else:
                return f"❌ Failed to create task: {response.text}"
    except Exception as e:
        return f"❌ Error creating task: {str(e)}"


@mcp.tool(name="get_tasks")
async def get_tasks(status: str = "all") -> str:
    """List tasks in Hephaestus.

    Args:
        status: Filter by status (all/pending/assigned/in_progress/done/failed)
    """
    try:
        async with httpx.AsyncClient() as client:
            params = {} if status == "all" else {"status": status}
            response = await client.get(
                f"{HEPHAESTUS_URL}/task_progress",
                params=params,
                headers={"X-Agent-ID": DEFAULT_AGENT_ID},
                timeout=10.0,
            )

            if response.status_code == 200:
                tasks = response.json()
                if not tasks:
                    return "📋 No tasks found"

                if isinstance(tasks, list):
                    task_list = []
                    for task in tasks:
                        task_list.append(
                            f"• [{task['status']}] {task['id'][:8]}: {task['description'][:60]}..."
                        )
                    return "📋 Tasks:\n" + "\n".join(task_list)
                else:
                    # Single task
                    return f"📋 Task {tasks['id'][:8]}: {tasks['status']} - {tasks['description']}"
            else:
                return f"❌ Failed to get tasks: {response.text}"
    except Exception as e:
        return f"❌ Error getting tasks: {str(e)}"


@mcp.tool(name="save_memory")
async def save_memory(
    content: str,
    agent_id: str = None,
    memory_type: str = "discovery",
    workflow_id: str = None,
    task_id: str = None,
) -> str:
    """Save a memory to Hephaestus knowledge base.

    Args:
        content: The memory content to save
        agent_id: Your agent ID (CRITICAL: must match YOUR agent ID from your initial prompt).
            Falls back to this process's own agent identity if omitted.
        memory_type: Type of memory (error_fix/discovery/decision/learning/warning/codebase_knowledge)
        workflow_id: Not used by this tool — accepted and ignored so agents that pass it
            (per the general "include workflow_id on every call" habit) don't get rejected.
        task_id: Not used by this tool — accepted and ignored for the same reason.

    CRITICAL: Use your actual agent UUID from your initial prompt.
    Example: agent_id="84f15f6c-35b1-4d57-97ac-92a3c0c94d29"
    DO NOT use 'agent-mcp' or any placeholder - it will cause errors!
    """
    # Models frequently omit agent_id on this call even when the rendered
    # prompt example shows it filled in -- fall back to the env var the
    # orchestrator sets for this agent's tmux session (see
    # src/agents/manager.py's HEPHAESTUS_AGENT_ID) instead of hard-failing
    # a call whose real identity is unambiguous from the process environment.
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error saving memory: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/save_memory",
                json={
                    "ai_agent_id": agent_id,
                    "memory_content": content,
                    "memory_type": memory_type,
                    "tags": [],
                    "related_files": [],
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                return f"✅ Memory saved! ID: {result.get('memory_id', 'unknown')}"
            else:
                return f"❌ Failed to save memory: {response.text}"
    except Exception as e:
        return f"❌ Error saving memory: {str(e)}"


@mcp.tool(name="search_memory")
async def search_memory(
    query: str,
    agent_id: str = None,
    limit: int = 10,
    memory_type: str = None,
) -> str:
    """Search the Hephaestus knowledge base for relevant memories.

    Args:
        query: What to search for (natural-language description)
        agent_id: Your agent ID — used to auto-scope results to your project
        limit: Max number of results to return (default 10)
        memory_type: Optional filter (error_fix/discovery/decision/learning/warning/codebase_knowledge)

    Search before reinventing something another agent already figured out.
    """
    try:
        async with httpx.AsyncClient() as client:
            payload = {"query": query, "limit": limit}
            if memory_type:
                payload["memory_type"] = memory_type

            headers = {"Content-Type": "application/json"}
            if agent_id:
                headers["X-Agent-ID"] = agent_id

            response = await client.post(
                f"{HEPHAESTUS_URL}/search_memory",
                json=payload,
                headers=headers,
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                results = result.get("results", [])
                if not results:
                    return "No matching memories found."
                lines = [f"Found {len(results)} memories:"]
                for r in results:
                    lines.append(
                        f"- [{r.get('memory_type', '?')}] {r.get('content', '')[:300]}"
                    )
                return "\n".join(lines)
            else:
                return f"❌ Failed to search memory: {response.text}"
    except Exception as e:
        return f"❌ Error searching memory: {str(e)}"


@mcp.tool(name="update_task_status")
async def update_task_status(
    task_id: str = None,
    agent_id: str = None,
    status: str = None,
    summary: str = "",
    failure_reason: str = "",
    key_learnings: list = None,
) -> str:
    """Update the status of a task in Hephaestus.

    Args:
        task_id: The ID of the task to update. Falls back to this process's own
            task if omitted.
        agent_id: Your agent ID (CRITICAL: must match YOUR agent ID from your initial prompt).
            Falls back to this process's own agent identity if omitted.
        status: New status (done/failed/in_progress)
        summary: Summary of what was accomplished (for done status)
        failure_reason: Reason for failure (for failed status)
        key_learnings: List of key learnings from the task

    CRITICAL: agent_id must match YOUR agent ID from your initial prompt.
    Example (use your actual ID from prompt):
        update_task_status(
            agent_id="6a062184-e189-4d8d-8376-89da987b9996",  # Your actual UUID
            task_id="dc2c0279-ba16-4a8d-9fd5-846259967e68",
            status="done",
            summary="Task completed successfully"
        )

    DO NOT use 'agent-mcp' or any placeholder - it will cause "Agent not authorized" errors!
    """
    # Same fallback as save_memory -- models occasionally omit these even
    # when the rendered prompt example shows them filled in. Note this only
    # helps when the model OMITS the param entirely: `x or env` still keeps
    # a WRONG-but-non-empty value the model supplied, since a truthy string
    # always wins over the fallback. Observed live: a model that
    # consistently passed a truncated 8-char task_id (this codebase's own
    # logs display them that way everywhere) never triggered this fallback
    # even once, because it always supplied *something*. complete_my_task
    # below closes that gap by not accepting task_id/agent_id as parameters
    # at all.
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    task_id = task_id or os.environ.get("HEPHAESTUS_TASK_ID")
    if not agent_id:
        return "❌ Failed to update task status: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    if not task_id:
        return "❌ Failed to update task status: task_id was not provided and HEPHAESTUS_TASK_ID is not set in the environment"
    if not status:
        return "❌ Failed to update task status: status is required (done/failed/in_progress)"
    return await _post_task_status(task_id, agent_id, status, summary, failure_reason, key_learnings)


async def _post_task_status(
    task_id: str,
    agent_id: str,
    status: str,
    summary: str,
    failure_reason: str,
    key_learnings: list,
) -> str:
    """Shared HTTP call + response formatting for update_task_status and
    complete_my_task -- the only difference between them is how task_id/
    agent_id get resolved before reaching here."""
    logger.info(
        f"_post_task_status CALL task_id={task_id} agent_id={agent_id} status={status}"
    )
    if status == "done" and not summary.strip():
        return (
            "❌ Failed to complete task: summary is required when status='done' "
            "— describe what you accomplished."
        )
    try:
        async with httpx.AsyncClient() as client:
            payload = {
                "task_id": task_id,
                "status": status,
                "agent_id": agent_id,
                "key_learnings": key_learnings or [],
            }

            if summary:
                payload["summary"] = summary
            if failure_reason:
                payload["failure_reason"] = failure_reason

            response = await client.post(
                f"{HEPHAESTUS_URL}/update_task_status",
                json=payload,
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )
            logger.info(
                f"_post_task_status RESPONSE task_id={task_id} "
                f"status_code={response.status_code} body={response.text[:500]}"
            )

            if response.status_code == 200:
                result = response.json()
                message = result.get("message", f"Task {status} successfully")

                # Use appropriate emoji based on message content
                if "validation" in message.lower():
                    status_emoji = "🔍"  # Magnifying glass for validation
                elif status == "done":
                    status_emoji = "✅"
                elif status == "failed":
                    status_emoji = "❌"
                else:
                    status_emoji = "🔄"

                return f"{status_emoji} {message}"
            else:
                return f"❌ Failed to update task status: {response.text}"
    except Exception as e:
        logger.exception(
            f"_post_task_status EXCEPTION task_id={task_id} agent_id={agent_id} status={status}"
        )
        return f"❌ Error updating task status: {str(e)}"


@mcp.tool(name="complete_my_task")
async def complete_my_task(
    status: str = None,
    summary: str = "",
    failure_reason: str = "",
    key_learnings: list = None,
) -> str:
    """Mark YOUR OWN currently-assigned task done or failed.

    IMPORTANT: Do NOT pass agent_id or task_id — this tool doesn't accept them.
    They are read from environment variables automatically.

    Args:
        status: New status (done/failed/in_progress)
        summary: Summary of what was accomplished. REQUIRED for done status.
        failure_reason: Reason for failure (for failed status)
        key_learnings: List of key learnings from the task

    Example:
        complete_my_task(status="done", summary="Fixed 3 vulnerabilities...")
    """
    agent_id = os.environ.get("HEPHAESTUS_AGENT_ID")
    task_id = os.environ.get("HEPHAESTUS_TASK_ID")
    if not agent_id or not task_id:
        return (
            "❌ Failed to complete task: HEPHAESTUS_AGENT_ID/HEPHAESTUS_TASK_ID "
            "are not set in this session's environment. Use "
            "update_task_status with the explicit task_id/agent_id from "
            "your initial prompt instead."
        )
    if not status:
        return "❌ Failed to complete task: status is required (done/failed/in_progress)"
    return await _post_task_status(task_id, agent_id, status, summary, failure_reason, key_learnings)


@mcp.tool(name="give_validation_review")
async def give_validation_review(
    task_id: str = None,
    validator_agent_id: str = None,
    validation_passed: bool = None,
    feedback: str = "",
    evidence: list = None,
    recommendations: list = None,
) -> str:
    """Submit validation review for a task.

    Args:
        task_id: The ID of the task being validated. Falls back to this
            process's own task if omitted.
        validator_agent_id: Your validator agent ID. Falls back to this
            process's own agent identity if omitted.
        validation_passed: Whether validation passed (true/false)
        feedback: Detailed feedback about what passed/failed
        evidence: List of evidence items supporting your decision (optional)
        recommendations: List of recommended follow-up tasks if validation passes (optional)

    This tool should only be called by validator agents after reviewing a task.
    """
    validator_agent_id = validator_agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    task_id = task_id or os.environ.get("HEPHAESTUS_TASK_ID")
    if not validator_agent_id:
        return "❌ Error submitting validation review: validator_agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    if not task_id:
        return "❌ Error submitting validation review: task_id was not provided and HEPHAESTUS_TASK_ID is not set in the environment"
    if validation_passed is None:
        return "❌ Error submitting validation review: validation_passed is required (true/false)"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/give_validation_review",
                json={
                    "task_id": task_id,
                    "validator_agent_id": validator_agent_id,
                    "validation_passed": validation_passed,
                    "feedback": feedback,
                    "evidence": evidence or [],
                    "recommendations": recommendations or [],
                },
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-ID": validator_agent_id,
                },
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                status_emoji = "✅" if result.get("status") == "completed" else "🔄"
                return f"""{status_emoji} Validation Review Submitted!
Status: {result.get("status", "unknown")}
Message: {result.get("message", "")}
Iteration: {result.get("iteration", "N/A")}"""
            else:
                return f"❌ Failed to submit validation review: {response.text}"
    except Exception as e:
        return f"❌ Error submitting validation review: {str(e)}"


@mcp.tool(name="validate_my_agent_id")
async def validate_my_agent_id(
    agent_id: str = None, workflow_id: str = None, task_id: str = None
) -> str:
    """Validate that your agent ID has the correct format before using it.

    Args:
        agent_id: The agent ID you plan to use. Falls back to this process's
            own agent identity if omitted.
        workflow_id: Not used by this tool — accepted and ignored so agents that pass it
            (per the general "include workflow_id on every call" habit) don't get rejected.
        task_id: Not used by this tool — accepted and ignored for the same reason.

    Returns:
        Validation result with helpful error messages if invalid

    Use this tool if you're unsure about your agent ID format!
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error validating agent ID: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/validate_agent_id/{agent_id}", timeout=5.0
            )

            if response.status_code == 200:
                result = response.json()
                if result["valid"]:
                    return f"✅ {result['message']}"
                else:
                    mistakes = "\n".join(f"  • {m}" for m in result["common_mistakes"])
                    return f"""❌ {result["message"]}

Common mistakes:
{mistakes}

Check your initial prompt for "Your Agent ID:" - it should be a UUID like:
  6a062184-e189-4d8d-8376-89da987b9996"""
            else:
                return f"❌ Validation failed: {response.text}"
    except Exception as e:
        return f"❌ Error validating agent ID: {str(e)}"


@mcp.tool(name="get_agent_status")
async def get_agent_status() -> str:
    """Get status of all active agents in Hephaestus"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/agent_status",
                headers={"X-Agent-ID": DEFAULT_AGENT_ID},
                timeout=10.0,
            )

            if response.status_code == 200:
                agents = response.json()
                if not agents:
                    return "🤖 No active agents"

                agent_list = []
                for agent in agents:
                    status_emoji = "🟢" if agent["status"] == "working" else "🔴"
                    agent_list.append(
                        f"{status_emoji} {agent['id'][:8]}: {agent['status']} - Task: {agent.get('current_task_id', 'none')[:8] if agent.get('current_task_id') else 'none'}"
                    )
                return "🤖 Active Agents:\n" + "\n".join(agent_list)
            else:
                return f"❌ Failed to get agent status: {response.text}"
    except Exception as e:
        return f"❌ Error getting agent status: {str(e)}"


@mcp.tool(name="submit_result")
async def submit_result(
    markdown_file_path: str,
    agent_id: str = None,
    explanation: str = "",
    evidence: list = None,
    extra_files: list = None,
) -> str:
    """Submit a workflow result with evidence for validation.

    Args:
        markdown_file_path: Path to markdown file with solution and evidence
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        explanation: Brief explanation of what was accomplished
        evidence: List of evidence supporting completion (optional)
        extra_files: List of additional file paths (e.g., patches, reproduction scripts) for validators (optional)

    Use when you have found the definitive solution to a workflow problem.
    The markdown file should contain comprehensive evidence including:
    - Clear solution statement
    - Execution outputs and proof
    - Step-by-step methodology
    - Reproduction steps for verification

    For SWEBench workflows, you should include:
    - extra_files: ["./solution.patch", "./reproduction_instructions.md"]
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error submitting result: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/submit_result",
                json={
                    "markdown_file_path": markdown_file_path,
                    "explanation": explanation,
                    "evidence": evidence or [],
                    "extra_files": extra_files or [],
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                validation_info = f"\n🔍 Validation: {'Triggered' if result.get('validation_triggered') else 'Not required'}"
                return f"""✅ Result submitted successfully!
Result ID: {result.get("result_id", "unknown")}
Workflow ID: {result.get("workflow_id", "unknown")}
Status: {result.get("status", "unknown")}{validation_info}
Message: {result.get("message", "")}"""
            else:
                return f"❌ Failed to submit result: {response.text}"
    except Exception as e:
        return f"❌ Error submitting result: {str(e)}"


@mcp.tool(name="submit_result_validation")
async def submit_result_validation(
    result_id: str, validation_passed: bool, feedback: str, evidence: list = None
) -> str:
    """Submit validation review for a workflow result.

    Args:
        result_id: ID of the result being validated (REQUIRED - this is the full result ID you were given)
        validation_passed: Whether the result meets criteria (true/false)
        feedback: Detailed validation feedback explaining decision
        evidence: Evidence supporting the decision (list of dicts, optional)

    This tool should only be called by result validator agents after reviewing
    a submitted workflow result against the configured criteria.

    IMPORTANT: You must use the complete result_id that was provided to you (e.g., result-a3145b59-e954-434e-a254-962ef2d1f669).
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/submit_result_validation",
                json={
                    "result_id": result_id,
                    "validation_passed": validation_passed,
                    "feedback": feedback,
                    "evidence": evidence or [],
                },
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                workflow_action = result.get("workflow_action_taken", "none")
                action_emoji = "🛑" if workflow_action == "workflow_terminated" else "▶️"
                action_text = (
                    f"\n{action_emoji} Workflow Action: {workflow_action}"
                    if workflow_action != "none"
                    else ""
                )

                return f"""✅ Result Validation Submitted!
Status: {result.get("status", "unknown")}
Message: {result.get("message", "")}{action_text}
Result ID: {result.get("result_id", "unknown")}"""
            else:
                return f"❌ Failed to submit result validation: {response.text}"
    except Exception as e:
        return f"❌ Error submitting result validation: {str(e)}"


@mcp.tool(name="get_workflow_results")
async def get_workflow_results(workflow_id: str = None) -> str:
    """Get all submitted results for a workflow.

    Args:
        workflow_id: ID of the workflow. Falls back to this process's own
            workflow if omitted.

    Returns list of results with their validation status and details.
    """
    workflow_id = workflow_id or os.environ.get("HEPHAESTUS_WORKFLOW_ID")
    if not workflow_id:
        return "❌ Error getting workflow results: workflow_id was not provided and HEPHAESTUS_WORKFLOW_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/workflows/{workflow_id}/results",
                headers={"X-Agent-ID": DEFAULT_AGENT_ID},
                timeout=10.0,
            )

            if response.status_code == 200:
                results = response.json()
                if not results:
                    return f"📋 No results found for workflow {workflow_id}"

                result_list = []
                for result in results:
                    status_emoji = (
                        "✅"
                        if result["status"] == "validated"
                        else ("❌" if result["status"] == "rejected" else "⏳")
                    )
                    # Show full result_id - critical for validators to use correct ID
                    result_list.append(
                        f"{status_emoji} {result['result_id']}: {result['status']} by {result['agent_id'][:8]}"
                    )
                return "📋 Workflow Results:\n" + "\n".join(result_list)
            else:
                return f"❌ Failed to get workflow results: {response.text}"
    except Exception as e:
        return f"❌ Error getting workflow results: {str(e)}"


@mcp.tool(name="broadcast_message")
async def broadcast_message(message: str, sender_agent_id: str = None) -> str:
    """Broadcast a message to all active agents in the system.

    Use this when you have information that ALL other agents should know about,
    or when you need help but don't know which specific agent to ask.

    Args:
        message: The message content to broadcast to all agents
        sender_agent_id: Your agent ID (REQUIRED - use your assigned agent ID)

    Examples of when to use broadcast:
    - "I found a critical bug in module X that affects everyone"
    - "Does anyone have information about how authentication works?"
    - "I've completed the database schema - all agents can now use it"
    - "Warning: The API endpoint /users is currently down"

    The message will be delivered to all active agents with the prefix:
    [AGENT {your_id} BROADCAST]: {your_message}
    """
    sender_agent_id = sender_agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not sender_agent_id:
        return "❌ Error broadcasting message: sender_agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/broadcast_message",
                json={"message": message},
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-ID": sender_agent_id,
                },
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                recipient_count = result.get("recipient_count", 0)
                if recipient_count == 0:
                    return "📢 Broadcast sent, but no other agents are currently active"
                return (
                    f"📢 Message broadcast successfully to {recipient_count} agent(s)"
                )
            else:
                return f"❌ Failed to broadcast message: {response.text}"
    except Exception as e:
        return f"❌ Error broadcasting message: {str(e)}"


@mcp.tool(name="send_message")
async def send_message(
    message: str, sender_agent_id: str = None, recipient_agent_id: str = None
) -> str:
    """Send a direct message to a specific agent.

    Use this when you know which specific agent you want to communicate with,
    such as asking for help from an agent working on a related task or
    providing targeted information to a specific agent.

    Args:
        message: The message content to send
        sender_agent_id: Your agent ID (REQUIRED - use your assigned agent ID)
        recipient_agent_id: The ID of the agent you want to message

    Examples of when to use direct messaging:
    - "Agent X: I need the API specs you were working on"
    - "Agent Y: Your task conflicts with mine - can we coordinate?"
    - "Agent Z: I found the answer to your earlier question about caching"
    - "Agent W: Can you review my implementation before I submit?"

    The message will be delivered with the prefix:
    [AGENT {your_id} TO AGENT {recipient_id}]: {your_message}

    Tip: Use get_agent_status() to see which agents are currently active
    and what tasks they're working on before sending a message.
    """
    sender_agent_id = sender_agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not sender_agent_id:
        return "❌ Error sending message: sender_agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    if not recipient_agent_id:
        return "❌ Error sending message: recipient_agent_id is required"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/send_message",
                json={"recipient_agent_id": recipient_agent_id, "message": message},
                headers={
                    "Content-Type": "application/json",
                    "X-Agent-ID": sender_agent_id,
                },
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("success"):
                    return (
                        f"✉️ Message sent successfully to agent {recipient_agent_id[:8]}"
                    )
                else:
                    return f"❌ {result.get('message', 'Failed to send message')}"
            else:
                return f"❌ Failed to send message: {response.text}"
    except Exception as e:
        return f"❌ Error sending message: {str(e)}"


# ==================== TICKET TRACKING SYSTEM TOOLS ====================


@mcp.tool(name="create_ticket")
async def create_ticket(
    title: str,
    description: str,
    agent_id: str = None,
    workflow_id: str = None,
    ticket_type: str = "task",
    priority: str = "medium",
    tags: list = None,
    blocked_by_ticket_ids: list = None,
    assigned_agent_id: str = None,
    parent_ticket_id: str = None,
    task_id: str = None,
    phase_id: str = None,
) -> str:
    """Create a new ticket in the workflow tracking system.

    Use this when you discover work that needs to be tracked separately from tasks.
    Returns similar tickets for duplicate detection.

    Args:
        agent_id: Your agent ID (CRITICAL: use YOUR UUID from initial prompt, e.g., "84f15f6c-35b1-4d57-97ac-92a3c0c94d29")
        workflow_id: Your workflow ID (CRITICAL: use YOUR workflow UUID from initial prompt)
        title: Short, descriptive title for the ticket (3-500 chars)
        description: Detailed description of what needs to be done (min 10 chars)
        ticket_type: Type of ticket (bug/feature/improvement/task/spike) - default: task
        priority: Priority level (low/medium/high/critical) - default: medium
        tags: Optional list of tags for categorization
        blocked_by_ticket_ids: List of ticket IDs that block this ticket
        assigned_agent_id: Optional agent to assign ticket to
        parent_ticket_id: Optional parent ticket for sub-tickets
        task_id: Optional task ID this ticket relates to (e.g., the task that found this issue)
        phase_id: Optional phase ID where this ticket was created

    CRITICAL: Both agent_id and workflow_id must be your actual UUIDs from your initial prompt!
    DO NOT use 'agent' or 'agent-mcp' - it will fail with "Agent not found"!

    IMPORTANT: Search for existing tickets before creating to avoid duplicates!
    Use search_tickets() with semantic search to find related work.
    """
    import logging
    import os

    logger = logging.getLogger(__name__)

    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    workflow_id = workflow_id or os.environ.get("HEPHAESTUS_WORKFLOW_ID")
    if not agent_id:
        return "❌ Error creating ticket: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    if not workflow_id:
        return "❌ Error creating ticket: workflow_id was not provided and HEPHAESTUS_WORKFLOW_ID is not set in the environment"

    logger.info("[MCP_CLIENT_TICKET] ========== START ==========")
    logger.info(f"[MCP_CLIENT_TICKET] Agent: {agent_id}")
    logger.info(f"[MCP_CLIENT_TICKET] Title: {title[:60]}...")
    logger.info(f"[MCP_CLIENT_TICKET] Type: {ticket_type}, Priority: {priority}")

    # Use MCP_TOOL_TIMEOUT if set (for human approval workflows), otherwise default to 10 seconds
    mcp_timeout_ms = os.environ.get("MCP_TOOL_TIMEOUT")
    if mcp_timeout_ms:
        timeout_seconds = float(mcp_timeout_ms) / 1000.0
        logger.info(
            f"[MCP_CLIENT_TICKET] Using MCP_TOOL_TIMEOUT: {timeout_seconds}s ({mcp_timeout_ms}ms)"
        )
    else:
        timeout_seconds = 10.0
        logger.info(
            f"[MCP_CLIENT_TICKET] No MCP_TOOL_TIMEOUT set, using default: {timeout_seconds}s"
        )

    try:
        async with httpx.AsyncClient() as client:
            payload = {
                "workflow_id": workflow_id,
                "title": title,
                "description": description,
                "ticket_type": ticket_type,
                "priority": priority,
                "tags": tags or [],
                "blocked_by_ticket_ids": blocked_by_ticket_ids or [],
                "assigned_agent_id": assigned_agent_id,
                "parent_ticket_id": parent_ticket_id,
                "task_id": task_id,
                "phase_id": phase_id,
                "agent_id": agent_id,
            }

            logger.info(f"[MCP_CLIENT_TICKET] Payload: {payload}")
            logger.info(
                f"[MCP_CLIENT_TICKET] Sending POST to {HEPHAESTUS_URL}/api/tickets/create"
            )

            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/create",
                json=payload,
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=timeout_seconds,
            )

            logger.info(f"[MCP_CLIENT_TICKET] Response status: {response.status_code}")
            logger.info(f"[MCP_CLIENT_TICKET] Response body: {response.text}")

            if response.status_code == 200:
                result = response.json()
                logger.info(
                    f"[MCP_CLIENT_TICKET] ✅ Success! Ticket ID: {result.get('ticket_id')}"
                )

                similar_msg = ""
                if result.get("similar_tickets"):
                    similar_msg = f"\n\n⚠️ Found {len(result['similar_tickets'])} similar tickets - check for duplicates!"

                success_message = f"""✅ Ticket created successfully!
Ticket ID: {result.get("ticket_id", "unknown")}
Status: {result.get("status", "unknown")}
Message: {result.get("message", "")}{similar_msg}"""
                logger.info("[MCP_CLIENT_TICKET] Returning success message to agent")
                logger.info("[MCP_CLIENT_TICKET] ========== SUCCESS ==========")
                return success_message
            else:
                error_message = f"❌ Failed to create ticket: {response.text}"
                logger.error(
                    f"[MCP_CLIENT_TICKET] ❌ HTTP {response.status_code}: {response.text}"
                )
                logger.error("[MCP_CLIENT_TICKET] Returning error message to agent")
                logger.error("[MCP_CLIENT_TICKET] ========== FAILED ==========")
                return error_message
    except Exception as e:
        error_message = f"❌ Error creating ticket: {str(e)}"
        logger.error(f"[MCP_CLIENT_TICKET] ❌ Exception: {type(e).__name__}: {e}")
        logger.error("[MCP_CLIENT_TICKET] ========== EXCEPTION ==========")
        return error_message


@mcp.tool(name="update_ticket")
async def update_ticket(
    ticket_id: str, updates: dict, agent_id: str = None, update_comment: str = None
) -> str:
    """Update ticket fields (title, description, priority, tags, assigned_agent_id, blocked_by_ticket_ids).

    Cannot change status - use change_ticket_status for that.

    Args:
        ticket_id: ID of the ticket to update
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        updates: Fields to update (dict with keys: title, description, priority, assigned_agent_id, ticket_type, tags, blocked_by_ticket_ids)
        update_comment: Optional comment explaining the update
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error updating ticket: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/update",
                json={
                    "ticket_id": ticket_id,
                    "updates": updates,
                    "update_comment": update_comment,
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                return f"""✅ Ticket updated successfully!
Ticket ID: {ticket_id}
Fields updated: {", ".join(result.get("fields_updated", []))}
Message: {result.get("message", "")}"""
            else:
                return f"❌ Failed to update ticket: {response.text}"
    except Exception as e:
        return f"❌ Error updating ticket: {str(e)}"


@mcp.tool(name="update_ticket_status")
async def change_ticket_status(
    ticket_id: str,
    new_status: str,
    comment: str,
    agent_id: str = None,
    commit_sha: str = None,
) -> str:
    """Move ticket to a different status column.

    IMPORTANT: Blocked tickets (with blocked_by_ticket_ids) cannot change status until blockers are resolved.

    Args:
        ticket_id: ID of the ticket
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        new_status: New status (must match board_config columns)
        comment: Required comment explaining status change (min 10 chars)
        commit_sha: Optional commit SHA to link to this status change
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error changing ticket status: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/change-status",
                json={
                    "ticket_id": ticket_id,
                    "new_status": new_status,
                    "comment": comment,
                    "commit_sha": commit_sha,
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                if result.get("blocked"):
                    blocking_ids = ", ".join(result.get("blocking_ticket_ids", []))
                    return f"""🔒 Ticket is BLOCKED!
Ticket ID: {ticket_id}
Blocked by: {blocking_ids}
Cannot change status until blocking tickets are resolved."""
                else:
                    return f"""✅ Ticket status changed!
Ticket ID: {ticket_id}
From: {result.get("old_status", "unknown")}
To: {result.get("new_status", "unknown")}"""
            else:
                return f"❌ Failed to change ticket status: {response.text}"
    except Exception as e:
        return f"❌ Error changing ticket status: {str(e)}"


@mcp.tool(name="add_ticket_comment")
async def add_ticket_comment(
    ticket_id: str,
    comment_text: str,
    agent_id: str = None,
    comment_type: str = "general",
    mentions: list = None,
) -> str:
    """Add a comment to a ticket.

    Use for progress updates, blockers, or communication with other agents.

    Args:
        ticket_id: ID of the ticket
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        comment_text: Comment text (min 1 char)
        comment_type: Type of comment (general/status_change/blocker/resolution) - default: general
        mentions: Agent/ticket IDs mentioned in comment
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error adding comment: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/comment",
                json={
                    "ticket_id": ticket_id,
                    "comment_text": comment_text,
                    "comment_type": comment_type,
                    "mentions": mentions or [],
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                return f"✅ Comment added to ticket {ticket_id}"
            else:
                return f"❌ Failed to add comment: {response.text}"
    except Exception as e:
        return f"❌ Error adding comment: {str(e)}"


@mcp.tool(name="search_tickets")
async def search_tickets(
    query: str,
    agent_id: str = None,
    workflow_id: str = None,
    search_type: str = "hybrid",
    filters: dict = None,
    limit: int = 10,
    include_comments: bool = True,
) -> str:
    """Search for tickets using HYBRID search (70% semantic + 30% keyword) by default.

    Use natural language queries. Shows blocked (🔒) and resolved (✅) indicators.

    Args:
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        workflow_id: Your workflow ID (REQUIRED - searches within this workflow only).
            Falls back to this process's own workflow if omitted.
        query: Search query (natural language, min 3 chars)
        search_type: Search mode (semantic/keyword/hybrid) - DEFAULT: hybrid = 70% semantic + 30% keyword
        filters: Optional filters (dict with keys: status, priority, ticket_type, assigned_agent_id, tags, is_blocked)
        limit: Max number of results (1-50) - default: 10
        include_comments: Whether to search in comments too - default: true

    BEST PRACTICE: Use hybrid search (default) for best results!
    - Hybrid combines semantic understanding with keyword precision
    - Semantic search is good for conceptual queries
    - Keyword search is good for exact term matching
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    workflow_id = workflow_id or os.environ.get("HEPHAESTUS_WORKFLOW_ID")
    if not agent_id:
        return "❌ Error searching tickets: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    if not workflow_id:
        return "❌ Error searching tickets: workflow_id was not provided and HEPHAESTUS_WORKFLOW_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/search",
                json={
                    "workflow_id": workflow_id,
                    "query": query,
                    "search_type": search_type,
                    "filters": filters or {},
                    "limit": limit,
                    "include_comments": include_comments,
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                if not result.get("results"):
                    return f"🔍 No tickets found for query: '{query}'"

                ticket_list = []
                for ticket in result.get("results", []):
                    blocked_icon = "🔒" if ticket.get("is_blocked") else ""
                    resolved_icon = "✅" if ticket.get("is_resolved") else ""
                    ticket_list.append(
                        f"{blocked_icon}{resolved_icon} {ticket['ticket_id'][:12]}: [{ticket['status']}] {ticket['title'][:60]} (score: {ticket.get('relevance_score', 0):.2f})"
                    )

                search_mode_msg = (
                    f"({search_type} search: "
                    + (
                        "70% semantic + 30% keyword"
                        if search_type == "hybrid"
                        else search_type
                    )
                    + ")"
                )

                return f"""🔍 Found {result.get("total_found", 0)} tickets {search_mode_msg}
Search time: {result.get("search_time_ms", 0):.0f}ms

{chr(10).join(ticket_list)}

💡 Tip: Use hybrid search (default) for best results!"""
            else:
                return f"❌ Failed to search tickets: {response.text}"
    except Exception as e:
        return f"❌ Error searching tickets: {str(e)}"


@mcp.tool(name="get_ticket")
async def get_ticket(ticket_id: str) -> str:
    """Get detailed information about a specific ticket by its exact ID.

    IMPORTANT: You MUST provide the EXACT, COMPLETE ticket ID.

    Args:
        ticket_id: The complete ticket ID (e.g., "ticket-c368a0d1-cbd7-4231-a374-0a3a7374064e")
                   Do NOT use shortened IDs like "ticket-c368a"!

    Returns:
        Complete ticket details including:
        - Full description
        - All comments with timestamps
        - Complete history of status changes
        - All linked commits with file changes
        - Blocking/blocked relationships
        - Tags and metadata

    If you DON'T know the exact ticket ID:
        1. Use search_tickets() to find tickets by title/description
        2. Use get_tickets() to list all tickets
        3. Then use this function with the exact ticket_id from those results

    Example workflow:
        # First, search for the ticket
        search_result = search_tickets(
            agent_id="your-id",
            query="Frontend Infrastructure",
            search_type="hybrid"
        )
        # Note the exact ticket_id from results: ticket-c368a0d1-cbd7-4231-a374-0a3a7374064e

        # Then get full details
        details = get_ticket("ticket-c368a0d1-cbd7-4231-a374-0a3a7374064e")
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/api/tickets/{ticket_id}", timeout=10.0
            )

            if response.status_code == 200:
                data = response.json()
                ticket = data.get("ticket", {})
                comments = data.get("comments", [])
                history = data.get("history", [])
                commits = data.get("commits", [])

                # Format the output
                result = []

                # Header
                blocked_icon = "🔒 " if ticket.get("is_blocked") else ""
                resolved_icon = "✅ " if ticket.get("is_resolved") else ""
                result.append(f"{'=' * 80}")
                result.append(f"{blocked_icon}{resolved_icon}TICKET: {ticket['id']}")
                result.append(f"{'=' * 80}")

                # Basic info
                result.append("\n📋 BASIC INFORMATION")
                result.append(f"Title: {ticket['title']}")
                result.append(f"Status: {ticket['status']}")
                result.append(f"Type: {ticket['ticket_type']}")
                result.append(f"Priority: {ticket['priority']}")
                result.append(f"Created: {ticket['created_at']}")
                result.append(f"Updated: {ticket['updated_at']}")

                if ticket.get("assigned_agent_id"):
                    result.append(f"Assigned to: {ticket['assigned_agent_id']}")

                if ticket.get("tags"):
                    result.append(f"Tags: {', '.join(ticket['tags'])}")

                # Blocking info
                if ticket.get("blocked_by_ticket_ids"):
                    result.append("\n🔒 BLOCKED BY:")
                    for blocking_id in ticket["blocked_by_ticket_ids"]:
                        result.append(f"  - {blocking_id}")

                # Description
                result.append("\n📝 DESCRIPTION")
                result.append(ticket["description"])

                # Comments
                if comments:
                    result.append(f"\n💬 COMMENTS ({len(comments)})")
                    for comment in comments:
                        result.append(
                            f"\n  [{comment['created_at']}] {comment['agent_id'][:8]}..."
                        )
                        result.append(f"  Type: {comment['comment_type']}")
                        result.append(f"  {comment['comment_text']}")

                # History
                if history:
                    result.append(f"\n📜 HISTORY ({len(history)})")
                    for event in history[-10:]:  # Last 10 events
                        result.append(
                            f"\n  [{event['changed_at']}] {event['change_type']}"
                        )
                        if event.get("old_value") and event.get("new_value"):
                            result.append(
                                f"  {event['old_value']} → {event['new_value']}"
                            )
                        if event.get("change_description"):
                            result.append(f"  {event['change_description']}")

                # Commits
                if commits:
                    result.append(f"\n🔨 LINKED COMMITS ({len(commits)})")
                    for commit in commits:
                        result.append(
                            f"\n  {commit['commit_sha'][:8]}: {commit['commit_message'][:60]}"
                        )
                        result.append(
                            f"  Files: {commit['files_changed']}, +{commit['insertions']} -{commit['deletions']}"
                        )
                        if commit.get("files_list"):
                            result.append(
                                f"  Modified: {', '.join(commit['files_list'][:5])}"
                            )

                result.append(f"\n{'=' * 80}")

                return "\n".join(result)

            elif response.status_code == 404:
                return f"❌ Ticket not found: {ticket_id}\n\nMake sure you're using the COMPLETE ticket ID (e.g., ticket-c368a0d1-cbd7-4231-a374-0a3a7374064e)\nUse search_tickets() or get_tickets() to find the correct ticket ID."
            else:
                return f"❌ Failed to get ticket: {response.text}"
    except Exception as e:
        return f"❌ Error getting ticket: {str(e)}"


@mcp.tool(name="get_tickets")
async def get_tickets(
    agent_id: str = None,
    workflow_id: str = None,
    status: str = None,
    ticket_type: str = None,
    priority: str = None,
    assigned_agent_id: str = None,
    include_completed: bool = True,
    limit: int = 50,
    offset: int = 0,
    sort_by: str = "created_at",
    sort_order: str = "desc",
) -> str:
    """List tickets with filtering and pagination.

    Shows blocked (🔒) and resolved (✅) indicators.

    Args:
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        workflow_id: Your workflow ID (REQUIRED - lists tickets in this workflow only).
            Falls back to this process's own workflow if omitted.
        status: Filter by status
        ticket_type: Filter by type
        priority: Filter by priority
        assigned_agent_id: Filter by assigned agent
        include_completed: Include completed tickets - default: true
        limit: Max number of results (1-200) - default: 50
        offset: Offset for pagination - default: 0
        sort_by: Sort field (created_at/updated_at/priority/status) - default: created_at
        sort_order: Sort order (asc/desc) - default: desc
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    workflow_id = workflow_id or os.environ.get("HEPHAESTUS_WORKFLOW_ID")
    if not agent_id:
        return "❌ Error getting tickets: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    if not workflow_id:
        return "❌ Error getting tickets: workflow_id was not provided and HEPHAESTUS_WORKFLOW_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            params = {
                "workflow_id": workflow_id,
                "include_completed": include_completed,
                "limit": limit,
                "offset": offset,
                "sort_by": sort_by,
                "sort_order": sort_order,
            }
            if status:
                params["status"] = status
            if ticket_type:
                params["ticket_type"] = ticket_type
            if priority:
                params["priority"] = priority
            if assigned_agent_id:
                params["assigned_agent_id"] = assigned_agent_id

            response = await client.get(
                f"{HEPHAESTUS_URL}/api/tickets",
                params=params,
                headers={"X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                if not result.get("tickets"):
                    return "📋 No tickets found"

                ticket_list = []
                for ticket in result.get("tickets", []):
                    blocked_icon = "🔒" if ticket.get("is_blocked") else ""
                    resolved_icon = "✅" if ticket.get("is_resolved") else ""
                    ticket_list.append(
                        f"{blocked_icon}{resolved_icon} {ticket['ticket_id'][:12]}: [{ticket['status']}] {ticket['title'][:60]}"
                    )

                return f"""📋 Found {result.get("total_count", 0)} tickets (showing {len(result.get("tickets", []))})
Has more: {result.get("has_more", False)}

{chr(10).join(ticket_list)}"""
            else:
                return f"❌ Failed to get tickets: {response.text}"
    except Exception as e:
        return f"❌ Error getting tickets: {str(e)}"


@mcp.tool(name="link_commit_to_ticket")
async def link_commit_to_ticket(
    ticket_id: str, commit_sha: str, agent_id: str = None, commit_message: str = None
) -> str:
    """Manually link a git commit to a ticket for traceability.

    Auto-linking happens on task completion.

    Args:
        ticket_id: ID of the ticket
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        commit_sha: Git commit SHA
        commit_message: Optional commit message (auto-fetched if not provided)
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error linking commit: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/link-commit",
                json={
                    "ticket_id": ticket_id,
                    "commit_sha": commit_sha,
                    "commit_message": commit_message,
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                return f"✅ Commit {commit_sha[:8]} linked to ticket {ticket_id}"
            else:
                return f"❌ Failed to link commit: {response.text}"
    except Exception as e:
        return f"❌ Error linking commit: {str(e)}"


@mcp.tool(name="get_commit_diff")
async def get_commit_diff(commit_sha: str, agent_id: str = None) -> str:
    """Get detailed git diff for a commit (used by Git Diff Window in UI).

    Returns structured diff data with file changes, insertions, deletions.

    Args:
        commit_sha: Git commit SHA to get diff for
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error getting commit diff: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/api/tickets/commit-diff/{commit_sha}",
                headers={"X-Agent-ID": agent_id},
                timeout=30.0,  # Longer timeout for git operations
            )

            if response.status_code == 200:
                result = response.json()
                file_summary = []
                for file_info in result.get("files", []):
                    file_summary.append(
                        f"  {file_info['status'][:1].upper()}: {file_info['path']} (+{file_info['insertions']} -{file_info['deletions']})"
                    )

                return f"""📊 Commit {commit_sha[:8]}
Message: {result.get("message", "No message")}
Author: {result.get("author_agent_id", "unknown")}
Files changed: {result.get("files_changed", 0)}
+{result.get("insertions", 0)} -{result.get("deletions", 0)}

{chr(10).join(file_summary) if file_summary else "No files changed"}"""
            else:
                return f"❌ Failed to get commit diff: {response.text}"
    except Exception as e:
        return f"❌ Error getting commit diff: {str(e)}"


@mcp.tool(name="resolve_ticket")
async def resolve_ticket(
    ticket_id: str, resolution_comment: str, agent_id: str = None, commit_sha: str = None
) -> str:
    """Mark ticket as resolved.

    IMPORTANT: Automatically unblocks all tickets that were blocked by this ticket.
    Returns list of unblocked ticket IDs.

    Args:
        ticket_id: ID of the ticket to resolve
        agent_id: Your agent ID. Falls back to this process's own agent identity if omitted.
        resolution_comment: Comment explaining resolution (min 10 chars)
        commit_sha: Optional commit SHA that resolved the ticket
    """
    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error resolving ticket: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/resolve",
                json={
                    "ticket_id": ticket_id,
                    "resolution_comment": resolution_comment,
                    "commit_sha": commit_sha,
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                unblocked = result.get("unblocked_tickets", [])
                unblocked_msg = ""
                if unblocked:
                    unblocked_msg = f"\n🔓 Unblocked {len(unblocked)} tickets: {', '.join([t[:12] for t in unblocked])}"

                return f"""✅ Ticket resolved!
Ticket ID: {ticket_id}
Message: {result.get("message", "")}{unblocked_msg}"""
            else:
                return f"❌ Failed to resolve ticket: {response.text}"
    except Exception as e:
        return f"❌ Error resolving ticket: {str(e)}"


@mcp.tool(name="request_ticket_clarification")
async def request_ticket_clarification(
    ticket_id: str,
    conflict_description: str,
    agent_id: str = None,
    context: str = "",
    potential_solutions: list = None,
) -> str:
    """Request LLM-powered clarification for a ticket with conflicting/unclear requirements.

    🎯 USE THIS WHEN:
    - You encounter conflicting requirements in a ticket
    - Instructions are ambiguous and could be interpreted multiple ways
    - You're uncertain about which approach to take
    - You need arbitration between different implementation options
    - You would otherwise create a new task to ask for clarification

    ⚠️ IMPORTANT: Use this INSTEAD of creating new tasks when unclear!
    This prevents infinite loops of task creation.

    The LLM arbitrator will:
    1. Analyze your conflict against the project goal
    2. Review all recent tickets and tasks for context
    3. Evaluate your potential solutions systematically
    4. Provide clear, actionable resolution with specific next steps
    5. Store the clarification as a comment on the ticket

    Args:
        ticket_id: ID of the ticket needing clarification
        agent_id: Your agent ID
        conflict_description: Clear description of the conflict or ambiguity (min 20 chars)
        context: Additional context that might help resolve the conflict
        potential_solutions: List of potential solutions you're considering (highly recommended!)

    Returns:
        Detailed markdown guidance including:
        - Analysis of the conflict
        - Evaluation of your proposed solutions
        - Recommended resolution with rationale
        - Specific ticket updates to make
        - Specific file changes needed
        - What to avoid

    Example:
        request_ticket_clarification(
            ticket_id="ticket-123",
            agent_id="agent-456",
            conflict_description="The ticket says to 'optimize performance' but also 'maintain compatibility'. These seem to conflict because the optimization would break the old API.",
            context="We have a legacy API used by external clients.",
            potential_solutions=[
                "Create new optimized API endpoint and keep old one",
                "Add versioning to API and optimize new version",
                "Optimize only internal parts, keep API unchanged"
            ]
        )
    """
    import logging

    potential_solutions = potential_solutions or []

    agent_id = agent_id or os.environ.get("HEPHAESTUS_AGENT_ID")
    if not agent_id:
        return "❌ Error requesting clarification: agent_id was not provided and HEPHAESTUS_AGENT_ID is not set in the environment"

    logger = logging.getLogger(__name__)
    logger.info(
        f"[MCP_CLARIFICATION] Agent {agent_id[:8]} requesting clarification for {ticket_id}"
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{HEPHAESTUS_URL}/api/tickets/request-clarification",
                json={
                    "ticket_id": ticket_id,
                    "conflict_description": conflict_description,
                    "context": context,
                    "potential_solutions": potential_solutions,
                },
                headers={"Content-Type": "application/json", "X-Agent-ID": agent_id},
                timeout=60.0,  # Longer timeout for LLM reasoning
            )

            if response.status_code == 200:
                result = response.json()
                clarification = result.get("clarification", "")
                comment_id = result.get("comment_id", "unknown")

                return f"""✅ **Clarification Received from Arbitration System**

{clarification}

---

💬 **Audit Trail**: This clarification has been stored as comment `{comment_id[:12]}` on ticket `{ticket_id}`

📝 **Next Steps**:
1. Review the "RESOLUTION & ACTION PLAN" section above
2. Update the ticket as specified
3. Make the file changes as outlined
4. Follow the testing requirements
5. Avoid the approaches listed in "What NOT to Do"

⚡ You now have clear direction - proceed with confidence!"""
            else:
                logger.error(
                    f"[MCP_CLARIFICATION] Failed: {response.status_code} - {response.text}"
                )
                return f"❌ Failed to get clarification: {response.text}"
    except Exception as e:
        logger.error(f"[MCP_CLARIFICATION] Exception: {e}", exc_info=True)
        return f"❌ Error requesting clarification: {str(e)}"


# ==================== WORKFLOW MANAGEMENT TOOLS ====================


@mcp.tool(name="list_workflow_definitions")
async def list_workflow_definitions() -> str:
    """List all available workflow definitions.

    Returns a list of workflow types (templates) that can be used to start new workflow executions.
    Each definition includes its name, description, number of phases, and whether it produces a result.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/api/workflow-definitions", timeout=10.0
            )

            if response.status_code == 200:
                result = response.json()
                definitions = result.get("definitions", [])

                if not definitions:
                    return "No workflow definitions loaded"

                def_list = []
                for d in definitions:
                    result_indicator = "Yes" if d.get("has_result") else "No"
                    def_list.append(
                        f"- {d['id']}: {d['name']} ({d['phases_count']} phases, result: {result_indicator})"
                    )

                return "Workflow Definitions:\n" + "\n".join(def_list)
            else:
                return f"Failed to get workflow definitions: {response.text}"
    except Exception as e:
        return f"Error getting workflow definitions: {str(e)}"


@mcp.tool(name="list_workflow_executions")
async def list_workflow_executions(status: str = "all") -> str:
    """List all workflow executions.

    Args:
        status: Filter by status (all/active/paused/completed/failed) - default: all

    Returns list of running and completed workflow instances with their current stats.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/api/workflow-executions",
                params={"status": status},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                executions = result.get("executions", [])

                if not executions:
                    return f"No workflow executions found (filter: {status})"

                exec_list = []
                for e in executions:
                    status_indicator = {
                        "active": "[ACTIVE]",
                        "paused": "[PAUSED]",
                        "completed": "[COMPLETED]",
                        "failed": "[FAILED]",
                    }.get(e["status"], "[UNKNOWN]")

                    stats = e.get("stats", {})
                    exec_list.append(
                        f"{status_indicator} {e['id'][:12]}: {e.get('description') or e.get('definition_name')}\n"
                        f"   Definition: {e.get('definition_name')} | "
                        f"Tasks: {stats.get('active_tasks', 0)}/{stats.get('total_tasks', 0)} | "
                        f"Agents: {stats.get('active_agents', 0)}"
                    )

                return f"Workflow Executions ({status}):\n\n" + "\n\n".join(exec_list)
            else:
                return f"Failed to get workflow executions: {response.text}"
    except Exception as e:
        return f"Error getting workflow executions: {str(e)}"


@mcp.tool(name="start_workflow_execution")
async def start_workflow_execution(
    definition_id: str, description: str, working_directory: str = None
) -> str:
    """Start a new workflow execution from a definition.

    Args:
        definition_id: ID of the workflow definition to use (e.g., "prd-to-software", "bugfix")
        description: Description of this execution (e.g., "Build URL shortener service")
        working_directory: Optional working directory for this workflow execution

    Returns:
        The workflow_id to use for all subsequent operations in this workflow.

    Use list_workflow_definitions() first to see available workflow types.

    Example:
        # First, see what workflow types are available
        list_workflow_definitions()

        # Then start a new execution
        result = start_workflow_execution(
            definition_id="prd-to-software",
            description="Build URL shortener from PRD"
        )
        # Returns workflow_id to use in create_task, create_ticket, etc.
    """
    try:
        async with httpx.AsyncClient() as client:
            payload = {
                "definition_id": definition_id,
                "description": description,
            }
            if working_directory:
                payload["working_directory"] = working_directory

            response = await client.post(
                f"{HEPHAESTUS_URL}/api/workflow-executions",
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10.0,
            )

            if response.status_code == 200:
                result = response.json()
                return f"""Workflow execution started!
Workflow ID: {result.get("workflow_id", "unknown")}
Status: {result.get("status", "unknown")}
Message: {result.get("message", "")}

Use this workflow_id in all subsequent create_task and create_ticket calls."""
            else:
                return f"Failed to start workflow execution: {response.text}"
    except Exception as e:
        return f"Error starting workflow execution: {str(e)}"


@mcp.tool(name="get_workflow_execution")
async def get_workflow_execution(workflow_id: str) -> str:
    """Get details of a specific workflow execution.

    Args:
        workflow_id: The workflow execution ID

    Returns detailed information about the workflow including stats and current activity.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{HEPHAESTUS_URL}/api/workflow-executions/{workflow_id}", timeout=10.0
            )

            if response.status_code == 200:
                e = response.json()
                stats = e.get("stats", {})

                status_indicator = {
                    "active": "[ACTIVE]",
                    "paused": "[PAUSED]",
                    "completed": "[COMPLETED]",
                    "failed": "[FAILED]",
                }.get(e["status"], "[UNKNOWN]")

                return f"""{status_indicator} Workflow Execution Details
ID: {e["id"]}
Definition: {e.get("definition_name", "N/A")} ({e.get("definition_id", "N/A")})
Description: {e.get("description", "N/A")}
Status: {e["status"]}
Working Directory: {e.get("working_directory", "N/A")}
Created: {e.get("created_at", "N/A")}

Stats:
  Total Tasks: {stats.get("total_tasks", 0)}
  Active Tasks: {stats.get("active_tasks", 0)}
  Done Tasks: {stats.get("done_tasks", 0)}
  Failed Tasks: {stats.get("failed_tasks", 0)}
  Active Agents: {stats.get("active_agents", 0)}"""
            elif response.status_code == 404:
                return f"Workflow not found: {workflow_id}"
            else:
                return f"Failed to get workflow execution: {response.text}"
    except Exception as e:
        return f"Error getting workflow execution: {str(e)}"


@mcp.tool(name="spawn_agent")
async def spawn_agent(
    agent_name: str,
    task: str,
    parent_task_id: str = None,
    agent_id: str = None,
    workflow_id: str = None,
) -> str:
    """Spawn a pi subagent with a specific Hephaestus agent configuration.

    This spawns a pi subagent using the installed Hephaestus agent files
    (e.g., hephaestus-development, hephaestus-architecture-design).

    Pi agents are invoked via --append-system-prompt with the agent file contents.

    Args:
        agent_name: Name of the Hephaestus agent to spawn
                    (e.g., 'hephaestus-development', 'hephaestus-architecture-design')
        task: The task description/prompt for the subagent
        parent_task_id: Parent task ID (sets parent_task_id on subagent's task)
        agent_id: Your agent ID (for creating the sub-task)
        workflow_id: Workflow ID (REQUIRED for task creation)

    Available agents:
        - hephaestus-product-requirements: Extract requirements from design docs
        - hephaestus-architecture-design: Create technical architecture
        - hephaestus-development: Implement components
        - hephaestus-adversarial-review: Harsh code review
        - hephaestus-doc-review: Documentation review
        - hephaestus-security-review: Security review
        - hephaestus-qa-validation: QA testing
        - hephaestus-product-validation: Final product validation
        - hephaestus-git-commit-push: Git commit and push
        - hephaestus-forensics-analysis: Pipeline self-improvement

    Example:
        spawn_agent(
            agent_name="hephaestus-development",
            task="Implement the user authentication module with JWT support",
            parent_task_id="abc-123",
            workflow_id="xyz-789"
        )
    """
    import os

    try:
        # Get the path to the pi agent file
        pi_agents_dir = os.path.expanduser("~/.pi/agent/agents")
        agent_file = os.path.join(pi_agents_dir, f"{agent_name}.md")

        if not os.path.exists(agent_file):
            return f"Error: Agent '{agent_name}' not found at {agent_file}"

        # Create a sub-task for this agent (workflow_id is required)
        sub_task_id = None
        if agent_id and workflow_id:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{HEPHAESTUS_URL}/create_task",
                        json={
                            "task_description": task,
                            "done_definition": "Complete the assigned task as described",
                            "ai_agent_id": agent_id,
                            "workflow_id": workflow_id,
                            "phase_id": 1,
                            "priority": "high",
                            "parent_task_id": parent_task_id,
                        },
                        headers={
                            "Content-Type": "application/json",
                            "X-Agent-ID": agent_id,
                        },
                        timeout=10.0,
                    )
                    if response.status_code == 200:
                        sub_task_id = response.json().get("task_id")
                    else:
                        return f"Failed to create sub-task: {response.text}"
            except Exception as e:
                return f"Error creating sub-task: {str(e)}"
        else:
            return "Error: agent_id and workflow_id are required to create sub-task"

        # Build the pi command using --append-system-prompt
        # Pi reads the agent file and appends it to the system prompt
        # Use cat to read file contents and pass via shell expansion
        cmd = f'pi --append-system-prompt "$(cat {agent_file})" -p "{task[:200]}"'

        return f"""Agent '{agent_name}' ready to spawn.

Sub-task created: {sub_task_id}

To run this agent, use bash:
{cmd}

The agent will use the system prompt from:
{agent_file}

MCP tools (save_memory, update_task_status) are available.
The sub-task ID is: {sub_task_id}"""

    except Exception as e:
        return f"Error spawning agent: {str(e)}"


# Run the MCP server
if __name__ == "__main__":
    print("🚀 Starting Claude MCP Client for Hephaestus...")
    print(f"📡 Connecting to Hephaestus server at {HEPHAESTUS_URL}")
    print("✨ Available tools:")
    print("\n📋 Task Management:")
    print("  - health_check: Check server status")
    print("  - create_task: Create a new task")
    print("  - get_tasks: List all tasks")
    print("  - update_task_status: Update task status (done/failed/in_progress)")
    print("  - save_memory: Save knowledge to memory")
    print("\n🔍 Validation & Results:")
    print(
        "  - give_validation_review: Submit validation review (validator agents only)"
    )
    print("  - submit_result: Submit workflow result with evidence")
    print(
        "  - submit_result_validation: Submit result validation (result validator agents only)"
    )
    print("  - get_workflow_results: Get all results for a workflow")
    print("\n👥 Agent Communication:")
    print("  - get_agent_status: Get agent statuses")
    print("  - broadcast_message: Broadcast a message to all active agents")
    print("  - send_message: Send a direct message to a specific agent")
    print("\n🎫 Ticket Tracking (when enabled for workflow):")
    print(
        "  - create_ticket: Create a new ticket (returns similar tickets for duplicate detection)"
    )
    print(
        "  - update_ticket: Update ticket fields (title, description, priority, tags, etc.)"
    )
    print("  - change_ticket_status: Move ticket to different status (checks blockers)")
    print(
        "  - add_ticket_comment: Add comment to ticket (for progress updates, blockers)"
    )
    print(
        "  - search_tickets: Search tickets using HYBRID search (70% semantic + 30% keyword) - DEFAULT"
    )
    print(
        "  - get_ticket: Get full details for a specific ticket by exact ID (description, comments, history, commits)"
    )
    print("  - get_tickets: List/filter tickets with pagination")
    print(
        "  - link_commit_to_ticket: Manually link git commit to ticket (auto-linking on task completion)"
    )
    print("  - get_commit_diff: Get detailed git diff for commit (for Git Diff Window)")
    print(
        "  - resolve_ticket: Mark ticket as resolved (auto-unblocks dependent tickets)"
    )
    print(
        "  - request_ticket_clarification: Request LLM arbitration for conflicting requirements (PREVENTS TASK LOOPS!)"
    )
    print("\n🔄 Workflow Management:")
    print("  - list_workflow_definitions: List available workflow types")
    print("  - list_workflow_executions: List all workflow instances")
    print("  - start_workflow_execution: Start a new workflow")
    print("  - get_workflow_execution: Get workflow details")
    print("\n💡 Ticket Tracking Tips:")
    print(
        "  - Search BEFORE creating to avoid duplicates (use search_tickets with hybrid mode)"
    )
    print(
        "  - Use get_ticket() with the EXACT ticket ID to see full details (description, comments, history)"
    )
    print(
        "  - If you don't know the exact ticket ID, search first with search_tickets() or get_tickets()"
    )
    print("  - Use blocked_by_ticket_ids to create dependencies between tickets")
    print(
        "  - Blocked tickets cannot change status until blocking tickets are resolved"
    )
    print("  - Resolving a ticket automatically unblocks all dependent tickets")
    print(
        "  - Hybrid search (default) combines semantic understanding + keyword precision"
    )
    try:
        mcp.run()
    except BaseException:
        # Whatever kills the process (uncaught exception, KeyboardInterrupt,
        # SystemExit) gets one last record here before it disappears --
        # without this, a crash here is otherwise silent (see logger setup
        # above for why that matters).
        logger.exception("mcp.run() terminated with an exception")
        raise
