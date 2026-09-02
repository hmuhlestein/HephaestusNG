"""Regression: get_design_status's per-feature has_report never found a
report doc_review filed under the design's own storage folder.

It only checked the live worktree (_resolve_live_feature_report) and the
archived features gallery under the project's workspace root
(_find_archived_feature_report) -- but PhaseManager only archives a feature
into that gallery once the WHOLE 12-phase pipeline for it finishes.
_scan_features (the Completed tab) and the feature-records endpoints already
had a third fallback for exactly this gap -- _resolve_feature_record_report,
which searches every design folder for this design_id -- but this function,
which drives the design-queue row's own per-feature report icon, never
called it. A completed feature whose report existed only there showed no
report icon at all.

Observed live: feat-6277bc33 "Backend Conversation Persistence", a
multi-repo feature built in the project's back-end child repo. Its worktree
was gone (working_directory NULL) and there was no matching folder in the
project's own features gallery -- the report existed only under
.hephaestus/specs/<design-run>/features/backend-persistence/, written by a
LATER pipeline run than the one autopilot_designs.designs_folder still
names (the same "which run's folder" drift the report-resolution fix from
earlier this session covers).
"""

import json

import pytest

from src.core.constants import CONTEXT_DIR_NAME


@pytest.fixture
def multi_repo_feature(tmp_path, monkeypatch):
    from src.core.database import (
        AutopilotDesign,
        AutopilotProject,
        DatabaseManager,
        Feature,
        Phase,
        Task,
        Workflow,
    )

    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("HEPHAESTUS_TEST_DB", db_path)
    db = DatabaseManager(db_path)
    db.create_tables()

    project_dir = tmp_path / "parent"
    # The design row's own designs_folder names an EARLIER run than the one
    # that actually produced the report -- the same drift
    # _resolve_feature_record_report was built to search across.
    recorded_run = project_dir / CONTEXT_DIR_NAME / "specs" / "20260101-000000_spec_des-x"
    later_run = project_dir / CONTEXT_DIR_NAME / "specs" / "20260102-000000_spec_des-x"
    (recorded_run / "features" / "backend-persistence").mkdir(parents=True)
    (later_run / "features" / "backend-persistence").mkdir(parents=True)
    (later_run / "features" / "backend-persistence" / "feature_report-abc12345.html").write_text(
        "<html>the report</html>"
    )

    with db.session_scope() as session:
        session.add(
            AutopilotProject(id="proj-1", name="parent", base_dir=str(project_dir))
        )
        session.add(
            AutopilotDesign(
                id="des-x",
                project_id="proj-1",
                filename="spec.md",
                name="spec",
                designs_folder=str(recorded_run),
                status="active",
            )
        )
        session.add(
            Workflow(
                id="wf-feature",
                name="autopilot",
                phases_folder_path="/tmp",
                status="completed",
                definition_id="autopilot",
                design_id="des-x",
                # Gone: the worktree was cleaned up, and this feature's repo
                # (a child repo) is never what PhaseManager's gallery archive
                # is rooted at either.
                working_directory=None,
            )
        )
        session.add(
            Phase(
                id="phase-doc-review",
                workflow_id="wf-feature",
                order=11,
                name="doc_review",
                description="d",
                done_definitions=["x"],
            )
        )
        session.add(
            Task(
                id="task-doc-review",
                workflow_id="wf-feature",
                phase_id="phase-doc-review",
                raw_description="r",
                done_definition="d",
                status="done",
            )
        )
        session.add(
            Feature(
                id="feat-1",
                design_id="des-x",
                feature_key="backend-persistence",
                name="Backend Conversation Persistence",
                scope="s",
                status="completed",
                workflow_id="wf-feature",
            )
        )
    return {"project_dir": project_dir, "db": db}


@pytest.mark.asyncio
async def test_has_report_is_found_in_the_designs_own_storage_folder(multi_repo_feature):
    import src.mcp.autopilot  # noqa: F401 -- resolve the circular import first
    from src.services.design_status_service import get_design_status

    result = await get_design_status(
        "proj-1",
        "spec.md",
        str(multi_repo_feature["project_dir"]),
        "",
        "spec",
        design_id="des-x",
    )

    feature = next(f for f in result["features"] if f["feature_key"] == "backend-persistence")
    assert feature["has_report"] is True
