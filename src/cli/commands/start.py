"""heph start — Start Hephaestus services."""

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from src.cli.utils import (
    check_backend,
    is_monitor_running,
    is_process_running,
    read_pid,
    save_pid,
)
from src.core.constants import HEPHAESTUS_LOGS_DIR

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent
logger = logging.getLogger(__name__)


class ProcessWatchdog:
    """Monitors detached processes and restarts them if they die unexpectedly."""

    def __init__(self, check_interval: int = 30, max_restarts: int = 3, restart_window: int = 300):
        self.check_interval = check_interval
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.restart_counts: dict[str, int] = {}
        self.last_restarts: dict[str, float] = {}
        self._restart_callbacks: dict[str, callable] = {}

    def register_service(self, name: str, restart_callback: callable) -> None:
        """Register a service with its restart callback."""
        self._restart_callbacks[name] = restart_callback

    def start(self) -> None:
        """Start the watchdog thread."""
        if self.thread is not None:
            return
        self.running = True
        self.thread = threading.Thread(target=self._watchdog_loop, daemon=True, name="ProcessWatchdog")
        self.thread.start()
        logger.info("Process watchdog started")

    def stop(self) -> None:
        """Stop the watchdog thread."""
        self.running = False
        if self.thread:
            self.thread.join(timeout=self.check_interval + 5)
            self.thread = None
        logger.info("Process watchdog stopped")

    def _watchdog_loop(self) -> None:
        """Main watchdog loop."""
        while self.running:
            time.sleep(self.check_interval)
            if not self.running:
                break
            self._check_services()

    def _check_services(self) -> None:
        """Check all registered services and restart if needed."""
        for name, callback in self._restart_callbacks.items():
            pid = read_pid(name)
            if pid and not is_process_running(pid):
                logger.warning(f"Process {name} (PID {pid}) died unexpectedly")
                self._maybe_restart(name, callback)

    def _maybe_restart(self, name: str, callback: callable) -> None:
        """Restart a service if restart limits allow it."""
        now = time.time()
        restart_count = self.restart_counts.get(name, 0)
        last_restart = self.last_restarts.get(name, 0)

        # Reset count if outside restart window
        if now - last_restart > self.restart_window:
            restart_count = 0

        if restart_count >= self.max_restarts:
            logger.error(f"Process {name} exceeded max restarts ({self.max_restarts}), not restarting")
            return

        try:
            logger.info(f"Attempting to restart {name} (attempt {restart_count + 1}/{self.max_restarts})...")
            success = callback()
            if success:
                self.restart_counts[name] = restart_count + 1
                self.last_restarts[name] = now
                logger.info(f"Successfully restarted {name}")
            else:
                logger.error(f"Failed to restart {name}")
        except Exception as e:
            logger.error(f"Failed to restart {name}: {e}")


def register(subparsers):
    p = subparsers.add_parser("start", help="Start Hephaestus services")
    p.add_argument("--backend-only", action="store_true", help="Start only the backend")
    p.add_argument("--no-monitor", action="store_true", help="Skip monitor")
    p.add_argument("--no-frontend", action="store_true", help="Skip frontend dashboard")
    p.add_argument("--no-watchdog", action="store_true", help="Disable process auto-restart")
    p.add_argument(
        "--reload", action="store_true", help="Enable auto-reload for development"
    )
    p.set_defaults(func=run)


def run(args):
    port = args.port
    results = {}

    # Check what's already running
    backend_running = check_backend(args)
    frontend_pid = read_pid("frontend")
    frontend_running = frontend_pid and is_process_running(frontend_pid)
    monitor_pid = read_pid("monitor")
    monitor_running = is_monitor_running()

    # Frontend (start first so output shows it first)
    if not args.backend_only and not args.no_frontend:
        if frontend_running:
            results["frontend"] = "already running"
        else:
            frontend_proc = _start_frontend()
            results["frontend"] = "started" if frontend_proc else "skipped"

    # Qdrant
    vector_backend = os.environ.get("VECTOR_STORE_BACKEND", "turbovec")
    if vector_backend == "qdrant":
        if _check_qdrant():
            results["qdrant"] = "already running"
        else:
            qdrant_ok = _ensure_qdrant()
            results["qdrant"] = "running" if qdrant_ok else "failed"
    else:
        results["qdrant"] = "skipped (turbovec)"

    # Backend
    if backend_running:
        results["backend"] = "already running"
    else:
        python = _find_python(HEPHAESTUS_DIR)
        if not python:
            print(
                "Error: Python not found. Run 'poetry install' first.", file=sys.stderr
            )
            return 1

        backend_proc = _start_backend(python, port, args.reload)
        if not backend_proc:
            results["backend"] = "failed"
            _print_results(results, port)
            return 1

        print(f"Waiting for backend on port {port}...", end="", flush=True)
        for _ in range(30):
            time.sleep(1)
            if check_backend(args):
                results["backend"] = "healthy"
                print(" ready")
                break
        else:
            results["backend"] = "started but not healthy"
            print(" timeout")

    # Monitor
    if not args.backend_only and not args.no_monitor:
        if monitor_running:
            results["monitor"] = "already running"
        else:
            python = _find_python(HEPHAESTUS_DIR)
            monitor_proc = _start_monitor(python)
            results["monitor"] = "started" if monitor_proc else "failed"

    # Start process watchdog for auto-restart (H-4)
    # Runs as its own detached subprocess (like backend/monitor/frontend) —
    # a thread inside this short-lived CLI process would die the moment
    # `heph start` returns and never actually supervise anything.
    if not getattr(args, 'no_watchdog', False):
        watchdog_pid = read_pid("watchdog")
        if watchdog_pid and is_process_running(watchdog_pid):
            results["watchdog"] = "already running"
        else:
            watchdog_proc = _start_watchdog(port, args)
            results["watchdog"] = "started" if watchdog_proc else "failed"

    _print_results(results, port)
    return 0


