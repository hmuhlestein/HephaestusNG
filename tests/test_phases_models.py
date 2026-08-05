"""Tests for src/phases/models.py's PhaseContext."""

from src.phases.models import PhaseContext
from src.sdk.models import Phase


def _phase(phase_id: int, name: str, additional_notes: str = "") -> Phase:
    return Phase(
        id=phase_id,
        name=name,
        description=f"Description of {name}.",
        done_definitions=[],
        working_directory=".",
        additional_notes=additional_notes,
    )


class TestToPromptContextPipelineListing:
    """Regression: the pipeline listing shown to an agent (used to build
    goto/arbitration task descriptions with `phase=N`) used to include every
    phase in the workflow, including ones far in the future the current
    phase has no business referencing yet. Only past (✓) and current (→)
    phases should be listed."""

    def test_only_lists_past_and_current_phases(self):
        phases = [
            _phase(1, "product_requirements"),
            _phase(2, "scope_review"),
            _phase(3, "architecture_design"),
            _phase(4, "development"),
            _phase(5, "architectural_review"),
            _phase(6, "deploy"),
        ]
        ctx = PhaseContext(
            phase_id="p4",
            workflow_id="w1",
            phase=phases[3],
            all_phases=phases,
        )

        rendered = ctx.to_prompt_context()

        assert "Phase 1: product_requirements" in rendered
        assert "Phase 3: architecture_design" in rendered
        assert "Phase 4: development" in rendered
        assert "Phase 5: architectural_review" not in rendered
        assert "Phase 6: deploy" not in rendered

    def test_markers_are_check_for_past_and_arrow_for_current(self):
        phases = [_phase(1, "a"), _phase(2, "b"), _phase(3, "c")]
        ctx = PhaseContext(
            phase_id="p2", workflow_id="w1", phase=phases[1], all_phases=phases
        )

        rendered = ctx.to_prompt_context()

        assert "✓ Phase 1: a" in rendered
        assert "→ Phase 2: b" in rendered
        assert "○" not in rendered


class TestToPromptContextAdditionalNotes:
    """Regression: to_prompt_context() is the ONLY place in the whole
    prompt-assembly path (AgentPromptBuilder.format_initial_message ->
    the phase_agent_instructions/phase_agent_resumed_instructions/
    non_phase_agent_instructions templates, none of which have an
    additional_notes placeholder of their own) that ever surfaces a
    phase's additional_notes -- omitting it here left every dispatched
    agent working from nothing but a one-line description and a plain
    done_definitions checklist, regardless of how much per-phase detail
    its YAML's additional_notes actually specified (step-by-step
    procedures, schema examples, mandatory sub-steps like
    security_review's ASH scan). Observed live: a security_review agent
    had no idea an ASH automated scan step existed at all."""

    def test_current_phase_additional_notes_is_included(self):
        phases = [
            _phase(1, "development"),
            _phase(2, "security_review", additional_notes="STEP 2: AUTOMATED SCAN (ash) — MANDATORY, DO NOT SKIP"),
        ]
        ctx = PhaseContext(
            phase_id="p2", workflow_id="w1", phase=phases[1], all_phases=phases
        )

        rendered = ctx.to_prompt_context()

        assert "STEP 2: AUTOMATED SCAN (ash) — MANDATORY, DO NOT SKIP" in rendered

    def test_no_additional_notes_adds_nothing(self):
        phases = [_phase(1, "development")]
        ctx = PhaseContext(
            phase_id="p1", workflow_id="w1", phase=phases[0], all_phases=phases
        )

        rendered = ctx.to_prompt_context()

        assert "PHASE-SPECIFIC INSTRUCTIONS" not in rendered

    def test_other_phases_additional_notes_are_not_leaked(self):
        """Only the CURRENT phase's own instructions belong in its prompt
        -- an earlier phase's unrelated procedure would just be noise (or
        actively misleading) for the phase actually running now."""
        phases = [
            _phase(1, "development", additional_notes="Development-only instructions."),
            _phase(2, "security_review", additional_notes="Security-only instructions."),
        ]
        ctx = PhaseContext(
            phase_id="p2", workflow_id="w1", phase=phases[1], all_phases=phases
        )

        rendered = ctx.to_prompt_context()

        assert "Security-only instructions." in rendered
        assert "Development-only instructions." not in rendered
