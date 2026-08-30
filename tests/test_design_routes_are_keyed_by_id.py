"""Regression: a Spec Kit design was unreachable through every endpoint that
addresses a design by name.

The autoscan stores a synthetic relative path as the filename --
"speckit/<repo>/<number>-<slug>.md" (orchestrator/queue.py) -- because
(project_id, filename) is the dedup key. It was also, wrongly, the address:
these routes declared a single-segment {filename}, and Starlette decodes %2F
before matching, so even an encodeURIComponent'd filename never matched.
Status, content, archive, unarchive and delete all 404'd. Observed live as a
design whose Phase 0 was genuinely running showing no phase row in the UI.

Every design endpoint is keyed by design_id now: it is the primary key, it is
always present, and a directory-sourced design has no filename at all (NFR-02)
so it could never have been addressed by one.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import (
    AutopilotDesign,
    AutopilotProject,
    DatabaseManager,
    directory_spec_key,
)

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
    bare = project_dir / ".hephaestus" / "specs" / "02-payments.md"
    bare.parent.mkdir(parents=True)
    bare.write_text("# Payments\n")

    with db.session_scope() as session:
        session.add(
            AutopilotProject(id="proj-1", name="ParentChat", base_dir=str(project_dir))
        )
        # Spec Kit: a slash-bearing synthetic filename, real file via file_path.
        session.add(
            AutopilotDesign(
                id="des-speckit",
                project_id="proj-1",
                filename=SPECKIT_FILENAME,
                name="001-conversation-history",
                ordinal=1,
                status="decomposing",
                file_path=str(spec),
            )
        )
        # The plain case: a bare filename resolved under the queue dir.
        session.add(
            AutopilotDesign(
                id="des-bare",
                project_id="proj-1",
                filename="02-payments.md",
                name="payments",
                ordinal=2,
                status="pending",
            )
        )
        # Directory-sourced: no filename at all.
        session.add(
            AutopilotDesign(
                id="des-dir",
                project_id="proj-1",
                # No filename to default spec_key from -- a directory-backed
                # design has to name its own source.
                spec_key=directory_spec_key("001-conversation-history"),
                filename=None,
                source_dir=str(spec.parent),
                name="001-conversation-history",
                ordinal=3,
                status="pending",
            )
        )

    from src.mcp.autopilot import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app, headers={"X-Agent-ID": "system"})


@pytest.mark.parametrize("design_id", ["des-speckit", "des-bare"])
@pytest.mark.parametrize("verb", ["content", "status"])
def test_a_design_is_reachable_by_id(client, design_id, verb):
    resp = client.get(f"/api/autopilot/projects/proj-1/designs/{design_id}/{verb}")
    assert resp.status_code == 200, resp.text


def test_archive_round_trip_is_keyed_by_id(client):
    archived = client.post("/api/autopilot/projects/proj-1/designs/des-speckit/archive")
    assert archived.status_code == 200, archived.text
    unarchived = client.post("/api/autopilot/projects/proj-1/designs/des-speckit/unarchive")
    assert unarchived.status_code == 200, unarchived.text


def test_content_says_a_directory_sourced_design_has_no_single_file(client):
    """409, not 404: the design exists, it just has no one file to return --
    which is exactly the distinction filename addressing could not express."""
    resp = client.get("/api/autopilot/projects/proj-1/designs/des-dir/content")
    assert resp.status_code == 409, resp.text
    assert "directory-sourced" in resp.json()["detail"]


def test_status_still_works_for_a_directory_sourced_design(client):
    """Status reports workflow and task progress, which exists whether or not
    the design has a readable source document."""
    resp = client.get("/api/autopilot/projects/proj-1/designs/des-dir/status")
    assert resp.status_code == 200, resp.text


def test_an_unknown_id_is_a_404(client):
    resp = client.get("/api/autopilot/projects/proj-1/designs/des-nope/status")
    assert resp.status_code == 404, resp.text


def test_a_filename_is_no_longer_an_address(client):
    """It never worked for a Spec Kit design and cannot work for a
    directory-sourced one -- it is a dedup key, not a locator."""
    resp = client.get(
        f"/api/autopilot/projects/proj-1/designs/{SPECKIT_FILENAME}/status"
    )
    assert resp.status_code == 404, resp.text
