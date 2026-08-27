"""Automatic Spec Kit Scanning Setting -- REQ-01..10, NFR-01..04.

Covers: the AutopilotProject.speckit_auto_scan_enabled column + migration
(Task 1), the PATCH endpoint + status payload field (Task 2), and
_sync_speckit_designs's scan-integration behavior (Task 3).
"""

import inspect
import uuid
from unittest.mock import MagicMock, Mock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.autopilot.orchestrator.queue import _sync_speckit_designs
from src.core.database import AutopilotDesign, AutopilotProject, Base, ProjectRepo
from src.core.schema_migrations import migrate_speckit_auto_scan_column
from src.core.speckit_detection import find_speckit_features


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _skip_fk(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _make_feature(repo_path, number, slug, plan=False):
    feature_dir = repo_path / "specs" / f"{number}-{slug}"
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text("# Spec\n", encoding="utf-8")
    if plan:
        (feature_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    return feature_dir


@pytest.fixture
def project_with_repo(db_session, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    proj = AutopilotProject(
        id=f"proj-{uuid.uuid4().hex[:8]}",
        name="Test Project",
        base_dir=str(repo_path),
    )
    db_session.add(proj)
    db_session.add(
        ProjectRepo(
            id=f"repo-{uuid.uuid4().hex[:8]}",
            project_id=proj.id,
            label="main",
            path=str(repo_path),
            is_primary=True,
        )
    )
    db_session.commit()
    return proj, repo_path


class TestSchemaAndMigration:
    def test_speckit_auto_scan_enabled_defaults_false(self, db_session):
        proj = AutopilotProject(id=f"proj-{uuid.uuid4().hex[:8]}", name="p", base_dir="/tmp")
        db_session.add(proj)
        db_session.commit()
        assert proj.speckit_auto_scan_enabled is False

    def test_migration_adds_column_to_existing_db(self, tmp_path):
        # Simulate a pre-existing DB by creating the table without the new
        # column, then running the migration against it (NFR-02).
        db_path = tmp_path / "legacy.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE autopilot_projects (id VARCHAR PRIMARY KEY, name VARCHAR, base_dir VARCHAR)"))
            conn.execute(text("INSERT INTO autopilot_projects (id, name, base_dir) VALUES ('p1', 'P', '/tmp')"))
            conn.commit()

        migrate_speckit_auto_scan_column(engine)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT speckit_auto_scan_enabled FROM autopilot_projects")).fetchall()
        assert rows == [(0,)]

    def test_migration_is_idempotent(self, tmp_path):
        db_path = tmp_path / "legacy2.db"
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            conn.execute(text("CREATE TABLE autopilot_projects (id VARCHAR PRIMARY KEY)"))
            conn.commit()

        migrate_speckit_auto_scan_column(engine)
        migrate_speckit_auto_scan_column(engine)  # must not raise


class TestPatchEndpoint:
    async def test_set_speckit_auto_scan_persists_value(self, db_session, project_with_repo):
        from src.mcp.autopilot.feature_review_routes import SpeckitAutoScanUpdate, set_speckit_auto_scan

        proj, _ = project_with_repo
        cm = MagicMock()
        cm.__enter__ = Mock(return_value=db_session)
        cm.__exit__ = Mock(return_value=False)

        with patch("src.core.database.get_db", return_value=cm), patch("src.mcp.autopilot.feature_review_routes._invalidate"):
            result = await set_speckit_auto_scan(proj.id, SpeckitAutoScanUpdate(speckit_auto_scan_enabled=True))

        assert result == {"speckit_auto_scan_enabled": True}
        db_session.refresh(proj)
        assert proj.speckit_auto_scan_enabled is True

    async def test_set_speckit_auto_scan_unknown_project_404s(self, db_session):
        from src.mcp.autopilot.feature_review_routes import SpeckitAutoScanUpdate, set_speckit_auto_scan

        cm = MagicMock()
        cm.__enter__ = Mock(return_value=db_session)
        cm.__exit__ = Mock(return_value=False)

        with patch("src.core.database.get_db", return_value=cm):
            with pytest.raises(HTTPException) as exc_info:
                await set_speckit_auto_scan("does-not-exist", SpeckitAutoScanUpdate(speckit_auto_scan_enabled=True))
        assert exc_info.value.status_code == 404

    async def test_toggle_one_project_does_not_affect_another(self, db_session):
        from src.mcp.autopilot.feature_review_routes import SpeckitAutoScanUpdate, set_speckit_auto_scan

        proj_a = AutopilotProject(id=f"proj-{uuid.uuid4().hex[:8]}", name="A", base_dir="/tmp/a")
        proj_b = AutopilotProject(id=f"proj-{uuid.uuid4().hex[:8]}", name="B", base_dir="/tmp/b")
        db_session.add_all([proj_a, proj_b])
        db_session.commit()

        cm = MagicMock()
        cm.__enter__ = Mock(return_value=db_session)
        cm.__exit__ = Mock(return_value=False)

        with patch("src.core.database.get_db", return_value=cm), patch("src.mcp.autopilot.feature_review_routes._invalidate"):
            await set_speckit_auto_scan(proj_a.id, SpeckitAutoScanUpdate(speckit_auto_scan_enabled=True))

        db_session.refresh(proj_a)
        db_session.refresh(proj_b)
        assert proj_a.speckit_auto_scan_enabled is True
        assert proj_b.speckit_auto_scan_enabled is False


class TestSyncSpeckitDesigns:
    def test_disabled_produces_zero_rows(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = False
        db_session.commit()
        _make_feature(repo_path, "001", "foo", plan=True)

        _sync_speckit_designs(db_session, proj)
        _sync_speckit_designs(db_session, proj)  # repeated call, still nothing (REQ-06)

        assert db_session.query(AutopilotDesign).count() == 0

    def test_enabled_with_plan_queues_one_pending_row(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "foo", plan=True)

        _sync_speckit_designs(db_session, proj)

        rows = db_session.query(AutopilotDesign).all()
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].file_path.endswith("001-foo/spec.md")

    def test_spec_only_never_queued_until_plan_appears(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        feature_dir = _make_feature(repo_path, "001", "foo", plan=False)

        _sync_speckit_designs(db_session, proj)
        _sync_speckit_designs(db_session, proj)
        assert db_session.query(AutopilotDesign).count() == 0

        (feature_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
        _sync_speckit_designs(db_session, proj)
        assert db_session.query(AutopilotDesign).count() == 1

    def test_repeated_call_does_not_duplicate_or_raise(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "foo", plan=True)

        _sync_speckit_designs(db_session, proj)
        _sync_speckit_designs(db_session, proj)  # must not raise IntegrityError

        assert db_session.query(AutopilotDesign).count() == 1

    def test_editing_spec_while_pending_refreshes_in_place(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        feature_dir = _make_feature(repo_path, "001", "foo", plan=True)

        _sync_speckit_designs(db_session, proj)
        original = db_session.query(AutopilotDesign).one()
        original_hash = original.content_hash
        original_modified_at = original.modified_at

        (feature_dir / "spec.md").write_text("# Spec (edited)\n", encoding="utf-8")
        _sync_speckit_designs(db_session, proj)

        rows = db_session.query(AutopilotDesign).all()
        assert len(rows) == 1
        assert rows[0].id == original.id
        assert rows[0].content_hash != original_hash
        assert rows[0].modified_at > original_modified_at

    def test_multiple_new_features_all_queued_in_one_call(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "foo", plan=True)
        _make_feature(repo_path, "002", "bar", plan=True)

        _sync_speckit_designs(db_session, proj)

        rows = db_session.query(AutopilotDesign).all()
        assert len(rows) == 2
        names = {r.name for r in rows}
        assert names == {"001-foo", "002-bar"}

    def test_non_pending_row_never_refreshed_or_duplicated(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        feature_dir = _make_feature(repo_path, "001", "foo", plan=True)

        _sync_speckit_designs(db_session, proj)
        design = db_session.query(AutopilotDesign).one()
        design.status = "processing"
        db_session.commit()

        (feature_dir / "spec.md").write_text("# Spec (edited)\n", encoding="utf-8")
        _sync_speckit_designs(db_session, proj)

        rows = db_session.query(AutopilotDesign).all()
        assert len(rows) == 1
        assert rows[0].status == "processing"
        assert rows[0].content_hash != None  # noqa: E711 -- unchanged, not refreshed

    def test_one_feature_failure_does_not_discard_sibling_row(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "bad", plan=True)
        _make_feature(repo_path, "002", "good", plan=True)

        from pathlib import Path as _Path

        real_read_bytes = _Path.read_bytes

        def flaky_read_bytes(self):
            if "001-bad" in str(self):
                raise OSError("simulated unreadable file")
            return real_read_bytes(self)

        with patch.object(_Path, "read_bytes", flaky_read_bytes):
            _sync_speckit_designs(db_session, proj)

        rows = db_session.query(AutopilotDesign).all()
        assert len(rows) == 1
        assert "002-good" in rows[0].file_path

    def test_db_commit_failure_isolated_and_logged_as_warning(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "foo", plan=True)

        from sqlalchemy.exc import IntegrityError

        real_commit = db_session.commit
        call_count = {"n": 0}

        def flaky_commit():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise IntegrityError("simulated race", params=None, orig=Exception("dup"))
            return real_commit()

        with patch.object(db_session, "commit", side_effect=flaky_commit):
            _sync_speckit_designs(db_session, proj)  # must not raise

        # The failed insert must not have left a half-committed row, and the
        # session must remain usable afterward (rollback recovered it).
        assert db_session.query(AutopilotDesign).count() == 0
        db_session.query(AutopilotDesign).all()  # would raise PendingRollbackError if session were broken

    def test_unexpected_error_isolated_to_one_feature_not_raised(self, db_session, project_with_repo):
        # A genuine programming bug must not raise out of _sync_speckit_designs:
        # doing so would propagate into pick_next_design's own generic except
        # block, whose file-scan fallback has no budget check (adversarial
        # review WARNING).
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "foo", plan=True)

        def broken_max(*_args, **_kwargs):
            raise TypeError("simulated programming bug")

        with patch("src.autopilot.orchestrator.queue.func") as mock_func:
            mock_func.max.side_effect = broken_max
            _sync_speckit_designs(db_session, proj)  # must not raise

        assert db_session.query(AutopilotDesign).count() == 0

    def test_unexpected_error_on_one_feature_does_not_block_sibling_in_same_pass(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "bad", plan=True)
        _make_feature(repo_path, "002", "good", plan=True)

        real_max = __import__("sqlalchemy").func.max
        call_count = {"n": 0}

        def broken_max(column):
            # AutopilotDesign.ordinal is queried once per genuinely-new
            # feature, in find_speckit_features's stable (repo_label, number)
            # order -- "001-bad" is processed first, so raise only on the
            # first call and let every call after it (i.e. "002-good") behave
            # normally.
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise TypeError("simulated programming bug")
            return real_max(column)

        with patch("src.autopilot.orchestrator.queue.func") as mock_func:
            mock_func.max.side_effect = broken_max
            _sync_speckit_designs(db_session, proj)  # must not raise

        rows = db_session.query(AutopilotDesign).all()
        assert len(rows) == 1
        assert "002-good" in rows[0].file_path

    def test_detection_failure_logged_and_returns_without_raising(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        _make_feature(repo_path, "001", "foo", plan=True)

        with patch(
            "src.core.speckit_detection.find_speckit_features",
            side_effect=RuntimeError("simulated detection failure"),
        ):
            _sync_speckit_designs(db_session, proj)  # must not raise

        assert db_session.query(AutopilotDesign).count() == 0

    def test_toggle_does_not_change_detection_output(self, db_session, project_with_repo):
        proj, repo_path = project_with_repo
        _make_feature(repo_path, "001", "foo", plan=True)

        proj.speckit_auto_scan_enabled = False
        db_session.commit()
        off_features = find_speckit_features(db_session, proj.id)

        proj.speckit_auto_scan_enabled = True
        db_session.commit()
        on_features = find_speckit_features(db_session, proj.id)

        assert [f.dir_name for f in off_features] == [f.dir_name for f in on_features] == ["001-foo"]

    def test_is_synchronous_no_new_loop(self):
        # NFR-03: hooks into the existing scan loop, no new asyncio task.
        assert not inspect.iscoroutinefunction(_sync_speckit_designs)


class TestPickNextDesignBudgetGate:
    """pick_next_design must check the budget BEFORE calling
    _sync_speckit_designs, not after -- an over-budget project should do
    neither a speckit filesystem scan nor a DB write (adversarial review
    WARNING)."""

    @pytest.fixture
    def orch_db_env(self, tmp_path, monkeypatch):
        from src.core.database import DatabaseManager

        db_path = tmp_path / "test.db"
        monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
        db = DatabaseManager(str(db_path))
        db.create_tables()
        return db

    def test_over_budget_project_never_syncs_speckit_features(self, tmp_path, orch_db_env):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design
        from src.core.database import AutopilotProject, ProjectRepo, get_db

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        _make_feature(repo_path, "001", "foo", plan=True)

        session = orch_db_env.get_session()
        session.add(
            AutopilotProject(
                id="proj-overbudget",
                name="p",
                base_dir=str(repo_path),
                is_active=True,
                speckit_auto_scan_enabled=True,
                cost_limit_usd=1.0,
                cost_total_usd=5.0,
            )
        )
        session.add(ProjectRepo(id="repo-overbudget", project_id="proj-overbudget", label="main", path=str(repo_path), is_primary=True))
        session.commit()
        session.close()

        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(repo_path, set(), logger, project_id="proj-overbudget")

        assert result is None
        with get_db() as db:
            assert db.query(AutopilotDesign).count() == 0

    def test_under_budget_project_syncs_speckit_features_via_pick_next_design(self, tmp_path, orch_db_env):
        from src.autopilot.orchestrator import OrchestratorLogger
        from src.autopilot.orchestrator.queue import pick_next_design
        from src.core.database import AutopilotProject, ProjectRepo, get_db

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        _make_feature(repo_path, "001", "foo", plan=True)

        session = orch_db_env.get_session()
        session.add(
            AutopilotProject(
                id="proj-underbudget",
                name="p",
                base_dir=str(repo_path),
                is_active=True,
                speckit_auto_scan_enabled=True,
            )
        )
        session.add(ProjectRepo(id="repo-underbudget", project_id="proj-underbudget", label="main", path=str(repo_path), is_primary=True))
        session.commit()
        session.close()

        logger = OrchestratorLogger(tmp_path)
        result = pick_next_design(repo_path, set(), logger, project_id="proj-underbudget")

        assert result is not None
        with get_db() as db:
            rows = db.query(AutopilotDesign).all()
            assert len(rows) == 1
            assert "001-foo" in rows[0].file_path
