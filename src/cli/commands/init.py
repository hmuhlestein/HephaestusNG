"""heph init — Initialize database and vector store."""

import os
import sys
import shutil
from pathlib import Path

from src.cli.utils import output

HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent


def register(subparsers):
    p = subparsers.add_parser("init", help="Initialize database and vector store")
    p.add_argument("--drop", action="store_true", help="Drop existing data first")
    p.set_defaults(func=run)


def run(args):
    python = str(HEPHAESTUS_DIR / ".venv" / "bin" / "python")
    if not Path(python).exists():
        python = sys.executable

    results = {}

    if args.drop:
        db = HEPHAESTUS_DIR / "hephaestus.db"
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(db) + suffix)
            if target.exists():
                # Backup before deleting
                backup = Path(str(target) + ".bak")
                if backup.exists():
                    backup.unlink()
                shutil.copy2(target, backup)
                os.remove(target)
        results["database"] = "dropped (backup created)"

    # Init DB
    import subprocess
    r = subprocess.run(
        [python, str(HEPHAESTUS_DIR / "scripts" / "init_db.py")],
        capture_output=True, text=True, cwd=str(HEPHAESTUS_DIR)
    )
    results["database"] = "initialized" if r.returncode == 0 else f"failed: {r.stderr[:100]}"

    # Init Qdrant
    r = subprocess.run(
        [python, str(HEPHAESTUS_DIR / "scripts" / "init_qdrant.py")],
        capture_output=True, text=True, cwd=str(HEPHAESTUS_DIR)
    )
    results["qdrant"] = "initialized" if r.returncode == 0 else f"failed: {r.stderr[:100]}"

    output(args, results, lambda d: [print(f"  {k}: {v}") for k, v in d.items()])
    return 0
