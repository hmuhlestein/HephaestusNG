"""Chrome DevTools Protocol (CDP) client for browser automation."""

import asyncio
import base64
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

import httpx
import websockets
import websockets.asyncio.client

logger = logging.getLogger(__name__)

MAX_LOG_ENTRIES = 5000
CONNECT_TIMEOUT = 10
SEND_TIMEOUT = 30
MAX_SESSION_ID_LEN = 128
SESSION_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")
SCREENSHOT_DIR = os.path.join(os.getcwd(), "data", "screenshots")


@dataclass
class ConsoleEntry:
    level: str
    text: str
    timestamp: float
    url: str = ""
    line_number: int = 0


@dataclass
class NetworkEntry:
    request_id: str
    url: str
    method: str
    status: int = 0
    resource_type: str = ""
    request_headers: Dict[str, str] = field(default_factory=dict)
    response_headers: Dict[str, str] = field(default_factory=dict)
    duration_ms: float = 0
    timestamp: float = 0


@dataclass
class PerformanceMetrics:
    dom_content_loaded: float = 0
    load_event: float = 0
    first_paint: float = 0
    first_contentful_paint: float = 0
    largest_contentful_paint: float = 0
    total_blocking_time: float = 0
    cumulative_layout_shift: float = 0
    navigation_start: float = 0


def validate_session_id(session_id: str) -> str:
    if not session_id or len(session_id) > MAX_SESSION_ID_LEN:
        raise ValueError(
            f"session_id must be 1-{MAX_SESSION_ID_LEN} alphanumeric/hyphen/underscore chars"
        )
    if not SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"session_id contains invalid characters: {session_id!r}")
    return session_id


def _safe_js_string(value: str) -> str:
    return json.dumps(value)


