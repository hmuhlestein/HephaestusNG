"""Regression tests for the self-review gate being silently disabled.

The gate (docs/GAP_CHECK_SELF_LOOP_DESIGN.md,
_update_task_status_steps._maybe_fire_self_review_gate) requires
`phase.self_review.get("enabled")`. A Phase row with self_review = NULL
therefore never fires it -- the task completes on its FIRST "done", which
is indistinguishable from a phase deliberately configured with
self-review off. Nothing errors, nothing logs; a quality gate just
stops existing for that workflow.

That happened. Two independent defects combined:

  1. `sdk/client.py`'s YAML phase loader never read the `self_review:`
     key, unlike workflow_engine/yaml_loader.py and phase_manager.py. So
     every Phase row it created had self_review = NULL.

  2. The backfill that was supposed to catch this
     (`migrate_self_review_columns`) lives in SCHEMA_MIGRATIONS, which
     records each id and skips it forever after the first run. It could
     not repair drift introduced later -- and (1) kept introducing it,
     once per newly seeded workflow.

Observed live: 2 of 35 development phase rows had self_review unset, and
a development task on one of them completed with self_review_done =
False, i.e. it was never asked to check its own work.
"""

import textwrap

import pytest
from sqlalchemy import text

from src.core.database import DatabaseManager, Phase


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "sr.db"))
    manager.create_tables()
    return manager


# ── defect 1: the SDK loader dropped the key ────────────────────────


def _write_phase_yaml(dir_path, filename, body):
    (dir_path / filename).write_text(textwrap.dedent(body))


def test_sdk_yaml_loader_reads_self_review(tmp_path):
    """The loader must carry `self_review:` from YAML onto the Phase."""
    from src.sdk.client import HephaestusSDK

    _write_phase_yaml(
        tmp_path,
        "05_development.yaml",
        """
        self_review:
          enabled: true
        description: implement things
        Done_Definitions:
          - done
        """,
    )

    sdk = HephaestusSDK.__new__(HephaestusSDK)
    sdk.phases_dir = str(tmp_path)
    sdk.phases_map = {}
    sdk._load_phases_from_yaml()

    phase = sdk.phases_map[5]
    assert phase.self_review == {"enabled": True}, (
        "the SDK loader dropped self_review, so the gate would never fire "
        "for any phase it seeded"
    )


def test_sdk_yaml_loader_leaves_self_review_unset_when_absent(tmp_path):
    """No key means None -- not a truthy default that would turn the gate
    on for phases that never asked for it."""
    from src.sdk.client import HephaestusSDK

    _write_phase_yaml(
        tmp_path,
        "03_design.yaml",
        """
        description: design things
        Done_Definitions:
          - done
        """,
    )

    sdk = HephaestusSDK.__new__(HephaestusSDK)
    sdk.phases_dir = str(tmp_path)
    sdk.phases_map = {}
    sdk._load_phases_from_yaml()

    assert sdk.phases_map[3].self_review is None


def test_sdk_yaml_loader_ignores_a_malformed_self_review(tmp_path):
    """Only a dict counts. A scalar would be truthy, and the gate calls
    .get() on it -- an AttributeError inside task completion."""
    from src.sdk.client import HephaestusSDK

    _write_phase_yaml(
        tmp_path,
        "07_qa.yaml",
        """
        self_review: yes-please
        description: qa things
        Done_Definitions:
          - done
        """,
    )

    sdk = HephaestusSDK.__new__(HephaestusSDK)
    sdk.phases_dir = str(tmp_path)
    sdk.phases_map = {}
    sdk._load_phases_from_yaml()

    assert sdk.phases_map[7].self_review is None


# ── defect 3: the HTTP registration step dropped the key too ────────


