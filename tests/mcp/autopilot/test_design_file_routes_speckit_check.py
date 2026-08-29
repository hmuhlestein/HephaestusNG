"""Tests for the REQ-01 project-id-keyed Spec Kit readiness route:
GET /api/autopilot/projects/{project_id}/speckit/check
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.database import AutopilotProject, ProjectRepo
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


def test_primary_repo_feature_readiness_round_trip_via_null_repo_label(db_manager, tmp_path):
    """Regression for the adversarial-review BLOCKER on commit 2edde877:
    /speckit/features nulls out repoLabel for the PRIMARY repo's own
    features (f2386caf), and the frontend echoes that null straight back
    as `repo_label` (dropped from the query string entirely by axios). A
    naive filter (`f.repo_label == repo_label`) matched a real label
    against `None` and 404'd on every primary-repo feature. `number` alone
    (no `repo_label` in the query) must resolve to the primary repo's
    feature."""
    base_dir = tmp_path / "primary"
    (base_dir / "specs" / "001-checkout-flow").mkdir(parents=True)
    (base_dir / "specs" / "001-checkout-flow" / "spec.md").write_text("spec", encoding="utf-8")
    (base_dir / "specs" / "001-checkout-flow" / "plan.md").write_text("plan", encoding="utf-8")

    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="proj-1", base_dir=str(base_dir)))
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="my-repo", path=str(base_dir), is_primary=True))

    client = client_for(db_manager)

    features_resp = client.get("/api/autopilot/projects/proj-1/speckit/features")
    assert features_resp.status_code == 200
    assert features_resp.json()[0]["repoLabel"] is None

    # Simulate the frontend round-trip: repoLabel=None means "omit the
    # repo_label query param entirely" (api.ts's `?? undefined` coalescing).
    check_resp = client.get("/api/autopilot/projects/proj-1/speckit/check", params={"number": "001"})
    assert check_resp.status_code == 200
    assert check_resp.json()["features"][0]["number"] == "001"


def test_secondary_repo_feature_still_requires_its_real_repo_label(db_manager, tmp_path):
    """Other half of the same fix: a genuinely non-primary repo's feature
    is NOT reachable by omitting repo_label (that resolves to the primary
    repo only) -- its real label is still required."""
    primary_dir = tmp_path / "primary"
    secondary_dir = tmp_path / "secondary"
    primary_dir.mkdir()
    (secondary_dir / "specs" / "002-login-page").mkdir(parents=True)
    (secondary_dir / "specs" / "002-login-page" / "spec.md").write_text("spec", encoding="utf-8")

    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="proj-1", base_dir=str(primary_dir)))
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="primary-repo", path=str(primary_dir), is_primary=True))
        session.add(ProjectRepo(id="repo-2", project_id="proj-1", label="secondary-repo", path=str(secondary_dir)))

    client = client_for(db_manager)

    # Omitting repo_label resolves to the PRIMARY repo -- the secondary
    # repo's feature must not be found this way.
    no_label_resp = client.get("/api/autopilot/projects/proj-1/speckit/check", params={"number": "002"})
    assert no_label_resp.status_code == 404

    # Its real label reaches it correctly.
    with_label_resp = client.get(
        "/api/autopilot/projects/proj-1/speckit/check",
        params={"number": "002", "repo_label": "secondary-repo"},
    )
    assert with_label_resp.status_code == 200
    assert with_label_resp.json()["features"][0]["number"] == "002"


def test_same_number_in_two_repos_omitted_repo_label_resolves_only_primary(db_manager, tmp_path):
    """Re-review property: omitting repo_label must never wildcard-match
    across repos. Both the primary and a secondary repo have a feature
    numbered 001 -- omitting repo_label must resolve to the PRIMARY repo's
    001 only, never merge/ambiguously pick either."""
    primary_dir = tmp_path / "primary"
    secondary_dir = tmp_path / "secondary"
    (primary_dir / "specs" / "001-primary-feature").mkdir(parents=True)
    (primary_dir / "specs" / "001-primary-feature" / "spec.md").write_text("spec", encoding="utf-8")
    (secondary_dir / "specs" / "001-secondary-feature").mkdir(parents=True)
    (secondary_dir / "specs" / "001-secondary-feature" / "spec.md").write_text("spec", encoding="utf-8")

    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="proj-1", base_dir=str(primary_dir)))
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="primary-repo", path=str(primary_dir), is_primary=True))
        session.add(ProjectRepo(id="repo-2", project_id="proj-1", label="secondary-repo", path=str(secondary_dir)))

    client = client_for(db_manager)

    resp = client.get("/api/autopilot/projects/proj-1/speckit/check", params={"number": "001"})
    assert resp.status_code == 200
    features = resp.json()["features"]
    assert len(features) == 1
    assert features[0]["slug"] == "primary-feature"

    # The secondary repo's same-numbered feature is only reachable with its
    # real label, never via the omitted-repo_label path above.
    secondary_resp = client.get(
        "/api/autopilot/projects/proj-1/speckit/check",
        params={"number": "001", "repo_label": "secondary-repo"},
    )
    assert secondary_resp.status_code == 200
    assert secondary_resp.json()["features"][0]["slug"] == "secondary-feature"


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