class CDPBrowser:
    """Chrome DevTools Protocol browser automation client.

    Uses a single reader task with a response dispatch table so that
    command responses and CDP events are handled without race conditions.
    """

    def __init__(self, debug_url: str = "http://localhost:9222"):
        self.debug_url = debug_url.rstrip("/")
        self.ws_url: Optional[str] = None
        self.ws: Optional[websockets.asyncio.client.ClientConnection] = None
        self.msg_id = 0
        self.console_logs: deque[ConsoleEntry] = deque(maxlen=MAX_LOG_ENTRIES)
        self.network_logs: deque[NetworkEntry] = deque(maxlen=MAX_LOG_ENTRIES)
        self.console_callbacks: List[Callable] = []
        self.network_callbacks: List[Callable] = []
        self._event_task: Optional[asyncio.Task] = None
        self._connected = False
        self._response_futures: Dict[int, asyncio.Future] = {}
        self._pending_event_ids: Set[int] = set()

    async def _open_websocket(
        self, url: str
    ) -> websockets.asyncio.client.ClientConnection:
        return await websockets.asyncio.client.connect(
            url,
            max_size=50 * 1024 * 1024,
            open_timeout=CONNECT_TIMEOUT,
        )

    async def discover_targets(self) -> List[Dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.debug_url}/json")
            resp.raise_for_status()
            return resp.json()

    async def get_version(self) -> Dict[str, Any]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.debug_url}/json/version")
            resp.raise_for_status()
            return resp.json()

    async def connect(self, target_index: int = 0) -> None:
        if self._connected:
            await self.close()
        targets = await self.discover_targets()
        page_targets = [t for t in targets if t.get("type") == "page"]
        if not page_targets:
            raise RuntimeError(
                "No page targets found. Is Chrome running with --remote-debugging-port?"
            )
        if target_index >= len(page_targets):
            raise IndexError(
                f"target_index {target_index} out of range (found {len(page_targets)} pages)"
            )
        target = page_targets[target_index]
        self.ws_url = target["webSocketDebuggerUrl"]
        self.ws = await self._open_websocket(self.ws_url)
        self._connected = True
        self._event_task = asyncio.create_task(self._reader_loop())
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("Console.enable")
        await self.send("Network.enable")
        logger.info("Connected to Chrome CDP: %s", self.ws_url)

    async def connect_to_url(self, url: str) -> None:
        if self._connected:
            await self.close()
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.debug_url}/json/new?{url}")
            resp.raise_for_status()
            target = resp.json()
        self.ws_url = target["webSocketDebuggerUrl"]
        self.ws = await self._open_websocket(self.ws_url)
        self._connected = True
        self._event_task = asyncio.create_task(self._reader_loop())
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        await self.send("Console.enable")
        await self.send("Network.enable")
        logger.info("Connected to new tab: %s", url)

    async def _reader_loop(self) -> None:
        """Single reader that dispatches responses vs events."""
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                msg_id = msg.get("id")
                if msg_id is not None and msg_id in self._response_futures:
                    fut = self._response_futures.pop(msg_id)
                    if not fut.done():
                        fut.set_result(msg)
                else:
                    self._dispatch_event(msg)
        except websockets.ConnectionClosed:
            for fut in self._response_futures.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("WebSocket closed"))
            self._response_futures.clear()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Unexpected error in CDP reader loop")

    def _dispatch_event(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method", "")
        params = msg.get("params", {})

        if method == "Console.messageAdded":
            message = params.get("message", {})
            entry = ConsoleEntry(
                level=message.get("level", "info"),
                text=message.get("text", ""),
                timestamp=time.time(),
                url=message.get("source", ""),
                line_number=message.get("line", 0),
            )
            self.console_logs.append(entry)
            for cb in self.console_callbacks:
                try:
                    cb(entry)
                except Exception:
                    logger.exception("Console callback error")

        elif method == "Network.requestWillBeSent":
            req = params.get("request", {})
            net_entry = NetworkEntry(
                request_id=params.get("requestId", ""),
                url=req.get("url", ""),
                method=req.get("method", "GET"),
                request_headers=req.get("headers", {}),
                resource_type=params.get("type", ""),
                timestamp=params.get("timestamp", 0),
            )
            self.network_logs.append(net_entry)
            for cb in self.network_callbacks:
                try:
                    cb(net_entry)
                except Exception:
                    logger.exception("Network callback error")

        elif method == "Network.responseReceived":
            resp_data = params.get("response", {})
            req_id = params.get("requestId", "")
            # net_entry, not entry: `entry` is already bound to a ConsoleEntry
            # earlier in this function, and Python scopes it to the whole
            # function -- so reusing the name here made every NetworkEntry
            # field access below look like a ConsoleEntry attribute error.
            for net_entry in reversed(self.network_logs):
                if net_entry.request_id == req_id:
                    net_entry.status = resp_data.get("status", 0)
                    net_entry.response_headers = resp_data.get("headers", {})
                    break

        elif method == "Network.loadingFinished":
            req_id = params.get("requestId", "")
            ts = params.get("timestamp", 0)
            for net_entry in reversed(self.network_logs):
                if net_entry.request_id == req_id and net_entry.duration_ms == 0:
                    net_entry.duration_ms = (ts - net_entry.timestamp) * 1000
                    break

    @property
    def is_connected(self) -> bool:
        # close_code, not .open: websockets removed ClientConnection.open in
        # its asyncio client (16.x is what's pinned here), so this raised
        # AttributeError exactly when the connection WAS live -- the two
        # earlier terms short-circuit while disconnected, so the crash only
        # surfaced once _connected was True and self.ws was set. close_code
        # stays None until the close handshake and exists on both the legacy
        # protocol and the current client.
        return self._connected and self.ws is not None and self.ws.close_code is None

    async def send(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if not self.ws or not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")
        self.msg_id += 1
        current_id = self.msg_id
        msg: Dict[str, Any] = {"id": current_id, "method": method}
        if params:
            msg["params"] = params

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._response_futures[current_id] = fut

        try:
            await self.ws.send(json.dumps(msg))
            raw = await asyncio.wait_for(fut, timeout=SEND_TIMEOUT)
        except asyncio.TimeoutError:
            self._response_futures.pop(current_id, None)
            raise RuntimeError(f"CDP command timed out after {SEND_TIMEOUT}s: {method}")

        if "error" in raw:
            raise RuntimeError(f"CDP error on {method}: {raw['error']}")
        return raw.get("result", {})

    async def navigate(self, url: str, timeout_ms: int = 30000) -> Dict[str, Any]:
        load_event = asyncio.Event()

        def on_load(msg: Dict[str, Any]):
            if msg.get("method") == "Page.loadEventFired":
                load_event.set()

        self.console_callbacks.append(on_load)
        try:
            await self.send("Page.navigate", {"url": url})
            try:
                await asyncio.wait_for(load_event.wait(), timeout=timeout_ms / 1000)
            except asyncio.TimeoutError:
                logger.warning("Page load timed out after %dms for %s", timeout_ms, url)
            return {"url": url, "status": "navigated"}
        finally:
            if on_load in self.console_callbacks:
                self.console_callbacks.remove(on_load)

    async def evaluate(self, expression: str, return_by_value: bool = True) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": return_by_value,
                "awaitPromise": True,
            },
        )
        remote = result.get("result", {})
        if remote.get("type") == "undefined":
            return None
        if return_by_value:
            return remote.get("value")
        return remote

    async def screenshot(
        self, path: Optional[str] = None, format: str = "png", quality: int = 80
    ) -> str:
        params: Dict[str, Any] = {"format": format}
        if format == "jpeg":
            params["quality"] = quality
        result = await self.send("Page.captureScreenshot", params)
        data = result.get("data", "")
        if path:
            abs_path = os.path.abspath(path)
            os.makedirs(os.path.dirname(abs_path) or ".", exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(base64.b64decode(data))
        return data

    async def get_page_title(self) -> str:
        return await self.evaluate("document.title") or ""

    async def get_page_url(self) -> str:
        return await self.evaluate("window.location.href") or ""

    async def get_page_html(self) -> str:
        return await self.evaluate("document.documentElement.outerHTML") or ""

    async def click(self, selector: str) -> None:
        js_selector = _safe_js_string(selector)
        await self.evaluate(f"document.querySelector({js_selector}).click()")

    async def fill(self, selector: str, value: str) -> None:
        js_selector = _safe_js_string(selector)
        js_value = _safe_js_string(value)
        await self.evaluate(f"""
            const el = document.querySelector({js_selector});
            el.value = {js_value};
            el.dispatchEvent(new Event('input', {{ bubbles: true }}));
            el.dispatchEvent(new Event('change', {{ bubbles: true }}));
        """)

    async def wait_for_selector(self, selector: str, timeout_ms: int = 5000) -> bool:
        asyncio.Event()
        js_selector = _safe_js_string(selector)
        observer_js = f"""
            (() => {{
                if (document.querySelector({js_selector})) {{
                    window.__heph_selector_found = true;
                    return;
                }}
                window.__heph_selector_found = false;
                const obs = new MutationObserver(() => {{
                    if (document.querySelector({js_selector})) {{
                        window.__heph_selector_found = true;
                        obs.disconnect();
                    }}
                }});
                obs.observe(document.body, {{ childList: true, subtree: true }});
            }})()
        """
        await self.evaluate(observer_js)

        start = time.monotonic()
        while (time.monotonic() - start) * 1000 < timeout_ms:
            result = await self.evaluate("window.__heph_selector_found === true")
            if result:
                return True
            await asyncio.sleep(0.05)
        return False

    async def get_console_logs(
        self, level: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        logs = list(self.console_logs)
        if level:
            logs = [entry for entry in logs if entry.level == level]
        return [
            {
                "level": entry.level,
                "text": entry.text,
                "timestamp": entry.timestamp,
                "url": entry.url,
                "line": entry.line_number,
            }
            for entry in logs
        ]

    async def get_network_logs(
        self,
        method: Optional[str] = None,
        status: Optional[int] = None,
        failed_only: bool = False,
    ) -> List[Dict[str, Any]]:
        logs = list(self.network_logs)
        if method:
            logs = [entry for entry in logs if entry.method.upper() == method.upper()]
        if status is not None:
            logs = [entry for entry in logs if entry.status == status]
        if failed_only:
            logs = [entry for entry in logs if entry.status >= 400 or entry.status == 0]
        return [
            {
                "url": entry.url,
                "method": entry.method,
                "status": entry.status,
                "resource_type": entry.resource_type,
                "duration_ms": round(entry.duration_ms, 2),
                "request_id": entry.request_id,
            }
            for entry in logs
        ]

    async def check_console_errors(self) -> List[Dict[str, Any]]:
        return await self.get_console_logs(level="error")

    async def check_failed_requests(self) -> List[Dict[str, Any]]:
        return await self.get_network_logs(failed_only=True)

    async def get_performance_metrics(self) -> Dict[str, Any]:
        raw = await self.evaluate("""
            (() => {
                const perf = performance;
                const entries = perf.getEntriesByType('navigation');
                const nav = entries[0] || {};
                const paint = perf.getEntriesByType('paint');
                const fcp = paint.find(e => e.name === 'first-contentful-paint');
                const fp = paint.find(e => e.name === 'first-paint');
                return {
                    dom_content_loaded: nav.domContentLoadedEventEnd || 0,
                    load_event: nav.loadEventEnd || 0,
                    first_paint: fp ? fp.startTime : 0,
                    first_contentful_paint: fcp ? fcp.startTime : 0,
                    navigation_start: nav.startTime || 0,
                };
            })()
        """)
        return raw or {}

    async def get_accessibility_snapshot(self) -> Dict[str, Any]:
        return await self.send("Accessibility.getFullAXTree")

    async def get_cookies(self) -> List[Dict[str, Any]]:
        result = await self.send("Network.getCookies")
        return result.get("cookies", [])

    async def set_cookie(
        self, name: str, value: str, domain: str = "", path: str = "/"
    ) -> None:
        params: Dict[str, Any] = {"name": name, "value": value, "path": path}
        if domain:
            params["domain"] = domain
        await self.send("Network.setCookie", params)

    async def clear_cookies(self) -> None:
        await self.send("Network.clearBrowserCookies")

    async def execute_script(self, script: str) -> Any:
        return await self.evaluate(script)

    async def get_dom_content(self, selector: str = "body") -> str:
        js_selector = _safe_js_string(selector)
        return (
            await self.evaluate(
                f"document.querySelector({js_selector})?.innerHTML || ''"
            )
            or ""
        )

    async def check_broken_images(self) -> List[Dict[str, Any]]:
        raw = await self.evaluate("""
            Array.from(document.querySelectorAll('img'))
                .filter(img => !img.complete || img.naturalWidth === 0)
                .map(img => ({ src: img.src, alt: img.alt }))
        """)
        return raw or []

    async def close(self) -> None:
        self._connected = False
        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass
            self._event_task = None
        for fut in self._response_futures.values():
            if not fut.done():
                fut.set_exception(ConnectionError("CDP connection closing"))
        self._response_futures.clear()
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        self.console_logs.clear()
        self.network_logs.clear()
        self.console_callbacks.clear()
        self.network_callbacks.clear()
        logger.info("CDP connection closed")


class DevToolsManager:
    """Manages multiple CDP browser connections."""

    def __init__(self):
        self._browsers: Dict[str, CDPBrowser] = {}

    async def connect(
        self, session_id: str, debug_url: str = "http://localhost:9222"
    ) -> CDPBrowser:
        session_id = validate_session_id(session_id)
        if session_id in self._browsers:
            await self.close(session_id)
        browser = CDPBrowser(debug_url)
        await browser.connect()
        self._browsers[session_id] = browser
        return browser

    async def connect_new_tab(
        self, session_id: str, url: str, debug_url: str = "http://localhost:9222"
    ) -> CDPBrowser:
        session_id = validate_session_id(session_id)
        if session_id in self._browsers:
            await self.close(session_id)
        browser = CDPBrowser(debug_url)
        await browser.connect_to_url(url)
        self._browsers[session_id] = browser
        return browser

    def get(self, session_id: str) -> Optional[CDPBrowser]:
        browser = self._browsers.get(session_id)
        if browser and not browser.is_connected:
            self._browsers.pop(session_id, None)
            return None
        return browser

    async def close(self, session_id: str) -> None:
        browser = self._browsers.pop(session_id, None)
        if browser:
            await browser.close()

    async def close_all(self) -> None:
        for session_id in list(self._browsers.keys()):
            await self.close(session_id)


devtools_manager = DevToolsManager()
