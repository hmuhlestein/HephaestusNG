"""heph start — Start Hephaestus services."""

import os
import sys
import time
import subprocess
from pathlib import Path

from src.cli.utils import output, check_backend, save_pid, read_pid, is_process_running

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent


def register(subparsers):
    p = subparsers.add_parser("start", help="Start Hephaestus services")
    p.add_argument("--backend-only", action="store_true", help="Start only the backend")
    p.add_argument("--no-monitor", action="store_true", help="Skip monitor")
    p.add_argument("--no-frontend", action="store_true", help="Skip frontend dashboard")
    p.add_argument("--reload", action="store_true", help="Enable auto-reload for development")
    p.set_defaults(func=run)


def run(args):
    port = args.port

    if check_backend(args):
        output(args, {"status": "already_running", "port": port},
               lambda d: print(f"Backend already running on port {port}"))
        return 0

    python = _find_python(HEPHAESTUS_DIR)
    if not python:
        print("Error: Python not found. Run 'poetry install' first.", file=sys.stderr)
        return 1

    results = {}

    # Start Qdrant (skip if using turbovec)
    vector_backend = os.environ.get("VECTOR_STORE_BACKEND", "turbovec")
    if vector_backend == "qdrant":
        qdrant_ok = _ensure_qdrant()
        results["qdrant"] = "running" if qdrant_ok else "failed"
    else:
        results["qdrant"] = "skipped (turbovec)"

    # Start backend
    backend_proc = _start_backend(python, port, args.reload)
    results["backend"] = "started" if backend_proc else "failed"

    if not backend_proc:
        output(args, results, lambda d: print("Failed to start backend"))
        return 1

    # Wait for backend
    print(f"Waiting for backend on port {port}...")
    for _ in range(30):
        time.sleep(1)
        if check_backend(args):
            results["backend"] = "healthy"
            break
    else:
        results["backend"] = "started_but_not_healthy"

    # Start monitor
    if not args.backend_only and not args.no_monitor:
        monitor_proc = _start_monitor(python)
        results["monitor"] = "started" if monitor_proc else "failed"

    # Start frontend
    if not args.backend_only and not args.no_frontend:
        frontend_proc = _start_frontend()
        results["frontend"] = "started" if frontend_proc else "skipped"

    output(args, results, lambda d: _print_results(d, port))
    return 0


def _find_python(project_dir: Path) -> str:
    venv_python = project_dir / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return sys.executable


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
        subprocess.run(
            ["docker", "start", "qdrant"],
            capture_output=True, timeout=10
        )
        time.sleep(3)
        r = httpx.get("http://localhost:6333/", timeout=2)
        if r.status_code == 200:
            return True
    except Exception:
        pass

    # Try creating new container
    try:
        subprocess.run(
            ["docker", "run", "-d", "-p", "6333:6333", "--name", "qdrant", "qdrant/qdrant"],
            capture_output=True, timeout=30
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

    log_dir = Path.home() / ".hephaestus" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "backend.log", "a")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(HEPHAESTUS_DIR),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        save_pid("backend", proc.pid)
        return True
    except Exception as e:
        print(f"Backend start error: {e}", file=sys.stderr)
        return False


def _start_monitor(python: str) -> bool:
    log_dir = Path.home() / ".hephaestus" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "monitor.log", "a")
    try:
        proc = subprocess.Popen(
            [python, str(HEPHAESTUS_DIR / "run_monitor.py")],
            cwd=str(HEPHAESTUS_DIR),
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        save_pid("monitor", proc.pid)
        return True
    except Exception as e:
        print(f"Monitor start error: {e}", file=sys.stderr)
        return False


def _start_frontend() -> bool:
    frontend_dir = HEPHAESTUS_DIR / "frontend"
    if not (frontend_dir / "package.json").exists():
        return False
    log_dir = Path.home() / ".hephaestus" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "frontend.log", "a")
    try:
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=str(frontend_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
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
    for service, status in results.items():
        icon = "OK" if status in ("running", "healthy", "started") else \
               "..." if status == "started_but_not_healthy" else \
               "SKIP" if status == "skipped" else "FAIL"
        print(f"  {service}: {icon} {status}")
    print()
    print(f"Backend: http://127.0.0.1:{port}")
    print(f"Health:  http://127.0.0.1:{port}/health")
