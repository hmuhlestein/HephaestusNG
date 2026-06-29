"""heph agent — Agent management."""

from src.cli.utils import api_get, api_post, output, require_backend, table


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
    term.add_argument(
        "agent_id", nargs="?", help="Agent ID (omit with --all to terminate all)"
    )
    term.add_argument("--all", action="store_true", help="Terminate all active agents")
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
        [
            a.get("id", a.get("agent_id", ""))[:20],
            a.get("status", ""),
            (a.get("current_task", {}) or {}).get("description", "")[:40]
            if a.get("current_task")
            else "",
            a.get("cli_type", a.get("cli_tool", "")),
        ]
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

    if args.all:
        data = api_get(args, "/api/agents", timeout=10)
        agents = (
            data if isinstance(data, list) else data.get("agents", []) if data else []
        )
        active = [a for a in agents if a.get("status") == "working"]
        if not active:
            print("No active agents.")
            return 0
        print(f"Terminating {len(active)} agents...")
        import sys
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _terminate_one(aid):
            try:
                result = api_post(
                    args, "/api/terminate_agent", {"agent_id": aid}, timeout=10
                )
                if result is None:
                    return aid, "connection error"
                elif isinstance(result, dict) and "error" in result:
                    return aid, str(result.get("detail", result["error"]))[:60]
                else:
                    return aid, "terminated"
            except Exception as e:
                return aid, str(e)[:60]

        aids = [
            a.get("id", a.get("agent_id", ""))
            for a in active
            if a.get("id") or a.get("agent_id")
        ]
        with ThreadPoolExecutor(max_workers=min(len(aids), 10)) as pool:
            futures = {pool.submit(_terminate_one, aid): aid for aid in aids}
            for future in as_completed(futures):
                aid, status = future.result()
                print(f"  {aid[:12]}... {status}")
                sys.stdout.flush()
        return 0

    if not args.agent_id:
        print("Error: provide an agent_id or use --all")
        return 1

    data = api_post(
        args, "/api/terminate_agent", {"agent_id": args.agent_id}, timeout=15
    )
    if data is None:
        print(f"Agent {args.agent_id}: connection error")
    elif isinstance(data, dict) and "error" in data:
        print(f"Agent {args.agent_id}: {data.get('detail', data['error'])}")
    else:
        status = (
            data.get("status", "terminated") if isinstance(data, dict) else "terminated"
        )
        print(f"Agent {args.agent_id}: {status}")
    return 0


def send_message(args):
    if not require_backend(args):
        return 1
    data = api_post(
        args,
        "/api/send_message",
        {
            "agent_id": args.agent_id,
            "message": args.message,
        },
    )
    output(args, data, lambda d: print(f"Message sent to {args.agent_id}"))
    return 0
