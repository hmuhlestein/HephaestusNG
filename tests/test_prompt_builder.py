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

import pytest

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


class TestFilePlacementGuardrail:
    """Regression: a FILE PLACEMENT instruction was first added to
    base_system_prompt/feature_architect_system_prompt in system_prompts.yaml
    -- but those templates are only used by the internal task-enrichment LLM
    call (src/interfaces/llm_interface.py), never by the actual worktree
    agent (built by AgentPromptBuilder.format_initial_message from
    phase_agent_instructions/non_phase_agent_instructions instead). The
    guardrail never reached a real agent. Moved to the templates that
    actually do."""

    def test_phase_agent_prompt_includes_file_placement(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "FILE PLACEMENT" in message
        assert ".hephaestus/scratch/" in message

    def test_non_phase_agent_prompt_includes_file_placement(self):
        task = _FakeTask()
        task.phase_id = None
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")
        assert "FILE PLACEMENT" in message
        assert ".hephaestus/scratch/" in message


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


@pytest.fixture
def ticket_db(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _seed_dev_phase_and_ticket(db, workflow_id, phase_id, is_resolved=False, ticket_type="bug"):
    from src.core.database import Phase, Ticket, Workflow

    with db.session_scope() as session:
        session.add(
            Workflow(id=workflow_id, name="t", phases_folder_path="/tmp", status="active")
        )
        session.add(
            Phase(
                id=phase_id,
                workflow_id=workflow_id,
                order=1,
                name="development",
                description="d",
                done_definitions=["done"],
            )
        )
        session.add(
            Ticket(
                id="ticket-abc12345",
                workflow_id=workflow_id,
                created_by_agent_id="agent-qa",
                title="Auth bypass on /admin",
                description="Missing auth check lets any user hit /admin routes.",
                ticket_type=ticket_type,
                priority="high",
                status="open",
                is_resolved=is_resolved,
            )
        )


class TestOpenTicketsInjection:
    """development.yaml's own instructions tell agents to check for open bug
    tickets, but a pull-based instruction is compliance-dependent -- this
    proactively injects them into the prompt instead, only on a goto
    re-entry (task.action == "goto"), matching where
    verify_no_open_tickets enforces resolution at task-completion time."""

    def test_injects_open_tickets_on_goto(self, ticket_db):
        _seed_dev_phase_and_ticket(ticket_db, "wf-456", "phase-789")

        task = _FakeTask()
        task.action = "goto"
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")

        assert "OPEN BUG TICKETS" in message
        assert "ticket-abc12345" in message
        assert "Auth bypass on /admin" in message
        assert "hephaestus_resolve_ticket" in message

    def test_no_injection_on_first_pass_continue(self, ticket_db):
        """action == 'continue' (first-time build from architecture_design)
        must not show tickets even if some exist for the workflow."""
        _seed_dev_phase_and_ticket(ticket_db, "wf-456", "phase-789")

        task = _FakeTask()
        task.action = "continue"
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")

        assert "OPEN BUG TICKETS" not in message

    def test_no_injection_when_tickets_resolved(self, ticket_db):
        _seed_dev_phase_and_ticket(ticket_db, "wf-456", "phase-789", is_resolved=True)

        task = _FakeTask()
        task.action = "goto"
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")

        assert "OPEN BUG TICKETS" not in message

    def test_no_injection_for_non_bug_tickets(self, ticket_db):
        _seed_dev_phase_and_ticket(ticket_db, "wf-456", "phase-789", ticket_type="feature")

        task = _FakeTask()
        task.action = "goto"
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")

        assert "OPEN BUG TICKETS" not in message

    def test_no_injection_outside_development_phase(self, ticket_db):
        from src.core.database import Phase, Ticket, Workflow

        with ticket_db.session_scope() as session:
            session.add(
                Workflow(id="wf-456", name="t", phases_folder_path="/tmp", status="active")
            )
            session.add(
                Phase(
                    id="phase-789",
                    workflow_id="wf-456",
                    order=1,
                    name="qa_validation",
                    description="d",
                    done_definitions=["done"],
                )
            )
            session.add(
                Ticket(
                    id="ticket-abc12345",
                    workflow_id="wf-456",
                    created_by_agent_id="agent-qa",
                    title="Auth bypass",
                    description="d",
                    ticket_type="bug",
                    priority="high",
                    status="open",
                    is_resolved=False,
                )
            )

        task = _FakeTask()
        task.action = "goto"
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")

        assert "OPEN BUG TICKETS" not in message


class _FakePhaseManagerForPhaseName:
    """Like _FakePhaseManagerWithContext, but lets the test pick the phase
    name so it can land on a real shared or unique session_role."""

    def __init__(self, phase_name: str):
        self.workflow_id = "wf-456"
        self._phase_name = phase_name

    def get_phase_context(self, phase_id):
        sdk_phase = SdkPhase(
            id=1,
            name=self._phase_name,
            description="Do the thing",
            done_definitions=["done"],
            working_directory=".",
        )
        return PhaseContext(
            phase_id=phase_id,
            workflow_id=self.workflow_id,
            phase=sdk_phase,
            all_phases=[sdk_phase],
            current_status="in_progress",
        )


class TestResumedSessionWarning:
    """Regression: architecture_design and architectural_review (and other
    phase pairs) intentionally share a session_role in workflow.yaml so the
    same agent/session resumes with full prior context. Observed live: an
    agent resuming that shared session kept re-confirming and re-reporting
    on its OLD, already-done task instead of ever touching its actual new
    one, despite the new task_id being stated clearly elsewhere in the
    prompt. The prompt must call this out explicitly rather than trusting
    the agent to infer a hard task boundary from a fresh ID alone.
    """

    def test_warns_when_phase_shares_a_session_role(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerForPhaseName("architectural_review")
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "RESUMED SESSION" in message
        assert "ALREADY COMPLETE" in message
        assert "task-123" in message  # the new task_id, stated as the only current one

    def test_no_warning_for_a_phase_with_a_unique_role(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerForPhaseName("development")
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "RESUMED SESSION" not in message
