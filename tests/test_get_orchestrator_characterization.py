"""Characterization tests for PhaseManager._get_orchestrator.

SOLID review 2.13: the method fuses a DB read, the sequential-vs-
orchestrated decision, a cache, and phase_order_map construction into one
82-line method. Its swallowed-errors half was fixed earlier (a transient
DB failure used to be folded into the same `None` that means "sequential",
silently bypassing every gate); the structural fusion is untouched.

Existing coverage already pins the cache identity, the config refresh on
cache hit, phase_order_map construction, and raise-vs-return-None
(test_phase_manager.py, test_condition_evaluation_fails_loudly.py).

These fill the two gaps that decomposition would otherwise be free to
break silently:

  1. The three `return None` paths individually. Only "no
     orchestrator_config" was covered; a refactor could collapse or
     reorder the workflow-missing / no-definition_id / sequential checks
     and every existing test would still pass. These are not
     interchangeable -- `None` is read by mark_phase_complete as
     "sequential mode, advance the phase", so each one that should return
     None must keep doing so, and none of them may start raising.

  2. The per-workflow max_total_gotos override, and specifically that it
     does NOT write back to the shared WorkflowDefinition row. That is a
     documented live incident: the override used to be a global mutation
     of the definition row, which every workflow of that definition_id
     shares, so run_phase0's hardcoded max_iterations=3 reset every
     in-flight feature pipeline's real budget (30) down to 3. Nothing
     currently tests _get_orchestrator's own override behaviour -- the
     only max_iterations coverage is one level up, at the caller.
"""

import pytest

from src.core.database import DatabaseManager, Phase, Workflow
from src.core.database import WorkflowDefinition as DBWorkflowDefinition
from src.phases.phase_manager import PhaseManager


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "orch.db"))
    manager.create_tables()
    return manager


def _seed(
    db,
    *,
    definition_id="def-1",
    orchestrator_config=None,
    launch_params=None,
    with_definition=True,
):
    if orchestrator_config is None:
        orchestrator_config = {"type": "evaluating", "max_total_gotos": 30}
    with db.session_scope() as session:
        if with_definition:
            session.add(
                DBWorkflowDefinition(
                    id="def-1",
                    name="Test Definition",
                    orchestrator_config=orchestrator_config,
                )
            )
        session.add(
            Workflow(
                id="wf-1",
                name="t",
                phases_folder_path="/tmp",
                status="active",
                definition_id=definition_id,
                launch_params=launch_params,
            )
        )
        session.add(
            Phase(
                id="phase-dev",
                workflow_id="wf-1",
                order=4,
                name="development",
                description="d",
                done_definitions=["x"],
            )
        )


# ── the three legitimate `None` answers ─────────────────────────────
# None means "sequential mode" to mark_phase_complete, which advances the
# phase past every gate. Each of these must keep returning None, and none
# of them may start raising (that is the 2.9-shape fail-open in reverse).


def test_returns_none_when_the_workflow_row_does_not_exist(db):
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        assert pm._get_orchestrator(session, "wf-does-not-exist") is None


def test_returns_none_when_the_workflow_has_no_definition_id(db):
    """Same first guard, different half: `not workflow.definition_id`."""
    _seed(db, definition_id=None)
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        assert pm._get_orchestrator(session, "wf-1") is None


def test_returns_none_when_the_definition_row_is_missing(db):
    """workflow.definition_id points at a row that isn't there."""
    _seed(db, definition_id="def-missing", with_definition=False)
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        assert pm._get_orchestrator(session, "wf-1") is None


def test_returns_none_for_sequential_type(db):
    """The workflow is orchestrated-capable but explicitly sequential."""
    _seed(db, orchestrator_config={"type": "sequential"})
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        assert pm._get_orchestrator(session, "wf-1") is None


def test_returns_an_orchestrator_for_a_non_sequential_config(db):
    """The positive case, so the None tests above prove something."""
    _seed(db)
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        assert pm._get_orchestrator(session, "wf-1") is not None


