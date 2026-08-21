"""Resolving a task's phase when phase_id may be an order number.

Task.phase_id holds either a real Phase.id UUID or a digit-string phase
*order* -- the MCP create_task tool sends order numbers through that field
(see TaskEnrichmentService.resolve_phase_id, the canonical resolver for the
write path). Every read site therefore has to branch on `.isdigit()`, and
that branch was reimplemented ten times (SOLID review 1.4).

The copies drifted. Five scoped the order lookup to the task's workflow;
five did not:

    session.query(Phase).filter_by(order=int(task.phase_id)).first()

Phase orders are per-workflow, not global. This database holds 427 phases
across 41 workflows, and the same order maps to genuinely different phases
depending on the definition -- order 1 is "product_requirements" or "Feature
Architect", order 4 is "development" or "design_review", order 5 is
"architectural_review" or "development". An unscoped lookup returns whichever
row comes back first.

That is not cosmetic in every caller: prompts/assembler.py used the result
for phase_description and done_definitions, so an agent could be handed
another workflow definition's instructions for its phase.
"""

from typing import Optional


def resolve_task_phase(session, task) -> Optional[object]:
    """The Phase a task belongs to, or None.

    Handles both forms of Task.phase_id and always scopes an order lookup to
    the task's own workflow. Returns None rather than raising when nothing
    matches: every caller renders a phase-less fallback, and a read path
    should not fail a request over a missing phase row.
    """
    from src.core.database import Phase

    if not task.phase_id:
        return None

    if str(task.phase_id).isdigit():
        query = session.query(Phase).filter_by(order=int(task.phase_id))
        # Scope to this task's workflow. Without it the same order resolves
        # to a different workflow definition's phase entirely.
        if task.workflow_id:
            query = query.filter_by(workflow_id=task.workflow_id)
        return query.first()

    return session.query(Phase).filter_by(id=task.phase_id).first()
