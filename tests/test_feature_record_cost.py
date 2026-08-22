"""The feature-record endpoint's cost comes from the DB, not the metrics file.

pipeline_metrics.json has never carried a cost field. Its only writer
(phase_manager's "stub") emits design_name / workflow_id / project_path /
docs_dir / feature_folder / completed_at / stop_reason / qa_passed /
product_validated -- so `metrics.get("cost_total", 0)` silently returned 0 for
every feature ever recorded, while the real figure sat in
Workflow.cost_total_usd the whole time.

Reading the DB rollup rather than writing cost into the JSON is deliberate:
cost_derivation already maintains that column from the CostEntry ledger, and
copying the number into a file at assembly time would create a second source
that goes stale the moment a late cost entry lands.
"""

from contextlib import contextmanager

import pytest

from src.mcp.autopilot.feature_record_routes import _feature_record_cost


@pytest.fixture
def db(monkeypatch, tmp_path):
    from src.core.database import Base, DatabaseManager, Workflow

    mgr = DatabaseManager(str(tmp_path / "t.db"))
    Base.metadata.create_all(mgr.engine, tables=[Workflow.__table__], checkfirst=True)

    @contextmanager
    def _get_db():
        session = mgr.get_session()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.database.get_db", _get_db)
    return mgr


def _seed(mgr, workflow_id, cost):
    from src.core.database import Workflow

    session = mgr.get_session()
    try:
        session.add(
            Workflow(
                id=workflow_id,
                name="wf",
                phases_folder_path="/tmp/phases",  # NOT NULL
                status="completed",
                cost_total_usd=cost,
            )
        )
        session.commit()
    finally:
        session.close()


class TestFeatureRecordCost:
    def test_reads_the_workflow_rollup(self, db):
        _seed(db, "wf-1", 12.3456)
        assert _feature_record_cost("wf-1") == pytest.approx(12.3456)

    def test_zero_when_the_workflow_has_no_recorded_cost(self, db):
        _seed(db, "wf-2", None)
        assert _feature_record_cost("wf-2") == 0.0

    def test_zero_for_an_unknown_workflow(self, db):
        """An archived feature whose Workflow row was pruned still renders,
        just without a figure."""
        assert _feature_record_cost("wf-does-not-exist") == 0.0

    def test_zero_when_the_metrics_file_has_no_workflow_id(self, db):
        assert _feature_record_cost(None) == 0.0
        assert _feature_record_cost("") == 0.0

    def test_the_metrics_writer_still_emits_no_cost_field(self):
        """Guards the premise. If a future change starts writing cost into
        pipeline_metrics.json, this fails and someone re-decides which source
        is authoritative rather than quietly ending up with two."""
        import re
        from pathlib import Path

        src = Path("src/phases/phase_manager.py").read_text()
        start = src.index("_metrics = {")
        keys = set(re.findall(r'"([a-z_]+)":', src[start : src.index("metrics_path.write_text", start)]))
        assert keys, "could not locate the pipeline_metrics.json writer"
        assert not {k for k in keys if "cost" in k}, (
            f"pipeline_metrics.json now writes a cost field ({keys}); the endpoint "
            "reads Workflow.cost_total_usd instead, so decide which one wins"
        )
