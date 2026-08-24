"""heph config — Show and edit configuration."""

from src.cli.utils import output
from src.core.constants import AUTOPILOT_STATE_DIR, HEPHAESTUS_INSTALL_DIR, HEPHAESTUS_LOGS_DIR

HEPHAESTUS_DIR = HEPHAESTUS_INSTALL_DIR


def register(subparsers):
    p = subparsers.add_parser("config", help="Show and edit configuration")
    sub = p.add_subparsers(dest="subcommand")

    s = sub.add_parser("show", help="Show current configuration")
    s.set_defaults(func=show)

    pth = sub.add_parser("path", help="Show config file paths")
    pth.set_defaults(func=show_paths)

    p.set_defaults(func=lambda a: p.print_help() or 0)


def show(args):
    config_file = HEPHAESTUS_DIR / "hephaestus_config.yaml"
    if not config_file.exists():
        print(f"Config not found: {config_file}")
        return 1

    data = config_file.read_text()
    if args.json:
        try:
            import yaml

            parsed = yaml.safe_load(data)
            # Overlay active project from DB
            _overlay_active_project(parsed)
            import json

            print(json.dumps(parsed, indent=2))
        except ImportError:
            print("pyyaml not installed, showing raw YAML")
            print(data)
        except Exception:
            print(data)
    else:
        print(data)
        # Show active project overlay
        _print_active_project_overlay()
    return 0


def _overlay_active_project(parsed: dict):
    """Overlay active project paths onto parsed config dict."""
    import sqlalchemy

    from src.core.database import DatabaseManager

    db_path = HEPHAESTUS_DIR / "hephaestus.db"
    if not db_path.exists():
        return
    try:
        db_manager = DatabaseManager(str(db_path))
        with db_manager.get_session() as session:
            active_rows = session.execute(
                sqlalchemy.text(
                    "SELECT name, base_dir FROM autopilot_projects WHERE is_active = 1"
                )
            ).fetchall()
            if active_rows:
                # paths.project_root/git.main_repo_path reflect only the
                # first row -- there's no single-path representation once
                # more than one project can be active at once.
                parsed.setdefault("paths", {})["project_root"] = active_rows[0][1]
                parsed.setdefault("git", {})["main_repo_path"] = active_rows[0][1]
                parsed["_active_projects"] = [
                    {"name": row[0], "path": row[1]} for row in active_rows
                ]
    except Exception:
        pass


def _print_active_project_overlay():
    """Print active project info below the YAML dump."""
    import sqlalchemy

    from src.core.database import DatabaseManager

    db_path = HEPHAESTUS_DIR / "hephaestus.db"
    if not db_path.exists():
        return
    try:
        db_manager = DatabaseManager(str(db_path))
        with db_manager.get_session() as session:
            active_rows = session.execute(
                sqlalchemy.text(
                    "SELECT name, base_dir FROM autopilot_projects WHERE is_active = 1"
                )
            ).fetchall()
            if active_rows:
                print(
                    "\n# Active project(s) (paths.project_root/git.main_repo_path "
                    "reflect only the first):"
                )
                for name, path in active_rows:
                    print(f"#   name: {name}")
                    print(f"#   path: {path}")
    except Exception:
        pass


def show_paths(args):
    paths = {
        "project_root": str(HEPHAESTUS_DIR),
        "config": str(HEPHAESTUS_DIR / "hephaestus_config.yaml"),
        "database": str(HEPHAESTUS_DIR / "hephaestus.db"),
        "logs": HEPHAESTUS_LOGS_DIR,
        "autopilot_logs": AUTOPILOT_STATE_DIR,
        "workflows": str(HEPHAESTUS_DIR / "example_workflows"),
        "phases": str(HEPHAESTUS_DIR / "example_workflows"),
        "scripts": str(HEPHAESTUS_DIR / "scripts"),
    }
    output(args, paths, lambda d: [print(f"  {k}: {v}") for k, v in d.items()])
    return 0
