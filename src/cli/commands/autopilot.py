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
        "--design-queue", help="Design queue directory (default: <project>/.hephaestus/specs)"
    )
    s.add_argument(
        "--max-iterations", type=int, default=3, help="Max iterations per design"
    )
    s.add_argument("--drop-db", action="store_true", help="Drop database first")
    s.add_argument("--feature", help="Spec Kit feature to build (NNN or NNN-name)")
    s.add_argument("--repo", help="Repo label to disambiguate --feature in a multi-repo project")
    s.set_defaults(func=start_pipeline)

    # check
    c = sub.add_parser("check", help="Voluntary Spec Kit feature readiness check (never blocks start)")
    c.add_argument("--project-path", "-p", required=True, help="Project directory")
    c.add_argument("--feature", help="Spec Kit feature to check (NNN or NNN-name); omit to check all")
    c.add_argument("--repo", help="Repo label to disambiguate --feature in a multi-repo project")
    c.set_defaults(func=check_speckit_readiness)

    # stop
    st = sub.add_parser("stop", help="Stop the autopilot pipeline")
    st.add_argument(
        "--project-path", "-p",
        help="Only stop this project (default: stop every currently running project)",
    )
    st.set_defaults(func=stop_pipeline)

    # status
    stat = sub.add_parser("status", help="Show autopilot status")
    stat.add_argument(
        "--project-path", "-p",
        help="Show status for this project only (default: summary across every running project)",
    )
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


def _resolve_project_id_by_path(project_path, api_base):
    """Resolve an AutopilotProject's id from its base_dir (realpath-
    resolved). Returns None if not found or the backend is unreachable --
    callers should decide what "not found" means for them (e.g. stop
    falls back to stopping every running project, matching /stop's own
    documented behavior when project_id is omitted)."""
    import requests

    try:
        resp = requests.get(f"{api_base}/api/autopilot/projects", timeout=5)
        if resp.status_code != 200:
            return None
        resolved = str(Path(project_path).resolve())
        for proj in resp.json():
            if str(Path(proj["base_dir"]).resolve()) == resolved:
                return proj["id"]
    except Exception:
        pass
    return None


def _print_speckit_selection_error(resp):
    """Render a 422 {code, message, candidates} body as readable text
    instead of a raw JSON blob (REQ-10's "error listing available
    directories")."""
    try:
        detail = resp.json().get("detail", {})
    except Exception:
        detail = {}
    message = detail.get("message") or resp.text
    candidates = detail.get("candidates") or []
    print(f"Error: {message}")
    if candidates:
        print("Available options:")
        for c in candidates:
            print(f"  - {c}")


def check_speckit_readiness(args):
    import requests

    project_path = Path(args.project_path).resolve()
    params = {"project_path": str(project_path)}
    if getattr(args, "feature", None):
        params["feature"] = args.feature
    if getattr(args, "repo", None):
        params["repo"] = args.repo

    try:
        resp = requests.get(f"{args.api_base}/api/autopilot/speckit/check", params=params, timeout=10)
    except requests.exceptions.ConnectionError:
        print("Error: Backend not running. Start it with: heph start")
        return 0
    except Exception as e:
        print(f"Error: {e}")
        return 0

    if resp.status_code == 422:
        _print_speckit_selection_error(resp)
        return 0
    if resp.status_code != 200:
        print(f"Error: {resp.status_code} - {resp.text}")
        return 0

    data = resp.json()
    features = data.get("features", [])
    if not data.get("multi_repo_scan", True):
        print("Note: project not yet registered -- single-repo scan only.")
    if not features:
        print("No Spec Kit features found.")
        return 0
    for f in features:
        label = f"{f['number']}-{f['slug']}" + (f" (repo: {f['repo_label']})" if f.get("repo_label") else "")
        print(f"{label}:")
        if f["missing_files"]:
            print(f"  Missing: {', '.join(f['missing_files'])}")
        if f["needs_clarification"]:
            print(f"  NEEDS CLARIFICATION ({len(f['needs_clarification'])}):")
            for marker in f["needs_clarification"]:
                print(f"    - {marker}")
        if not f["missing_files"] and not f["needs_clarification"]:
            print("  Ready.")
    return 0  # voluntary check -- never fails the command (REQ-15)


