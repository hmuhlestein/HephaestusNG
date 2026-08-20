"""Review API for forensics-proposed prompt rewrites (finding 8).

Backs the autopilot Improvements tab: list pending proposals with a real
before/after diff, approve (which writes the YAML and commits it), reject, or
revert something already applied.

Every guard lives in prompt_proposal_service, not here and not in the UI --
see that module's SAFETY MODEL. These routes are a thin transport over it.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.core.database import PromptProposal, get_db
from src.services.prompt_proposal_service import (
    EDITABLE_FIELDS,
    apply_proposal,
    create_proposal,
    current_value,
    revert_proposal,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _repo_root() -> Path:
    """The Hephaestus checkout whose config/workflows/ holds the prompts.

    Derived from this file's location rather than a config value: the prompts
    being edited are the ones this running instance loads, which are by
    definition the ones in its own checkout.
    """
    return Path(__file__).resolve().parents[3]


class ProposalCreate(BaseModel):
    phase_name: str
    field: str
    proposed_value: Any
    rationale: str
    evidence: Optional[str] = None
    quoted_current_value: Optional[Any] = None
    workflow_definition: str = "autopilot"
    workflow_id: Optional[str] = None
    proposing_phase: Optional[str] = None
    created_by_agent_id: Optional[str] = None


class ProposalReview(BaseModel):
    note: Optional[str] = None


def _serialize(p: PromptProposal, include_current: bool = False) -> dict:
    data = {
        "id": p.id,
        "workflow_id": p.workflow_id,
        "workflow_definition": p.workflow_definition,
        "phase_name": p.phase_name,
        "field": p.field,
        "proposing_phase": p.proposing_phase,
        "proposed_value": p.proposed_value,
        "quoted_current_value": p.quoted_current_value,
        "previous_value": p.previous_value,
        "rationale": p.rationale,
        "evidence": p.evidence,
        "status": p.status,
        "review_note": p.review_note,
        "applied_commit_sha": p.applied_commit_sha,
        "reverted_commit_sha": p.reverted_commit_sha,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "reviewed_at": p.reviewed_at.isoformat() if p.reviewed_at else None,
        "applied_at": p.applied_at.isoformat() if p.applied_at else None,
    }
    # Only for proposals still awaiting a decision, for two reasons.
    #
    # Correctness: once a proposal is APPLIED the file holds proposed_value, so
    # current_value would equal it and the UI's `current_value ?? previous_value`
    # would render every historical row as an empty diff -- destroying the audit
    # trail this feature exists to provide. Omitting it makes the frontend fall
    # back to previous_value, which is the correct "before" for a change that
    # already landed.
    #
    # Cost: this parses a phase YAML off disk per proposal, and the Autopilot
    # page polls this endpoint every 30s purely to render a badge count. There
    # is no reason to re-read a phase file for every row of resolved history on
    # every tick.
    if include_current and p.status == "pending":
        # Read live rather than echoing what the agent quoted: the file may
        # have changed since the proposal was filed, and a stale "before" would
        # make the diff a fiction the reviewer cannot detect.
        data["current_value"] = current_value(p.workflow_definition, p.phase_name, p.field)
        data["is_stale"] = (
            p.quoted_current_value is not None
            and p.quoted_current_value != data["current_value"]
        )
    return data


@router.get("/prompt_proposals")
async def list_prompt_proposals(status: Optional[str] = None, limit: int = 100):
    """List proposals, newest first. `status` filters (pending/applied/...)."""

    def _work():
        with get_db() as db:
            q = db.query(PromptProposal)
            if status:
                q = q.filter(PromptProposal.status == status)
            rows = q.order_by(PromptProposal.created_at.desc()).limit(limit).all()
            return [_serialize(r, include_current=True) for r in rows]

    proposals = await asyncio.get_running_loop().run_in_executor(None, _work)
    return {
        "proposals": proposals,
        "count": len(proposals),
        "pending_count": sum(1 for p in proposals if p["status"] == "pending"),
        "editable_fields": list(EDITABLE_FIELDS),
    }


@router.post("/prompt_proposals")
async def create_prompt_proposal(req: ProposalCreate):
    """File a proposal. Validated here so a malformed or out-of-bounds one is
    rejected at the source, where the agent still has context to react, rather
    than surfacing much later as a broken row in the review UI."""
    def _work():
        return create_proposal(
            phase_name=req.phase_name,
            field=req.field,
            proposed_value=req.proposed_value,
            rationale=req.rationale,
            evidence=req.evidence,
            quoted_current_value=req.quoted_current_value,
            workflow_definition=req.workflow_definition,
            workflow_id=req.workflow_id,
            proposing_phase=req.proposing_phase,
            created_by_agent_id=req.created_by_agent_id,
        )

    try:
        created = await asyncio.get_running_loop().run_in_executor(None, _work)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"success": True, "proposal": created}


@router.post("/prompt_proposals/{proposal_id}/approve")
async def approve_prompt_proposal(proposal_id: str, req: ProposalReview):
    """Apply the edit and commit it.

    Runs in a thread: the edit does real filesystem and git work, which must
    not block the event loop.
    """

    def _work():
        with get_db() as db:
            row = db.query(PromptProposal).filter_by(id=proposal_id).first()
            if not row:
                return ("missing", None)
            if row.status != "pending":
                return ("not_pending", row.status)
            try:
                result = apply_proposal(
                    _repo_root(),
                    row.workflow_definition,
                    row.phase_name,
                    row.field,
                    row.proposed_value,
                    row.id,
                    row.proposing_phase,
                )
            except Exception as e:
                # Record the failure on the row rather than losing it to a 500:
                # a proposal that could not be applied is a thing the reviewer
                # needs to see, not an error that vanishes with the response.
                row.status = "failed"
                row.review_note = f"apply failed: {e}"
                row.reviewed_at = datetime.utcnow()
                db.commit()
                return ("failed", str(e))
            row.status = "applied"
            row.previous_value = result["previous_value"]
            row.applied_commit_sha = result["commit_sha"]
            row.review_note = req.note
            row.reviewed_at = datetime.utcnow()
            row.applied_at = datetime.utcnow()
            db.commit()
            return ("ok", _serialize(row))

    outcome, payload = await asyncio.get_running_loop().run_in_executor(None, _work)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail=f"No proposal {proposal_id}")
    if outcome == "not_pending":
        raise HTTPException(
            status_code=409, detail=f"Proposal is already {payload}, not pending"
        )
    if outcome == "failed":
        raise HTTPException(status_code=400, detail=f"Could not apply: {payload}")
    return {"success": True, "proposal": payload}


@router.post("/prompt_proposals/{proposal_id}/reject")
async def reject_prompt_proposal(proposal_id: str, req: ProposalReview):
    def _work():
        with get_db() as db:
            row = db.query(PromptProposal).filter_by(id=proposal_id).first()
            if not row:
                return ("missing", None)
            if row.status != "pending":
                return ("not_pending", row.status)
            row.status = "rejected"
            row.review_note = req.note
            row.reviewed_at = datetime.utcnow()
            db.commit()
            return ("ok", _serialize(row))

    outcome, payload = await asyncio.get_running_loop().run_in_executor(None, _work)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail=f"No proposal {proposal_id}")
    if outcome == "not_pending":
        raise HTTPException(
            status_code=409, detail=f"Proposal is already {payload}, not pending"
        )
    return {"success": True, "proposal": payload}


@router.post("/prompt_proposals/{proposal_id}/revert")
async def revert_prompt_proposal(proposal_id: str):
    """Undo an applied proposal by restoring the value recorded at apply time."""

    def _work():
        with get_db() as db:
            row = db.query(PromptProposal).filter_by(id=proposal_id).first()
            if not row:
                return ("missing", None)
            if row.status != "applied":
                return ("not_applied", row.status)
            try:
                result = revert_proposal(
                    _repo_root(),
                    row.workflow_definition,
                    row.phase_name,
                    row.field,
                    row.previous_value,
                    row.id,
                )
            except Exception as e:
                return ("failed", str(e))
            row.status = "reverted"
            row.reverted_commit_sha = result["commit_sha"]
            row.reverted_at = datetime.utcnow()
            db.commit()
            return ("ok", _serialize(row))

    outcome, payload = await asyncio.get_running_loop().run_in_executor(None, _work)
    if outcome == "missing":
        raise HTTPException(status_code=404, detail=f"No proposal {proposal_id}")
    if outcome == "not_applied":
        raise HTTPException(
            status_code=409, detail=f"Proposal is {payload}, only an applied one can be reverted"
        )
    if outcome == "failed":
        raise HTTPException(status_code=400, detail=f"Could not revert: {payload}")
    return {"success": True, "proposal": payload}
