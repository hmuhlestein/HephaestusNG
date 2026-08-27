"""find_speckit_features / select_speckit_feature / check_readiness — REQ-01/02/11/12/18."""

from pathlib import Path

import pytest

from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo
from src.core.speckit_detection import (
    AmbiguousSpecKitFeatureError,
    NoSpecKitFeatureError,
    ReadinessIssue,
    SpecKitFeature,
    check_readiness,
    find_speckit_features,
    select_speckit_feature,
)


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_feature(repo_path, number, slug, plan=False, tasks=False, needs_clarification=None):
    feature_dir = repo_path / "specs" / f"{number}-{slug}"
    feature_dir.mkdir(parents=True)
    spec_text = "# Spec\n"
    if needs_clarification:
        for marker in needs_clarification:
            spec_text += f"[NEEDS CLARIFICATION: {marker}]\n"
    (feature_dir / "spec.md").write_text(spec_text, encoding="utf-8")
    if plan:
        (feature_dir / "plan.md").write_text("# Plan\n", encoding="utf-8")
    if tasks:
        (feature_dir / "tasks.md").write_text("# Tasks\n", encoding="utf-8")
    return feature_dir


def _seed_single_repo_project(db_manager, tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(repo_path)))
        session.add(ProjectRepo(id="repo-1", project_id="proj-1", label="main", path=str(repo_path), is_primary=True))
    return repo_path


def _stub_feature(number, slug, repo_id, repo_label, has_plan=False, has_tasks=False):
    return SpecKitFeature(
        dir_path=Path(f"specs/{number}-{slug}"),
        number=number,
        slug=slug,
        repo_id=repo_id,
        repo_label=repo_label,
        has_plan=has_plan,
        has_tasks=has_tasks,
    )


def _seed_multi_repo_project(db_manager, tmp_path):
    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-mr", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-backend", project_id="proj-mr", label="backend", path=str(backend), is_primary=True))
        session.add(ProjectRepo(id="repo-frontend", project_id="proj-mr", label="frontend", path=str(frontend), is_primary=False))
    return backend, frontend


# --- find_speckit_features -------------------------------------------------


def test_find_speckit_features_zero_repos_with_specs(db_manager, tmp_path):
    _seed_single_repo_project(db_manager, tmp_path)
    with db_manager.session_scope() as session:
        assert find_speckit_features(session, "proj-1") == []


def test_find_speckit_features_one_repo_one_feature(db_manager, tmp_path):
    repo_path = _seed_single_repo_project(db_manager, tmp_path)
    _make_feature(repo_path, "001", "checkout-flow", plan=True)
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-1")
    assert len(features) == 1
    assert features[0].dir_name == "001-checkout-flow"
    assert features[0].number == "001"
    assert features[0].slug == "checkout-flow"
    assert features[0].repo_id == "repo-1"
    assert features[0].has_plan is True
    assert features[0].has_tasks is False


def test_find_speckit_features_one_repo_two_features(db_manager, tmp_path):
    repo_path = _seed_single_repo_project(db_manager, tmp_path)
    _make_feature(repo_path, "001", "checkout-flow")
    _make_feature(repo_path, "002", "payments")
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-1")
    assert [f.number for f in features] == ["001", "002"]


def test_find_speckit_features_two_repos_same_number(db_manager, tmp_path):
    backend, frontend = _seed_multi_repo_project(db_manager, tmp_path)
    _make_feature(backend, "001", "checkout-flow")
    _make_feature(frontend, "001", "checkout-flow")
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-mr")
    assert len(features) == 2
    assert {f.repo_id for f in features} == {"repo-backend", "repo-frontend"}


def test_find_speckit_features_skips_non_speckit_dir_names(db_manager, tmp_path):
    repo_path = _seed_single_repo_project(db_manager, tmp_path)
    non_speckit = repo_path / "specs" / "not-numbered"
    non_speckit.mkdir(parents=True)
    (non_speckit / "spec.md").write_text("# Spec\n", encoding="utf-8")
    _make_feature(repo_path, "001", "checkout-flow")
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-1")
    assert len(features) == 1
    assert features[0].dir_name == "001-checkout-flow"


def test_find_speckit_features_isolates_bad_repo(db_manager, tmp_path):
    backend, frontend = _seed_multi_repo_project(db_manager, tmp_path)
    _make_feature(frontend, "001", "payments")
    with db_manager.session_scope() as session:
        session.query(ProjectRepo).filter_by(id="repo-backend").update({"path": str(tmp_path / "does-not-exist")})
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-mr")
    assert len(features) == 1
    assert features[0].repo_id == "repo-frontend"


