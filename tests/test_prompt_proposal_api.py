"""End-to-end tests for the prompt-proposal review loop.

Covers the path a real proposal takes: forensics files it via the MCP tool,
it appears pending in the review API with a live "before", a human approves,
the YAML is written and committed, and a revert puts it back.

The service-level guards are tested in test_prompt_proposal_service.py; what
matters here is that the transport layers actually go through them rather than
around them.
"""

import subprocess

import pytest
import yaml

from src.services import prompt_proposal_service as svc


@pytest.fixture
def prompt_repo(tmp_path, monkeypatch):
    """A throwaway checkout with one editable phase, wired in as the workflows
    directory the service reads."""
    repo = tmp_path / "repo"
    wf = repo / "config" / "workflows" / "demo"
    wf.mkdir(parents=True)
    (wf / "demo_phase.yaml").write_text(
        "id: 1\n"
        "name: demo_phase\n"
        "spec_gate: true\n"
        "description: |\n"
        "  original description\n"
        "done_definitions:\n"
        '  - "do the thing"\n'
        "\n"
        "# a load-bearing comment about outputs\n"
        "outputs:\n"
        '  - "thing.md"\n'
    )
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "init"],
    ):
        subprocess.run(cmd, cwd=repo, check=True)
    monkeypatch.setattr(svc, "_workflows_dir", lambda: repo / "config" / "workflows")
    return repo


@pytest.fixture
def db(monkeypatch, tmp_path):
    """An isolated SQLite DB with the prompt_proposals table."""
    from src.core.database import Base, DatabaseManager, PromptProposal

    mgr = DatabaseManager(str(tmp_path / "t.db"))
    Base.metadata.create_all(mgr.engine, tables=[PromptProposal.__table__], checkfirst=True)

    from contextlib import contextmanager

    @contextmanager
    def _get_db():
        session = mgr.get_session()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr("src.core.database.get_db", _get_db)
    # The routes module does `from src.core.database import get_db` at import
    # time, so patching the source module alone only works when routes has not
    # been imported yet -- i.e. it passes in isolation and fails once another
    # test in the file has already imported it. Patch the bound name too so the
    # fixture does not depend on import order.
    from src.mcp.autopilot import prompt_proposal_routes as _routes

    monkeypatch.setattr(_routes, "get_db", _get_db)
    return mgr


class TestCreateProposal:
    def test_filing_a_proposal_persists_it_as_pending(self, prompt_repo, db):
        result = svc.create_proposal(
            phase_name="demo_phase",
            field="description",
            proposed_value="rewritten\n",
            rationale="the run showed X",
            workflow_definition="demo",
            proposing_phase="forensics_analysis",
        )
        assert result["status"] == "pending"
        assert result["id"].startswith("prop-")

    def test_guards_apply_on_the_creation_path_too(self, prompt_repo, db):
        """The tool and the route both go through create_proposal, so a caller
        cannot reach the edit engine without passing validate_proposal."""
        with pytest.raises(ValueError, match="not editable"):
            svc.create_proposal(
                phase_name="demo_phase",
                field="spec_gate",
                proposed_value=False,
                rationale="I would like to disable this gate",
                workflow_definition="demo",
            )

    def test_rationale_is_required(self, prompt_repo, db):
        with pytest.raises(ValueError, match="rationale is required"):
            svc.create_proposal(
                phase_name="demo_phase",
                field="description",
                proposed_value="x\n",
                rationale="   ",
                workflow_definition="demo",
            )


