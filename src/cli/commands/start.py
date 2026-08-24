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
from src.core.constants import HEPHAESTUS_INSTALL_DIR, HEPHAESTUS_LOGS_DIR
from src.core.simple_config import get_config as _get_config

HEPHAESTUS_DIR = HEPHAESTUS_INSTALL_DIR
logger = logging.getLogger(__name__)


class ProcessWatchdog:
    """Monitors detached processes and restarts them if they die unexpectedly."""

    def __init__(
        self,
        check_interval: int = 30,
        max_restarts: int = 3,
        restart_window: int = 300,
        unresponsive_threshold: int = 3,
    ):
        self.check_interval = check_interval
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        # Consecutive failed /health checks before treating the backend as
        # hung rather than just slow -- at the default 30s check_interval,
        # 3 gives ~90s of grace before acting.
        self.unresponsive_threshold = unresponsive_threshold
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.restart_counts: dict[str, int] = {}
        self.last_restarts: dict[str, float] = {}
        self._restart_callbacks: dict[str, callable] = {}
        self._backend_health_failures = 0
        # Grace period (seconds) after a watchdog-initiated restart before
        # health checks resume -- the backend takes 60-70s to fully
        # initialize (LLM models, embeddings, autopilot resume). Without
        # this, the watchdog kills the backend right before it becomes
        # healthy, creating an infinite restart loop.
        self._backend_restart_grace = 120
        self._backend_last_restart = 0.0
        # Port the backend listens on -- set to the actual value by the
        # caller (run_watchdog.py) so _check_services can reconcile the
        # PID file against port listeners before restarting.
        self._backend_port: Optional[int] = None

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
                # For backend: before concluding it died, check if the port
                # is already occupied by a live (untracked) process. This
                # catches the PID-file-stale scenario: the real backend is
                # alive on the port, but the PID file points to an old
                # process. Restarting blindly would spawn a new backend that
                # hits _exit_if_port_in_use, dies within seconds, and wastes
                # the restart budget. Instead, reconcile the PID file.
                if name == "backend":
                    backend_port = getattr(self, "_backend_port", None)
                    if backend_port and self._reconcile_backend_pid(backend_port, pid):
                        continue  # PID file fixed, no restart needed
                logger.warning(f"Process {name} (PID {pid}) died unexpectedly")
                self._maybe_restart(name, callback)

    def _reconcile_backend_pid(self, port: int, stale_pid: int) -> bool:
        """If the backend PID is stale but a live process owns the port,
        update the PID file and return True. Returns False if no live
        process can be found on the port."""
        from src.cli.utils.ports import get_port_listeners

        try:
            pids = get_port_listeners(port, {"python", "uvicorn"})
        except Exception:
            return False

        if not pids:
            return False

        # Pick the oldest (lowest PID) — most likely the original backend.
        live_pid = min(pids)
        if live_pid == stale_pid:
            return False

        logger.warning(
            f"Backend PID {stale_pid} is dead but PID {live_pid} owns port "
            f"{port} — reconciling PID file (no restart needed)"
        )
        save_pid("backend", live_pid)
        # Clear any phantom restart accounting accumulated while the
        # stale PID file kept triggering false "died unexpectedly" cycles.
        self.restart_counts.pop("backend", None)
        self.last_restarts.pop("backend", None)
        self._backend_health_failures = 0
        self._backend_last_restart = 0.0
        return True

    def _kill_duplicates(self, service_name: str, pids: list[int], context: str) -> None:
        """Kill every pid in `pids` except the one tracked for `service_name`.

        Shared by the port-based (backend) and pgrep-based (monitor) checks
        below. If the tracked PID isn't among `pids` at all (e.g. a stale
        PID file), keep the lowest PID (oldest/first-started) rather than
        guessing further, and kill the rest.
        """
        tracked_pid = read_pid(service_name)
        keep = tracked_pid if tracked_pid in pids else min(pids)
        logger.warning(
            f"Found {len(pids)} {service_name} processes {context} ({pids}) -- "
            f"expected 1 (keeping PID {keep})"
        )
        import signal

        for pid in pids:
            if pid == keep:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
                logger.warning(f"Killed duplicate {service_name} process {pid} ({context})")
            except OSError as e:
                logger.debug(f"Could not kill duplicate process {pid}: {e}")

    def check_duplicate_port_listeners(self, port: int) -> None:
        """Kill any extra python process bound to `port` beyond the tracked backend.

        A second backend process racing the tracked one creates two
        independent AutopilotService singletons against the same DB -- one
        can pause a workflow the other just started, or a task can get
        assigned by one process and never picked back up by the other.
        Observed live: a standalone `python -m src.autopilot.orchestrator`
        CLI run left running for hours (its own health-self-check spuriously
        failing against a momentarily-busy backend) spawned a competing
        backend on the tracked one's port. Neither _check_services above
        (which only watches the *tracked* PID) nor the backend's own
        assume_backend_running fix (which only covers the in-process
        AutopilotService path) catches a rogue process like that -- this
        does, by looking at what's actually bound to the port instead of
        trusting any single PID-tracking mechanism.

        Uses get_port_listeners to filter both by LISTEN socket state AND
        process command name. VS Code Remote SSH also creates a LISTEN
        socket on the same port (with `node` as the command) -- killing
        that nukes the user's entire remote session.
        """
        from src.cli.utils.ports import get_port_listeners
        try:
            pids = get_port_listeners(port, {"python", "uvicorn"})
        except Exception as e:
            logger.debug(f"Duplicate-backend port check failed: {e}")
            return

        if len(pids) <= 1:
            return

        self._kill_duplicates("backend", pids, f"bound to port {port}")

    def check_duplicate_monitor_processes(self) -> None:
        """Kill any extra run_monitor.py process beyond the tracked one.

        Unlike the backend, the monitor doesn't bind a port, so this uses
        `pgrep -f` instead of `lsof`. Observed live immediately after a
        `heph restart`: two run_monitor.py processes ended up running
        simultaneously -- the CLI's own spawn_monitor and (very likely) the
        in-process AutopilotService's sdk.start() call both raced through
        is_monitor_running()'s pgrep check before either process was
        visible to the other's check yet (a plain TOCTOU race any
        check-then-spawn pattern has without a lock). Two monitors
        independently evaluating and acting on the same agents/workflows is
        the same class of problem as two backends.
        """
        try:
            result = subprocess.run(
                ["pgrep", "-f", "run_monitor.py"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            pids = [int(p) for p in result.stdout.strip().split("\n") if p.strip()]
        except Exception as e:
            logger.debug(f"Duplicate-monitor check failed: {e}")
            return

        if len(pids) <= 1:
            return

        self._kill_duplicates("monitor", pids, "running")

    def check_backend_health(self, port: int) -> None:
        """Detect a backend that's alive but not answering -- a hang plain
        PID-liveness (_check_services) can't see at all, since the process
        never exits.

        Observed live: `heph status` reported "unreachable" while the
        backend's own PID was still running and its background pipeline
        thread was still actively executing (py-spy dump showed
        run_continuous_pipeline mid-stride in its own ThreadPoolExecutor
        thread) -- consistent with the async event loop stalling on
        something like DB connection pool starvation while a long-running
        background thread holds connections, not a crash. A watchdog that
        only checks "is the PID alive" waits forever for a process that will
        never die on its own.
        """
        pid = read_pid("backend")
        if not pid or not is_process_running(pid):
            return  # _check_services' own PID-liveness check already covers this

        # Skip health checks during the grace period after a restart --
        # the backend needs 60-70s to initialize and we don't want to
        # count failures during that window.
        elapsed_since_restart = time.time() - self._backend_last_restart
        if elapsed_since_restart < self._backend_restart_grace:
            remaining = int(self._backend_restart_grace - elapsed_since_restart)
            logger.debug(
                f"Backend health check skipped -- {remaining}s left in "
                f"post-restart grace period"
            )
            return

        try:
            import httpx

            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=15)
            healthy = resp.status_code == 200 and resp.json().get("status") == "healthy"
        except Exception:
            healthy = False

        if healthy:
            self._backend_health_failures = 0
            return

        self._backend_health_failures += 1
        logger.warning(
            f"Backend health check failed ({self._backend_health_failures}/"
            f"{self.unresponsive_threshold}) -- PID {pid} alive but not "
            "answering /health"
        )
        if self._backend_health_failures < self.unresponsive_threshold:
            return

        logger.warning(
            f"Backend unresponsive for {self._backend_health_failures} "
            f"consecutive checks (~{self._backend_health_failures * self.check_interval}s) "
            "-- killing and restarting"
        )
        self._backend_health_failures = 0
        # SIGKILL, not SIGTERM: a process this unresponsive is unlikely to
        # honor a graceful shutdown signal either, and waiting to find out
        # just delays recovery further.
        import signal

        try:
            os.kill(pid, signal.SIGKILL)
        except OSError as e:
            logger.debug(f"Could not kill unresponsive backend {pid}: {e}")

        callback = self._restart_callbacks.get("backend")
        if callback:
            self._maybe_restart("backend", callback)

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
                # Record backend restart time for grace period
                if name == "backend":
                    self._backend_last_restart = now
                    self._backend_health_failures = 0
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
    read_pid("monitor")
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
        for _ in range(90):
            time.sleep(1)
            if check_backend(args):
                results["backend"] = "healthy"
                print(" ready")
                break
        else:
            results["backend"] = "started but not healthy"
            print(" timeout")
            _print_backend_error()

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

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(HEPHAESTUS_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            # run_server.py owns backend.log directly via a daily-rotating
            # handler (src/core/logging_config.py) -- a raw stdout redirect
            # here could never rotate (that has to happen from inside the
            # writing process), which is exactly how backend.log reached
            # 253MB unattended. Only output from before configure_logging()
            # runs (a handful of import lines) is invisible now; everything
            # after, including uncaught exceptions, still reaches
            # backend.log via logging_config's sys.excepthook.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach into own session — survives launcher/shell exit
        )
        # Give run_server.py's own _exit_if_port_in_use guard a moment to
        # fire and self-terminate if another backend already owns the port.
        # Without this, save_pid below unconditionally overwrites the PID
        # file with this about-to-die PID, orphaning the tracking of the
        # REAL, still-alive backend. The watchdog's next _check_services
        # cycle then sees the (already-dead) tracked PID, concludes the
        # backend "died", and spawns yet another one -- which also hits the
        # same guard and dies the same way. Observed live: a fresh
        # duplicate backend process appearing every single watchdog cycle,
        # indefinitely, once the PID file got poisoned by one bad spawn.
        time.sleep(1.0)
        if proc.poll() is not None:
            logger.warning(
                f"Backend process {proc.pid} exited immediately (code "
                f"{proc.returncode}) -- likely refused to start because "
                "another instance already owns the port. Not overwriting "
                "the tracked PID."
            )
            return False
        save_pid("backend", proc.pid)
        return True
    except Exception as e:
        print(f"Backend start error: {e}", file=sys.stderr)
        return False


def _start_monitor(python: str) -> bool:
    log_dir = Path(HEPHAESTUS_LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [python, str(HEPHAESTUS_DIR / "run_monitor.py")],
            cwd=str(HEPHAESTUS_DIR),
            stdin=subprocess.DEVNULL,
            # run_monitor.py owns monitor.log directly via a daily-rotating
            # handler (src/core/logging_config.py) -- see _start_backend's
            # identical comment for why the raw redirect was removed.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,  # detach into own session — survives launcher/shell exit (else reaped by SIGKILL)
        )
        # Same reasoning as _start_backend above: give run_monitor.py's own
        # _exit_if_already_running guard a moment to fire before trusting
        # this PID enough to overwrite the tracked one.
        time.sleep(1.0)
        if proc.poll() is not None:
            logger.warning(
                f"Monitor process {proc.pid} exited immediately (code "
                f"{proc.returncode}) -- likely refused to start because "
                "another instance is already running. Not overwriting "
                "the tracked PID."
            )
            return False
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

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(HEPHAESTUS_DIR),
            stdin=subprocess.DEVNULL,
            # run_watchdog.py owns watchdog.log directly via a daily-rotating
            # handler (src/core/logging_config.py) -- see _start_backend's
            # identical comment for why the raw redirect was removed.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,  # detach into own session — survives launcher/shell exit
        )
        save_pid("watchdog", proc.pid)
        return True
    except Exception as e:
        print(f"Watchdog start error: {e}", file=sys.stderr)
        return False


