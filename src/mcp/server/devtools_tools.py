"""Devtools-bridge tool handlers, referenced by mcp_protocol's tool dispatch.

Extracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).
"""

import asyncio
import logging
from typing import Any, Dict

from fastapi import (
    HTTPException,
)

# Import routers at module level for test compatibility

logger = logging.getLogger("src.mcp.server.devtools_tools")

async def _devtools_connect(arguments: Dict[str, Any], session_id: str):
    from src.mcp.devtools import devtools_manager

    debug_url = arguments.get("debug_url", "http://localhost:9222")
    target_url = arguments.get("target_url")
    if target_url:
        browser = await devtools_manager.connect_new_tab(session_id, target_url, debug_url)
    else:
        browser = await devtools_manager.connect(session_id, debug_url)
    version = await browser.get_version()
    return {
        "success": True,
        "session_id": session_id,
        "browser": version.get("Browser", "unknown"),
    }

async def _devtools_navigate(browser, arguments):
    result = await browser.navigate(arguments["url"])
    return {"success": True, "result": result}

async def _devtools_evaluate(browser, arguments):
    result = await browser.evaluate(arguments["expression"])
    return {"success": True, "result": result}

async def _devtools_screenshot(browser, arguments):
    path = arguments.get("path")
    fmt = arguments.get("format", "png")
    data = await browser.screenshot(path=path, format=fmt)
    return {"success": True, "data_length": len(data) if data else 0, "saved_to": path}

async def _devtools_click(browser, arguments):
    await browser.click(arguments["selector"])
    return {"success": True}

async def _devtools_fill(browser, arguments):
    await browser.fill(arguments["selector"], arguments["value"])
    return {"success": True}

async def _devtools_get_console_errors(browser, arguments):
    errors = await browser.check_console_errors()
    return {"success": True, "errors": errors, "count": len(errors)}

async def _devtools_get_failed_requests(browser, arguments):
    logs = await browser.get_network_logs(failed_only=True, status=arguments.get("status"))
    return {"success": True, "failed_requests": logs, "count": len(logs)}

async def _devtools_get_network_logs(browser, arguments):
    logs = await browser.get_network_logs(
        method=arguments.get("method"),
        status=arguments.get("status"),
        failed_only=arguments.get("failed_only", False),
    )
    return {"success": True, "logs": logs, "count": len(logs)}

async def _devtools_get_performance(browser, arguments):
    metrics = await browser.get_performance_metrics()
    return {"success": True, "metrics": metrics}

async def _devtools_get_page_info(browser, arguments):
    title, url = await asyncio.gather(browser.get_page_title(), browser.get_page_url())
    return {"success": True, "title": title, "url": url}

async def _devtools_check_broken_images(browser, arguments):
    broken = await browser.check_broken_images()
    return {"success": True, "broken_images": broken, "count": len(broken)}

async def _devtools_wait_for_selector(browser, arguments):
    found = await browser.wait_for_selector(arguments["selector"], timeout_ms=arguments.get("timeout_ms", 5000))
    return {"success": True, "found": found}

async def _devtools_get_cookies(browser, arguments):
    cookies = await browser.get_cookies()
    return {"success": True, "cookies": cookies, "count": len(cookies)}

async def _devtools_close(browser, arguments, session_id: str):
    from src.mcp.devtools import devtools_manager

    await devtools_manager.close(session_id)
    return {"success": True, "message": f"Session '{session_id}' closed"}

_DEVTOOLS_TOOLS: Dict[str, tuple] = {
    "devtools_connect": ([], _devtools_connect),
    "devtools_navigate": (["url"], _devtools_navigate),
    "devtools_evaluate": (["expression"], _devtools_evaluate),
    "devtools_screenshot": ([], _devtools_screenshot),
    "devtools_click": (["selector"], _devtools_click),
    "devtools_fill": (["selector", "value"], _devtools_fill),
    "devtools_get_console_errors": ([], _devtools_get_console_errors),
    "devtools_get_failed_requests": ([], _devtools_get_failed_requests),
    "devtools_get_network_logs": ([], _devtools_get_network_logs),
    "devtools_get_performance": ([], _devtools_get_performance),
    "devtools_get_page_info": ([], _devtools_get_page_info),
    "devtools_check_broken_images": ([], _devtools_check_broken_images),
    "devtools_wait_for_selector": (["selector"], _devtools_wait_for_selector),
    "devtools_get_cookies": ([], _devtools_get_cookies),
    "devtools_close": ([], _devtools_close),
}

async def _handle_devtools_tool(tool_name: str, arguments: Dict[str, Any]):
    from src.mcp.devtools import devtools_manager, validate_session_id

    entry = _DEVTOOLS_TOOLS.get(tool_name)
    if entry is None:
        raise HTTPException(status_code=400, detail=f"Unknown devtools tool: {tool_name}")
    required, handler = entry

    missing = [k for k in required if k not in arguments]
    if missing:
        raise HTTPException(status_code=400, detail=f"Missing required arguments: {', '.join(missing)}")

    raw_session = arguments.get("session_id", "default")
    try:
        session_id = validate_session_id(raw_session)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        if tool_name == "devtools_connect":
            return await handler(arguments, session_id)

        browser = devtools_manager.get(session_id)
        if not browser:
            raise HTTPException(
                status_code=404,
                detail=f"No browser session '{session_id}'. Call devtools_connect first.",
            )

        if tool_name == "devtools_close":
            return await handler(browser, arguments, session_id)

        return await handler(browser, arguments)

    except HTTPException:
        raise
    except (KeyError, TypeError) as e:
        raise HTTPException(status_code=400, detail=f"Invalid arguments for {tool_name}: {e}")
    except Exception as e:
        logger.error(f"DevTools tool error: {tool_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"DevTools error: {str(e)}")