def test_find_speckit_features_glob_oserror_isolated_to_one_repo(db_manager, tmp_path, monkeypatch):
    backend, frontend = _seed_multi_repo_project(db_manager, tmp_path)
    _make_feature(backend, "001", "checkout-flow")
    _make_feature(frontend, "001", "payments")

    real_glob = Path.glob

    def _raising_glob(self, pattern):
        if self == backend / "specs":
            raise OSError("permission denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", _raising_glob)

    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-mr")
    assert len(features) == 1
    assert features[0].repo_id == "repo-frontend"


# --- select_speckit_feature --------------------------------------------------


def test_select_speckit_feature_implicit_single_match(db_manager, tmp_path):
    repo_path = _seed_single_repo_project(db_manager, tmp_path)
    _make_feature(repo_path, "001", "checkout-flow")
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-1")
    selected = select_speckit_feature(features, None, None)
    assert selected.dir_name == "001-checkout-flow"


def test_select_speckit_feature_implicit_multiple_raises_ambiguous():
    a = _stub_feature("001", "a", "r1", "main")
    b = _stub_feature("002", "b", "r1", "main")
    with pytest.raises(AmbiguousSpecKitFeatureError) as exc_info:
        select_speckit_feature([a, b], None, None)
    assert exc_info.value.candidates == [a, b]


def test_select_speckit_feature_empty_raises_no_feature():
    with pytest.raises(NoSpecKitFeatureError):
        select_speckit_feature([], None, None)


def test_select_speckit_feature_by_full_dir_name():
    a = _stub_feature("001", "checkout-flow", "r1", "main")
    b = _stub_feature("002", "payments", "r1", "main")
    selected = select_speckit_feature([a, b], "001-checkout-flow", None)
    assert selected is a


def test_select_speckit_feature_bare_number_matches_full_name_result():
    a = _stub_feature("001", "checkout-flow", "r1", "main")
    by_number = select_speckit_feature([a], "001", None)
    by_name = select_speckit_feature([a], "001-checkout-flow", None)
    assert by_number == by_name == a


def test_select_speckit_feature_unmatched_selector_raises_no_feature():
    a = _stub_feature("001", "checkout-flow", "r1", "main")
    with pytest.raises(NoSpecKitFeatureError) as exc_info:
        select_speckit_feature([a], "999-nope", None)
    assert exc_info.value.selector == "999-nope"


def test_select_speckit_feature_cross_repo_same_number_requires_repo():
    a = _stub_feature("001", "x", "r-backend", "backend")
    b = _stub_feature("001", "x", "r-frontend", "frontend")
    with pytest.raises(AmbiguousSpecKitFeatureError) as exc_info:
        select_speckit_feature([a, b], "001-x", None)
    assert exc_info.value.candidates == [a, b]


def test_select_speckit_feature_cross_repo_disambiguated_by_repo_label():
    a = _stub_feature("001", "x", "r-backend", "backend")
    b = _stub_feature("001", "x", "r-frontend", "frontend")
    selected = select_speckit_feature([a, b], "001-x", "frontend")
    assert selected is b


def test_select_speckit_feature_repo_label_matches_nothing_raises_no_feature():
    a = _stub_feature("001", "x", "r-backend", "backend")
    with pytest.raises(NoSpecKitFeatureError):
        select_speckit_feature([a], "001-x", "nonexistent-repo")


# --- check_readiness ---------------------------------------------------------


def test_check_readiness_reports_missing_plan_and_all_markers(db_manager, tmp_path):
    repo_path = _seed_single_repo_project(db_manager, tmp_path)
    _make_feature(repo_path, "001", "checkout-flow", needs_clarification=["scope?", "auth method?", "timeout?", "fourth marker?"])
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-1")
    issues = check_readiness(features[0])
    assert ReadinessIssue(kind="missing_file", detail="plan.md missing") in issues
    assert sum(1 for i in issues if i.kind == "needs_clarification") == 4


def test_check_readiness_empty_for_fully_ready_feature(db_manager, tmp_path):
    repo_path = _seed_single_repo_project(db_manager, tmp_path)
    _make_feature(repo_path, "001", "checkout-flow", plan=True)
    with db_manager.session_scope() as session:
        features = find_speckit_features(session, "proj-1")
    assert check_readiness(features[0]) == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
