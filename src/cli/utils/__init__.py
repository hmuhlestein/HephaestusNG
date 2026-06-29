"""Shared CLI utilities."""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from src.core.constants import HEPHAESTUS_PIDS_DIR

PID_DIR = Path(HEPHAESTUS_PIDS_DIR)


def api_get(args, endpoint: str, timeout: int = 5) -> Optional[Dict]:
    """GET request to the Hephaestus API."""
    try:
        r = httpx.get(f"{args.api_base}{endpoint}", timeout=timeout)
        if r.status_code == 200:
            return r.json()
        return {"error": r.status_code, "detail": r.text[:200]}
    except httpx.ConnectError:
        return None
    except Exception as e:
        return {"error": str(e)}


def api_post(
    args, endpoint: str, data: dict = None, timeout: int = 10
) -> Optional[Dict]:
    """POST request to the Hephaestus API."""
    try:
        r = httpx.post(f"{args.api_base}{endpoint}", json=data or {}, timeout=timeout)
        if r.status_code in (200, 201):
            return r.json()
        return {"error": r.status_code, "detail": r.text[:200]}
    except httpx.ConnectError:
        return None
    except Exception as e:
        return {"error": str(e)}


def api_delete(args, endpoint: str, timeout: int = 5) -> Optional[Dict]:
    """DELETE request to the Hephaestus API."""
    try:
        r = httpx.delete(f"{args.api_base}{endpoint}", timeout=timeout)
        if r.status_code in (200, 204):
            return r.json() if r.text else {"status": "deleted"}
        return {"error": r.status_code, "detail": r.text[:200]}
    except httpx.ConnectError:
        return None
    except Exception as e:
        return {"error": str(e)}


def output(args, data, human_formatter=None):
    """Output data as JSON or human-readable."""
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    elif human_formatter:
        human_formatter(data)
    else:
        print(json.dumps(data, indent=2, default=str))


def check_backend(args) -> bool:
    """Check if backend is reachable and healthy."""
    result = api_get(args, "/health")
    if result is None:
        return False
    status = result.get("status", "")
    return status == "healthy"


def require_backend(args) -> bool:
    """Require backend to be running, print error if not."""
    if not check_backend(args):
        if args.json:
            print(
                json.dumps(
                    {"error": "Backend not running", "hint": "Run 'heph start' first"}
                )
            )
        else:
            print(
                "Error: Backend not running. Run 'heph start' first.", file=sys.stderr
            )
        return False
    return True


def save_pid(name: str, pid: int):
    """Save a process PID to disk."""
    PID_DIR.mkdir(parents=True, exist_ok=True)
    (PID_DIR / f"{name}.pid").write_text(str(pid))


def read_pid(name: str) -> Optional[int]:
    """Read a saved PID."""
    pid_file = PID_DIR / f"{name}.pid"
    if pid_file.exists():
        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None
    return None


def remove_pid(name: str):
    """Remove a saved PID file."""
    pid_file = PID_DIR / f"{name}.pid"
    if pid_file.exists():
        pid_file.unlink()


def is_process_running(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    import os

    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_monitor_running() -> bool:
    """Check if a monitor process is actually running (not just a reused PID)."""
    import subprocess

    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_monitor.py"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def is_backend_running() -> bool:
    """Check if a backend process is actually running (not just a reused PID)."""
    import subprocess

    try:
        result = subprocess.run(
            ["pgrep", "-f", "run_server.py"], capture_output=True, text=True, timeout=5
        )
        return result.returncode == 0
    except Exception:
        return False


def truncate(s: str, max_len: int = 80) -> str:
    """Truncate string to max length."""
    if not s:
        return ""
    return s[:max_len] + "..." if len(s) > max_len else s


def table(headers: List[str], rows: List[List[str]], indent: int = 0):
    """Print a simple table."""
    if not rows:
        print("  (none)")
        return

    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(col_widths):
                col_widths[i] = max(col_widths[i], len(str(cell)))

    prefix = " " * indent
    header_line = "  ".join(str(h).ljust(col_widths[i]) for i, h in enumerate(headers))
    print(f"{prefix}{header_line}")
    print(f"{prefix}{'  '.join('-' * w for w in col_widths)}")

    for row in rows:
        line = "  ".join(str(cell).ljust(col_widths[i]) for i, cell in enumerate(row))
        print(f"{prefix}{line}")


def status_icon(status: str) -> str:
    """Return a text icon for a status."""
    status = (status or "").lower()
    if status in (
        "done",
        "completed",
        "healthy",
        "running",
        "pass",
        "passed",
        "validated",
    ):
        return "OK"
    elif status in ("failed", "error", "crashed", "unhealthy"):
        return "FAIL"
    elif status in ("in_progress", "working", "active", "pending"):
        return "..."
    else:
        return "?"


def time_ago(iso_str: str) -> str:
    """Convert ISO timestamp to relative time."""
    if not iso_str:
        return "never"
    try:
        from datetime import datetime, timezone

        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = (now - dt).total_seconds()
        if diff < 60:
            return f"{int(diff)}s ago"
        elif diff < 3600:
            return f"{int(diff // 60)}m ago"
        elif diff < 86400:
            return f"{int(diff // 3600)}h ago"
        else:
            return f"{int(diff // 86400)}d ago"
    except Exception:
        return str(iso_str)[:16]
