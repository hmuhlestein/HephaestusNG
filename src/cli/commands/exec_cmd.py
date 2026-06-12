"""heph exec — Execute commands and interact with services."""

import argparse
import os
import sys
import time
import json
import subprocess
from pathlib import Path
import httpx
from src.cli.utils import api_get, api_post, output, table


LOG_DIR = Path.home() / ".hephaestus" / "logs"


def register(subparsers):
    p = subparsers.add_parser("exec", help="Execute commands and interact with services")
    sub = p.add_subparsers(dest="subcommand")

    # run
    rn = sub.add_parser("run", help="Run a shell command and capture output")
    rn.add_argument("command", nargs=argparse.REMAINDER, help="Command to execute")
    rn.add_argument("--timeout", type=int, default=120, help="Timeout in seconds (default: 120)")
    rn.add_argument("--cwd", default=None, help="Working directory")
    rn.add_argument("--log", default=None, help="Log file path (default: ~/.hephaestus/logs/exec-<ts>.log)")
    rn.set_defaults(func=run_command)

    # ping
    pg = sub.add_parser("ping", help="Ping the backend")
    pg.set_defaults(func=ping)

    # tool
    tl = sub.add_parser("tool", help="Execute an MCP tool directly")
    tl.add_argument("tool_name", help="MCP tool name")
    tl.add_argument("--args", dest="tool_args", default="{}", help="Tool arguments (JSON)")
    tl.set_defaults(func=exec_tool)

    # endpoints
    ep = sub.add_parser("endpoints", help="List all API endpoints")
    ep.set_defaults(func=list_endpoints)

    # raw
    raw = sub.add_parser("raw", help="Make a raw API request")
    raw.add_argument("method", choices=["GET", "POST", "PUT", "DELETE"])
    raw.add_argument("path", help="API path (e.g. /api/tasks)")
    raw.add_argument("--data", default=None, help="Request body (JSON)")
    raw.set_defaults(func=raw_request)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def run_command(args):
    """Run a shell command, capturing stdout+stderr to a unified log file."""
    if not args.command:
        print("Error: No command specified.", file=sys.stderr)
        return 1

    cmd_str = " ".join(args.command)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = Path(args.log) if args.log else LOG_DIR / f"exec-{int(time.time())}.log"

    env = os.environ.copy()
    env["HEPH_LOG_DIR"] = str(LOG_DIR)

    print(f"Running: {cmd_str}")
    print(f"Log:     {log_path}")

    start = time.time()
    try:
        proc = subprocess.run(
            args.command,
            cwd=args.cwd,
            capture_output=True,
            text=True,
            timeout=args.timeout,
            env=env,
        )
        elapsed = time.time() - start

        with open(log_path, "w") as f:
            f.write(f"$ {cmd_str}\n")
            f.write(f"exit code: {proc.returncode}\n")
            f.write(f"elapsed: {elapsed:.2f}s\n")
            f.write("-" * 60 + "\n")
            f.write("STDOUT:\n")
            f.write(proc.stdout or "(empty)\n")
            f.write("-" * 60 + "\n")
            f.write("STDERR:\n")
            f.write(proc.stderr or "(empty)\n")

        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)

        print(f"\nExit: {proc.returncode} ({elapsed:.1f}s) — log: {log_path}")
        return proc.returncode

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        with open(log_path, "w") as f:
            f.write(f"$ {cmd_str}\n")
            f.write(f"exit code: TIMEOUT\n")
            f.write(f"elapsed: {elapsed:.2f}s\n")
            f.write(f"killed after {args.timeout}s timeout\n")
        print(f"Timeout: killed after {args.timeout}s — log: {log_path}")
        return 124
    except FileNotFoundError:
        print(f"Error: Command not found: {args.command[0]}", file=sys.stderr)
        return 127
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def ping(args):
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/health", timeout=5)
        elapsed = time.time() - start
        data = {"status": r.status_code, "latency_ms": round(elapsed * 1000, 1)}
        output(args, data, lambda d: print(f"Pong: {d['status']} ({d['latency_ms']}ms)"))
    except Exception as e:
        output(args, {"error": str(e)}, lambda d: print(f"Unreachable: {d['error']}"))
        return 1
    return 0


def exec_tool(args):
    import json
    try:
        tool_args = json.loads(args.tool_args)
    except json.JSONDecodeError:
        print(f"Error: --args must be valid JSON", file=sys.stderr)
        return 1

    data = api_post(args, "/tools/execute", {
        "tool_name": args.tool_name,
        "arguments": tool_args,
    })
    output(args, data, lambda d: print(json.dumps(d, indent=2)))
    return 0


def list_endpoints(args):
    data = api_get(args, "/tools")
    if isinstance(data, list):
        tools = data
    elif isinstance(data, dict):
        tools = data.get("tools", [])
    else:
        tools = []

    if args.json:
        import json
        print(json.dumps(tools, indent=2))
    else:
        if not tools:
            print("No tools registered.")
            return 0
        rows = [[t.get("name", ""), t.get("description", "")[:60]] for t in tools]
        table(["Tool", "Description"], rows)
    return 0


def raw_request(args):
    import json

    # Validate path doesn't traverse outside API
    if ".." in args.path:
        print("Error: Path traversal not allowed", file=sys.stderr)
        return 1

    try:
        if args.method == "GET":
            r = httpx.get(f"{args.api_base}{args.path}", timeout=10)
        elif args.method == "POST":
            body = json.loads(args.data) if args.data else {}
            r = httpx.post(f"{args.api_base}{args.path}", json=body, timeout=10)
        elif args.method == "PUT":
            body = json.loads(args.data) if args.data else {}
            r = httpx.put(f"{args.api_base}{args.path}", json=body, timeout=10)
        elif args.method == "DELETE":
            r = httpx.delete(f"{args.api_base}{args.path}", timeout=10)
        else:
            print(f"Unsupported method: {args.method}")
            return 1

        print(f"Status: {r.status_code}")
        try:
            print(json.dumps(r.json(), indent=2))
        except Exception:
            print(r.text[:500])
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0