def start_pipeline(args):
    import requests

    project_path = Path(args.project_path).resolve()
    design_queue = args.design_queue or str(project_path / DESIGN_CONTEXT_SUBDIR)

    # Same rule (and wording) as POST /autopilot/start, which this posts to
    # -- see git_repo_error. No project_id to resolve the multi-repo exemption
    # with here, so a workspace root is left to the API's own check, which
    # runs after _get_or_create_project_id and can see its ProjectRepo rows.
    from src.core.repo_resolution import git_repo_error

    repo_problem = git_repo_error(project_path, allow_workspace_root=True)
    if repo_problem:
        print(f"Error: {repo_problem}")
        return 1

    os.makedirs(design_queue, exist_ok=True)

    print(f"Project:      {project_path}")
    print(f"Design queue: {design_queue}")
    print(f"Iterations:   {args.max_iterations}")
    print()

    # Call the API to start the pipeline (single spawn path)
    try:
        params = {
            "project_path": str(project_path),
            "design_queue": str(design_queue),
            "max_iterations": args.max_iterations,
        }
        feature = getattr(args, "feature", None)
        if feature:
            params["feature"] = feature
            repo = getattr(args, "repo", None)
            if repo:
                params["repo"] = repo

        resp = requests.post(
            f"{args.api_base}/api/autopilot/start",
            params=params,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            print("Autopilot pipeline started!")
            print(f"Project: {data.get('project', str(project_path))}")
            print()
            print("Press Ctrl+C to stop.")
            print()
        elif resp.status_code == 422:
            _print_speckit_selection_error(resp)
            return 1
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
                    f"{args.api_base}/api/autopilot/status",
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
            # Scope to just this project -- /stop with no project_id stops
            # EVERY currently running project (no single global service to
            # fall back to now that projects run concurrently), which would
            # silently kill an unrelated project's pipeline Ctrl+C here was
            # never meant to touch.
            stop_project_id = _resolve_project_id_by_path(
                project_path, args.api_base
            )
            requests.post(
                f"{args.api_base}/api/autopilot/stop",
                # /stop's clear_state/project_id are bare scalar params, not
                # a Pydantic body model -- FastAPI binds those from the
                # query string, not JSON body.
                params={"project_id": stop_project_id} if stop_project_id else {},
                timeout=30,
            )
            print("Pipeline stopped.")
        except Exception as e:
            print(f"Error stopping: {e}")
        return 0


def stop_pipeline(args):
    import requests

    params = {"clear_state": False}
    project_path = getattr(args, "project_path", None)
    if project_path:
        project_id = _resolve_project_id_by_path(project_path, args.api_base)
        if not project_id:
            print(f"Error: No registered project found for path: {project_path}")
            return 1
        params["project_id"] = project_id
        print(f"Stopping autopilot pipeline for {project_path}...")
    else:
        print("Stopping every running autopilot pipeline...")

    try:
        # /stop's clear_state/project_id are bare scalar params, not a
        # Pydantic body model -- FastAPI binds those from the query
        # string, not JSON body.
        resp = requests.post(
            f"{args.api_base}/api/autopilot/stop",
            params=params,
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

    params = {}
    project_path = getattr(args, "project_path", None)
    if project_path:
        project_id = _resolve_project_id_by_path(project_path, args.api_base)
        if not project_id:
            print(f"Error: No registered project found for path: {project_path}")
            return 1
        params["project_id"] = project_id

    try:
        resp = requests.get(
            f"{args.api_base}/api/autopilot/status", params=params, timeout=5
        )
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

    # Only present for the global (no --project-path) status check --
    # lists EVERY currently running project, not just the one this
    # response's other fields (current_design, etc.) happen to reflect.
    running_projects = data.get("running_projects") or []
    if running_projects:
        print()
        print("Running projects:")
        for proj in running_projects:
            print(f"  - {proj.get('name') or proj.get('base_dir')}")

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
    """Show the design queue, via the backend's DB-authoritative /queue
    endpoint -- matching add_to_queue's own pattern of calling the backend
    API rather than globbing the filesystem directly. A plain filesystem
    glob here would disagree with what /designs/add actually recorded: a
    design's file_path can point anywhere on disk, not just inside
    .hephaestus/specs/.
    """
    import requests

    project_id = _resolve_project_id_by_path(args.project_path, args.api_base)
    if not project_id:
        print(f"Error: No registered project found for path: {args.project_path}")
        return 1

    try:
        resp = requests.get(
            f"{args.api_base}/api/autopilot/queue",
            params={"project_id": project_id},
            timeout=10,
        )
        if resp.status_code == 200:
            output(args, resp.json(), _print_queue)
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


def _print_queue(designs):
    if not designs:
        print("Design queue is empty.")
        print("Add designs: heph autopilot add <file> --project-path <path>")
        return
    print(f"Design queue ({len(designs)} items):")
    for d in designs:
        print(f"  {d['name']:40s}  ({d['size_bytes']} bytes)")


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
            f"{args.api_base}/api/autopilot/designs/add",
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