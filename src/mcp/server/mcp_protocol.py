"""MCP tool + resource protocol surface.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import asyncio
import json
import logging
from typing import Any, Dict

from fastapi import (
    APIRouter,
    HTTPException,
)
from fastapi.responses import StreamingResponse

from src.core.database import Task, utc_now
from src.mcp.server._mcp_tool_registry import (
    _MCP_TOOLS,
    MCP_TOOL_NAMES,  # noqa: F401 -- re-exported, see tests/test_mcp_tool_registry.py
    MCP_TOOL_REGISTRY,
)
from src.mcp.server._shared import server_state
from src.mcp.server.devtools_tools import _DEVTOOLS_TOOLS, _handle_devtools_tool

logger = logging.getLogger("src.mcp.server.mcp_protocol")

router = APIRouter()

@router.get("/tools")
async def list_tools():
    """List available MCP tools.

    Core (non-devtools) tool entries are generated from MCP_TOOL_REGISTRY
    (defined further down, after their handlers) rather than hand-written
    here -- see MCPToolSpec's docstring for why (Phase 2 §4.10). devtools_*
    entries are still hand-written below; only their "required" list is
    unduplicated (Phase 2 §4.10), the rest of the standing hand-maintained
    duplication for those 15 tools is left for a follow-up.
    """
    core_tools = [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.input_schema,
        }
        for t in MCP_TOOL_REGISTRY
    ]
    devtools_tools = [
            {
                "name": "devtools_connect",
                "description": "Connect to Chrome DevTools Protocol for browser automation",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Session identifier for this browser connection",
                        },
                        "debug_url": {
                            "type": "string",
                            "description": "Chrome DevTools debug URL (default: http://localhost:9222)",
                        },
                        "target_url": {
                            "type": "string",
                            "description": "URL to open in a new tab (optional, connects to existing page if omitted)",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_navigate",
                "description": "Navigate the browser to a URL",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "url": {"type": "string", "description": "URL to navigate to"},
                    },
                    "required": ["session_id", "url"],
                },
            },
            {
                "name": "devtools_evaluate",
                "description": "Execute JavaScript in the browser context",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "expression": {
                            "type": "string",
                            "description": "JavaScript expression to evaluate",
                        },
                    },
                    "required": ["session_id", "expression"],
                },
            },
            {
                "name": "devtools_screenshot",
                "description": "Capture a screenshot of the current page",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "path": {
                            "type": "string",
                            "description": "File path to save screenshot (optional, returns base64 if omitted)",
                        },
                        "format": {
                            "type": "string",
                            "enum": ["png", "jpeg"],
                            "description": "Image format (default: png)",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_click",
                "description": "Click an element by CSS selector",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the element to click",
                        },
                    },
                    "required": ["session_id", "selector"],
                },
            },
            {
                "name": "devtools_fill",
                "description": "Fill an input field with a value",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for the input element",
                        },
                        "value": {"type": "string", "description": "Value to fill in"},
                    },
                    "required": ["session_id", "selector", "value"],
                },
            },
            {
                "name": "devtools_get_console_errors",
                "description": "Get console errors from the browser",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_failed_requests",
                "description": "Get failed network requests from the browser",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "status": {
                            "type": "integer",
                            "description": "Filter by HTTP status code (optional)",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_network_logs",
                "description": "Get all network request logs from the browser",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "method": {
                            "type": "string",
                            "description": "Filter by HTTP method (GET, POST, etc.)",
                        },
                        "status": {
                            "type": "integer",
                            "description": "Filter by HTTP status code",
                        },
                        "failed_only": {
                            "type": "boolean",
                            "description": "Only return failed requests",
                        },
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_performance",
                "description": "Get page performance metrics",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_get_page_info",
                "description": "Get current page title, URL, and content summary",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_check_broken_images",
                "description": "Find broken images on the page",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_wait_for_selector",
                "description": "Wait for a CSS selector to appear in the DOM",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector to wait for",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 5000)",
                        },
                    },
                    "required": ["session_id", "selector"],
                },
            },
            {
                "name": "devtools_get_cookies",
                "description": "Get all browser cookies",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID",
                        }
                    },
                    "required": ["session_id"],
                },
            },
            {
                "name": "devtools_close",
                "description": "Close the browser session",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Browser session ID to close",
                        }
                    },
                    "required": ["session_id"],
                },
            },
    ]
    # "required" above is hand-typed to keep each entry's shape readable
    # inline, but it must agree with _DEVTOOLS_TOOLS' own required_args --
    # the actual argument-presence check _handle_devtools_tool enforces at
    # dispatch time -- so it's overwritten from that one source of truth
    # here rather than trusted to stay in sync by hand (Phase 2 §4.10).
    for entry in devtools_tools:
        required_args, _handler = _DEVTOOLS_TOOLS[entry["name"]]
        entry["input_schema"]["required"] = required_args
    return {"tools": core_tools + devtools_tools}


@router.post("/tools/execute")
async def execute_tool(request: Dict[str, Any]):
    """Execute an MCP tool."""
    tool_name = request.get("tool")
    arguments = request.get("arguments", {})

    # Strip heph_ prefix if present — the MCP adapter adds the server
    # name as a prefix, but our registry uses bare names.
    if tool_name and tool_name.startswith("heph_"):
        tool_name = tool_name[5:]

    if tool_name in _MCP_TOOLS:
        return await _MCP_TOOLS[tool_name](arguments)
    elif tool_name and tool_name.startswith("devtools_"):
        return await _handle_devtools_tool(tool_name, arguments)
    else:
        raise HTTPException(status_code=400, detail=f"Unknown tool: {tool_name}")


@router.get("/resources")
async def list_resources():
    """List available MCP resources."""
    session = server_state.db_manager.get_session()
    try:
        tasks = session.query(Task).filter(Task.status != "done").all()
        return {
            "resources": [
                {
                    "uri": f"task://{task.id}",
                    "name": f"Task: {task.id[:8]}",
                    "description": (task.enriched_description or task.raw_description)[:100],
                    "mimeType": "application/json",
                }
                for task in tasks
            ]
        }
    finally:
        session.close()

@router.get("/resources/{resource_uri:path}")
async def get_resource(resource_uri: str):
    """Get a specific MCP resource."""
    if resource_uri.startswith("task://"):
        task_id = resource_uri.replace("task://", "")
        session = server_state.db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            if task:
                return {
                    "uri": resource_uri,
                    "content": {
                        "id": task.id,
                        "description": task.enriched_description or task.raw_description,
                        "status": task.status,
                        "assigned_agent": task.assigned_agent_id,
                        # SOLID review 1.10: missing the "Z" UTC suffix every
                        # other timestamp in this codebase's API responses uses.
                        "created_at": task.created_at.isoformat() + "Z" if task.created_at else None,
                    },
                }
            else:
                raise HTTPException(status_code=404, detail="Task not found")
        finally:
            session.close()
    else:
        raise HTTPException(status_code=404, detail="Resource not found")

@router.get("/sse")
async def sse_endpoint():
    """Server-Sent Events endpoint for Claude MCP integration."""

    async def event_generator():
        """Generate SSE events."""
        # Send initial connection event
        yield f"data: {json.dumps({'type': 'connected', 'message': 'Connected to Hephaestus MCP Server', 'timestamp': utc_now().isoformat()})}\n\n"

        # Create a queue for this SSE connection
        event_queue = asyncio.Queue(maxsize=100)
        server_state.sse_queues.append(event_queue)

        try:
            while True:
                # Wait for events to send
                try:
                    # Check for events with timeout to send keepalive
                    event = await asyncio.wait_for(event_queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    # Send keepalive event every 30 seconds
                    yield f"data: {json.dumps({'type': 'keepalive', 'timestamp': utc_now().isoformat()})}\n\n"
        except asyncio.CancelledError:
            # Clean up when connection is closed
            if event_queue in server_state.sse_queues:
                server_state.sse_queues.remove(event_queue)
            raise
        finally:
            # Ensure cleanup
            if event_queue in server_state.sse_queues:
                server_state.sse_queues.remove(event_queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
