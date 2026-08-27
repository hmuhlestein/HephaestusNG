"""Message and log routes. — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query

# Import authentication function from server module
from src.mcp.autopilot._shared import MessageItem, _cached, _get_latest_run_dir, _store

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/messages", response_model=List[MessageItem])
async def get_messages(limit: int = Query(50, ge=1, le=500), project_id: Optional[str] = None):
    # project_id wasn't previously a param here at all -- events.jsonl was
    # found via _get_latest_run_dir(), a global (not project-scoped) "most
    # recently created run dir" scan, so under concurrent multi-project
    # runs this could silently return a DIFFERENT project's messages.
    # AutopilotPipelineEvent rows carry a real project_id column, so
    # querying by it (when given) actually fixes that instead of just
    # relocating the same bug into the DB.
    cache_key = f"messages:{limit}:{project_id}"
    cached = _cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    from src.core.database import AutopilotPipelineEvent
    from src.core.database import get_db as _get_db

    try:
        with _get_db() as db:
            query = db.query(AutopilotPipelineEvent)
            if project_id:
                query = query.filter(AutopilotPipelineEvent.project_id == project_id)
            rows = query.order_by(AutopilotPipelineEvent.created_at.desc()).limit(limit).all()
    except Exception:
        rows = []

    result = [
        MessageItem(
            timestamp=row.created_at.isoformat(),
            type=row.event_type,
            data=row.data or {},
        )
        for row in rows
    ]
    return _store(cache_key, result)

@router.get("/messages/archived")
async def get_archived_messages():
    """Get archived message IDs."""
    from sqlalchemy import text

    from src.core.database import get_db

    with get_db() as db:
        try:
            db.execute(text("SELECT 1 FROM archived_events LIMIT 1"))
        except Exception:
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS archived_events (
                id TEXT PRIMARY KEY,
                message_type TEXT,
                timestamp TEXT,
                archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            )
            db.commit()

        result = db.execute(text("SELECT id FROM archived_events")).fetchall()
        return {"archived_ids": [r[0] for r in result]}

@router.post("/messages/archive")
async def archive_message(request: dict):
    """Archive a message by its ID."""
    from sqlalchemy import text

    from src.core.database import get_db

    msg_id = request.get("message_id")
    msg_type = request.get("message_type", "unknown")
    timestamp = request.get("timestamp", "")

    if not msg_id:
        raise HTTPException(400, "message_id is required")

    with get_db() as db:
        try:
            db.execute(text("SELECT 1 FROM archived_events LIMIT 1"))
        except Exception:
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS archived_events (
                id TEXT PRIMARY KEY,
                message_type TEXT,
                timestamp TEXT,
                archived_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )""")
            )
            db.commit()

        db.execute(
            text("INSERT OR IGNORE INTO archived_events (id, message_type, timestamp) VALUES (:id, :type, :ts)"),
            {"id": msg_id, "type": msg_type, "ts": timestamp},
        )
        db.commit()
    return {"archived": True}

@router.post("/messages/unarchive")
async def unarchive_message(request: dict):
    """Unarchive a message by its ID."""
    from sqlalchemy import text

    from src.core.database import get_db

    msg_id = request.get("message_id")
    if not msg_id:
        raise HTTPException(400, "message_id is required")

    with get_db() as db:
        db.execute(text("DELETE FROM archived_events WHERE id = :id"), {"id": msg_id})
        db.commit()
    return {"unarchived": True}

@router.post("/messages/unarchive-all")
async def unarchive_all_messages():
    """Unarchive all messages."""
    from sqlalchemy import text

    from src.core.database import get_db

    with get_db() as db:
        db.execute(text("DELETE FROM archived_events"))
        db.commit()
    return {"unarchived": True}

@router.post("/messages/cleanup-archives")
async def cleanup_old_archives():
    """Remove archived messages older than 30 days."""
    from sqlalchemy import text

    from src.core.database import get_db

    with get_db() as db:
        db.execute(text("DELETE FROM archived_events WHERE archived_at < datetime('now', '-30 days')"))
        db.commit()
    return {"cleaned": True}

@router.get("/logs")
async def get_logs(lines: int = Query(100, ge=1, le=2000)):
    cache_key = f"logs:{lines}"
    cached = _cached(cache_key, ttl=5.0)
    if cached is not None:
        return cached

    run_dir = _get_latest_run_dir()
    if not run_dir:
        return _store(cache_key, {"lines": []})

    log_path = run_dir / "orchestrator.log"
    if not log_path.exists():
        return _store(cache_key, {"lines": []})

    try:
        all_lines = log_path.read_text(errors="replace").splitlines()
        return _store(cache_key, {"lines": all_lines[-lines:]})
    except Exception:
        return _store(cache_key, {"lines": []})
