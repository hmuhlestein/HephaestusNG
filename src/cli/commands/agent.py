"""heph agent — Agent management."""

from src.cli.utils import api_get, api_post, output, require_backend, table, truncate


def register(subparsers):
    p = subparsers.add_parser("agent", help="Agent management")
    sub = p.add_subparsers(dest="subcommand")

    ls = sub.add_parser("list", help="List agents")
    ls.add_argument("--status", help="Filter by status")
    ls.set_defaults(func=list_agents)

    log = sub.add_parser("logs", help="Get agent output")
    log.add_argument("agent_id", help="Agent ID")
    log.set_defaults(func=get_logs)

    term = sub.add_parser("terminate", help="Terminate an agent")
    term.add_argument("agent_id", help="Agent ID")
    term.set_defaults(func=terminate)

    msg = sub.add_parser("message", help="Send message to agent")
    msg.add_argument("agent_id", help="Agent ID")
    msg.add_argument("message", help="Message text")
    msg.set_defaults(func=send_message)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def list_agents(args):
    if not require_backend(args):
        return 1
    data = api_get(args, "/api/agents")
    agents = data if isinstance(data, list) else data.get("agents", []) if data else []

    if args.status:
        agents = [a for a in agents if a.get("status") == args.status]

    output(args, agents, lambda d: _print_agents(d))


def _print_agents(agents):
    if not agents:
        print("No agents.")
        return
    rows = [
        [a.get("agent_id", "")[:20], a.get("status", ""), a.get("task_description", "")[:40], a.get("cli_tool", "")]
        for a in agents
    ]
    table(["Agent ID", "Status", "Task", "CLI Tool"], rows)


def get_logs(args):
    if not require_backend(args):
        return 1
    data = api_get(args, f"/api/agents/{args.agent_id}/output")
    if isinstance(data, dict) and "output" in data:
        print(data["output"])
    else:
        output(args, data, lambda d: print(d if isinstance(d, str) else str(d)))
    return 0


def terminate(args):
    if not require_backend(args):
        return 1
    data = api_post(args, "/api/terminate_agent", {"agent_id": args.agent_id})
    output(args, data, lambda d: print(f"Agent {args.agent_id}: {d.get('status', 'terminated')}"))
    return 0


def send_message(args):
    if not require_backend(args):
        return 1
    data = api_post(args, "/api/send_message", {
        "agent_id": args.agent_id,
        "message": args.message,
    })
    output(args, data, lambda d: print(f"Message sent to {args.agent_id}"))
    return 0
