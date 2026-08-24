"""Human-input intervention routes (file-based). — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.core.constants import (
    AUTOPILOT_STATE_DIR,
)

# Import authentication function from server module

from src.mcp.autopilot._shared import _invalidate

logger = logging.getLogger(__name__)

router = APIRouter()

STALE_INPUT_SECONDS = 3600  # 1 hour

class HumanInputRequest(BaseModel):
    id: str
    reason: str
    timestamp: str
    options: List[str]
    labels: Dict[str, str]
    project_id: Optional[str] = None
    # Present on an arbitration-deadlock escalation (written by
    # _escalate_arbitration_deadlock_to_human), absent on every other kind
    # of request -- BaseModel silently drops any field not declared here,
    # so without these the frontend's row-correlation (which workflow is
    # this request for?) never receives them even though the request file
    # on disk always has them.
    workflow_id: Optional[str] = None
    phase_id: Optional[str] = None
    # Structured breakdown of an arbitration-deadlock escalation's actual
    # attempt history (see arbitration.py's _build_arbitration_decision_
    # context) -- None for every other kind of human-input request.
    decision_context: Optional[Dict[str, Any]] = None

class HumanInputResponse(BaseModel):
    request_id: str
    choice: str
    message: Optional[str] = None
    # Required when choice == "g" (send an arbitration-deadlocked phase
    # back to a specific phase for another attempt) -- ignored otherwise.
    target_phase: Optional[str] = None

def _find_pending_input() -> Optional[Path]:
    """Find the first non-stale input request file.

    "arbitration_escalation" requests (see arbitration.py's
    _escalate_arbitration_deadlock_to_human) are exempt from the
    staleness cutoff below -- that function's own docstring is explicit
    that it "deliberately does NOT time out," because auto-continuing
    past a confirmed, unresolved architectural BLOCKER with no actual
    decision defeats the entire point of escalating it to a human in the
    first place. Before this exemption, this cleanup silently deleted an
    arbitration escalation's request file once it crossed
    STALE_INPUT_SECONDS regardless of kind -- and
    _maybe_resolve_human_arbitration_escalations treats a missing request
    file with no response as an explicit dismissal (the same convention
    prompt_human's other callers use for a real UI X-button click),
    auto-continuing the deadlocked phase as if a human had actually
    chosen to. Observed live: an arbitration escalation nobody ever
    answered force-continued itself past design_review after silently
    expiring, with no visible sign anything had happened.
    """
    input_dir = Path(AUTOPILOT_STATE_DIR)
    if not input_dir.exists():
        return None
    for f in sorted(input_dir.glob("input_request_*.json")):
        try:
            data = json.loads(f.read_text())
            if data.get("kind") == "arbitration_escalation":
                return f
            ts = datetime.fromisoformat(data["timestamp"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > STALE_INPUT_SECONDS:
                f.unlink(missing_ok=True)
                # Also clean up any orphaned response
                rid = data.get("id", "")
                resp = input_dir / f"input_response_{rid}.json"
                resp.unlink(missing_ok=True)
                continue
            return f
        except Exception as e:
            # Previously silent -- a malformed-but-not-yet-stale request
            # file was skipped with no visible sign, so the UI never shows
            # the pending question and the orchestrator stays blocked
            # until the stale-timeout eventually cleans the file up.
            logger.warning(f"Skipping unreadable pending-input file {f}: {e}")
            continue
    return None

@router.get("/input", response_model=Optional[HumanInputRequest])
async def get_human_input_request():
    """Check if the orchestrator is waiting for human input."""
    request_file = _find_pending_input()
    if not request_file:
        return None
    try:
        data = json.loads(request_file.read_text())
        return HumanInputRequest(**data)
    except Exception as e:
        logger.warning(f"Failed to read pending-input file {request_file}: {e}")
        return None

@router.post("/input")
async def submit_human_input(resp: HumanInputResponse):
    """Submit a human input response to the orchestrator."""
    if resp.choice not in ("c", "s", "q", "m", "g"):
        raise HTTPException(400, "Invalid choice. Must be 'c', 's', 'q', 'm', or 'g'.")
    if resp.choice == "g" and not resp.target_phase:
        raise HTTPException(400, "target_phase is required when choice is 'g'.")

    # Verify the request still exists
    request_file = Path(AUTOPILOT_STATE_DIR) / f"input_request_{resp.request_id}.json"
    if not request_file.exists():
        raise HTTPException(404, "Input request not found or already answered.")

    response_file = Path(AUTOPILOT_STATE_DIR) / f"input_response_{resp.request_id}.json"
    response_file.parent.mkdir(parents=True, exist_ok=True)

    # Atomic write via temp+rename
    payload = {
        "request_id": resp.request_id,
        "choice": resp.choice,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if resp.message:
        payload["message"] = resp.message
    if resp.target_phase:
        payload["target_phase"] = resp.target_phase
    payload = json.dumps(payload)
    tmp = response_file.with_suffix(".tmp")
    tmp.write_text(payload)
    os.rename(tmp, response_file)

    _invalidate("status")
    return {"submitted": resp.choice, "request_id": resp.request_id}

@router.delete("/input/{request_id}")
async def dismiss_human_input(request_id: str):
    """Dismiss a pending human input request without responding."""
    request_file = Path(AUTOPILOT_STATE_DIR) / f"input_request_{request_id}.json"
    if not request_file.exists():
        raise HTTPException(404, "Request not found.")
    # Only delete the request — orchestrator will see it's gone and stop polling
    request_file.unlink(missing_ok=True)
    _invalidate("status")
    return {"dismissed": request_id}