def test_register_workflow_definitions_preserves_self_review(monkeypatch):
    """A THIRD, later-discovered instance of the exact same defect class:
    _register_workflow_definitions flattens each SDK Phase into a plain
    dict to POST to /api/workflow-definitions, and that flattening had no
    self_review line at all (unlike the analogous ones for outputs,
    next_steps, cli_tool, etc.) -- so even though the YAML loader (defect
    1, above) correctly parsed self_review onto the Phase object, it
    never survived this HTTP registration step. phase_manager.py's
    Phase(...) insert then read self_review from the resulting DB
    workflow-definition row and got None for every phase of every
    per-feature workflow launch, not just development."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from src.sdk.client import HephaestusSDK
    from src.sdk.models import Phase, WorkflowDefinition

    sdk = HephaestusSDK.__new__(HephaestusSDK)
    sdk.config = SimpleNamespace(mcp_host="localhost", mcp_port=8300)
    sdk.definitions = {
        "autopilot": WorkflowDefinition(
            id="autopilot",
            name="Autopilot",
            phases=[
                Phase(
                    id=5, name="development", description="d",
                    done_definitions=["done"], working_directory=".",
                    self_review={"enabled": True},
                )
            ],
        )
    }

    captured = {}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return MagicMock(raise_for_status=lambda: None)

    monkeypatch.setattr("src.sdk.client.requests.post", fake_post)

    sdk._register_workflow_definitions()

    phase_dict = captured["payload"]["phases_config"][0]
    assert phase_dict.get("self_review") == {"enabled": True}, (
        "self_review was dropped by the HTTP registration payload, so the "
        "gate would never fire for any per-feature workflow launch"
    )


# ── defect 2: the repair must be recurring, not one-shot ────────────


def test_backfill_is_not_registered_as_a_one_shot_migration():
    """If this is ever added to SCHEMA_MIGRATIONS it becomes one-shot
    again -- recorded in schema_migrations and skipped forever -- and
    stops being able to heal drift that appears later."""
    from src.core.schema_migrations import SCHEMA_MIGRATIONS

    ids = {mid for mid, _fn in SCHEMA_MIGRATIONS}
    assert not any("backfill_self_review" in i for i in ids), (
        "backfill_self_review_defaults must stay OUT of the one-shot "
        "registry so it runs on every startup"
    )


def test_backfill_repairs_a_development_row_with_null_self_review(db):
    from src.core.schema_migrations import backfill_self_review_defaults

    with db.session_scope() as s:
        s.add(
            Phase(
                id="p-null",
                workflow_id="wf-1",
                order=5,
                name="development",
                description="d",
                done_definitions=["x"],
                self_review=None,
            )
        )

    backfill_self_review_defaults(db.engine)

    with db.session_scope() as s:
        assert s.query(Phase).filter_by(id="p-null").first().self_review == {
            "enabled": True
        }


def test_backfill_repairs_the_json_null_literal_too(db):
    """A column written as the four-byte string 'null' is not SQL NULL, so
    an `IS NULL`-only predicate skips it and the phase stays silently
    ungated."""
    from src.core.schema_migrations import backfill_self_review_defaults

    with db.session_scope() as s:
        s.add(
            Phase(
                id="p-jsonnull",
                workflow_id="wf-1",
                order=5,
                name="development",
                description="d",
                done_definitions=["x"],
            )
        )
    with db.engine.connect() as conn:
        conn.execute(
            text("UPDATE phases SET self_review = 'null' WHERE id = 'p-jsonnull'")
        )
        conn.commit()

    backfill_self_review_defaults(db.engine)

    with db.session_scope() as s:
        assert s.query(Phase).filter_by(id="p-jsonnull").first().self_review == {
            "enabled": True
        }


def test_backfill_does_not_touch_other_phases(db):
    """Scoped to development; it must not switch a gate on for a phase
    that never configured one."""
    from src.core.schema_migrations import backfill_self_review_defaults

    with db.session_scope() as s:
        s.add(
            Phase(
                id="p-other",
                workflow_id="wf-1",
                order=3,
                name="architecture_design",
                description="d",
                done_definitions=["x"],
                self_review=None,
            )
        )

    backfill_self_review_defaults(db.engine)

    with db.session_scope() as s:
        assert s.query(Phase).filter_by(id="p-other").first().self_review is None


def test_create_tables_runs_the_backfill_every_time(db):
    """The whole point: repair happens on ordinary startup, not only on a
    first-ever migration run. Simulates drift appearing AFTER the one-shot
    migration was already recorded."""
    with db.session_scope() as s:
        s.add(
            Phase(
                id="p-drift",
                workflow_id="wf-2",
                order=5,
                name="development",
                description="d",
                done_definitions=["x"],
                self_review=None,
            )
        )

    # A later startup against the same (already-migrated) database.
    db.create_tables()

    with db.session_scope() as s:
        assert s.query(Phase).filter_by(id="p-drift").first().self_review == {
            "enabled": True
        }, "drift introduced after the one-shot migration was never repaired"
