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
from src.phases.models import PhaseContext
from src.sdk.models import Phase as SdkPhase


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


def _make_phase_context(phase_id="phase-789"):
    sdk_phase = SdkPhase(
        id=1,
        name="Test Phase",
        description="Do the thing",
        done_definitions=["done"],
        working_directory=".",
    )
    return PhaseContext(
        phase_id=phase_id,
        workflow_id="wf-456",
        phase=sdk_phase,
        all_phases=[sdk_phase],
        current_status="in_progress",
    )


class _FakePhaseManagerWithContext:
    """Real PhaseContext/Phase objects, not Mocks -- so an attribute-name
    mismatch (e.g. .phase_definition vs .phase) raises exactly like it would
    in production instead of silently succeeding against a Mock's
    auto-generated attributes."""

    def __init__(self):
        self.workflow_id = "wf-456"

    def get_phase_context(self, phase_id):
        return _make_phase_context(phase_id)

    def get_workflow_config(self, workflow_id):
        return _FakeWorkflowConfig(False)


class TestPhaseContextSection:
    """Regression: format_initial_message referenced
    phase_ctx.phase_definition.name, but PhaseContext's actual field is
    named `phase` -- every single phase-agent prompt hit an AttributeError
    here (caught and logged, not crashed), silently dropping the entire
    phase-context section (all_phases, current phase description) from
    every agent's prompt. Found live via repeated
    "Exception getting phase context ... 'PhaseContext' object has no
    attribute 'phase_definition'" in backend.log across many different
    phase_ids over an hour of a real run.
    """

    def test_phase_context_section_included_when_available(self):
        builder = AgentPromptBuilder(phase_manager=_FakePhaseManagerWithContext())
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "Test Phase" in message


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
