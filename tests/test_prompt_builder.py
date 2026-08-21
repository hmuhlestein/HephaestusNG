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


class TestFeatureArchitectRepoAssignmentRule:
    """REQ-19/20: unlike the standard 10 autopilot phases (development,
    architectural_review, etc. -- each has a pre-generated pi agent file
    under ~/.pi/agent/agents/, generated only from config/workflows/
    autopilot/*.yaml by scripts/generate_pi_agents.py), feature_architect
    has no such file: it's defined under config/workflows/feature_architect/,
    a directory that generator never scans. PiAgent.get_launch_command's
    agent_file branch is unreachable for it, so it falls to the branch
    that uses `system_prompt` (built from feature_architect_system_prompt
    via get_phase_system_prompt) directly -- unlike base_system_prompt's
    now-removed FILE PLACEMENT guardrail above, this template's content
    DOES reach the real agent for this one phase. Static text here is a
    belt-and-suspenders duplicate of the same hard rule get_project_context()
    injects dynamically into {project_context} for multi-repo projects
    (REQ-19/20's dynamic mechanism, verified separately in
    test_agent_dispatch_service.py and test_orchestrator_helpers.py)."""

    def test_hard_rule_present_in_rendered_prompt(self):
        from src.prompts.loader import get_phase_system_prompt

        rendered = get_phase_system_prompt(
            phase_name="feature_architect",
            agent_id="agent-abc",
            task_id="task-123",
            memory_context="",
            project_context="",
        )

        assert rendered is not None
        assert "MUST be bound to exactly one" in rendered
        assert "Feature.depends_on" in rendered

    def test_no_other_phase_gets_this_specialized_prompt(self):
        """Only feature_architect opts into a specialized template --
        every other phase name falls back to base_system_prompt (per
        get_phase_system_prompt's own documented convention)."""
        from src.prompts.loader import get_phase_system_prompt

        assert get_phase_system_prompt(
            phase_name="development",
            agent_id="a", task_id="t", memory_context="", project_context="",
        ) is None


class TestFeatureArchitectWorkflowYamlRepoRule:
    """REQ-19/20, take 2: product_validation kept finding this unmet even
    after feature_architect_system_prompt (system_prompts.yaml, verified
    above) got the hard-rule text -- because that's not the only prompt
    content the architect actually reads. config/workflows/feature_architect/
    01_feature_architect.yaml's additional_notes field is injected via
    PhaseContext.to_prompt_context() (src/phases/models.py's "##
    PHASE-SPECIFIC INSTRUCTIONS" section, appended to project_context in
    build_dispatch_context) -- and it's this file, not system_prompts.yaml,
    that shows the architect the concrete features.json schema example it
    actually copies. The schema example had no "repo" key at all and
    Step 7's own validation checklist never mentioned repos, so an
    architect could read the hard rule in feature_architect_system_prompt
    and still emit a schema with nowhere to put the repo assignment."""

    def _load_additional_notes(self) -> str:
        from src.services.prompt_proposal_service import phase_yaml_path

        path = phase_yaml_path("feature_architect", "feature_architect")
        assert path is not None
        import yaml

        with open(path) as f:
            data = yaml.safe_load(f)
        return data["additional_notes"]

    def test_multi_repo_rule_present(self):
        notes = self._load_additional_notes()
        assert "MULTI-REPO PROJECTS" in notes
        assert "PROJECT REPOS" in notes

    def test_schema_example_includes_repo_field(self):
        notes = self._load_additional_notes()
        assert '"repo"' in notes

    def test_validation_checklist_mentions_repo(self):
        notes = self._load_additional_notes()
        step7 = notes.split("Step 7")[1].split("Step 8")[0]
        assert "repo" in step7.lower()


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
            line for line in message.splitlines() if "create_task(" in line
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


