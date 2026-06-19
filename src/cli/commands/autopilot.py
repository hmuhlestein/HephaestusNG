"""heph autopilot — Autopilot pipeline control."""

import os
import sys
import subprocess
from pathlib import Path

from src.cli.utils import output

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent


def register(subparsers):
    p = subparsers.add_parser("autopilot", help="Autopilot pipeline control")
    sub = p.add_subparsers(dest="subcommand")

    # start
    s = sub.add_parser("start", help="Start the autopilot pipeline")
    s.add_argument("--project-path", "-p", required=True, help="Project directory")
    s.add_argument("--design-queue", help="Design queue directory (default: <project>/docs/design-queue)")
    s.add_argument("--max-iterations", type=int, default=3, help="Max iterations per design")
    s.add_argument("--drop-db", action="store_true", help="Drop database first")
    s.set_defaults(func=start_pipeline)

    # stop
    st = sub.add_parser("stop", help="Stop the autopilot pipeline")
    st.set_defaults(func=stop_pipeline)

    # status
    stat = sub.add_parser("status", help="Show autopilot status")
    stat.set_defaults(func=pipeline_status)

    # queue
    q = sub.add_parser("queue", help="Show design queue contents")
    q.add_argument("--project-path", "-p", required=True, help="Project directory")
    q.set_defaults(func=show_queue)

    # add
    a = sub.add_parser("add", help="Add a design document to the queue")
    a.add_argument("file", help="Design document file (.md, .txt)")
    a.add_argument("--project-path", "-p", required=True, help="Project directory")
    a.set_defaults(func=add_to_queue)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def start_pipeline(args):
    project_path = Path(args.project_path).resolve()
    design_queue = args.design_queue or str(project_path / "docs" / "design-queue")

    if not project_path.exists():
        print(f"Error: Project path does not exist: {project_path}")
        return 1

    # Verify it's a git repo (required for worktree-based agent isolation)
    git_dir = project_path / ".git"
    if not git_dir.exists():
        print(f"Error: Project path is not a git repository: {project_path}")
        print(f"Run 'git init' in {project_path} first.")
        return 1

    os.makedirs(design_queue, exist_ok=True)

    python = str(HEPHAESTUS_DIR / ".venv" / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable

    cmd = [
        python, "-m", "src.autopilot.orchestrator",
        "--project-path", str(project_path),
        "--design-queue", str(design_queue),
        "--max-iterations", str(args.max_iterations),
    ]
    if args.drop_db:
        cmd.append("--drop-db")

    print(f"Project:      {project_path}")
    print(f"Design queue: {design_queue}")
    print(f"Iterations:   {args.max_iterations}")
    print()
    print("Starting autopilot pipeline (Ctrl+C to stop)...")
    print()

    try:
        proc = subprocess.Popen(cmd, cwd=str(HEPHAESTUS_DIR))
        from src.cli.utils import save_pid
        save_pid("orchestrator", proc.pid)
        proc.wait()
        return proc.returncode or 0
    except KeyboardInterrupt:
        print("\nStopped.")
        proc.terminate()
        return 0


def stop_pipeline(args):
    import signal as sig
    import requests
    from src.cli.utils import read_pid, remove_pid, is_process_running

    # First, try to stop via API (properly pauses workflows and terminates agents)
    try:
        resp = requests.post("http://127.0.0.1:8300/api/autopilot/stop", json={"clear_state": True}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            print(f"Autopilot stopped: {data.get('agents_terminated', 0)} agents terminated, workflows paused")
        else:
            print(f"API stop returned {resp.status_code}: {resp.text}")
    except requests.exceptions.ConnectionError:
        print("Backend not running, killing orchestrator directly")
    except Exception as e:
        print(f"API stop failed: {e}")

    # Also terminate any remaining active agents
    try:
        resp = requests.get("http://127.0.0.1:8300/api/agents", timeout=5)
        if resp.status_code == 200:
            agents = resp.json().get("agents", resp.json()) if isinstance(resp.json(), dict) else resp.json()
            active = [a for a in agents if isinstance(a, dict) and a.get("status") in ("working", "starting", "idle")]
            for agent in active:
                agent_id = agent.get("id", "")
                try:
                    requests.post("http://127.0.0.1:8300/api/terminate_agent", json={"agent_id": agent_id}, timeout=5)
                    print(f"  Terminated agent {agent_id[:8]}")
                except Exception:
                    pass
    except Exception:
        pass

    # Also kill orchestrator process if still running
    pid = read_pid("orchestrator")
    if pid and is_process_running(pid):
        try:
            os.kill(pid, sig.SIGTERM)
            import time
            for _ in range(5):
                time.sleep(0.5)
                if not is_process_running(pid):
                    break
            else:
                if is_process_running(pid):
                    os.kill(pid, sig.SIGKILL)
            print(f"Killed orchestrator process (pid {pid})")
        except OSError as e:
            print(f"Error killing orchestrator: {e}")
        finally:
            remove_pid("orchestrator")
    else:
        remove_pid("orchestrator")

    # Clear pipeline state
    try:
        from pathlib import Path
        state_dir = Path.home() / ".hephaestus" / "autopilot"
        (state_dir / "pipeline_state.json").unlink(missing_ok=True)
        (state_dir / "orchestrator.pid").unlink(missing_ok=True)
        (state_dir / "orchestrator_agent_id").unlink(missing_ok=True)
    except Exception:
        pass

    return 0


def pipeline_status(args):
    # Check if orchestrator is running
    import subprocess
    r = subprocess.run(["pgrep", "-f", "orchestrator.py"], capture_output=True, text=True)
    running = r.returncode == 0

    data = {"running": running}
    if running:
        data["pid"] = r.stdout.strip()

    # Check for state file
    state_files = sorted(Path.home().glob(".hephaestus/autopilot/run-*/state.json"), reverse=True)
    if state_files:
        import json
        try:
            state = json.loads(state_files[0].read_text())
            data["latest_run"] = state
            data["state_file"] = str(state_files[0])
        except Exception:
            pass

    output(args, data, _print_pipeline_status)
    return 0


def _print_pipeline_status(data):
    if data.get("running"):
        print(f"Autopilot: RUNNING (pid {data.get('pid', '?')})")
    else:
        print("Autopilot: NOT RUNNING")

    run = data.get("latest_run")
    if run:
        print()
        print("Latest run:")
        print(f"  Processed:  {run.get('designs_processed', 0)}")
        print(f"  Succeeded:  {run.get('designs_succeeded', 0)}")
        print(f"  Failed:     {run.get('designs_failed', 0)}")
        print(f"  Current:    {run.get('current_design', 'none')}")
        qs = run.get("queue_status", {})
        print(f"  Queue:      {qs.get('status', '?')}")


def show_queue(args):
    queue_dir = Path(args.project_path) / "docs" / "design-queue"
    if not queue_dir.exists():
        print(f"Queue directory not found: {queue_dir}")
        return 0

    designs = []
    for ext in ("*.md", "*.txt"):
        for f in sorted(queue_dir.glob(ext)):
            designs.append({
                "name": f.stem,
                "file": f.name,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })

    output(args, designs, _print_queue)
    return 0


def _print_queue(designs):
    if not designs:
        print("Design queue is empty.")
        print("Add designs: heph autopilot add <file> --project-path <path>")
        return
    print(f"Design queue ({len(designs)} items):")
    for d in designs:
        print(f"  {d['name']:40s}  ({d['size']} bytes)")


def add_to_queue(args):
    source = Path(args.file).resolve()
    if not source.exists():
        print(f"File not found: {source}")
        return 1

    queue_dir = Path(args.project_path) / "docs" / "design-queue"
    queue_dir.mkdir(parents=True, exist_ok=True)

    dest = queue_dir / source.name
    if dest.exists():
        print(f"Already in queue: {dest.name}")
        return 0

    import shutil
    shutil.copy2(source, dest)
    print(f"Added to queue: {dest.name}")
    return 0
