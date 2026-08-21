#!/usr/bin/env python3
"""Main entry point for running the Hephaestus MCP server."""

import logging
import os
import subprocess
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

import uvicorn

from src.core.constants import HEPHAESTUS_LOGS_DIR
from src.core.logging_config import configure_logging
from src.core.simple_config import get_config

# Configure logging using shared helper (L-2 fix). Logs to backend.log
# directly via a daily-rotating handler -- start.py no longer redirects
# this process's stdout to that same path (rotation has to happen from
# inside the writing process; a second FileHandler here would also just
# duplicate every line into a stray hephaestus_server.log at the repo root).
configure_logging(log_file=str(Path(HEPHAESTUS_LOGS_DIR) / "backend.log"))

logger = logging.getLogger(__name__)


def _exit_if_port_in_use(port: int) -> None:
    """Refuse to start if another process is already bound to `port`.

    Two backend processes can coexist on the "same" port without an
    "address already in use" error if they end up bound to different
    addresses (observed live: one on `*:8300`, another on `localhost:8300`)
    -- the OS doesn't reject the second bind in that case. Each would then
    drive its own independent AutopilotService singleton against the same
    DB, racing each other (one can pause a workflow the other just
    started). The watchdog's periodic duplicate-port check (see
    src/cli/commands/start.py's check_duplicate_port_listeners) cleans up
    an extra instance after the fact, but that leaves a window where both
    are live. Checking here, before uvicorn ever binds, closes it to
    effectively zero -- mirrors run_monitor.py's _exit_if_already_running.

    Must filter to LISTEN sockets only (-sTCP:LISTEN) -- a plain
    `lsof -ti :port` also matches outbound CLIENT connections to that port
    (e.g. an in-flight request from a curl call, the frontend's Vite proxy,
    or the monitor's health polling). Without the filter, this check could
    see a legitimate in-flight client request and conclude "another backend
    already owns this port" when no server is running there at all yet --
    refusing to start a legitimate restart over nothing.
    """
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
    except Exception:
        return  # can't check (no lsof, etc.) -- fail open rather than block startup

    other_pids = [p for p in pids if p != os.getpid()]
    if other_pids:
        logger.error(
            f"Another process is already bound to port {port} "
            f"(PID(s) {other_pids}) -- refusing to start a second backend. Exiting."
        )
        sys.exit(1)


def main():
    """Run the MCP server."""
    config = get_config()
    _exit_if_port_in_use(config.server.mcp_port)

    logger.info("Starting Hephaestus MCP Server")
    logger.info(f"Server will run on {config.server.mcp_host}:{config.server.mcp_port}")
    logger.info(f"Using LLM provider: {config.llm.llm_provider}")
    logger.info(f"Using model: {config.llm.llm_model}")

    # Validate configuration
    try:
        config.validate()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)

    # Run the server
    try:
        uvicorn.run(
            "src.mcp.server:app",
            host=config.server.mcp_host,
            port=config.server.mcp_port,
            reload=False,
            workers=1,
            log_level="info" if not config.server.debug else "debug",
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
