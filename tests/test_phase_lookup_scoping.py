"""A task's phase must resolve within its own workflow.

Task.phase_id holds either a Phase.id UUID or a digit-string phase *order*
(the MCP create_task tool sends order numbers through that field). Ten read
sites branched on `.isdigit()` independently (SOLID review 1.4), and they had
drifted: five scoped the order lookup to the task's workflow, five did not.

Phase orders are per-workflow, not global. In this repo's own database that
is 427 phases across 41 workflows, with the same order mapping to genuinely
different phases -- order 1 is "product_requirements" or "Feature Architect",
order 4 is "development" or "design_review". An unscoped
`filter_by(order=N).first()` returns whichever row comes back first.

The worst caller was prompts/assembler.py, which used the result for
phase_description and done_definitions -- so an agent could be handed another
workflow definition's instructions for its phase.

Uses a real database: the whole defect is about which row a query returns, so
a mocked session would accept the buggy and the fixed version equally.
"""

from datetime import datetime

import pytest

from src.core.database import DatabaseManager, Phase, Task, Workflow
from src.core.phase_lookup import resolve_task_phase


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "phases.db"))
    manager.create_tables()
    session = manager.get_session()
    for wf_id, name, order in (
        ("wf-autopilot", "development", 4),
        ("wf-feature", "design_review", 4),
    ):
        session.add(
            Workflow(
                id=wf_id,
                name=wf_id,
                phases_folder_path="/tmp",
                status="active",
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            Phase(
                id=f"phase-{wf_id}",
                workflow_id=wf_id,
                order=order,
                name=name,
                description=f"{name} description",
                done_definitions=[f"{name} done"],
            )
        )
    session.commit()
    session.close()
    return manager


def _task(task_id, workflow_id, phase_id):
    return Task(
        id=task_id,
        raw_description="x",
        done_definition="done",
        status="pending",
        workflow_id=workflow_id,
        phase_id=phase_id,
    )


class TestOrderLookupIsWorkflowScoped:
    @pytest.mark.parametrize(
        "workflow_id,expected",
        [("wf-autopilot", "development"), ("wf-feature", "design_review")],
    )
    def test_the_same_order_resolves_per_workflow(self, db, workflow_id, expected):
        """Order 4 exists in both workflows and names a different phase in
        each. Unscoped, one of these two necessarily got the wrong one."""
        session = db.get_session()
        try:
            task = _task("t1", workflow_id, "4")
            phase = resolve_task_phase(session, task)
            assert phase is not None
            assert phase.name == expected
            assert phase.workflow_id == workflow_id
        finally:
            session.close()

    def test_the_resolved_description_matches_the_task_s_own_workflow(self, db):
        """The assembler consequence: description/done_definitions feed an
        agent's prompt, so resolving cross-workflow hands it the wrong
        instructions."""
        session = db.get_session()
        try:
            phase = resolve_task_phase(session, _task("t2", "wf-feature", "4"))
            assert phase.description == "design_review description"
            assert phase.done_definitions == ["design_review done"]
        finally:
            session.close()

    def test_an_order_absent_from_this_workflow_resolves_to_nothing(self, db):
        """Returning None is the honest answer -- better than silently
        handing back another workflow's phase at that order."""
        session = db.get_session()
        try:
            assert resolve_task_phase(session, _task("t3", "wf-feature", "99")) is None
        finally:
            session.close()


class TestOtherForms:
    def test_a_uuid_phase_id_still_resolves(self, db):
        session = db.get_session()
        try:
            phase = resolve_task_phase(
                session, _task("t4", "wf-autopilot", "phase-wf-autopilot")
            )
            assert phase.name == "development"
        finally:
            session.close()

    def test_no_phase_id_resolves_to_none(self, db):
        session = db.get_session()
        try:
            assert resolve_task_phase(session, _task("t5", "wf-autopilot", None)) is None
        finally:
            session.close()

    def test_an_order_without_a_workflow_resolves_to_nothing(self, db):
        """An order is only meaningful inside a workflow. Returning some
        arbitrary workflow's phase at that order would be a guess presented
        as fact, and agents_api's sites already returned None here -- so
        strictness is what keeps all ten call sites consistent.

        Costs nothing in practice: of 1324 tasks in this repo's database, 753
        use the order form and 249 have no workflow, and those sets do not
        overlap.
        """
        session = db.get_session()
        try:
            assert resolve_task_phase(session, _task("t6", None, "4")) is None
        finally:
            session.close()

    def test_a_uuid_still_resolves_without_a_workflow(self, db):
        """Strictness applies only to the order form -- a UUID identifies a
        phase on its own, so it must keep working."""
        session = db.get_session()
        try:
            phase = resolve_task_phase(session, _task("t7", None, "phase-wf-feature"))
            assert phase is not None
            assert phase.name == "design_review"
        finally:
            session.close()
