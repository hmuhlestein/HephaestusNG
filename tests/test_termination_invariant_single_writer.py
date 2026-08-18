"""Structural guard for the agent-termination invariant (Phase 2 §4.2).

The invariant -- status="terminated" implies current_task_id IS NULL and
terminated_at IS NOT NULL -- has independently recurred as a bug eight times
in this codebase's history, twice causing confirmed live data loss. §4.2's
stated goal was not "patch the remaining sites" but making the invariant
structurally impossible to violate, by giving it exactly one writer.

Per-call-site tests cannot deliver that: they only cover the sites someone
remembered to write a test for, and the recurring failure has always been a
*new* raw write appearing somewhere nobody was looking. This test asserts the
property directly instead -- there is one implementation, and adding a twelfth
hand-rolled copy fails here rather than in production six weeks later.
"""

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# The single legitimate writer: engine_client.terminate_agent's _do_terminate.
ALLOWED = {"src/autopilot/orchestrator/engine_client.py"}


def _raw_terminated_writes(path: Path):
    """Yield line numbers of `<anything>.status = "terminated"` assignments."""
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not (isinstance(node.value, ast.Constant) and node.value.value == "terminated"):
            continue
        for target in node.targets:
            if isinstance(target, ast.Attribute) and target.attr == "status":
                yield node.lineno


def test_termination_invariant_has_exactly_one_writer():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC.parent).as_posix()
        if rel in ALLOWED:
            continue
        for lineno in _raw_terminated_writes(path):
            offenders.append(f"{rel}:{lineno}")

    assert not offenders, (
        "Raw agent-termination writes found outside the shared primitive:\n  "
        + "\n  ".join(offenders)
        + "\n\nCall engine_client.terminate_agent(agent_id, session=...) instead. "
        "It sets all three invariant fields together and releases any Task still "
        "pointing at the agent, in that order -- the ordering exists because a "
        "dying agent's in-flight completion call landing in the gap has twice "
        "destroyed real completed work (91699b1, 92caa82)."
    )


def test_the_one_allowed_writer_still_exists():
    """Guards against the allowlist silently outliving the code it describes."""
    primitive = SRC.parent / "src/autopilot/orchestrator/engine_client.py"
    assert list(_raw_terminated_writes(primitive)), (
        "engine_client.py no longer contains the termination write the allowlist "
        "exempts -- either the primitive moved (update ALLOWED) or this guard is "
        "now vacuous."
    )
