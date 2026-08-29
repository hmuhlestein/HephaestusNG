"""Tests for the REQ-01 project-id-keyed Spec Kit readiness route:
GET /api/autopilot/projects/{project_id}/speckit/check
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import AutopilotProject
from src.mcp.autopilot import router


def client_for(db_manager):
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _make_project(db_manager, tmp_path, project_id="proj-1"):
    base_dir = tmp_path / project_id
    (base_dir / "specs").mkdir(parents=True)
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id=project_id, name=project_id, base_dir=str(base_dir)))
    return base_dir


def _write_feature(base_dir, number, slug, spec_body="No markers here.", with_plan=True):
    feature_dir = base_dir / "specs" / f"{number}-{slug}"
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text(spec_body, encoding="utf-8")
    if with_plan:
        (feature_dir / "plan.md").write_text("plan", encoding="utf-8")


def test_unknown_project_returns_404(db_manager, tmp_path):
    client = client_for(db_manager)
    resp = client.get("/api/autopilot/projects/does-not-exist/speckit/check")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Project not found"


def test_number_given_but_no_match_returns_404(db_manager, tmp_path):
    base_dir = _make_project(db_manager, tmp_path)
    _write_feature(base_dir, "001", "checkout-flow")

    client = client_for(db_manager)
    resp = client.get("/api/autopilot/projects/proj-1/speckit/check", params={"number": "999"})
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Spec Kit feature not found"


def test_happy_path_reports_clarification_and_missing_files(db_manager, tmp_path):
    base_dir = _make_project(db_manager, tmp_path)
    _write_feature(
        base_dir,
        "001",
        "checkout-flow",
        spec_body="Intro.\n[NEEDS CLARIFICATION: What auth scheme?]\nMore text.",
        with_plan=False,
    )

    client = client_for(db_manager)
    resp = client.get("/api/autopilot/projects/proj-1/speckit/check", params={"number": "001"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["features"]) == 1
    feature = data["features"][0]
    assert feature["number"] == "001"
    assert feature["slug"] == "checkout-flow"
    assert feature["needs_clarification"] == ["What auth scheme?"]
    assert "plan.md" in feature["missing_files"]


def test_happy_path_fully_ready_feature_returns_empty_lists(db_manager, tmp_path):
    base_dir = _make_project(db_manager, tmp_path)
    feature_dir = base_dir / "specs" / "002-login-page"
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text("All clear.", encoding="utf-8")
    (feature_dir / "plan.md").write_text("plan", encoding="utf-8")
    (feature_dir / "tasks.md").write_text("tasks", encoding="utf-8")

    client = client_for(db_manager)
    resp = client.get("/api/autopilot/projects/proj-1/speckit/check", params={"number": "002"})
    assert resp.status_code == 200
    feature = resp.json()["features"][0]
    assert feature["needs_clarification"] == []
    assert feature["missing_files"] == []


def test_number_omitted_returns_report_for_every_feature(db_manager, tmp_path):
    base_dir = _make_project(db_manager, tmp_path)
    _write_feature(base_dir, "001", "checkout-flow", with_plan=False)
    _write_feature(base_dir, "002", "login-page", with_plan=True)

    client = client_for(db_manager)
    resp = client.get("/api/autopilot/projects/proj-1/speckit/check")
    assert resp.status_code == 200
    numbers = {f["number"] for f in resp.json()["features"]}
    assert numbers == {"001", "002"}


def test_route_never_mutates_project(db_manager, tmp_path):
    base_dir = _make_project(db_manager, tmp_path)
    _write_feature(base_dir, "001", "checkout-flow")

    client = client_for(db_manager)
    resp = client.get("/api/autopilot/projects/proj-1/speckit/check", params={"number": "001"})
    assert resp.status_code == 200

    with db_manager.session_scope() as session:
        proj = session.query(AutopilotProject).filter_by(id="proj-1").first()
        assert proj is not None
        assert proj.base_dir == str(base_dir)
