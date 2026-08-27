#!/usr/bin/env python3
"""Check that new/changed code meets the coverage floor.

Reads new_code_coverage_floor from hephaestus_config.yaml (default 80%).
Uses diff-cover to measure coverage of lines changed vs. origin/main.

Usage:
    # 1. Run tests with coverage first:
    pytest --cov=src --cov-report=xml

    # 2. Check new-code coverage:
    python scripts/check_coverage.py

    # Or with a custom base branch:
    python scripts/check_coverage.py --base=origin/develop

Exit codes:
    0 — new-code coverage >= floor
    1 — coverage below floor (or diff-cover not installed)
"""

import argparse
import subprocess
import sys
from pathlib import Path


def get_floor() -> int:
    """Read new_code_coverage_floor from hephaestus_config.yaml."""
    try:
        # Repo root (not src/) on sys.path -- matches fix_commit_stats.py and
        # add_diagnostic_agent_support.py, and matches simple_config.py's own
        # internal absolute `from src.core...` imports, which require the
        # repo root on the path, not src/ itself.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.core.simple_config import get_config

        return get_config().testing.new_code_coverage_floor
    except Exception:
        return 80  # safe default


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default="origin/main",
        help="Base branch to diff against (default: origin/main)",
    )
    parser.add_argument(
        "--coverage-xml", default="coverage.xml",
        help="Path to coverage XML report (default: coverage.xml)",
    )
    parser.add_argument(
        "--floor", type=int, default=None,
        help="Override new_code_coverage_floor from config",
    )
    args = parser.parse_args()

    floor = args.floor if args.floor is not None else get_floor()
    xml_path = args.coverage_xml

    if not Path(xml_path).exists():
        print(f"ERROR: {xml_path} not found. Run pytest with --cov-report=xml first.")
        sys.exit(1)

    # Run diff-cover
    try:
        result = subprocess.run(
            [
                "diff-cover", xml_path,
                f"--compare-branch={args.base}",
                f"--fail-under={floor}",
                "--quiet",
            ],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("ERROR: diff-cover not installed. Install with: pip install diff-cover")
        print("       Then re-run: pytest --cov=src --cov-report=xml && python scripts/check_coverage.py")
        sys.exit(1)

    if result.returncode == 0:
        print(f"✅ New-code coverage >= {floor}% (vs. {args.base})")
    else:
        print(f"❌ New-code coverage below {floor}% (vs. {args.base})")
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
