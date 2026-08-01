"""Tests for src/phases/models.py's PhaseContext."""

from src.phases.models import PhaseContext
from src.sdk.models import Phase


def _phase(phase_id: int, name: str) -> Phase:
    return Phase(
        id=phase_id,
        name=name,
        description=f"Description of {name}.",
        done_definitions=[],
        working_directory=".",
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
