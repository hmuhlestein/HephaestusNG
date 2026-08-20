"""heph workflow — Workflow management."""

from src.cli.utils import api_get, api_post, output, require_backend, table, time_ago


def register(subparsers):
    p = subparsers.add_parser("workflow", help="Workflow management")
    sub = p.add_subparsers(dest="subcommand")

    # list
    ls = sub.add_parser("list", help="List workflow definitions")
    ls.set_defaults(func=list_definitions)

    # executions
    ex = sub.add_parser("executions", help="List workflow executions")
    ex.add_argument("--status", help="Filter by status")
    ex.set_defaults(func=list_executions)

    # launch
    la = sub.add_parser("launch", help="Launch a workflow")
    la.add_argument("definition_id", help="Workflow definition ID")
    la.add_argument("--description", "-d", required=True, help="Description")
    la.add_argument("--path", help="Working directory")
    la.set_defaults(func=launch)

    # status
    st = sub.add_parser("status", help="Get workflow execution status")
    st.add_argument("workflow_id", help="Workflow execution ID")
    st.set_defaults(func=get_status)

    # stop
    sp = sub.add_parser("stop", help="Stop a running workflow")
    sp.add_argument(
        "workflow_id", nargs="?", help="Workflow execution ID (omit with --all)"
    )
    sp.add_argument("--all", action="store_true", help="Stop all active workflows")
    sp.set_defaults(func=stop_workflow)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def list_definitions(args):
    if not require_backend(args):
        return 1
    data = api_get(args, "/api/workflow-definitions")
    if isinstance(data, list):
        defs = data
    else:
        defs = data.get("definitions", []) if data else []

    output(args, defs, lambda d: _print_definitions(d))
    return 0


def _print_definitions(defs):
    if not defs:
        print("No workflow definitions registered.")
        return
    rows = [
        [d.get("id", ""), d.get("name", ""), d.get("description", "")[:50]]
        for d in defs
    ]
    table(["ID", "Name", "Description"], rows)


def list_executions(args):
    if not require_backend(args):
        return 1
    endpoint = "/api/workflow-executions"
    data = api_get(args, endpoint)
    execs = (
        data if isinstance(data, list) else data.get("executions", []) if data else []
    )

    if args.status:
        execs = [e for e in execs if e.get("status") == args.status]

    output(args, execs, lambda d: _print_executions(d))
    return 0


def _print_executions(execs):
    if not execs:
        print("No workflow executions.")
        return
    rows = [
        [
            e.get("id", "")[:12],
            e.get("definition_id", ""),
            e.get("status", ""),
            time_ago(e.get("created_at", "")),
        ]
        for e in execs
    ]
    table(["ID", "Definition", "Status", "Started"], rows)


def launch(args):
    if not require_backend(args):
        return 1
    payload = {
        "definition_id": args.definition_id,
        "description": args.description,
    }
    if args.path:
        payload["working_directory"] = args.path

    data = api_post(args, "/api/workflow-executions", payload)
    output(args, data, lambda d: print(f"Launched: {d.get('id', d)}"))
    return 0


def get_status(args):
    if not require_backend(args):
        return 1
    data = api_get(args, f"/api/workflow-executions/{args.workflow_id}")
    output(args, data, lambda d: _print_single_execution(d))
    return 0


def _print_single_execution(e):
    if not e:
        print("Workflow not found.")
        return
    print(f"ID:          {e.get('id', '')}")
    print(f"Definition:  {e.get('definition_id', '')}")
    print(f"Status:      {e.get('status', '')}")
    print(f"Description: {e.get('description', '')}")
    print(f"Created:     {time_ago(e.get('created_at', ''))}")


def stop_workflow(args):
    if not require_backend(args):
        return 1

    if args.all:
        # First: terminate all active agents
        try:
            agents_data = api_get(args, "/api/agents")
            agents = (
                agents_data
                if isinstance(agents_data, list)
                else agents_data.get("agents", [])
                if agents_data
                else []
            )
            active_agents = [
                a
                for a in agents
                if isinstance(a, dict)
                and a.get("status") in ("working", "starting", "idle")
            ]
            if active_agents:
                print(f"Terminating {len(active_agents)} active agent(s)...")
                for agent in active_agents:
                    agent_id = agent.get("id", "")
                    if not agent_id:
                        continue
                    try:
                        result = api_post(
                            args,
                            "/api/terminate_agent",
                            {"agent_id": agent_id},
                            timeout=10,
                        )
                        if result is None:
                            print(f"  {agent_id[:8]}... connection error")
                        else:
                            print(f"  {agent_id[:8]}... terminated")
                    except Exception as e:
                        print(f"  {agent_id[:8]}... error: {e}")
        except Exception as e:
            print(f"Warning: Could not check agents: {e}")

        # Second: stop all active workflows
        data = api_get(args, "/api/workflow-executions?status=active")
        execs = (
            data
            if isinstance(data, list)
            else data.get("executions", [])
            if data
            else []
        )
        if not execs:
            print("No active workflows.")
            return 0
        print(f"Stopping {len(execs)} workflow(s)...")
        for e in execs:
            eid = e.get("id", "")
            if not eid:
                continue
            result = api_post(args, f"/api/workflow-executions/{eid}/stop", timeout=15)
            if result is None:
                print(f"  {eid[:12]}... connection error")
            elif isinstance(result, dict) and "error" in result:
                print(f"  {eid[:12]}... {result.get('detail', result['error'])}")
            else:
                msg = (
                    result.get("message", "stopped")
                    if isinstance(result, dict)
                    else "stopped"
                )
                print(f"  {eid[:12]}... {msg}")
        return 0

    if not args.workflow_id:
        print("Error: provide a workflow_id or use --all")
        return 1

    # Single workflow stop — also terminate its agents
    try:
        agents_data = api_get(args, "/api/agents")
        agents = (
            agents_data
            if isinstance(agents_data, list)
            else agents_data.get("agents", [])
            if agents_data
            else []
        )
        wf_agents = [
            a
            for a in agents
            if isinstance(a, dict)
            and a.get("status") in ("working", "starting", "idle")
            and a.get("workflow", {}).get("id") == args.workflow_id
        ]
        for agent in wf_agents:
            agent_id = agent.get("id", "")
            try:
                api_post(
                    args, "/api/terminate_agent", {"agent_id": agent_id}, timeout=10
                )
            except Exception as e:
                print(f"  Warning: could not terminate agent {agent_id[:8]}: {e}")
    except Exception as e:
        print(f"  Warning: could not check/terminate this workflow's agents: {e}")

    data = api_post(
        args, f"/api/workflow-executions/{args.workflow_id}/stop", timeout=15
    )
    if data is None:
        print("Connection error")
        return 1
    if isinstance(data, dict) and "error" in data:
        print(f"Error: {data.get('detail', data['error'])}")
        return 1
    print(data.get("message", "Workflow stopped"))
    return 0
