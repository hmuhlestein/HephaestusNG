"""heph exec — Test and execute services."""

import sys
import time
import httpx
from src.cli.utils import api_get, api_post, output, table


def register(subparsers):
    p = subparsers.add_parser("exec", help="Test and execute services")
    sub = p.add_subparsers(dest="subcommand")

    # test health
    t = sub.add_parser("test", help="Run health checks against services")
    t.add_argument("--timeout", type=int, default=5, help="Request timeout")
    t.set_defaults(func=test_services)

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


def test_services(args):
    """Run health checks against all services."""
    results = []

    # Backend
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/health", timeout=args.timeout)
        elapsed = time.time() - start
        results.append(["Backend", f"{r.status_code}", f"{elapsed*1000:.0f}ms", "OK" if r.status_code == 200 else r.text[:40]])
    except Exception as e:
        results.append(["Backend", "ERR", "-", str(e)[:50]])

    # Qdrant
    start = time.time()
    try:
        r = httpx.get("http://localhost:6333/", timeout=args.timeout)
        elapsed = time.time() - start
        results.append(["Qdrant", f"{r.status_code}", f"{elapsed*1000:.0f}ms", "OK" if r.status_code == 200 else "WARN"])
    except Exception as e:
        results.append(["Qdrant", "ERR", "-", str(e)[:50]])

    # MCP tools
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/tools", timeout=args.timeout)
        elapsed = time.time() - start
        tools = r.json() if r.status_code == 200 else []
        tool_count = len(tools) if isinstance(tools, list) else 0
        results.append(["MCP Tools", f"{r.status_code}", f"{elapsed*1000:.0f}ms", f"{tool_count} tools"])
    except Exception as e:
        results.append(["MCP Tools", "ERR", "-", str(e)[:50]])

    # Workflow API
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/api/workflow-definitions", timeout=args.timeout)
        elapsed = time.time() - start
        results.append(["Workflow API", f"{r.status_code}", f"{elapsed*1000:.0f}ms", "OK" if r.status_code == 200 else "WARN"])
    except Exception as e:
        results.append(["Workflow API", "ERR", "-", str(e)[:50]])

    # SSE
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/sse", timeout=2, stream=True)
        elapsed = time.time() - start
        results.append(["SSE Stream", f"{r.status_code}", f"{elapsed*1000:.0f}ms", "OK"])
        r.close()
    except Exception:
        results.append(["SSE Stream", "SKIP", "-", "not available"])

    if args.json:
        import json
        print(json.dumps(results))
    else:
        print(f"Service health checks ({args.api_base}):")
        print()
        table(["Service", "Status", "Latency", "Details"], results, indent=2)

    all_ok = all(r[1] in ("200", "OK", "SKIP") for r in results)
    return 0 if all_ok else 1


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
