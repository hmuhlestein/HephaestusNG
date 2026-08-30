"""Regression: a Spec Kit design was unreachable through every endpoint that
addresses a design by filename.

The autoscan stores a synthetic relative path as the filename --
"speckit/<repo>/<number>-<slug>.md" (orchestrator/queue.py), which is also the
(project_id, filename) dedup key -- while these routes declared a
single-segment {filename}. Starlette decodes %2F before matching, so even an
encodeURIComponent'd filename (what the frontend sends) never matched: status,
content, archive, unarchive and delete all 404'd. Observed live as a design
whose Phase 0 was genuinely running showing no phase row in the UI.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import AutopilotDesign, AutopilotProject, DatabaseManager

SPECKIT_FILENAME = "speckit/_workspace/001-conversation-history.md"


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db = DatabaseManager(db_path)
    db.create_tables()

    project_dir = tmp_path / "project"
    spec = project_dir / "specs" / "001-conversation-history" / "spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("# Conversation history\n")
    # The single-segment case these routes were written for, in the queue dir
    # they resolve against when a design has no file_path of its own.
    bare = project_dir / ".hephaestus" / "specs" / "02-payments.md"
    bare.parent.mkdir(parents=True)
    bare.write_text("# Payments\n")

    with db.session_scope() as session:
        session.add(
            AutopilotProject(
                id="proj-1", name="ParentChat", base_dir=str(project_dir)
            )
        )
        session.add(
            AutopilotDesign(
                id="des-1",
                project_id="proj-1",
                filename=SPECKIT_FILENAME,
                name="001-conversation-history",
                ordinal=1,
                status="decomposing",
                file_path=str(spec),
            )
        )
        session.add(
            AutopilotDesign(
                id="des-2",
                project_id="proj-1",
                filename="02-payments.md",
                name="payments",
                ordinal=2,
                status="pending",
            )
        )

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, headers={"X-Agent-ID": "system"})


@pytest.mark.parametrize("suffix", ["/status", "/content"])
def test_speckit_design_is_reachable(client, suffix):
    resp = client.get(f"/api/autopilot/projects/proj-1/designs/{SPECKIT_FILENAME}{suffix}")
    assert resp.status_code == 200, resp.text


def test_percent_encoded_filename_also_matches(client):
    """What the frontend actually sends -- encodeURIComponent turns the
    separators into %2F, which Starlette decodes before routing."""
    encoded = SPECKIT_FILENAME.replace("/", "%2F")
    resp = client.get(f"/api/autopilot/projects/proj-1/designs/{encoded}/status")
    assert resp.status_code == 200, resp.text


def test_archive_round_trip(client):
    """archive/unarchive are keyed by design id (des-1), not filename -- a
    directory-sourced design has filename=NULL, so id is the one
    identifier every design row actually has."""
    archived = client.post("/api/autopilot/projects/proj-1/designs/des-1/archive")
    assert archived.status_code == 200, archived.text
    unarchived = client.post("/api/autopilot/projects/proj-1/designs/des-1/unarchive")
    assert unarchived.status_code == 200, unarchived.text


def test_a_bare_filename_still_works(client):
    """The single-segment case these routes were written for."""
    resp = client.get("/api/autopilot/projects/proj-1/designs/02-payments.md/status")
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "bad", ["../../../etc/passwd", "speckit/../../etc/passwd", "/etc/passwd"]
)
def test_traversal_is_still_refused(client, bad):
    """Allowing interior separators must not weaken what the old blanket
    check was actually there for."""
    resp = client.get(f"/api/autopilot/projects/proj-1/designs/{bad}/status")
    assert resp.status_code in (400, 404), resp.text
    assert resp.status_code != 200
