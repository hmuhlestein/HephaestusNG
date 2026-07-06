"""Tests for AgentPromptBuilder.format_initial_message — the tool-call
signatures baked into the task-assignment prompt sent to agents.

Regression coverage for a real bug found during smoke testing: the prompt
told agents to call hephaestus_create_task(description=..., phase=N, ...),
but the actual MCP tool schema (src/mcp/server.py) requires
task_description/phase_id and rejects both `description` and `phase` as
unknown properties, and doesn't accept agent_id at all. Every agent that
tried to create a subtask following these instructions verbatim failed
with a validation error.
"""

from src.agents.prompt_builder import AgentPromptBuilder


class _FakeTask:
    def __init__(self):
        self.id = "task-123"
        self.workflow_id = "wf-456"
        self.phase_id = "phase-789"
        self.raw_description = "do the thing"
        self.enriched_description = None
        self.done_definition = "the thing is done"


class TestCreateTaskToolSignature:
    def test_uses_real_parameter_names(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "task_description=" in message
        assert "phase_id=" in message
        assert "phase-789" in message

    def test_does_not_use_wrong_parameter_names(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert 'hephaestus_create_task(description=' not in message
        assert "phase=N" not in message

    def test_does_not_instruct_agent_id_for_create_task(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        create_task_line = next(
            line for line in message.splitlines() if "hephaestus_create_task(" in line
        )
        assert "agent_id=" not in create_task_line


class TestUpdateTaskStatusToolSignature:
    """Regression coverage for a real bug found during smoke testing: the
    prompt's own hephaestus_update_task_status examples omitted agent_id
    even though the surrounding instructions say "always pass agent_id=...".
    Agents copied the example verbatim and got
    "Input validation error: 'agent_id' is a required property" on every
    completion attempt.
    """

    def test_phase_agent_examples_include_agent_id(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        status_lines = [
            line
            for line in message.splitlines()
            if "hephaestus_update_task_status(" in line
        ]
        assert status_lines, "expected at least one update_task_status example"
        for line in status_lines:
            assert 'agent_id="agent-abc"' in line, line

    def test_non_phase_agent_examples_include_agent_id(self):
        task = _FakeTask()
        task.phase_id = None
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")
        status_lines = [
            line
            for line in message.splitlines()
            if "hephaestus_update_task_status(" in line
        ]
        assert status_lines, "expected at least one update_task_status example"
        for line in status_lines:
            assert 'agent_id="agent-abc"' in line, line


class _FakeWorkflowConfig:
    def __init__(self, enable_tickets):
        self.enable_tickets = enable_tickets
        self.result_criteria = None


class _FakePhaseManager:
    def __init__(self, enable_tickets):
        self._enable_tickets = enable_tickets

    def get_workflow_config(self, workflow_id):
        return _FakeWorkflowConfig(self._enable_tickets)


class TestTicketTrackingNote:
    """Regression: hephaestus_create_task rejects any call with no ticket_id
    when the workflow has ticket tracking enabled ("MCP agents MUST provide
    ticket_id") -- observed live: every agent discovered this only after a
    failed first attempt, wasting a full round trip each time, since nothing
    in the prompt warned about it up front."""

    def test_mentions_ticket_requirement_when_enabled(self):
        builder = AgentPromptBuilder(phase_manager=_FakePhaseManager(True))
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "ticket_id" in message
        assert "hephaestus_create_ticket" in message

    def test_omits_ticket_requirement_when_disabled(self):
        builder = AgentPromptBuilder(phase_manager=_FakePhaseManager(False))
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "Ticket tracking is ON" not in message

    def test_omits_ticket_requirement_when_no_phase_manager(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "Ticket tracking is ON" not in message
