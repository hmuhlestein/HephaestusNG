#!/usr/bin/env python3
"""Audited freshness inventory for src/agents/manager.py (AgentManager).

Parses the live file via ast and emits per-method line ranges plus
responsibility clusters to support the manager decomposition plan in
design_docs/manager_py_decomposition_prompt.md.

Default output is human-readable text.  Pass --json for a machine-readable
report.  Exit code is always 0 unless the target file is missing.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "src" / "agents" / "manager.py"

# Responsibility clusters used for the decomposition planning.
#
# The grouping intentionally includes only methods that already exist in the
# live file so the inventory stays verifiable against HEAD.  If a planned
# method is missing, we report it explicitly instead of silently inventing a
# range.
CLUSTERS: Dict[str, List[str]] = {
    "launch_pipeline": [
        "create_agent_for_task",
        "_check_duplicate_active_agent",
        "_resolve_phase_config",
        "_resolve_worktree",
        "_resolve_env_and_model",
        "_resolve_phase_name_and_thinking",
        "_resolve_session_id",
        "_prepare_launch_environment",
        "_build_and_send_launch_command",
        "_detect_launch_failure",
        "_deliver_initial_prompt",
        "_create_tmux_session",
        "_export_env_vars_and_verify",
        "_send_goal_command",
        "_verify_instructions_file_read",
        "_gather_worktree_context",
        "_format_initial_message",
        "_verify_prompt_delivery",
        "_record_cli_session",
        "_send_initial_prompt_with_retry",
        "_wait_for_shell_ready",
        "_build_instructions_pointer",
        "_write_task_instructions",
        "restart_agent",
    ],
    "termination": [
        "terminate_agent",
        "_commit_wip_in_shared_worktree",
    ],
    "output_capture": [
        "get_agent_output",
    ],
    "messaging_delegation": [
        "send_message_to_agent",
        "broadcast_message_to_all_agents",
        "send_direct_message",
    ],
    "utility_helpers": [
        "get_agent",
        "get_agents",
        "get_active_agents",
        "update_agent_status",
    ],
}


def _method_name(node: ast.stmt) -> str | None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node.name
    return None


def _collect_methods(tree: ast.Module) -> List[Tuple[str, int, int, bool]]:
    rows: List[Tuple[str, int, int, bool]] = []

    # Top-level functions (none expected, but support them for completeness).
    for node in tree.body:
        name = _method_name(node)
        if name:
            rows.append((name, node.lineno, node.end_lineno, False))

    # Methods on the AgentManager class.
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "AgentManager":
            for child in node.body:
                name = _method_name(child)
                if name:
                    rows.append((name, child.lineno, child.end_lineno, True))
            break

    return rows


def _hits(method_names: set[str], cluster: List[str]) -> Tuple[List[str], List[str]]:
    present, missing = [], []
    for name in cluster:
        (present if name in method_names else missing).append(name)
    return present, missing


def main() -> None:
    output_json = "--json" in sys.argv

    if not TARGET.exists():
        message = f"target not found: {TARGET}"
        if output_json:
            print(json.dumps({"ok": False, "error": message}, indent=2))
        else:
            print(f"FAIL: {message}")
        sys.exit(1)

    source = TARGET.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)
    methods = _collect_methods(tree)
    unique_names = {name for name, *_ in methods}

    payload = {
        "ok": True,
        "file": str(TARGET.relative_to(ROOT)),
        "total_lines": len(lines),
        "agent_manager_method_count": sum(1 for *_, is_method in methods if is_method),
        "methods": [],
        "clusters": {},
    }

    for name, start, end, is_method in sorted(methods, key=lambda r: r[1]):
        payload["methods"].append(
            {
                "name": name,
                "is_method": is_method,
                "start": start,
                "end": end,
                "length": end - start + 1,
            }
        )

    for cluster_name, expected in CLUSTERS.items():
        present, missing = _hits(unique_names, expected)
        payload["clusters"][cluster_name] = {
            "expected": len(expected),
            "present": len(present),
            "missing": missing,
            "present_names": present,
        }

    payload["verification"] = {
        "all_expected_clusters_present": all(
            not cluster["missing"] for cluster in payload["clusters"].values()
        ),
        "unexpected_methods": sorted(unique_names - {
            name for names in CLUSTERS.values() for name in names
        }),
    }

    if output_json:
        print(json.dumps(payload, indent=2))
        return

    print(f"Freshness inventory for {payload['file']}")
    print(f"Total lines: {payload['total_lines']}")
    print(f"AgentManager method count: {payload['agent_manager_method_count']}")
    print()
    print("Methods (line order):")
    for method in payload["methods"]:
        if method["is_method"]:
            print(
                f"  {method['name']}: lines {method['start']}-{method['end']} "
                f"({method['length']} lines)"
            )
    print()
    print("Cluster verification:")
    for cluster_name, cluster in payload["clusters"].items():
        status = "OK" if not cluster["missing"] else "MISSING"
        print(f"  [{status}] {cluster_name}: {cluster['present']}/{cluster['expected']}")
        if cluster["missing"]:
            for missing in cluster["missing"]:
                print(f"    - missing: {missing}")
    if payload["verification"]["unexpected_methods"]:
        print()
        print("Unexpected names not listed in any cluster:")
        for name in payload["verification"]["unexpected_methods"]:
            print(f"  - {name}")


if __name__ == "__main__":
    main()