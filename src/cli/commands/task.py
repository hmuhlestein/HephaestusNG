"""heph task — Task management."""

from src.cli.utils import api_get, api_post, output, require_backend, table, truncate, time_ago


def register(subparsers):
    p = subparsers.add_parser("task", help="Task management")
    sub = p.add_subparsers(dest="subcommand")

    ls = sub.add_parser("list", help="List tasks")
    ls.add_argument("--status", help="Filter by status (pending, in_progress, done, failed)")
    ls.add_argument("--limit", type=int, default=20, help="Max results")
    ls.set_defaults(func=list_tasks)

    insp = sub.add_parser("inspect", help="Inspect a task")
    insp.add_argument("task_id", help="Task ID")
    insp.set_defaults(func=inspect_task)

    create = sub.add_parser("create", help="Create a task")
    create.add_argument("description", help="Task description")
    create.add_argument("--priority", default="medium", help="Priority (critical, high, medium, low)")
    create.add_argument("--phase", help="Phase ID")
    create.set_defaults(func=create_task)

    cancel = sub.add_parser("cancel", help="Cancel a task")
    cancel.add_argument("task_id", help="Task ID (full or prefix)")
    cancel.set_defaults(func=cancel_task)

    done = sub.add_parser("complete", help="Mark task as done")
    done.add_argument("task_id", help="Task ID (full or prefix)")
    done.add_argument("--summary", default="Task completed", help="Completion summary")
    done.add_argument("--learnings", nargs="*", default=[], help="Key learnings")
    done.set_defaults(func=complete_task)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def list_tasks(args):
    if not require_backend(args):
        return 1
    endpoint = "/api/tasks"
    if args.status:
        endpoint += f"?status={args.status}"
    data = api_get(args, endpoint)
    tasks = data if isinstance(data, list) else data.get("tasks", []) if data else []

    tasks = tasks[:args.limit]

    output(args, tasks, lambda d: _print_tasks(d))


def _print_tasks(tasks):
    if not tasks:
        print("No tasks.")
        return
    rows = [
        [t.get("id", "")[:12], t.get("status", ""), t.get("priority", ""),
         truncate(t.get("description", ""), 50), time_ago(t.get("created_at", ""))]
        for t in tasks
    ]
    table(["ID", "Status", "Priority", "Description", "Created"], rows)


def inspect_task(args):
    if not require_backend(args):
        return 1
    data = api_get(args, f"/api/tasks/{args.task_id}/full-details")
    if not data:
        data = api_get(args, f"/api/tasks/{args.task_id}")
    output(args, data, _print_task_detail)
    return 0


def _print_task_detail(t):
    if not t:
        print("Task not found.")
        return
    task = t.get("task", t)
    print(f"ID:          {task.get('id', '')}")
    print(f"Status:      {task.get('status', '')}")
    print(f"Priority:    {task.get('priority', '')}")
    print(f"Description: {task.get('description', '')}")
    print(f"Agent:       {task.get('agent_id', 'none')}")
    print(f"Phase:       {task.get('phase_id', 'none')}")
    print(f"Created:     {time_ago(task.get('created_at', ''))}")
    print(f"Updated:     {time_ago(task.get('updated_at', ''))}")
    if task.get("error"):
        print(f"Error:       {task['error']}")
    if task.get("output_log"):
        print(f"Output:      {task['output_log'][:200]}")


def create_task(args):
    if not require_backend(args):
        return 1
    payload = {"description": args.description, "priority": args.priority}
    if args.phase:
        payload["phase_id"] = args.phase
    data = api_post(args, "/create_task", payload)
    output(args, data, lambda d: print(f"Created task: {d.get('task_id', d)}"))
    return 0


def cancel_task(args):
    if not require_backend(args):
        return 1
    data = api_post(args, f"/api/tasks/{args.task_id}/cancel", {})
    if data:
        print(f"Cancelled task {args.task_id[:8]}...")
    else:
        print(f"Failed to cancel task {args.task_id[:8]}...")
    return 0


def complete_task(args):
    if not require_backend(args):
        return 1
    payload = {
        "status": "done",
        "summary": args.summary,
        "key_learnings": args.learnings,
        "code_changes": [],
    }
    data = api_post(args, f"/api/tasks/{args.task_id}/status", payload)
    if data:
        print(f"Task {args.task_id[:8]} marked as done")
        return 0
    print(f"Failed to complete task {args.task_id[:8]}")
    return 1
