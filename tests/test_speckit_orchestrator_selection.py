"""Tests for src/autopilot/orchestrator/speckit.py (REQ-01..REQ-15, REQ-21..23)."""

from pathlib import Path

import pytest

from src.autopilot.orchestrator.speckit import (
    SpecKitFeature,
    SpecKitSelectionError,
    _scan_one_repo,
    check_feature_readiness,
    discover_speckit_features,
    discover_speckit_features_unregistered,
    resolve_feature_selection,
    speckit_feature_dir_for_path,
)


def _make_feature_dir(tmp_path: Path, name: str, files: dict) -> Path:
    d = tmp_path / "specs" / name
    d.mkdir(parents=True)
    for fname, content in files.items():
        (d / fname).write_text(content)
    return d


def test_single_feature_no_design_md_resolves_without_error(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec", "plan.md": "# plan"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    assert len(features) == 1
    resolved = resolve_feature_selection(features, feature_arg=None, repo_arg=None, design_md_present=False)
    assert resolved.number == "001"


def test_single_feature_with_design_md_raises_both_inputs_present(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    with pytest.raises(SpecKitSelectionError) as exc:
        resolve_feature_selection(features, feature_arg=None, repo_arg=None, design_md_present=True)
    assert exc.value.code == "BOTH_INPUTS_PRESENT"
    labels = {c.label() for c in exc.value.candidates}
    assert labels == {"001-x", "spec.md"}


def test_feature_arg_resolves_even_with_design_md_present(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    resolved = resolve_feature_selection(features, feature_arg="001-x", repo_arg=None, design_md_present=True)
    assert resolved.slug == "x"


def test_discover_sorts_numerically_not_lexicographically(tmp_path):
    _make_feature_dir(tmp_path, "10-bar", {"spec.md": "# spec"})
    _make_feature_dir(tmp_path, "2-foo", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    assert [f.number for f in features] == ["2", "10"]


def test_two_features_no_selector_raises_multiple_features(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(tmp_path, "002-y", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    with pytest.raises(SpecKitSelectionError) as exc:
        resolve_feature_selection(features, feature_arg=None, repo_arg=None, design_md_present=False)
    assert exc.value.code == "MULTIPLE_FEATURES"
    assert len(exc.value.candidates) == 2


def test_bare_numeric_feature_arg_matches_exact_prefix_only(tmp_path):
    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(tmp_path, "0012-y", {"spec.md": "# spec"})
    features = discover_speckit_features_unregistered(str(tmp_path))
    resolved = resolve_feature_selection(features, feature_arg="001", repo_arg=None, design_md_present=False)
    assert resolved.slug == "x"


def test_feature_arg_matching_two_repos_without_repo_raises_ambiguous_repo():
    backend = SpecKitFeature(
        dir_path=Path("/repo-a/specs/001-x"), number="001", slug="x",
        repo_id="repo-a", repo_label="backend", spec_path=Path("/repo-a/specs/001-x/spec.md"),
    )
    frontend = SpecKitFeature(
        dir_path=Path("/repo-b/specs/001-x"), number="001", slug="x",
        repo_id="repo-b", repo_label="frontend", spec_path=Path("/repo-b/specs/001-x/spec.md"),
    )
    with pytest.raises(SpecKitSelectionError) as exc:
        resolve_feature_selection([backend, frontend], feature_arg="001-x", repo_arg=None, design_md_present=False)
    assert exc.value.code == "AMBIGUOUS_REPO"
    assert len(exc.value.candidates) == 2


def test_malformed_specs_dir_never_counted_and_never_raises(tmp_path):
    (tmp_path / "specs" / "not-numbered").mkdir(parents=True)
    (tmp_path / "specs" / "001-no-spec").mkdir(parents=True)
    features = _scan_one_repo(tmp_path / "specs", None, None)
    assert features == []


def test_discover_speckit_features_scans_each_registered_repo(tmp_path, monkeypatch):
    """discover_speckit_features's registered-repo branch (REQ-02) -- the
    unregistered-fallback branch is covered elsewhere via
    discover_speckit_features_unregistered."""
    from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo

    backend_dir = tmp_path / "backend"
    frontend_dir = tmp_path / "frontend"
    _make_feature_dir(backend_dir, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(frontend_dir, "002-y", {"spec.md": "# spec"})

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-a", project_id="proj-1", label="backend", path=str(backend_dir), is_primary=True))
        session.add(ProjectRepo(id="repo-b", project_id="proj-1", label="frontend", path=str(frontend_dir)))
        session.flush()

        features = discover_speckit_features(session, "proj-1", str(tmp_path))

    assert {(f.repo_label, f.number) for f in features} == {("backend", "001"), ("frontend", "002")}


def test_discover_speckit_features_also_scans_workspace_root_for_a_multi_repo_project(tmp_path, monkeypatch):
    """Regression, observed live: project "ParentChat" (workspace root
    /parent, child repos /parent/back-end and /parent/front-end) had
    `specify init` run at the workspace root, putting specs/ there rather
    than inside either child repo -- auto-scan never found it, since
    neither registered repo's own specs/ nor (pre-fix) the workspace root
    were ever checked once ProjectRepo rows existed. The workspace-root
    feature must surface with repo_id=None/repo_label=None (it belongs to
    no single child repo), alongside each repo's own features."""
    from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo

    backend_dir = tmp_path / "back-end"
    frontend_dir = tmp_path / "front-end"
    _make_feature_dir(backend_dir, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(tmp_path, "002-conversation-history", {"spec.md": "# spec"})

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-a", project_id="proj-1", label="backend", path=str(backend_dir), is_primary=True))
        session.add(ProjectRepo(id="repo-b", project_id="proj-1", label="frontend", path=str(frontend_dir)))
        session.flush()

        features = discover_speckit_features(session, "proj-1", str(tmp_path))

    assert {(f.repo_label, f.number) for f in features} == {("backend", "001"), (None, "002")}


def test_discover_speckit_features_skips_workspace_root_scan_when_it_equals_the_primary_repo(tmp_path, monkeypatch):
    """The traditional single-repo case (ProjectRepo.path == base_dir)
    must not double-count that repo's own features via a second,
    redundant workspace-root scan."""
    from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo

    _make_feature_dir(tmp_path, "001-x", {"spec.md": "# spec"})

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-1", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-a", project_id="proj-1", label="main", path=str(tmp_path), is_primary=True))
        session.flush()

        features = discover_speckit_features(session, "proj-1", str(tmp_path))

    assert len(features) == 1
    assert features[0].repo_label == "main"


def test_speckit_feature_dir_for_path_recognizes_spec_md(tmp_path):
    d = tmp_path / "specs" / "001-x"
    d.mkdir(parents=True)
    spec_path = d / "spec.md"
    spec_path.write_text("# spec")

    assert speckit_feature_dir_for_path(spec_path) == d


def test_speckit_feature_dir_for_path_rejects_non_spec_md_filename(tmp_path):
    d = tmp_path / "specs" / "001-x"
    d.mkdir(parents=True)
    plan_path = d / "plan.md"
    plan_path.write_text("# plan")

    assert speckit_feature_dir_for_path(plan_path) is None


def test_speckit_feature_dir_for_path_rejects_non_speckit_parent_dir(tmp_path):
    spec_path = tmp_path / "docs" / "spec.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# spec")

    assert speckit_feature_dir_for_path(spec_path) is None


def test_check_feature_readiness_reports_missing_plan_and_markers(tmp_path):
    d = _make_feature_dir(
        tmp_path, "001-x",
        {"spec.md": "Some text [NEEDS CLARIFICATION: what auth method?] more text"},
    )
    feature = SpecKitFeature(
        dir_path=d, number="001", slug="x", repo_id=None, repo_label=None, spec_path=d / "spec.md",
    )
    report = check_feature_readiness(feature)
    assert report.missing_files == ["plan.md", "tasks.md"]
    assert report.needs_clarification == ["what auth method?"]


def test_check_feature_readiness_swallows_unreadable_spec_without_raising(tmp_path):
    """REQ-15/NFR-07: readiness is best-effort and must never raise, even
    when spec.md is unreadable (deleted mid-check, not valid UTF-8, etc.)
    -- an earlier, now-unified duplicate implementation
    (src/core/speckit_detection.py) raised OSError here instead; this
    module's swallow-and-log behavior is the one actually wired into the
    /speckit/check route, which does not catch OSError from this call."""
    d = _make_feature_dir(tmp_path, "001-x", {"spec.md": "placeholder"})
    (d / "spec.md").write_bytes(b"\xff\xfe not valid utf-8")
    feature = SpecKitFeature(
        dir_path=d, number="001", slug="x", repo_id=None, repo_label=None, spec_path=d / "spec.md",
    )
    report = check_feature_readiness(feature)  # must not raise
    assert report.needs_clarification == []
    assert report.missing_files == ["plan.md", "tasks.md"]


def test_discover_speckit_features_falls_back_to_workspace_root_with_genuinely_zero_repos(tmp_path, monkeypatch):
    """A registered project with literally zero ProjectRepo rows
    (pre-migration edge case) must still find a feature sitting directly
    at the project's own base_dir."""
    from src.core.database import AutopilotProject, DatabaseManager

    _make_feature_dir(tmp_path, "001-checkout-flow", {"spec.md": "# spec"})

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-none", name="p", base_dir=str(tmp_path)))
        session.flush()

        features = discover_speckit_features(session, "proj-none", str(tmp_path))

    assert len(features) == 1
    assert features[0].repo_id is None
    assert features[0].dir_name == "001-checkout-flow"


def test_discover_speckit_features_isolates_a_repo_whose_path_does_not_exist(tmp_path, monkeypatch):
    """One repo's registered path being gone (moved/deleted on disk, DB
    row stale) must not prevent discovery of every other repo's features."""
    from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo

    frontend_dir = tmp_path / "frontend"
    _make_feature_dir(frontend_dir, "001-payments", {"spec.md": "# spec"})

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-mr", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-backend", project_id="proj-mr", label="backend", path=str(tmp_path / "does-not-exist"), is_primary=True))
        session.add(ProjectRepo(id="repo-frontend", project_id="proj-mr", label="frontend", path=str(frontend_dir)))
        session.flush()

        features = discover_speckit_features(session, "proj-mr", str(tmp_path))

    assert len(features) == 1
    assert features[0].repo_id == "repo-frontend"


def test_discover_speckit_features_iterdir_oserror_isolated_to_one_repo(tmp_path, monkeypatch):
    """A filesystem error scanning ONE repo's specs/ dir (permission
    denied, transient mount issue) must not abort discovery for sibling
    repos -- error isolation is per-location, not all-or-nothing."""
    from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo

    backend_dir = tmp_path / "backend"
    frontend_dir = tmp_path / "frontend"
    _make_feature_dir(backend_dir, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(frontend_dir, "001-y", {"spec.md": "# spec"})

    real_iterdir = Path.iterdir
    backend_specs = backend_dir / "specs"

    def _raising_iterdir(self):
        if self == backend_specs:
            raise OSError("permission denied")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _raising_iterdir)

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-mr", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-backend", project_id="proj-mr", label="backend", path=str(backend_dir), is_primary=True))
        session.add(ProjectRepo(id="repo-frontend", project_id="proj-mr", label="frontend", path=str(frontend_dir)))
        session.flush()

        features = discover_speckit_features(session, "proj-mr", str(tmp_path))

    assert len(features) == 1
    assert features[0].repo_id == "repo-frontend"


def test_discover_speckit_features_mid_iteration_failure_discards_partial_repo_results(tmp_path, monkeypatch):
    """A repo with 2+ features whose directory listing raises AFTER
    yielding the first one must contribute ZERO features, not just the
    first -- true all-or-nothing isolation per repo, not a partial leak."""
    from src.core.database import AutopilotProject, DatabaseManager, ProjectRepo

    backend_dir = tmp_path / "backend"
    frontend_dir = tmp_path / "frontend"
    _make_feature_dir(backend_dir, "001-x", {"spec.md": "# spec"})
    _make_feature_dir(backend_dir, "002-y", {"spec.md": "# spec"})
    _make_feature_dir(frontend_dir, "001-z", {"spec.md": "# spec"})

    real_iterdir = Path.iterdir
    backend_specs = backend_dir / "specs"

    def _flaky_iterdir(self):
        if self == backend_specs:
            def _gen():
                entries = list(real_iterdir(self))
                yield entries[0]
                raise OSError("disk error mid-scan")
            return _gen()
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _flaky_iterdir)

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    with db_manager.session_scope() as session:
        session.add(AutopilotProject(id="proj-mr", name="p", base_dir=str(tmp_path)))
        session.add(ProjectRepo(id="repo-backend", project_id="proj-mr", label="backend", path=str(backend_dir), is_primary=True))
        session.add(ProjectRepo(id="repo-frontend", project_id="proj-mr", label="frontend", path=str(frontend_dir)))
        session.flush()

        features = discover_speckit_features(session, "proj-mr", str(tmp_path))

    assert len(features) == 1
    assert features[0].repo_id == "repo-frontend"
