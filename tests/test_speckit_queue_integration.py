"""Spec-Kit-aware design-queue integration tests (REQ-03/04/05/06/07/11/12/13).

Covers scan_design_queue's directory detection + auto-scan gate, and
pick_next_design's source_dir reconstruction and auto-create-row write
site -- the pieces that must agree on the source_dir/file_path mutual
exclusivity invariant (NFR-02) for a Spec-Kit design to ever round-trip.
"""

import pytest


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    from src.core.database import DatabaseManager

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


def _make_project(db, project_id, base_dir, speckit_autoscan_enabled=False):
    from src.core.database import AutopilotProject

    with db.session_scope() as session:
        session.add(
            AutopilotProject(
                id=project_id,
                name=project_id,
                base_dir=str(base_dir),
                is_active=True,
                speckit_autoscan_enabled=speckit_autoscan_enabled,
            )
        )


def _make_repo(db, repo_id, project_id, label, path, is_primary=True):
    from src.core.database import ProjectRepo

    with db.session_scope() as session:
        session.add(
            ProjectRepo(
                id=repo_id,
                project_id=project_id,
                label=label,
                path=str(path),
                is_primary=is_primary,
            )
        )


def _make_feature_dir(repo_path, dir_name, has_plan=False):
    feature_dir = repo_path / "specs" / dir_name
    feature_dir.mkdir(parents=True)
    (feature_dir / "spec.md").write_text(f"spec for {dir_name}")
    if has_plan:
        (feature_dir / "plan.md").write_text(f"plan for {dir_name}")
    return feature_dir


class _NullLogger:
    def __getattr__(self, name):
        return lambda *a, **k: None


