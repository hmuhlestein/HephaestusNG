"""heph autopilot — Autopilot pipeline control."""

import os
from pathlib import Path

from src.cli.utils import output
from src.core.constants import DESIGN_CONTEXT_SUBDIR


def register(subparsers):
    p = subparsers.add_parser("autopilot", help="Autopilot pipeline control")
    sub = p.add_subparsers(dest="subcommand")

    # start
    s = sub.add_parser("start", help="Start the autopilot pipeline")
    s.add_argument("--project-path", "-p", required=True, help="Project directory")
    s.add_argument(
        "--design-queue", help="Design queue directory (default: <project>/.hephaestus/designs)"
    )
    s.add_argument(
        "--max-iterations", type=int, default=3, help="Max iterations per design"
    )
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
    import requests

    project_path = Path(args.project_path).resolve()
    design_queue = args.design_queue or str(project_path / DESIGN_CONTEXT_SUBDIR)

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

    print(f"Project:      {project_path}")
    print(f"Design queue: {design_queue}")
    print(f"Iterations:   {args.max_iterations}")
    print()

    # Call the API to start the pipeline (single spawn path)
    try:
        resp = requests.post(
            "http://127.0.0.1:8300/api/autopilot/start",
            params={
                "project_path": str(project_path),
                "design_queue": str(design_queue),
                "max_iterations": args.max_iterations,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            print("Autopilot pipeline started!")
            print(f"Project: {data.get('project', str(project_path))}")
            print()
            print("Press Ctrl+C to stop.")
            print()
        else:
            print(f"Error: {resp.status_code} - {resp.text}")
            return 1
    except requests.exceptions.ConnectionError:
        print("Error: Backend not running. Start it with: heph start")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1

    # Block until pipeline stops or user presses Ctrl+C
    import time

    try:
        while True:
            time.sleep(5)
            try:
                status_resp = requests.get(
                    "http://127.0.0.1:8300/api/autopilot/status",
                    timeout=5,
                )
                if status_resp.status_code == 200:
                    status = status_resp.json()
                    if not status.get("running", False):
                        print("\nPipeline finished.")
                        return 0
            except Exception:
                pass
    except KeyboardInterrupt:
        print("\nStopping pipeline...")
        try:
            requests.post("http://127.0.0.1:8300/api/autopilot/stop", timeout=30)
            print("Pipeline stopped.")
        except Exception as e:
            print(f"Error stopping: {e}")
        return 0


def stop_pipeline(args):
    import requests

    print("Stopping autopilot pipeline...")

    try:
        resp = requests.post(
            "http://127.0.0.1:8300/api/autopilot/stop",
            json={"clear_state": False},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            agents = data.get("agents_terminated", 0)
            print(f"Autopilot stopped: {agents} agents terminated")
            return 0
        else:
            print(f"Error: {resp.status_code} - {resp.text}")
            return 1
    except requests.exceptions.ConnectionError:
        print("Error: Backend not running.")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


def pipeline_status(args):
    import requests

    try:
        resp = requests.get("http://127.0.0.1:8300/api/autopilot/status", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            output(args, data, _print_pipeline_status)
            return 0
        else:
            print(f"Error: {resp.status_code} - {resp.text}")
            return 1
    except requests.exceptions.ConnectionError:
        print("Error: Backend not running. Start it with: heph start")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


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
    queue_dir = Path(args.project_path) / DESIGN_CONTEXT_SUBDIR
    if not queue_dir.exists():
        print(f"Queue directory not found: {queue_dir}")
        return 0

    designs = []
    for ext in ("*.md", "*.txt"):
        for f in sorted(queue_dir.glob(ext)):
            designs.append(
                {
                    "name": f.stem,
                    "file": f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                }
            )

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
    """Add a design document to the queue.

    Resolves the file to an absolute path and calls POST /api/autopilot/designs/add.
    Does NOT copy the file - stores the file_path in the database.
    """
    source = Path(args.file).resolve()
    if not source.exists():
        print(f"File not found: {source}")
        return 1

    import requests

    try:
        resp = requests.post(
            "http://127.0.0.1:8300/api/autopilot/designs/add",
            json={
                "file_path": str(source),
                "project_path": str(Path(args.project_path).resolve()),
            },
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json()
            print(f"Added to queue: {data.get('name', source.name)}")
            print(f"  ID: {data.get('id')}")
            print(f"  Status: {data.get('status')}")
            return 0
        elif resp.status_code == 409:
            print(f"Design already in queue: {source.name}")
            return 0
        else:
            print(f"Error: {resp.status_code} - {resp.text}")
            return 1
    except requests.exceptions.ConnectionError:
        print("Error: Backend not running. Start it with: heph start")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1
