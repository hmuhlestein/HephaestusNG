"""REQ-16: _resolve_recovery_project_path must resolve via the workflow's
own project/repo instead of a single process-wide $PROJECT_PATH env var,
which ignores which project a workflow belongs to entirely -- on a
multi-repo (or just multi-project) setup that fallback could point
recovery's destructive git ops (reset --hard, clean -fd) at a repo
unrelated to this workflow.
"""

from src.core.database import AutopilotProject, ProjectRepo, Workflow


def test_falls_back_to_project_primary_repo_not_env_var(db_manager, monkeypatch, tmp_path):
    from src.autopilot.orchestrator.policy import _resolve_recovery_project_path

    unrelated_dir = tmp_path / "unrelated-global-default"
    unrelated_dir.mkdir()
    monkeypatch.setenv("PROJECT_PATH", str(unrelated_dir))

    repo_dir = tmp_path / "the-actual-project-repo"
    repo_dir.mkdir()

    session = db_manager.get_session()
    try:
        project = AutopilotProject(id="proj-1", name="p", base_dir=str(repo_dir))
        session.add(project)
        session.flush()
        session.add(
            ProjectRepo(
                id="repo-1",
                project_id="proj-1",
                label="main",
                path=str(repo_dir),
                is_primary=True,
            )
        )
        session.add(
            Workflow(
                id="wf-1",
                name="wf",
                status="active",
                project_id="proj-1",
                phases_folder_path=str(tmp_path),
                # No working_directory set -- e.g. its worktree was already
                # torn down. This is the exact case that used to fall
                # through to the global $PROJECT_PATH env var.
                working_directory=None,
            )
        )
        session.commit()
    finally:
        session.close()

    result = _resolve_recovery_project_path("wf-1")

    assert result == str(repo_dir)
    assert result != str(unrelated_dir)


def test_falls_back_to_env_var_when_workflow_has_no_project(db_manager, monkeypatch, tmp_path):
    """A workflow with no project association at all still has nowhere
    better to resolve to -- $PROJECT_PATH remains the last resort."""
    from src.autopilot.orchestrator.policy import _resolve_recovery_project_path

    fallback_dir = tmp_path / "global-default"
    fallback_dir.mkdir()
    monkeypatch.setenv("PROJECT_PATH", str(fallback_dir))

    session = db_manager.get_session()
    try:
        session.add(
            Workflow(
                id="wf-2",
                name="wf",
                status="active",
                project_id=None,
                phases_folder_path=str(tmp_path),
                working_directory=None,
            )
        )
        session.commit()
    finally:
        session.close()

    result = _resolve_recovery_project_path("wf-2")

    assert result == str(fallback_dir)
