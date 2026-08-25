"""Regression test for REQ-01/REQ-02 (des-c7b9 recovery/cleanup repo
threading): _resolve_recovery_project_path must fall back to the
workflow's own repo, not the single global $PROJECT_PATH env var, when
Workflow.working_directory is unset or missing on disk.

Before this fix, a multi-repo project's recovery cycle for a workflow
scoped to a non-primary child repo silently ran destructive git commands
(merge --abort, clean -fd, reset --hard) against whatever repo
$PROJECT_PATH happened to point at instead.
"""

import pytest

from src.core.database import (
    AutopilotProject,
    DatabaseManager,
    Feature,
    ProjectRepo,
    Workflow,
)


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", str(db_path))
    manager = DatabaseManager(str(db_path))
    manager.create_tables()
    return manager


def _make_project_with_repos(session, tmp_path):
    project = AutopilotProject(id="proj-1", name="Multi", base_dir=str(tmp_path / "primary"))
    session.add(project)
    primary = ProjectRepo(id="repo-primary", project_id="proj-1", label="primary", path=str(tmp_path / "primary"), is_primary=True)
    child = ProjectRepo(id="repo-child", project_id="proj-1", label="child", path=str(tmp_path / "child"), is_primary=False)
    session.add_all([primary, child])
    session.commit()
    return project, primary, child


class TestResolveRecoveryProjectPath:
    def test_falls_back_to_workflow_repo_not_project_path_env(self, tmp_path, test_db, monkeypatch):
        from src.autopilot.orchestrator.policy import _resolve_recovery_project_path

        session = test_db.get_session()
        _make_project_with_repos(session, tmp_path)
        feature = Feature(
            id="feat-1",
            design_id="design-1",
            feature_key="child-feature",
            repo_id="repo-child",
            name="Child feature",
            scope="scope",
        )
        session.add(feature)
        session.add(
            Workflow(
                id="wf-1",
                name="wf",
                phases_folder_path="/tmp",
                working_directory=None,
                project_id="proj-1",
                feature_id="feat-1",
            )
        )
        session.commit()
        session.close()

        monkeypatch.setenv("PROJECT_PATH", str(tmp_path / "primary"))

        result = _resolve_recovery_project_path("wf-1")

        assert result == str(tmp_path / "child")

    def test_single_repo_project_unaffected(self, tmp_path, test_db, monkeypatch):
        from src.autopilot.orchestrator.policy import _resolve_recovery_project_path

        session = test_db.get_session()
        project = AutopilotProject(id="proj-2", name="Single", base_dir=str(tmp_path / "solo"))
        session.add(project)
        primary = ProjectRepo(id="repo-solo", project_id="proj-2", label="primary", path=str(tmp_path / "solo"), is_primary=True)
        session.add(primary)
        session.add(
            Workflow(
                id="wf-2",
                name="wf",
                phases_folder_path="/tmp",
                working_directory=None,
                project_id="proj-2",
                feature_id=None,
            )
        )
        session.commit()
        session.close()

        monkeypatch.setenv("PROJECT_PATH", "/should/not/be/used")

        result = _resolve_recovery_project_path("wf-2")

        assert result == str(tmp_path / "solo")

    def test_working_directory_used_when_present_on_disk(self, tmp_path, test_db, monkeypatch):
        from src.autopilot.orchestrator.policy import _resolve_recovery_project_path

        working_dir = tmp_path / "wd"
        working_dir.mkdir()

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-3",
                name="wf",
                phases_folder_path="/tmp",
                working_directory=str(working_dir),
                project_id=None,
            )
        )
        session.commit()
        session.close()

        result = _resolve_recovery_project_path("wf-3")

        assert result == str(working_dir)

    def test_no_project_id_falls_back_to_env(self, test_db, monkeypatch):
        from src.autopilot.orchestrator.policy import _resolve_recovery_project_path

        session = test_db.get_session()
        session.add(
            Workflow(
                id="wf-4",
                name="wf",
                phases_folder_path="/tmp",
                working_directory=None,
                project_id=None,
            )
        )
        session.commit()
        session.close()

        monkeypatch.setenv("PROJECT_PATH", "/env/fallback")

        result = _resolve_recovery_project_path("wf-4")

        assert result == "/env/fallback"

    def test_dangling_repo_id_falls_back_to_env(self, tmp_path, test_db, monkeypatch):
        """Feature.repo_id points at a ProjectRepo that no longer belongs to
        (or exists for) the project -- resolve_repo_path raises RepoNotFoundError
        rather than silently substituting a different repo's path."""
        from src.autopilot.orchestrator.policy import _resolve_recovery_project_path

        session = test_db.get_session()
        project = AutopilotProject(id="proj-3", name="Dangling", base_dir=str(tmp_path / "dangling"))
        session.add(project)
        feature = Feature(
            id="feat-2",
            design_id="design-2",
            feature_key="ghost-feature",
            repo_id="repo-does-not-exist",
            name="Ghost feature",
            scope="scope",
        )
        session.add(feature)
        session.add(
            Workflow(
                id="wf-5",
                name="wf",
                phases_folder_path="/tmp",
                working_directory=None,
                project_id="proj-3",
                feature_id="feat-2",
            )
        )
        session.commit()
        session.close()

        monkeypatch.setenv("PROJECT_PATH", "/env/fallback")

        result = _resolve_recovery_project_path("wf-5")

        assert result == "/env/fallback"

    def test_db_failure_falls_back_to_env(self, test_db, monkeypatch):
        """get_db() itself raising (e.g. DB unreachable) must not propagate --
        recovery falls back to $PROJECT_PATH rather than crashing the sweep."""
        from src.autopilot.orchestrator import policy

        def _raise(*args, **kwargs):
            raise RuntimeError("db unavailable")

        monkeypatch.setattr(policy, "get_db", _raise)
        monkeypatch.setenv("PROJECT_PATH", "/env/fallback")

        result = policy._resolve_recovery_project_path("wf-does-not-matter")

        assert result == "/env/fallback"
