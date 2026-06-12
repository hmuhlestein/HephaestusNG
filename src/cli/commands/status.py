"""heph status — System health and status."""

from src.cli.utils import api_get, output, check_backend, table, status_icon


def register(subparsers):
    p = subparsers.add_parser("status", help="Show system health and status")
    p.set_defaults(func=run)


def run(args):
    data = {}

    # Backend health
    health = api_get(args, "/health")
    data["backend"] = "healthy" if health and "healthy" in str(health) else "unreachable"

    if data["backend"] == "unreachable":
        output(args, data, _print_unreachable)
        return 1

    # Agents
    agents = api_get(args, "/api/agents")
    agent_list = agents if isinstance(agents, list) else agents.get("agents", []) if agents else []
    data["agents"] = {
        "total": len(agent_list),
        "working": sum(1 for a in agent_list if a.get("status") == "working"),
        "idle": sum(1 for a in agent_list if a.get("status") in ("idle", "waiting")),
        "error": sum(1 for a in agent_list if a.get("status") == "error"),
    }

    # Tasks
    for task_status in ("pending", "in_progress", "done", "failed"):
        tasks = api_get(args, f"/api/tasks?status={task_status}")
        task_list = tasks if isinstance(tasks, list) else tasks.get("tasks", []) if tasks else []
        data[f"tasks_{task_status}"] = len(task_list)

    # Workflows
    wf_defs = api_get(args, "/api/workflow-definitions")
    wf_execs = api_get(args, "/api/workflow-executions")
    data["workflow_definitions"] = len(wf_defs) if isinstance(wf_defs, list) else 0
    data["workflow_executions"] = len(wf_execs) if isinstance(wf_execs, list) else 0

    # Queue
    queue = api_get(args, "/api/queue_status")
    data["queue"] = queue if queue else {}

    output(args, data, _print_status)
    return 0


def _print_unreachable(data):
    print("Backend: UNREACHABLE")
    print()
    print("Start services with: heph start")


def _print_status(data):
    icon = status_icon(data["backend"])
    print(f"Backend:   {icon} {data['backend']}")
    print()

    a = data.get("agents", {})
    print(f"Agents:    {a.get('total', 0)} total  "
          f"({a.get('working', 0)} working, "
          f"{a.get('idle', 0)} idle, "
          f"{a.get('error', 0)} error)")
    print()

    print("Tasks:")
    print(f"  pending:      {data.get('tasks_pending', 0)}")
    print(f"  in_progress:  {data.get('tasks_in_progress', 0)}")
    print(f"  done:         {data.get('tasks_done', 0)}")
    print(f"  failed:       {data.get('tasks_failed', 0)}")
    print()

    print(f"Workflows: {data.get('workflow_definitions', 0)} definitions, "
          f"{data.get('workflow_executions', 0)} executions")
