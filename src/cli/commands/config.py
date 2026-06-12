"""heph config — Show and edit configuration."""

import os
from pathlib import Path
from src.cli.utils import output

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent


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
            import json
            print(json.dumps(parsed, indent=2))
        except ImportError:
            print("pyyaml not installed, showing raw YAML")
            print(data)
        except Exception:
            print(data)
    else:
        print(data)
    return 0


def show_paths(args):
    paths = {
        "project_root": str(HEPHAESTUS_DIR),
        "config": str(HEPHAESTUS_DIR / "hephaestus_config.yaml"),
        "database": str(HEPHAESTUS_DIR / "hephaestus.db"),
        "logs": str(Path.home() / ".hephaestus" / "logs"),
        "autopilot_logs": str(Path.home() / ".hephaestus" / "autopilot"),
        "workflows": str(HEPHAESTUS_DIR / "example_workflows"),
        "phases": str(HEPHAESTUS_DIR / "src" / "phases"),
        "scripts": str(HEPHAESTUS_DIR / "scripts"),
    }
    output(args, paths, lambda d: [print(f"  {k}: {v}") for k, v in d.items()])
    return 0