# ── per-workflow max_total_gotos override ───────────────────────────


def test_launch_params_max_iterations_overrides_the_goto_budget(db):
    _seed(db, launch_params={"max_iterations": 3})
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        orch = pm._get_orchestrator(session, "wf-1")
    assert orch.config.max_total_gotos == 3


def test_without_the_override_the_definitions_budget_is_used(db):
    _seed(db, launch_params={})
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        orch = pm._get_orchestrator(session, "wf-1")
    assert orch.config.max_total_gotos == 30


def test_non_dict_launch_params_are_tolerated(db):
    """`launch_params if isinstance(..., dict) else {}` -- the column has
    held a JSON string in older rows."""
    _seed(db, launch_params="not-a-dict")
    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        orch = pm._get_orchestrator(session, "wf-1")
    assert orch.config.max_total_gotos == 30


def test_override_does_not_mutate_the_shared_definition_row(db):
    """THE documented incident. max_total_gotos used to be written back to
    the WorkflowDefinition row, which every workflow of that definition_id
    shares -- so run_phase0's hardcoded max_iterations=3 reset every
    in-flight feature pipeline's real budget (30, from workflow.yaml) down
    to 3. Observed live: a workflow re-arbitrating forever, total_gotos in
    the hundreds, its budget reset out from under it by unrelated Phase 0
    runs.

    The override must stay scoped to this workflow's own config object."""
    _seed(db, launch_params={"max_iterations": 3})
    pm = PhaseManager(db_manager=db)

    with db.session_scope() as session:
        orch = pm._get_orchestrator(session, "wf-1")
        assert orch.config.max_total_gotos == 3

    with db.session_scope() as session:
        definition = (
            session.query(DBWorkflowDefinition).filter_by(id="def-1").first()
        )
        assert definition.orchestrator_config["max_total_gotos"] == 30, (
            "the per-workflow override leaked back into the shared "
            "WorkflowDefinition row -- this is the exact regression that "
            "reset every concurrent workflow's goto budget"
        )


def test_a_second_workflow_of_the_same_definition_keeps_its_own_budget(db):
    """The incident's actual shape: two workflows sharing one definition,
    one launched with a smaller budget, must not affect each other."""
    _seed(db, launch_params={"max_iterations": 3})
    with db.session_scope() as session:
        session.add(
            Workflow(
                id="wf-2",
                name="other",
                phases_folder_path="/tmp",
                status="active",
                definition_id="def-1",
                launch_params={},
            )
        )

    pm = PhaseManager(db_manager=db)
    with db.session_scope() as session:
        small = pm._get_orchestrator(session, "wf-1")
        big = pm._get_orchestrator(session, "wf-2")

    assert small.config.max_total_gotos == 3
    assert big.config.max_total_gotos == 30, (
        "wf-1's launch-time override bled into wf-2, which shares def-1"
    )


def test_override_is_re_read_from_the_db_on_a_cache_hit(db):
    """Config -- including this override -- is rebuilt from the DB and
    reassigned onto the cached instance on EVERY call, not just the build.

    Asserting the override merely survives a second call proves nothing:
    the cached instance already carries it from the build, so that passes
    even if the cache-hit path never refreshes anything (confirmed by
    mutation). Changing launch_params between the two calls is what
    actually distinguishes "re-read every call" from "built once".
    """
    _seed(db, launch_params={"max_iterations": 3})
    pm = PhaseManager(db_manager=db)

    with db.session_scope() as session:
        first = pm._get_orchestrator(session, "wf-1")
        assert first.config.max_total_gotos == 3

    with db.session_scope() as session:
        session.query(Workflow).filter_by(id="wf-1").first().launch_params = {
            "max_iterations": 12
        }

    with db.session_scope() as session:
        second = pm._get_orchestrator(session, "wf-1")

    assert first is second, "expected the cached instance, not a rebuild"
    assert second.config.max_total_gotos == 12, (
        "the cache-hit path returned a stale config instead of re-reading "
        "it from the DB"
    )
