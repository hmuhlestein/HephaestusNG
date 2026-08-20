"""System-wide default spend cap.

Stored in the ProjectContext KV table rather than a new table: it is already a
global key/value store, so a runtime-editable setting needs no migration.

The default seeds NEW projects only. Raising it must never silently widen the
cap on a project someone deliberately constrained, which is why nothing here
touches existing rows.
"""

from contextlib import contextmanager

import pytest

import src.services.system_settings as ss


@pytest.fixture
def db(monkeypatch, tmp_path):
    from src.core.database import AutopilotProject, Base, DatabaseManager, ProjectContext

    mgr = DatabaseManager(str(tmp_path / "t.db"))
    Base.metadata.create_all(
        mgr.engine,
        tables=[ProjectContext.__table__, AutopilotProject.__table__],
        checkfirst=True,
    )

    @contextmanager
    def _get_db():
        session = mgr.get_session()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.database.get_db", _get_db)
    return mgr


class TestDefaultCostLimit:
    def test_unset_by_default(self, db):
        """Absent the setting, projects stay unlimited -- the behaviour that
        existed before this feature."""
        assert ss.get_default_cost_limit() is None

    def test_round_trips(self, db):
        assert ss.set_default_cost_limit(25.0) == 25.0
        assert ss.get_default_cost_limit() == 25.0

    def test_none_clears_it(self, db):
        ss.set_default_cost_limit(25.0)
        assert ss.set_default_cost_limit(None) is None
        assert ss.get_default_cost_limit() is None

    def test_overwrites_rather_than_duplicating_the_key(self, db):
        """ProjectContext.key is unique; a second write must update, not
        insert."""
        from src.core.database import ProjectContext

        ss.set_default_cost_limit(10.0)
        ss.set_default_cost_limit(20.0)
        session = db.get_session()
        try:
            rows = session.query(ProjectContext).filter_by(key=ss.DEFAULT_COST_LIMIT_KEY).all()
            assert len(rows) == 1
        finally:
            session.close()
        assert ss.get_default_cost_limit() == 20.0

    @pytest.mark.parametrize("bad", [0, -1, -0.01])
    def test_rejects_non_positive(self, db, bad):
        """A stored 0 would put every new project instantly over budget, which
        is never what someone means by a default."""
        with pytest.raises(ValueError, match="greater than 0"):
            ss.set_default_cost_limit(bad)

    def test_rejects_non_numeric(self, db):
        with pytest.raises(ValueError, match="must be a number"):
            ss.set_default_cost_limit("abc")

    def test_a_rejected_write_leaves_the_previous_value_intact(self, db):
        ss.set_default_cost_limit(15.0)
        for bad in (0, -5, "abc"):
            with pytest.raises(ValueError):
                ss.set_default_cost_limit(bad)
        assert ss.get_default_cost_limit() == 15.0

    def test_a_corrupt_stored_value_reads_as_unset(self, db):
        """Hand-edited or legacy garbage must degrade to "no default" rather
        than raising inside project creation."""
        ss._set(ss.DEFAULT_COST_LIMIT_KEY, "not-a-number")
        assert ss.get_default_cost_limit() is None

    def test_a_stored_zero_reads_as_unset(self, db):
        ss._set(ss.DEFAULT_COST_LIMIT_KEY, 0)
        assert ss.get_default_cost_limit() is None

    def test_accepts_a_caller_supplied_session(self, db):
        """Project creation reads this inside the transaction it already
        holds -- opening a nested get_db() mid-flush is how SQLite deadlocks."""
        ss.set_default_cost_limit(30.0)
        session = db.get_session()
        try:
            assert ss.get_default_cost_limit(session) == 30.0
        finally:
            session.close()


class TestReadFailsSoft:
    """Project creation worked before this setting existed. It must not start
    failing because a settings lookup did."""

    def test_a_broken_store_reads_as_no_default(self, db, caplog):
        from unittest.mock import patch

        with patch.object(ss, "_get", side_effect=RuntimeError("table missing")):
            assert ss.get_default_cost_limit() is None
        assert "treating as no default" in caplog.text

    def test_the_write_path_still_raises(self, db):
        """Asymmetric on purpose: the caller explicitly asked to save, and
        swallowing that would be silent data loss."""
        from unittest.mock import patch

        with patch.object(ss, "_set", side_effect=RuntimeError("db locked")):
            with pytest.raises(RuntimeError):
                ss.set_default_cost_limit(10.0)


class TestSeedsNewProjectsOnly:
    """The whole contract: raising or clearing the default must never reach
    back and change a project someone deliberately configured."""

    @staticmethod
    def _create(mgr, name):
        """Mirrors the real creation sites -- read inside the in-flight session."""
        import uuid

        from src.core.database import AutopilotProject

        session = mgr.get_session()
        try:
            proj = AutopilotProject(
                id=f"proj-{uuid.uuid4().hex[:12]}",
                name=name,
                base_dir=f"/tmp/{name}",
                cost_limit_usd=ss.get_default_cost_limit(session),
            )
            session.add(proj)
            session.commit()
            return proj.cost_limit_usd
        finally:
            session.close()

    @staticmethod
    def _cap(mgr, name):
        from src.core.database import AutopilotProject

        session = mgr.get_session()
        try:
            return session.query(AutopilotProject).filter_by(name=name).first().cost_limit_usd
        finally:
            session.close()

    def test_seeding_and_isolation(self, db):
        assert self._create(db, "before") is None       # no default yet

        ss.set_default_cost_limit(50.0)
        assert self._create(db, "after") == 50.0        # seeded
        assert self._cap(db, "before") is None          # earlier project untouched

        ss.set_default_cost_limit(None)
        assert self._create(db, "cleared") is None      # new ones unlimited again
        assert self._cap(db, "after") == 50.0           # the capped one stays capped


class TestAppliedAtProjectCreation:
    def test_every_construction_site_passes_the_default(self):
        """Four places construct AutopilotProject. A site that forgets this
        silently creates an uncapped project, which is exactly the case the
        setting exists to prevent -- and it would only show up as a surprise
        bill."""
        import re
        from pathlib import Path

        sites = [
            "src/mcp/autopilot/project_routes.py",
            "src/mcp/autopilot/queue_routes.py",
            "src/cli/commands/project.py",
            "src/autopilot/orchestrator/state.py",
        ]
        for site in sites:
            text = Path(site).read_text()
            for match in re.finditer(r"AutopilotProject\(\n(.*?)\n\s*\)", text, re.S):
                block = match.group(1)
                assert "cost_limit_usd=get_default_cost_limit(" in block, (
                    f"{site} constructs AutopilotProject without the default spend cap"
                )