def test_detection_unconditional_but_not_queued_when_disabled(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue
    from src.core.database import get_db
    from src.core.speckit_detection import find_speckit_features

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _make_feature_dir(repo_path, "003-checkout-flow", has_plan=True)
    _make_project(queue_db, "proj-a", repo_path, speckit_autoscan_enabled=False)
    _make_repo(queue_db, "repo-a", "proj-a", "primary", repo_path)

    with get_db() as db:
        detected = find_speckit_features(db, "proj-a")
    assert len(detected) == 1  # detection is unconditional (REQ-03/NFR-05)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    for _ in range(3):  # "regardless of elapsed time" -- repeated scans
        designs = scan_design_queue(queue_dir, set(), project_id="proj-a")
        assert designs == []  # detected, never queued while disabled (REQ-12)


def test_enabled_withholds_until_plan_present(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "004-signup", has_plan=False)
    _make_project(queue_db, "proj-b", repo_path, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-b", "proj-b", "primary", repo_path)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    designs = scan_design_queue(queue_dir, set(), project_id="proj-b")
    assert designs == []  # enabled, but no plan.md yet (REQ-13)

    (feature_dir / "plan.md").write_text("now with a plan")
    designs = scan_design_queue(queue_dir, set(), project_id="proj-b")
    assert len(designs) == 1
    assert designs[0].source_dir == feature_dir
    assert designs[0].repo_id == "repo-b"


def test_self_heal_requeues_directory_sourced_design_with_no_real_progress(queue_db, tmp_path):
    """REQ-05: the existing [SELF-HEAL] re-queue check works unchanged for
    directory-sourced designs -- a design already in processed_hashes but
    whose features are all still pending (or don't exist) gets re-queued
    on the next scan, same as the file-sourced branch already does.
    """
    from src.autopilot.orchestrator.engine_client import directory_content_hash
    from src.autopilot.orchestrator.queue import scan_design_queue
    from src.core.database import AutopilotDesign, Feature

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "008-heal", has_plan=True)
    _make_project(queue_db, "proj-h", repo_path, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-h", "proj-h", "primary", repo_path)

    content_hash = directory_content_hash(feature_dir)
    with queue_db.session_scope() as session:
        session.add(
            AutopilotDesign(
                id="des-h",
                project_id="proj-h",
                filename=None,
                file_path=None,
                source_dir=str(feature_dir),
                repo_id="repo-h",
                name="008-heal",
                content_hash=content_hash,
                status="processing",
            )
        )
        session.add(
            Feature(
                id="feat-h",
                design_id="des-h",
                feature_key="only-feature",
                name="Only Feature",
                scope="scope",
                status="pending",  # not started -- no real progress yet
            )
        )

    processed_hashes = {content_hash}  # marked processed, but stuck
    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    designs = scan_design_queue(queue_dir, processed_hashes, project_id="proj-h")

    assert len(designs) == 1
    assert designs[0].source_dir == feature_dir
    assert content_hash not in processed_hashes  # self-heal discarded it


def test_self_heal_does_not_requeue_directory_sourced_design_with_real_progress(queue_db, tmp_path):
    """Mirror of the above: a feature that has actually started (status
    other than pending) means real progress happened -- must NOT be
    treated as stuck and re-queued.
    """
    from src.autopilot.orchestrator.engine_client import directory_content_hash
    from src.autopilot.orchestrator.queue import scan_design_queue
    from src.core.database import AutopilotDesign, Feature

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "009-progress", has_plan=True)
    _make_project(queue_db, "proj-i", repo_path, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-i", "proj-i", "primary", repo_path)

    content_hash = directory_content_hash(feature_dir)
    with queue_db.session_scope() as session:
        session.add(
            AutopilotDesign(
                id="des-i",
                project_id="proj-i",
                filename=None,
                file_path=None,
                source_dir=str(feature_dir),
                repo_id="repo-i",
                name="009-progress",
                content_hash=content_hash,
                status="active",
            )
        )
        session.add(
            Feature(
                id="feat-i",
                design_id="des-i",
                feature_key="only-feature",
                name="Only Feature",
                scope="scope",
                status="active",  # real progress
            )
        )

    processed_hashes = {content_hash}
    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    designs = scan_design_queue(queue_dir, processed_hashes, project_id="proj-i")

    assert designs == []
    assert content_hash in processed_hashes  # not disturbed


def test_multi_repo_scan_tags_correct_repo(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue

    backend = tmp_path / "backend"
    frontend = tmp_path / "frontend"
    backend.mkdir()
    frontend.mkdir()
    _make_feature_dir(backend, "001-x", has_plan=True)
    _make_feature_dir(frontend, "002-y", has_plan=True)
    _make_project(queue_db, "proj-c", backend, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-backend", "proj-c", "backend", backend, is_primary=True)
    _make_repo(queue_db, "repo-frontend", "proj-c", "frontend", frontend, is_primary=False)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    designs = scan_design_queue(queue_dir, set(), project_id="proj-c")
    assert len(designs) == 2
    by_repo = {d.repo_id for d in designs}
    assert by_repo == {"repo-backend", "repo-frontend"}


def test_regression_file_only_scan_unaffected(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import scan_design_queue

    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    (queue_dir / "design.md").write_text("hello")

    without_project = scan_design_queue(queue_dir, set())
    with_project_none = scan_design_queue(queue_dir, set(), project_id=None)
    assert [d.path for d in without_project] == [d.path for d in with_project_none]
    assert len(without_project) == 1
    assert without_project[0].source_dir is None


def test_pick_next_design_reconstructs_from_source_dir(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "005-z", has_plan=True)
    _make_project(queue_db, "proj-d", repo_path)
    _make_repo(queue_db, "repo-d", "proj-d", "primary", repo_path)

    with queue_db.session_scope() as session:
        session.add(
            AutopilotDesign(
                id="des-1",
                project_id="proj-d",
                filename=None,
                file_path=None,
                source_dir=str(feature_dir),
                repo_id="repo-d",
                name="005-z",
                content_hash="abc123",
                status="pending",
            )
        )

    entry = pick_next_design(tmp_path / "unused_queue", set(), _NullLogger(), project_id="proj-d")
    assert entry is not None
    assert entry.source_dir == feature_dir
    assert entry.path == feature_dir
    assert entry.repo_id == "repo-d"

    with queue_db.session_scope() as session:
        design = session.query(AutopilotDesign).filter_by(id="des-1").first()
        assert design.status == "processing"


def test_mixed_source_queue_sorts_by_name_not_null_filename_first(queue_db, tmp_path):
    """Gotchas item 5 / architectural review FIX: a directory-sourced row
    (filename=NULL) and a file-sourced row at the same ordinal must sort
    deterministically by name/filename, not with the NULL-filename row
    always first regardless of name. Picks the design with the
    alphabetically-earlier filename/name to prove it, not just "any"
    deterministic order.
    """
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    # Note: a leading digit sorts before a leading letter in plain
    # lexicographic order (ASCII '9' < 'a') -- name/dir-slug choices below
    # deliberately avoid that trap so the assertion isolates the NULL-bias
    # behavior this test targets, not an unrelated digit-vs-letter quirk.
    feature_dir = _make_feature_dir(repo_path, "zzz-dir-design", has_plan=True)
    _make_project(queue_db, "proj-sort", repo_path)
    _make_repo(queue_db, "repo-sort", "proj-sort", "primary", repo_path)

    with queue_db.session_scope() as session:
        # filename "aaa.md" must sort before the directory-sourced row's
        # name "zzz-dir-design" -- if the NULL-filename row were still
        # biased first (the pre-fix bug), this design would be picked
        # instead regardless of the names.
        session.add(
            AutopilotDesign(
                id="des-file",
                project_id="proj-sort",
                filename="aaa.md",
                file_path=str(tmp_path / "aaa.md"),
                name="File Design",
                content_hash="filehash",
                ordinal=0,
                status="pending",
            )
        )
        session.add(
            AutopilotDesign(
                id="des-dir",
                project_id="proj-sort",
                filename=None,
                file_path=None,
                source_dir=str(feature_dir),
                repo_id="repo-sort",
                name="zzz-dir-design",
                content_hash="dirhash",
                ordinal=0,
                status="pending",
            )
        )
    (tmp_path / "aaa.md").write_text("file design content")

    entry = pick_next_design(tmp_path / "unused_queue", set(), _NullLogger(), project_id="proj-sort")
    assert entry is not None
    assert entry.db_id == "des-file"


def test_pick_next_design_missing_source_dir_returns_none(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    _make_project(queue_db, "proj-e", tmp_path / "repo")
    with queue_db.session_scope() as session:
        session.add(
            AutopilotDesign(
                id="des-2",
                project_id="proj-e",
                filename=None,
                file_path=None,
                source_dir=str(tmp_path / "does-not-exist"),
                name="gone",
                content_hash="def456",
                status="pending",
            )
        )

    entry = pick_next_design(tmp_path / "unused_queue", set(), _NullLogger(), project_id="proj-e")
    assert entry is None

    with queue_db.session_scope() as session:
        design = session.query(AutopilotDesign).filter_by(id="des-2").first()
        # Adversarial review BLOCKER: this row was marked "processing" just
        # before the directory-missing check ran. pending_designs/
        # active_designs queries only ever select "pending"/"active" rows --
        # left at "processing" it would never be picked up (or surfaced as
        # failed) again.
        assert design.status == "failed"
        assert design.error is not None


def test_pick_next_design_hash_failure_returns_none_not_generic_fallback(queue_db, tmp_path):
    """spec.md deleted between an earlier scan and this pick, with no
    content_hash cached on the row: directory_content_hash raises inside
    the DB-first path. That must return None directly (adversarial review
    WARNING fix), not propagate to the outer except and fall through to a
    file-scan that could pick a completely different design.

    A real design.md sits in the fallback queue_dir specifically so the
    pre-fix behavior (exception bubbles up, DB-first path abandoned,
    generic file-scan fallback runs) has something to wrongly pick up --
    without it, both the buggy and fixed code happen to return None for
    unrelated reasons and the test wouldn't distinguish them.
    """
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = repo_path / "specs" / "007-gone"
    feature_dir.mkdir(parents=True)  # exists, but spec.md inside does not

    _make_project(queue_db, "proj-g", repo_path)
    with queue_db.session_scope() as session:
        session.add(
            AutopilotDesign(
                id="des-3",
                project_id="proj-g",
                filename=None,
                file_path=None,
                source_dir=str(feature_dir),
                name="007-gone",
                content_hash=None,  # never computed -- forces the hash path
                status="pending",
            )
        )

    queue_dir = tmp_path / "fallback_queue"
    queue_dir.mkdir()
    (queue_dir / "unrelated-design.md").write_text("a completely different design")

    entry = pick_next_design(queue_dir, set(), _NullLogger(), project_id="proj-g")
    assert entry is None

    with queue_db.session_scope() as session:
        design = session.query(AutopilotDesign).filter_by(id="des-3").first()
        assert design.status == "failed"  # not left stranded at "processing"
        assert design.error is not None

    with queue_db.session_scope() as session:
        # Still exactly one row -- the fallback scan never ran (and so
        # never auto-created a row for unrelated-design.md in its place).
        assert session.query(AutopilotDesign).count() == 1


def test_auto_create_row_for_directory_sourced_design_and_round_trip(queue_db, tmp_path):
    from src.autopilot.orchestrator.queue import pick_next_design
    from src.core.database import AutopilotDesign

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "006-w", has_plan=True)
    _make_project(queue_db, "proj-f", repo_path, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-f", "proj-f", "primary", repo_path)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()

    first = pick_next_design(queue_dir, set(), _NullLogger(), project_id="proj-f")
    assert first is not None
    assert first.db_id is not None

    with queue_db.session_scope() as session:
        rows = session.query(AutopilotDesign).filter_by(project_id="proj-f").all()
        assert len(rows) == 1
        row = rows[0]
        assert row.source_dir == str(feature_dir)
        assert row.repo_id == "repo-f"
        assert row.filename is None
        assert row.file_path is None

    # Round-trip: a second call picks the now-pending DB row via the
    # source_dir branch, not a second auto-create.
    second = pick_next_design(queue_dir, set(), _NullLogger(), project_id="proj-f")
    assert second is not None
    assert second.source_dir == feature_dir
    with queue_db.session_scope() as session:
        assert session.query(AutopilotDesign).filter_by(project_id="proj-f").count() == 1


def test_self_heal_check_failure_is_logged_not_silently_swallowed(queue_db, tmp_path, monkeypatch, caplog):
    """Adversarial review WARNING: the self-heal re-queue check's
    except Exception used to `continue` with zero logging. A transient DB
    error there must be visible (WARNING log), not indistinguishable from
    "correctly didn't need re-queuing".
    """
    import logging

    import src.core.database as core_db
    from src.autopilot.orchestrator.engine_client import directory_content_hash
    from src.autopilot.orchestrator.queue import scan_design_queue

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    feature_dir = _make_feature_dir(repo_path, "010-flaky", has_plan=True)
    _make_project(queue_db, "proj-j", repo_path, speckit_autoscan_enabled=True)
    _make_repo(queue_db, "repo-j", "proj-j", "primary", repo_path)

    content_hash = directory_content_hash(feature_dir)
    processed_hashes = {content_hash}  # forces the self-heal branch to run

    original_get_db = core_db.get_db
    call_count = {"n": 0}

    def _get_db_fails_on_self_heal_call(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:  # 1st call = detection session, 2nd = self-heal
            raise RuntimeError("simulated self-heal DB failure")
        return original_get_db(*args, **kwargs)

    monkeypatch.setattr(core_db, "get_db", _get_db_fails_on_self_heal_call)

    queue_dir = tmp_path / "empty_queue"
    queue_dir.mkdir()
    with caplog.at_level(logging.WARNING):
        designs = scan_design_queue(queue_dir, processed_hashes, project_id="proj-j")

    assert designs == []  # failure -> treated as "don't re-queue", not a crash
    assert any("[SELF-HEAL]" in r.message and "simulated self-heal DB failure" in r.message for r in caplog.records)
