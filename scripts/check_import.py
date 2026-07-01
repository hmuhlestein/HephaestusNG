#!/usr/bin/env python3
"""Verify that one or more modules import cleanly.

Only accepts dotted module paths (e.g. `src.autopilot.orchestrator`) via
importlib — no arbitrary code execution like `python -c "..."` allows.
"""

import importlib
import sys


def main() -> int:
    modules = sys.argv[1:]
    if not modules:
        print("usage: check_import.py <module> [<module> ...]", file=sys.stderr)
        return 2

    failed = False
    for name in modules:
        try:
            importlib.import_module(name)
            print(f"OK   {name}")
        except Exception as e:
            print(f"FAIL {name}: {type(e).__name__}: {e}", file=sys.stderr)
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