class TestCompleteMyTaskToolSignature:
    """complete_my_task is self-identifying (marks the caller's own current
    task/agent from server-side context) and its schema rejects agent_id
    outright -- the task_assignment_header carries an explicit warning
    ("DO NOT pass agent_id to complete_my_task — it will be REJECTED").
    This used to be the opposite contract (agent_id was required, and its
    absence from the example was the bug -- see git history), so every
    example of the tool call must NOT include agent_id now."""

    def test_phase_agent_examples_omit_agent_id(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        status_lines = [
            line
            for line in message.splitlines()
            if "complete_my_task(" in line
        ]
        assert status_lines, "expected at least one complete_my_task example"
        for line in status_lines:
            assert "agent_id=" not in line, line

    def test_non_phase_agent_examples_omit_agent_id(self):
        task = _FakeTask()
        task.phase_id = None
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(task=task, agent_id="agent-abc")
        status_lines = [
            line
            for line in message.splitlines()
            if "complete_my_task(" in line
        ]
        assert status_lines, "expected at least one complete_my_task example"
        for line in status_lines:
            assert "agent_id=" not in line, line


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
        assert "heph_create_ticket" in message

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
    from src.core.database import Agent, Phase, Ticket, Workflow

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
        session.add(Agent(id="agent-qa", system_prompt="t", cli_type="pi"))
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
        assert "heph_update_ticket_status" in message

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
        from src.core.database import Agent, Phase, Ticket, Workflow

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
            session.add(Agent(id="agent-qa", system_prompt="t", cli_type="pi"))
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

    def __init__(self, phase_name: str, role_previously_completed: bool = False):
        self.workflow_id = "wf-456"
        self._phase_name = phase_name
        self._role_previously_completed = role_previously_completed

    def phase_role_previously_completed(self, phase_id, role):
        return self._role_previously_completed

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

    def test_warns_when_an_earlier_same_role_phase_already_completed(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerForPhaseName(
                "architectural_review", role_previously_completed=True
            )
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "RESUMED SESSION" in message
        assert "ALREADY COMPLETE" in message
        assert "task-123" in message  # the new task_id, stated as the only current one

    def test_no_warning_for_a_phase_with_a_unique_role(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerForPhaseName(
                "development", role_previously_completed=False
            )
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "RESUMED SESSION" not in message

    def test_no_warning_for_the_first_occurrence_of_a_shared_role(self):
        """Regression: architecture_design is the FIRST phase in the
        pipeline to use the "architect" role. A role appearing more than
        once in workflow.yaml's session_roles used to be treated, by
        itself, as evidence the session was reused -- so this phase's very
        first, session-less invocation was told its session was
        "previously used" and "already complete", directly contradicting
        pi's own "No project session found ... creating a new session"
        log line for that exact session id. Confirmed live for task
        52f22f1f (architecture_design phase, apple-foundation-integration
        feature).
        """
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerForPhaseName(
                "architecture_design", role_previously_completed=False
            )
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "RESUMED SESSION" not in message


class _FakePhaseManagerFull:
    """Supports every method format_initial_message calls on phase_manager,
    with real non-empty content behind each one -- used to prove resumed-
    session trimming actually SKIPS content that would otherwise be
    present, rather than content that just happened to be empty already."""

    def __init__(self, phase_name: str, role_previously_completed: bool):
        self.workflow_id = "wf-456"
        self._phase_name = phase_name
        self._role_previously_completed = role_previously_completed

    def get_workflow(self, workflow_id):
        return type("W", (), {"description": "Build a URL shortener with analytics."})()

    def get_workflow_config(self, workflow_id):
        return type(
            "C",
            (),
            {
                "enable_tickets": True,
                "result_criteria": "All tests pass and the feature is deployed.",
            },
        )()

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

    def phase_role_previously_completed(self, phase_id, role):
        return self._role_previously_completed


class TestResumedSessionTrimming:
    """Regression: a resumed session used to get the FULL initial-message
    template resent every time -- workflow description, ticket/result-
    criteria rules, and the entire tool-call instructions block -- even
    though pi already has all of that in its conversation history from the
    earlier phase that established the shared session. Only genuinely new
    content (task id/description, completion criteria, updated pipeline
    position, live open tickets, the resumed-session warning) needs to be
    sent again.
    """

    def test_resumed_session_omits_static_workflow_content(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerFull(
                "architectural_review", role_previously_completed=True
            )
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "Build a URL shortener" not in message
        assert "WORKFLOW-LEVEL GOAL" not in message
        assert "Ticket tracking is ON" not in message
        assert "RESUMED SESSION" in message

    def test_resumed_session_omits_full_tool_instructions_but_keeps_reminder(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerFull(
                "architectural_review", role_previously_completed=True
            )
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "FILE PLACEMENT" not in message
        assert "hephaestus_search_memory" not in message
        assert "unchanged from earlier in this session" in message
        # complete_my_task is self-identifying (no task_id/agent_id accepted
        # -- see TestCompleteMyTaskToolSignature) even in the condensed
        # resumed-session reminder.
        status_lines = [
            line
            for line in message.splitlines()
            if "complete_my_task(" in line
        ]
        assert status_lines, "expected at least one complete_my_task example"
        for line in status_lines:
            assert "task_id=" not in line
            assert "agent_id=" not in line

    def test_resumed_session_keeps_pipeline_position(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerFull(
                "architectural_review", role_previously_completed=True
            )
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "Pipeline (use phase=N" in message

    def test_non_resumed_session_keeps_all_static_content(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerFull(
                "architecture_design", role_previously_completed=False
            )
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc"
        )
        assert "Build a URL shortener" in message
        assert "WORKFLOW-LEVEL GOAL" in message
        assert "Ticket tracking is ON" in message
        assert "FILE PLACEMENT" in message
        assert "RESUMED SESSION" not in message


class _FakePhaseManagerWithProjectRoot:
    """get_workflow returns a workflow with no project_id (so the
    AutopilotProject DB lookup is skipped) but a real launch_params --
    exercises the fallback path format_initial_message actually hits for
    every autopilot pipeline workflow (project_id is frequently unset;
    launch_params.project_path is what create the worktree/agent dispatch
    itself already relies on elsewhere, e.g. sweep_completed_workflow_worktrees)."""

    def get_workflow(self, workflow_id):
        return type(
            "W",
            (),
            {
                "description": "",
                "project_id": None,
                "launch_params": {"project_path": "/Users/dev/myproject"},
            },
        )()


class TestProjectRootInjection:
    """Regression: qa_validation.yaml, deploy.yaml, and forensics_analysis.yaml
    all instruct the agent to read "Project Root (absolute): <path>" from its
    own task description (e.g. to locate TESTING.md/DEPLOY.md, which live in
    the real project root, not the isolated worktree branch_path points at)
    -- but nothing ever injected that field. Confirmed live: a qa_validation
    agent's task description had no such field at all, so it couldn't
    determine where TESTING.md was and skipped straight past the mandatory
    first step, tripping a steering intervention."""

    def test_project_root_injected_from_launch_params(self):
        builder = AgentPromptBuilder(
            phase_manager=_FakePhaseManagerWithProjectRoot()
        )
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc", branch_path="/worktrees/wt-1"
        )
        assert "Project Root (absolute): /Users/dev/myproject" in message

    def test_no_project_root_line_when_unresolvable(self):
        """No project_id and no launch_params.project_path -- must not
        inject a bogus/empty "Project Root (absolute):" line."""
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc", branch_path="/worktrees/wt-1"
        )
        assert "Project Root (absolute)" not in message


class TestArtifactsPathInjection:
    """Regression, same class as TestProjectRootInjection above: 13 of the 14
    autopilot phase prompts tell the agent to read or write an "Artifacts
    Path", and qa_validation/security_review/architectural_review go as far
    as "Read: Your task description for the 'Artifacts Path (absolute):'
    line" -- but nothing ever injected that field either. _create_phase_task
    builds a description of f"Execute {phase.name}: {phase.description}"
    plus optional goto feedback, and this builder injected only Working
    Directory / Project Root, so the lookup those prompts describe had
    nothing to find."""

    def test_artifacts_path_injected_from_branch_path(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc", branch_path="/worktrees/wt-1"
        )
        assert "Artifacts Path (absolute): /worktrees/wt-1/.hephaestus" in message

    def test_artifacts_path_survives_a_workflow_lookup_failure(self):
        """Injected outside the workflow try/except that wraps the Project
        Root lookup: a get_workflow failure must not silently cost every
        phase its artifact directory too."""

        class _Exploding:
            def get_workflow(self, workflow_id):
                raise RuntimeError("db down")

        builder = AgentPromptBuilder(phase_manager=_Exploding())
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc", branch_path="/worktrees/wt-1"
        )
        assert "Artifacts Path (absolute): /worktrees/wt-1/.hephaestus" in message

    def test_no_artifacts_path_line_without_a_worktree(self):
        builder = AgentPromptBuilder(phase_manager=None)
        message = builder.format_initial_message(
            task=_FakeTask(), agent_id="agent-abc", branch_path=None
        )
        assert "Artifacts Path (absolute)" not in message
