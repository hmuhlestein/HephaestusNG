"""FastAPI router exposing tmux session output and control via HTTP endpoints."""

from typing import Dict, Any, List, Optional
from fastapi import APIRouter, Query, HTTPException
from pydantic import BaseModel

from .tmux_manager import TmuxSessionManager

router = APIRouter()

_manager: Optional[TmuxSessionManager] = None


def get_manager() -> TmuxSessionManager:
    global _manager
    if _manager is None:
        _manager = TmuxSessionManager()
    return _manager


def init_manager(manager: TmuxSessionManager):
    """Inject a custom TmuxSessionManager instance."""
    global _manager
    _manager = manager


class SendMessageRequest(BaseModel):
    message: str
    enter: bool = True


class CreateSessionRequest(BaseModel):
    session_name: str
    working_directory: Optional[str] = None
    env_vars: Optional[Dict[str, str]] = None


@router.get("/sessions")
async def list_sessions(prefix: Optional[str] = Query(None)):
    """List all tmux sessions."""
    return get_manager().list_sessions(prefix_filter=prefix)


@router.post("/sessions")
async def create_session(req: CreateSessionRequest):
    """Create a new tmux session."""
    try:
        session = get_manager().create_session(
            session_name=req.session_name,
            working_directory=req.working_directory,
            env_vars=req.env_vars,
        )
        return {"session_name": session.name, "status": "created"}
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/sessions/{session_name}")
async def kill_session(session_name: str):
    """Kill a tmux session."""
    killed = get_manager().kill_session(session_name)
    if not killed:
        raise HTTPException(status_code=404, detail=f"Session '{session_name}' not found")
    return {"session_name": session_name, "status": "killed"}


@router.get("/sessions/{session_name}/output")
async def get_session_output(
    session_name: str,
    lines: int = Query(2000, ge=10, le=10000),
):
    """Get terminal output from a tmux session."""
    output = get_manager().get_output(session_name, lines=lines)
    return {
        "session_name": session_name,
        "output": output,
        "line_count": len(output.split("\n")) if output else 0,
    }


@router.post("/sessions/{session_name}/send")
async def send_to_session(session_name: str, req: SendMessageRequest):
    """Send a message to a tmux session."""
    sent = get_manager().send_message(session_name, req.message, enter=req.enter)
    if not sent:
        raise HTTPException(
            status_code=404, detail=f"Session '{session_name}' not found or send failed"
        )
    return {"session_name": session_name, "status": "sent"}


@router.get("/sessions/{session_name}/exists")
async def check_session(session_name: str):
    """Check if a tmux session exists."""
    exists = get_manager().session_exists(session_name)
    return {"session_name": session_name, "exists": exists}