class TestMcpToolPath:
    """The forensics agent's entry point. It must never edit anything itself,
    and must report a refusal rather than raising."""

    @pytest.mark.asyncio
    async def test_tool_files_a_proposal_without_changing_the_file(self, prompt_repo, db):
        from src.mcp.server._mcp_tool_registry import _tool_propose_prompt_change

        target = prompt_repo / "config" / "workflows" / "demo" / "demo_phase.yaml"
        before = target.read_text()

        result = await _tool_propose_prompt_change({
            "phase_name": "demo_phase",
            "field": "description",
            "proposed_value": "rewritten by the agent\n",
            "rationale": "development looped 4 times on an ambiguous instruction",
            "proposing_phase": "forensics_analysis",
            "workflow_definition": "demo",
        })
        assert result["success"] is True
        assert target.read_text() == before, "filing a proposal must not touch the file"

    @pytest.mark.asyncio
    async def test_tool_reports_a_refusal_instead_of_raising(self, prompt_repo, db):
        """A refused proposal should let the agent record it and carry on with
        the rest of its report, not look like a tool failure worth retrying."""
        from src.mcp.server._mcp_tool_registry import _tool_propose_prompt_change

        result = await _tool_propose_prompt_change({
            "phase_name": "demo_phase",
            "field": "outputs",
            "proposed_value": ["something_else.md"],
            "rationale": "trying to change the gate artifact",
            "workflow_definition": "demo",
        })
        assert result["success"] is False and result["rejected"] is True
        assert "not editable" in result["reason"]

    @pytest.mark.asyncio
    async def test_tool_blocks_forensics_editing_its_own_prompt(self, prompt_repo, db):
        from src.mcp.server._mcp_tool_registry import _tool_propose_prompt_change

        wf = prompt_repo / "config" / "workflows" / "demo"
        (wf / "forensics_analysis.yaml").write_text(
            "id: 2\nname: forensics_analysis\ndescription: |\n  mine\n"
        )
        result = await _tool_propose_prompt_change({
            "phase_name": "forensics_analysis",
            "field": "description",
            "proposed_value": "a better me\n",
            "rationale": "self-improvement",
            "proposing_phase": "forensics_analysis",
            "workflow_definition": "demo",
        })
        assert result["rejected"] is True and "own prompt" in result["reason"]


