"""Regression: bugfix/workflow.yaml and autopilot/workflow.yaml's
phase_1_task_prompt each used to instruct the phase-1 agent to manually
create_task the SECOND phase's task ("Create the next task for
adversarial_review" / "Create a Phase 2 task"). This directly contradicts
the system-wide rule every other phase follows (create_task only creates
SUBTASKS within the calling agent's OWN current phase -- see
system_prompts.yaml and architecture_design.yaml's own "Do NOT call
create_task for Phase 3" fix for the same bug class) and the orchestrator
already auto-creates the next phase's task once phase 1 is marked done
(Case 1 / _case_completed_with_successor in phase_transitions.py).

Observed live: a bugfix workflow's development agent, with no reliable way
to know adversarial_review's phase UUID, called create_task without a
valid target phase_id. The fallback resolved it to the agent's OWN phase
(development), filing "Adversarial review: ..." work under development
instead -- development effectively ran three times for one bug fix.
"""

from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / "config" / "workflows"


def _phase_1_task_prompt(workflow_name: str) -> str:
    with open(WORKFLOWS_DIR / workflow_name / "workflow.yaml") as f:
        cfg = yaml.safe_load(f)
    return cfg["launch_template"]["phase_1_task_prompt"]


def test_bugfix_phase_1_prompt_does_not_tell_agent_to_create_next_task():
    prompt = _phase_1_task_prompt("bugfix")
    assert "create the next task" not in prompt.lower()
    assert "do not call create_task" in prompt.lower()


def test_autopilot_phase_1_prompt_does_not_tell_agent_to_create_next_task():
    prompt = _phase_1_task_prompt("autopilot")
    assert "create a phase 2 task" not in prompt.lower()
    assert "do not call create_task" in prompt.lower()