def _find_python(project_dir: Path) -> str:
    venv_python = project_dir / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


def _check_qdrant() -> bool:
    import httpx

    try:
        r = httpx.get("http://localhost:6333/", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _ensure_qdrant() -> bool:
    """Ensure Qdrant is running (Docker or local)."""
    import httpx

    try:
        r = httpx.get("http://localhost:6333/", timeout=2)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    # Try starting existing container
    try:
        subprocess.run(["docker", "start", "qdrant"], capture_output=True, timeout=10)
        time.sleep(3)
        r = httpx.get("http://localhost:6333/", timeout=2)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    # Try creating new container
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "-p",
                "6333:6333",
                "--name",
                "qdrant",
                "qdrant/qdrant",
            ],
            capture_output=True,
            timeout=30,
        )
        time.sleep(5)
        r = httpx.get("http://localhost:6333/", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _start_backend(python: str, port: int, reload: bool) -> bool:
    env = os.environ.copy()
    env["MCP_PORT"] = str(port)

    cmd = [python, str(HEPHAESTUS_DIR / "run_server.py")]
    if reload:
        cmd.append("--reload")

    log_dir = Path(HEPHAESTUS_LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "backend.log", "a")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(HEPHAESTUS_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach into own session — survives launcher/shell exit
        )
        save_pid("backend", proc.pid)
        return True
    except Exception as e:
        print(f"Backend start error: {e}", file=sys.stderr)
        return False


def _start_monitor(python: str) -> bool:
    log_dir = Path(HEPHAESTUS_LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "monitor.log", "a")
    try:
        proc = subprocess.Popen(
            [python, str(HEPHAESTUS_DIR / "run_monitor.py")],
            cwd=str(HEPHAESTUS_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach into own session — survives launcher/shell exit (else reaped by SIGKILL)
        )
        save_pid("monitor", proc.pid)
        return True
    except Exception as e:
        print(f"Monitor start error: {e}", file=sys.stderr)
        return False


def _start_watchdog(port: int, args) -> bool:
    python = _find_python(HEPHAESTUS_DIR)
    cmd = [python, str(HEPHAESTUS_DIR / "run_watchdog.py"), "--port", str(port)]
    if getattr(args, "backend_only", False):
        cmd.append("--backend-only")
    if getattr(args, "no_monitor", False):
        cmd.append("--no-monitor")
    if getattr(args, "reload", False):
        cmd.append("--reload")

    log_dir = Path(HEPHAESTUS_LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "watchdog.log", "a")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(HEPHAESTUS_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach into own session — survives launcher/shell exit
        )
        save_pid("watchdog", proc.pid)
        return True
    except Exception as e:
        print(f"Watchdog start error: {e}", file=sys.stderr)
        return False


def _kill_port(port: int) -> None:
    """Kill any process using the given port."""
    import signal

    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip():
            for pid_str in result.stdout.strip().split("\n"):
                try:
                    pid = int(pid_str)
                    os.kill(pid, signal.SIGKILL)
                except (ValueError, OSError):
                    pass
            time.sleep(1)  # Wait for port to free
    except Exception:
        pass


def _start_frontend() -> bool:
    frontend_dir = HEPHAESTUS_DIR / "frontend"
    if not (frontend_dir / "package.json").exists():
        return False
    # Ensure port 5173 is free
    _kill_port(5173)
    log_dir = Path(HEPHAESTUS_LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "frontend.log", "a")
    try:
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=str(frontend_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach into own session — survives launcher/shell exit
        )
        save_pid("frontend", proc.pid)
        return True
    except FileNotFoundError:
        print("npm not found. Install Node.js to run the frontend.", file=sys.stderr)
        return False
    except Exception as e:
        print(f"Frontend start error: {e}", file=sys.stderr)
        return False


def _print_results(results, port):
    print()
    for service, status in results.items():
        if status == "already running":
            icon = "OK"
        elif status in ("running", "healthy", "started"):
            icon = "OK"
        elif status == "started but not healthy":
            icon = "..."
        elif status.startswith("skipped"):
            icon = "--"
        else:
            icon = "FAIL"
        print(f"  {service:12s} {icon:4s} {status}")
    print()
    print("  Frontend:  http://localhost:5173")
    print(f"  Backend:   http://127.0.0.1:{port}")
    print(f"  Health:    http://127.0.0.1:{port}/health")
