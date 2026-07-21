"""heph status — System health and status."""

import time

import httpx

from src.cli.utils import (
    api_get,
    is_process_running,
    output,
    read_pid,
    status_icon,
    table,
)


def register(subparsers):
    p = subparsers.add_parser("status", help="Show system health and status")
    p.set_defaults(func=run)


def run(args):
    data = {}

    # Backend health (check once, reuse result)
    start = time.time()
    health = api_get(args, "/health")
    backend_latency_ms = round((time.time() - start) * 1000)
    data["backend"] = (
        "healthy" if health and "healthy" in str(health) else "unreachable"
    )

    # Frontend (independent of backend)
    frontend_pid = read_pid("frontend")
    if frontend_pid and is_process_running(frontend_pid):
        data["frontend"] = "running"
    else:
        data["frontend"] = "not running"

    if data["backend"] == "unreachable":
        output(args, data, _print_unreachable)
        return 1

    # Agents
    agents = api_get(args, "/api/agents")
    agent_list = (
        agents
        if isinstance(agents, list)
        else agents.get("agents", [])
        if agents
        else []
    )
    data["agents"] = {
        "total": len(agent_list),
        "working": sum(1 for a in agent_list if a.get("status") == "working"),
        "idle": sum(1 for a in agent_list if a.get("status") in ("idle", "waiting")),
        "error": sum(1 for a in agent_list if a.get("status") == "error"),
    }

    # Tasks
    for task_status in ("pending", "in_progress", "done", "failed"):
        tasks = api_get(args, f"/api/tasks?status={task_status}")
        task_list = (
            tasks
            if isinstance(tasks, list)
            else tasks.get("tasks", [])
            if tasks
            else []
        )
        data[f"tasks_{task_status}"] = len(task_list)

    # Workflows
    wf_defs = api_get(args, "/api/workflow-definitions")
    wf_execs = api_get(args, "/api/workflow-executions")
    wf_def_list = (
        wf_defs
        if isinstance(wf_defs, list)
        else wf_defs.get("definitions", [])
        if wf_defs
        else []
    )
    wf_exec_list = (
        wf_execs
        if isinstance(wf_execs, list)
        else wf_execs.get("executions", [])
        if wf_execs
        else []
    )
    data["workflow_definitions"] = len(wf_def_list)
    data["workflow_executions"] = len(wf_exec_list)

    # Queue
    queue = api_get(args, "/api/queue_status")
    data["queue"] = queue if queue else {}

    # Service health checks
    import os

    services = []
    timeout = 5

    # Backend (reuse earlier result)
    services.append(
        {"name": "Backend", "status": 200, "latency_ms": backend_latency_ms, "ok": True}
    )

    # Qdrant (only if using qdrant backend)
    vector_backend = os.environ.get("VECTOR_STORE_BACKEND", "turbovec")
    if vector_backend == "qdrant":
        start = time.time()
        try:
            r = httpx.get("http://localhost:6333/", timeout=timeout)
            elapsed = time.time() - start
            services.append(
                {
                    "name": "Qdrant",
                    "status": r.status_code,
                    "latency_ms": round(elapsed * 1000),
                    "ok": r.status_code == 200,
                }
            )
        except Exception as e:
            services.append(
                {
                    "name": "Qdrant",
                    "status": "ERR",
                    "latency_ms": None,
                    "ok": False,
                    "detail": str(e)[:50],
                }
            )
    else:
        services.append(
            {
                "name": "Qdrant",
                "status": "SKIP",
                "latency_ms": None,
                "ok": True,
                "detail": "turbovec",
            }
        )

    # MCP Tools
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/tools", timeout=timeout)
        elapsed = time.time() - start
        tools = r.json() if r.status_code == 200 else []
        tool_list = (
            tools
            if isinstance(tools, list)
            else tools.get("tools", [])
            if tools
            else []
        )
        tool_count = len(tool_list)
        services.append(
            {
                "name": "MCP Tools",
                "status": r.status_code,
                "latency_ms": round(elapsed * 1000),
                "ok": r.status_code == 200,
                "detail": f"{tool_count} tools",
            }
        )
    except Exception as e:
        services.append(
            {
                "name": "MCP Tools",
                "status": "ERR",
                "latency_ms": None,
                "ok": False,
                "detail": str(e)[:50],
            }
        )

    # Workflow API
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/api/workflow-definitions", timeout=timeout)
        elapsed = time.time() - start
        services.append(
            {
                "name": "Workflow API",
                "status": r.status_code,
                "latency_ms": round(elapsed * 1000),
                "ok": r.status_code == 200,
            }
        )
    except Exception as e:
        services.append(
            {
                "name": "Workflow API",
                "status": "ERR",
                "latency_ms": None,
                "ok": False,
                "detail": str(e)[:50],
            }
        )

    # SSE
    start = time.time()
    try:
        r = httpx.get(f"{args.api_base}/sse", timeout=2, stream=True)
        elapsed = time.time() - start
        services.append(
            {
                "name": "SSE Stream",
                "status": r.status_code,
                "latency_ms": round(elapsed * 1000),
                "ok": True,
            }
        )
        r.close()
    except Exception:
        services.append(
            {
                "name": "SSE Stream",
                "status": "SKIP",
                "latency_ms": None,
                "ok": True,
                "detail": "not available",
            }
        )

    data["services"] = services

    output(args, data, _print_status)
    return 0


def _print_unreachable(data):
    fe_status = data.get("frontend", "not running")
    fe_icon = "OK" if fe_status == "running" else "FAIL"
    print("Backend:   FAIL unreachable")
    print(f"Frontend:  {fe_icon} {fe_status}")
    print()
    print("Start services with: heph start")


def _print_status(data):
    icon = status_icon(data["backend"])
    print(f"Backend:   {icon} {data['backend']}")

    fe_status = data.get("frontend", "not running")
    fe_icon = "OK" if fe_status == "running" else "FAIL"
    print(f"Frontend:  {fe_icon} {fe_status}")
    print()

    a = data.get("agents", {})
    print(
        f"Agents:    {a.get('total', 0)} total  "
        f"({a.get('working', 0)} working, "
        f"{a.get('idle', 0)} idle, "
        f"{a.get('error', 0)} error)"
    )
    print()

    print("Tasks:")
    print(f"  pending:      {data.get('tasks_pending', 0)}")
    print(f"  in_progress:  {data.get('tasks_in_progress', 0)}")
    print(f"  done:         {data.get('tasks_done', 0)}")
    print(f"  failed:       {data.get('tasks_failed', 0)}")
    print()

    print(
        f"Workflows: {data.get('workflow_definitions', 0)} definitions, "
        f"{data.get('workflow_executions', 0)} executions"
    )
    print()

    services = data.get("services", [])
    if services:
        print("Services:")
        rows = []
        for s in services:
            latency = f"{s['latency_ms']}ms" if s.get("latency_ms") is not None else "-"
            detail = s.get("detail", "OK" if s.get("ok") else "FAIL")
            status_str = str(s["status"])
            rows.append([s["name"], status_str, latency, detail])
        table(["Service", "Status", "Latency", "Details"], rows, indent=2)
