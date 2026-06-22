"""heph start — Start Hephaestus services."""

import os
import sys
import time
import subprocess
from pathlib import Path

from src.cli.utils import check_backend, save_pid, read_pid, is_process_running, is_monitor_running, is_backend_running

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
            print("Error: Python not found. Run 'poetry install' first.", file=sys.stderr)
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


def _kill_port(port: int) -> None:
    """Kill any process using the given port."""
    import signal
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=5,
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
    log_dir = Path.home() / ".hephaestus" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "frontend.log", "a")
    try:
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=str(frontend_dir),
            stdin=subprocess.DEVNULL,
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