class TestReviewLoop:
    """Approve applies and commits; revert restores. Both through the API's
    own handlers, not the service directly."""

    @pytest.mark.asyncio
    async def test_approve_applies_commits_and_records_the_sha(
        self, prompt_repo, db, monkeypatch
    ):
        from src.mcp.autopilot import prompt_proposal_routes as routes

        monkeypatch.setattr(routes, "_repo_root", lambda: prompt_repo)
        filed = svc.create_proposal(
            phase_name="demo_phase",
            field="description",
            proposed_value="rewritten\n",
            rationale="evidence from the run",
            workflow_definition="demo",
        )

        resp = await routes.approve_prompt_proposal(filed["id"], routes.ProposalReview(note="ok"))
        proposal = resp["proposal"]
        assert proposal["status"] == "applied"
        assert proposal["applied_commit_sha"]
        assert proposal["previous_value"] == "original description\n"

        target = prompt_repo / "config" / "workflows" / "demo" / "demo_phase.yaml"
        parsed = yaml.safe_load(target.read_text())
        assert parsed["description"] == "rewritten\n"
        # The guarded fields and the comments are untouched.
        assert parsed["spec_gate"] is True and parsed["outputs"] == ["thing.md"]
        assert "# a load-bearing comment about outputs" in target.read_text()

    @pytest.mark.asyncio
    async def test_revert_restores_and_marks_the_row(self, prompt_repo, db, monkeypatch):
        from src.mcp.autopilot import prompt_proposal_routes as routes

        monkeypatch.setattr(routes, "_repo_root", lambda: prompt_repo)
        filed = svc.create_proposal(
            phase_name="demo_phase",
            field="description",
            proposed_value="rewritten\n",
            rationale="evidence",
            workflow_definition="demo",
        )
        await routes.approve_prompt_proposal(filed["id"], routes.ProposalReview())
        resp = await routes.revert_prompt_proposal(filed["id"])

        assert resp["proposal"]["status"] == "reverted"
        target = prompt_repo / "config" / "workflows" / "demo" / "demo_phase.yaml"
        assert yaml.safe_load(target.read_text())["description"] == "original description\n"

    @pytest.mark.asyncio
    async def test_reject_leaves_the_file_alone(self, prompt_repo, db, monkeypatch):
        from src.mcp.autopilot import prompt_proposal_routes as routes

        monkeypatch.setattr(routes, "_repo_root", lambda: prompt_repo)
        target = prompt_repo / "config" / "workflows" / "demo" / "demo_phase.yaml"
        before = target.read_text()
        filed = svc.create_proposal(
            phase_name="demo_phase",
            field="description",
            proposed_value="rewritten\n",
            rationale="evidence",
            workflow_definition="demo",
        )
        resp = await routes.reject_prompt_proposal(
            filed["id"], routes.ProposalReview(note="not convinced")
        )
        assert resp["proposal"]["status"] == "rejected"
        assert resp["proposal"]["review_note"] == "not convinced"
        assert target.read_text() == before

    @pytest.mark.asyncio
    async def test_a_proposal_cannot_be_approved_twice(self, prompt_repo, db, monkeypatch):
        from fastapi import HTTPException

        from src.mcp.autopilot import prompt_proposal_routes as routes

        monkeypatch.setattr(routes, "_repo_root", lambda: prompt_repo)
        filed = svc.create_proposal(
            phase_name="demo_phase",
            field="description",
            proposed_value="rewritten\n",
            rationale="evidence",
            workflow_definition="demo",
        )
        await routes.approve_prompt_proposal(filed["id"], routes.ProposalReview())
        with pytest.raises(HTTPException) as exc:
            await routes.approve_prompt_proposal(filed["id"], routes.ProposalReview())
        assert exc.value.status_code == 409

    @pytest.mark.asyncio
    async def test_listing_shows_the_live_before_and_flags_staleness(
        self, prompt_repo, db, monkeypatch
    ):
        """The 'before' must be read from disk, not echoed from what the agent
        quoted -- otherwise a file that changed after the proposal was filed
        produces a diff the reviewer cannot tell is fiction."""
        from src.mcp.autopilot import prompt_proposal_routes as routes

        monkeypatch.setattr(routes, "_repo_root", lambda: prompt_repo)
        svc.create_proposal(
            phase_name="demo_phase",
            field="description",
            proposed_value="rewritten\n",
            rationale="evidence",
            quoted_current_value="original description\n",
            workflow_definition="demo",
        )
        listing = await routes.list_prompt_proposals()
        assert listing["pending_count"] == 1
        assert listing["proposals"][0]["current_value"] == "original description\n"
        assert listing["proposals"][0]["is_stale"] is False

        # Someone edits the file behind the proposal's back.
        target = prompt_repo / "config" / "workflows" / "demo" / "demo_phase.yaml"
        svc.apply_edit(target, "description", "changed by a human\n")
        target.write_text(svc.apply_edit(target, "description", "changed by a human\n"))

        listing = await routes.list_prompt_proposals()
        assert listing["proposals"][0]["current_value"] == "changed by a human\n"
        assert listing["proposals"][0]["is_stale"] is True

    @pytest.mark.asyncio
    async def test_apply_failure_is_recorded_on_the_row_not_lost(
        self, prompt_repo, db, monkeypatch
    ):
        """A proposal that cannot be applied is something the reviewer needs to
        see, not an error that vanishes with the HTTP response."""
        from fastapi import HTTPException

        from src.core.database import PromptProposal, get_db
        from src.mcp.autopilot import prompt_proposal_routes as routes

        monkeypatch.setattr(routes, "_repo_root", lambda: prompt_repo)
        filed = svc.create_proposal(
            phase_name="demo_phase",
            field="description",
            proposed_value="rewritten\n",
            rationale="evidence",
            workflow_definition="demo",
        )
        # Delete the target so the apply cannot succeed.
        (prompt_repo / "config" / "workflows" / "demo" / "demo_phase.yaml").unlink()

        with pytest.raises(HTTPException) as exc:
            await routes.approve_prompt_proposal(filed["id"], routes.ProposalReview())
        assert exc.value.status_code == 400

        with get_db() as session:
            row = session.query(PromptProposal).filter_by(id=filed["id"]).first()
            assert row.status == "failed"
            assert "apply failed" in row.review_note
