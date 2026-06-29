"""heph project — Unified project management."""

import uuid
from pathlib import Path

from src.cli.utils import api_delete, api_get, api_post

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent


def register(subparsers):
    p = subparsers.add_parser("project", help="Project management")
    sub = p.add_subparsers(dest="subcommand")

    # list
    ls = sub.add_parser("list", help="List all projects")
    ls.set_defaults(func=list_projects)

    # create / setup
    cr = sub.add_parser("create", help="Create a project")
    cr.add_argument("name", help="Project name")
    cr.add_argument("path", help="Project directory path")
    cr.add_argument("--default", action="store_true", help="Set as default project")
    cr.set_defaults(func=create_project)

    su = sub.add_parser(
        "setup", help="Create and activate a project (alias for create)"
    )
    su.add_argument("name", help="Project name")
    su.add_argument("path", help="Project directory path")
    su.add_argument("--default", action="store_true", help="Set as default project")
    su.set_defaults(func=create_project)

    # activate
    act = sub.add_parser("activate", help="Activate a project")
    act.add_argument("project", help="Project ID or name")
    act.set_defaults(func=activate_project)

    # current
    cur = sub.add_parser("current", help="Show active project")
    cur.set_defaults(func=current_project)

    # delete
    rm = sub.add_parser("delete", help="Delete a project")
    rm.add_argument("project", help="Project ID or name")
    rm.add_argument("--force", action="store_true", help="Skip confirmation")
    rm.set_defaults(func=delete_project)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def list_projects(args):
    projects = api_get(args, "/api/projects")
    if projects is None:
        return 1

    if not projects:
        print(
            "No projects configured. Create one with: heph project create <name> <path>"
        )
        return 0

    if args.json:
        import json

        print(json.dumps(projects, indent=2))
        return 0

    print(f"{'':2s} {'Name':20s} {'Path':40s} {'Status':10s}")
    print(f"{'':2s} {'─' * 20} {'─' * 40} {'─' * 10}")
    for p in projects:
        active = "active" if p.get("is_active") else ""
        default = "default" if p.get("is_default") else ""
        status = ", ".join(filter(None, [active, default]))
        name = p["name"]
        path = p["base_dir"]
        if len(path) > 38:
            path = "..." + path[-35:]
        marker = "*" if p.get("is_active") else " "
        print(f"{marker:2s} {name:20s} {path:40s} {status:10s}")

    return 0


def create_project(args):
    resolved = str(Path(args.path).resolve())

    # Try API first
    result = api_post(
        args,
        "/api/projects",
        {
            "name": args.name,
            "base_dir": resolved,
            "is_default": args.default,
        },
    )

    if result is None:
        # Backend not running — offline mode (direct DB write)
        print("Backend not running. Creating project directly in database...")
        return _create_offline(args.name, resolved, args.default)

    print(f"Created project: {result['name']}")
    print(f"  Path: {result['base_dir']}")
    print(f"  ID:   {result['id']}")
    if result.get("is_active"):
        print("  Status: active")
    return 0


def _create_offline(name: str, path: str, is_default: bool):
    """Create project directly in SQLite when backend is not running."""
    import sqlalchemy

    db_path = HEPHAESTUS_DIR / "hephaestus.db"
    if not db_path.exists():
        print(f"Error: Database not found at {db_path}. Run 'heph init' first.")
        return 1

    # Verify path exists and is a git repo
    p = Path(path)
    if not p.exists():
        print(f"Error: Path does not exist: {path}")
        return 1
    if not (p / ".git").exists():
        print(f"Error: Not a git repository: {path}")
        print(f"Run 'git init' in {path} first.")
        return 1

    from src.core.database import AutopilotProject, DatabaseManager

    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()

    # Run migration
    try:
        with db_manager.get_session() as session:
            session.execute(
                sqlalchemy.text(
                    "ALTER TABLE autopilot_projects ADD COLUMN is_active BOOLEAN DEFAULT 0"
                )
            )
            session.commit()
    except Exception:
        pass

    with db_manager.get_session() as session:
        existing = session.query(AutopilotProject).filter_by(base_dir=path).first()
        if existing:
            print(f"Error: Project already exists for directory: {path}")
            return 1

        # Clear other active/default if this is default
        if is_default:
            session.query(AutopilotProject).update(
                {"is_default": False, "is_active": False}
            )

        is_first = session.query(AutopilotProject).count() == 0

        proj = AutopilotProject(
            id=f"proj-{uuid.uuid4().hex[:12]}",
            name=name,
            base_dir=path,
            is_default=is_default or is_first,
            is_active=is_first or is_default,
        )
        session.add(proj)
        session.commit()

        print(f"Created project: {proj.name}")
        print(f"  Path: {proj.base_dir}")
        print(f"  ID:   {proj.id}")
        if proj.is_active:
            print("  Status: active")
        return 0


def activate_project(args):
    # Resolve project ID from name if needed
    project_id = _resolve_project_id(args, args.project)
    if not project_id:
        return 1

    result = api_post(args, f"/api/projects/{project_id}/activate", {})
    if result is None:
        print("Error: Backend not running. Start it with 'heph start' first.")
        return 1

    print(f"Activated: {result['name']} ({result['base_dir']})")
    return 0


def current_project(args):
    result = api_get(args, "/api/projects/active")
    if result is None:
        return 1

    if not result:
        print("No active project. Create one with: heph project create <name> <path>")
        return 0

    if args.json:
        import json

        print(json.dumps(result, indent=2))
    else:
        print(f"Active project: {result['name']}")
        print(f"  Path:  {result['base_dir']}")
        print(f"  ID:    {result['id']}")
    return 0


def delete_project(args):
    project_id = _resolve_project_id(args, args.project)
    if not project_id:
        return 1

    if not args.force:
        # Get project info first
        info = api_get(args, "/api/projects")
        proj = next((p for p in (info or []) if p["id"] == project_id), None)
        name = proj["name"] if proj else project_id
        confirm = input(f"Delete project '{name}'? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return 0

    result = api_delete(args, f"/api/projects/{project_id}")
    if result is None:
        return 1

    print(f"Deleted project: {project_id}")
    return 0


def _resolve_project_id(args, identifier: str) -> str:
    """Resolve a project ID from an ID or name."""
    projects = api_get(args, "/api/projects")
    if projects is None:
        return None

    # Try exact ID match
    for p in projects:
        if p["id"] == identifier:
            return p["id"]

    # Try name match (case-insensitive)
    for p in projects:
        if p["name"].lower() == identifier.lower():
            return p["id"]

    # Try partial match
    matches = [p for p in projects if identifier.lower() in p["name"].lower()]
    if len(matches) == 1:
        return matches[0]["id"]
    elif len(matches) > 1:
        print(f"Ambiguous project '{identifier}'. Matches:")
        for m in matches:
            print(f"  {m['id']} — {m['name']}")
        return None

    print(f"Project not found: {identifier}")
    return None