def _kill_port(port: int) -> None:
    """Kill node/npm processes LISTENing on the given port.

    Filters by command name to avoid killing VS Code Remote SSH
    port-forwarding proxies (also `node`, also LISTEN).
    """
    from src.cli.utils.ports import kill_port_listeners
    kill_port_listeners(port, {"node", "npm"})


def _start_frontend() -> bool:
    frontend_dir = HEPHAESTUS_DIR / "frontend"
    if not (frontend_dir / "package.json").exists():
        return False
    config = _get_config()
    frontend_port = config.server.frontend_port
    _kill_port(frontend_port)
    log_dir = Path(HEPHAESTUS_LOGS_DIR)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(log_dir / "frontend.log", "a")
    try:
        env = os.environ.copy()
        env["FRONTEND_PORT"] = str(frontend_port)
        env["BACKEND_PORT"] = str(config.server.mcp_port)
        proc = subprocess.Popen(
            ["npm", "run", "dev"],
            cwd=str(frontend_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            env=env,
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
    config = _get_config()
    print(f"  Frontend:  http://localhost:{config.server.frontend_port}")
    print(f"  Backend:   http://127.0.0.1:{port}")
    print(f"  Health:    http://127.0.0.1:{port}/health")


def _print_backend_error() -> None:
    """Tail the backend log and print the likely cause of a startup failure."""
    log_path = Path(HEPHAESTUS_LOGS_DIR) / "backend.log"
    if not log_path.exists():
        print()
        print(f"  Check the backend log for details:")
        print(f"    {log_path}")
        return
    try:
        lines = log_path.read_text().splitlines()
    except OSError:
        return
    tail = lines[-50:]
    error_keys = ("ERROR", "Error", "Traceback", "Exception", "NoSuchPath",
                  "FATAL", "fatal", "Set paths", "heph project")
    error_lines = [ln for ln in tail if any(k in ln for k in error_keys)]
    if error_lines:
        print()
        print("  Error details:")
        for ln in error_lines[-10:]:
            print(f"    {ln}")
        print(f"  Full log: {log_path}")
    else:
        print()
        print(f"  Check the backend log for details:")
        print(f"    {log_path}")
