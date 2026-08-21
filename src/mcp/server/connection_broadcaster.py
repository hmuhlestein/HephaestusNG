"""Fan-out to connected WebSocket/SSE clients.

Extracted from ServerState (SOLID review 1.6). Owning the set of connected
clients and pushing updates to them is a distinct responsibility from
composing and holding references to the app's service instances (db_manager,
agent_manager, and the rest of ServerState.initialize()) -- this class knows
nothing about any of those.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionBroadcaster:
    """Holds the live WebSocket/SSE client lists and broadcasts to them."""

    def __init__(self):
        self.active_websockets: List[WebSocket] = []
        self.sse_queues: List[asyncio.Queue] = []

    async def broadcast_update(
        self,
        message: Dict[str, Any],
        project_id: Optional[str] = None,
        project_name: Optional[str] = None,
    ):
        """Broadcast update to all connected WebSocket and SSE clients.

        project_id/project_name: when known, merged into the payload so
        clients can filter events by their currently-selected project
        (and label them by name) instead of rendering every project's
        activity indiscriminately -- with more than one project able to
        run concurrently, an unfiltered feed mixes together events from
        projects the viewer isn't even looking at. Broadcasting itself
        stays global (every connected client still receives every message;
        there's no per-connection project subscription to route through)
        -- this only adds the fields a client-side filter/label needs.
        Callers that don't have a project in scope (e.g. non-project-scoped
        triggers) omit these rather than guess.
        """
        if project_id:
            message = {**message, "project_id": project_id, "project_name": project_name}
        disconnected = []
        for websocket in self.active_websockets:
            try:
                await websocket.send_json(message)
            except Exception:
                disconnected.append(websocket)

        # Remove disconnected clients
        for ws in disconnected:
            self.active_websockets.remove(ws)

        # Send to SSE clients
        for queue in self.sse_queues:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                logger.warning("SSE queue full, skipping event")
