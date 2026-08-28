"""Regression test for PhaseManager._fail_workflow silently dropping its
own `reason` argument on the floor.

_fail_workflow set workflow.status = "failed" but never wrote `reason` to
Workflow.status_reason -- only logged it via logger.error. A comment
elsewhere in this same file (near _handle_goto's "no phase" failure path)
explicitly claims "the reason... travels to Workflow.status_reason...
via _fail_workflow", which was aspirational, not what the code did.

Confirmed live: workflow e35be066 (feature speckit-cli-integration) sat
"failed" with an empty status_reason, so the only way to learn why was to
grep raw backend logs across a day's worth of timestamped files -- the
column that exists specifically to answer this had nothing in it.
"""

from src.core.database import DatabaseManager, Workflow
from src.phases.phase_manager import PhaseManager


def _seed_workflow(db_manager):
    with db_manager.session_scope() as session:
        session.add(
            Workflow(
                id="wf-1",
                name="wf-1",
                status="active",
                phases_folder_path="/tmp",
            )
        )


def test_fail_workflow_persists_status_reason(tmp_path):
    db_manager = DatabaseManager(str(tmp_path / "test.db"))
    db_manager.create_tables()
    _seed_workflow(db_manager)

    manager = PhaseManager(db_manager, workflow_id="wf-1")
    with db_manager.session_scope() as session:
        manager._fail_workflow(session, "evaluator rejected the output twice")

    with db_manager.session_scope() as session:
        wf = session.query(Workflow).filter_by(id="wf-1").first()
        assert wf.status == "failed"
        assert wf.status_reason == "evaluator rejected the output twice"
