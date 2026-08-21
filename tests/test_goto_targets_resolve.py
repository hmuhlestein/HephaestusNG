"""Every `target:` in a shipped workflow must name a real phase.

Both goto handlers in phase_manager fail open when a target cannot be
resolved. _handle_evaluation_goto (a gate's decision) and _handle_force_goto
(an arbiter's decision) each do the same thing:

    if not target_phase:
        logger.warning(f"Target phase not found: {...}")
        return self._advance_or_complete(session, phase.id)

That is the opposite of what was decided. A gate says "go back to
development", or an arbiter resolves an escalation with "goto X", and the
pipeline advances instead -- with a warning as the only trace.

No shipped workflow currently has an unresolvable target, so this is latent
rather than live. The trigger is mundane: rename a phase and miss one
`target:` reference, and every goto aimed at it silently becomes an advance.
This repo renamed a phase (git_commit_push -> git_expert) recently, which is
exactly the shape of change that would do it.

This guards the config so the latent bug cannot become live. It does not
change the runtime fail-open, which is recorded separately -- what a goto
should do when its target is unresolvable (escalate? fail the workflow?) is a
policy decision, and for _handle_force_goto it is specifically "what happens
when the arbiter's own decision cannot be carried out", which needs an owner.
"""

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / "config" / "workflows"


def _phase_names(workflow_dir: Path) -> set:
    """Phase names as the loader sees them.

    Read from each phase file's own `name:` field, NOT the filename: the
    files are prefixed for ordering (01_feature_architect.yaml) while the
    declared name is unprefixed (feature_architect). Deriving names from
    stems makes every prefixed workflow look broken.
    """
    names = set()
    for path in workflow_dir.glob("*.yaml"):
        if path.name == "workflow.yaml":
            continue
        try:
            doc = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and doc.get("name"):
            names.add(doc["name"])

    # Some workflows declare phases inline instead of per-file.
    workflow_yaml = workflow_dir / "workflow.yaml"
    if workflow_yaml.exists():
        doc = yaml.safe_load(workflow_yaml.read_text()) or {}
        for phase in doc.get("phases") or []:
            if isinstance(phase, dict) and phase.get("name"):
                names.add(phase["name"])
            elif isinstance(phase, str):
                names.add(phase)
    return names


def _targets(node, found=None):
    found = found if found is not None else []
    if isinstance(node, dict):
        target = node.get("target")
        if isinstance(target, str) and target:
            found.append(target)
        for value in node.values():
            _targets(value, found)
    elif isinstance(node, list):
        for value in node:
            _targets(value, found)
    return found


def _workflow_dirs():
    return sorted(d for d in WORKFLOWS.iterdir() if d.is_dir() and (d / "workflow.yaml").exists())


@pytest.mark.parametrize("workflow_dir", _workflow_dirs(), ids=lambda d: d.name)
def test_every_goto_target_names_a_real_phase(workflow_dir):
    doc = yaml.safe_load((workflow_dir / "workflow.yaml").read_text()) or {}
    phase_names = _phase_names(workflow_dir)
    assert phase_names, f"no phases found for {workflow_dir.name}"

    unresolved = sorted(
        {
            t
            for t in _targets(doc)
            # A numeric target is a phase order, resolved by a different path.
            if not t.isdigit() and t not in phase_names
        }
    )

    assert not unresolved, (
        f"{workflow_dir.name}: goto target(s) {unresolved} name no phase in this "
        f"workflow (known: {sorted(phase_names)}). Both goto handlers fall back to "
        "advancing the phase when the target does not resolve, so this would "
        "silently turn a 'go back' decision into 'move forward'."
    )


def test_the_guard_actually_found_targets_to_check():
    """Guards the guard: if `target:` is renamed or restructured in the
    config schema, the extraction returns nothing and the test above passes
    vacuously for every workflow."""
    total = sum(
        len(_targets(yaml.safe_load((d / "workflow.yaml").read_text()) or {}))
        for d in _workflow_dirs()
    )
    assert total >= 3, f"expected several goto targets across workflows, found {total}"
