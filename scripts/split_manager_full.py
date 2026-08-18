#!/usr/bin/env python3
"""Full scripted extraction of the remaining clusters from src/agents/manager.py.

Extracts:
  1. LaunchPipeline (launch steps + create/restart orchestrators)
  2. Terminator (terminate_agent + _commit_wip_in_shared_worktree)

Messaging delegators stay on AgentManager (already delegated to AgentMessenger).
Utility methods stay on AgentManager (thin, few callers).

Usage:
  python scripts/split_manager_full.py          # verify only (dry run)
  python scripts/split_manager_full.py --apply  # write files
"""

import ast
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANAGER = ROOT / "src" / "agents" / "manager.py"
LAUNCH_MODULE = ROOT / "src" / "agents" / "launch_pipeline.py"
TERMINATOR_MODULE = ROOT / "src" / "agents" / "terminator.py"

LAUNCH_METHODS = [
    "_build_glm_env_vars", "_resolve_mcp_timeout_ms", "_resolve_project_base_dir",
    "_scoped_worktree_manager", "_ensure_codegraph_initialized",
    "_check_termination_race", "_detect_launch_failure",
    "_check_duplicate_active_agent", "_resolve_phase_config", "_resolve_worktree",
    "_resolve_env_and_model", "_resolve_phase_name_and_thinking", "_resolve_session_id",
    "_prepare_launch_environment", "_build_and_send_launch_command", "_deliver_initial_prompt",
    "_wait_for_shell_ready", "_create_tmux_session", "_export_env_vars_and_verify",
    "_write_task_instructions", "_build_instructions_pointer", "_send_goal_command",
    "_verify_instructions_file_read", "_gather_worktree_context", "_format_initial_message",
    "_verify_prompt_delivery", "_record_cli_session", "_send_initial_prompt_with_retry",
    "create_agent_for_task", "restart_agent",
]

TERMINATOR_METHODS = ["terminate_agent", "_commit_wip_in_shared_worktree"]

TARGET_NAMES = set(LAUNCH_METHODS + TERMINATOR_METHODS)


def fail(msg):
    print(f"FAIL: {msg}")
    sys.exit(1)


def collect_class_methods(tree, class_name):
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in child.decorator_list)
                    start = child.decorator_list[0].lineno if child.decorator_list else child.lineno
                    methods[child.name] = (start, child.end_lineno, isinstance(child, ast.AsyncFunctionDef), is_static, child)
            return methods
    return {}


def extract_signature(node):
    """Extract (signature_str, arg_names_str) from a function AST node.
    signature_str is the full 'def name(args) -> ret:' line.
    arg_names_str is 'arg1, arg2, arg3' for forwarding calls."""
    source_lines = MANAGER.read_text().splitlines()
    # Get the def line (may span multiple source lines)
    def_line_idx = node.lineno - 1
    # Collect lines until we find the colon
    sig_lines = []
    i = def_line_idx
    depth = 0
    while i < len(source_lines):
        line = source_lines[i]
        sig_lines.append(line.strip())
        for ch in line:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        if ':' in line and depth <= 0:
            break
        i += 1

    # Build forwarding args: extract parameter names from AST
    args = []
    for arg in node.args.args:
        if arg.arg == 'self':
            continue
        args.append(arg.arg)
    for arg in node.args.kwonlyargs:
        args.append(f"{arg.arg}={arg.arg}")

    # Handle *args and **kwargs
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")

    arg_names = ", ".join(args)
    return sig_lines, arg_names


def main():
    apply = "--apply" in sys.argv

    if not MANAGER.exists():
        fail(f"target not found: {MANAGER}")

    source = MANAGER.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)
    methods = collect_class_methods(tree, "AgentManager")

    missing = TARGET_NAMES - set(methods)
    if missing:
        fail(f"methods not found: {missing}")

    init_end = methods["__init__"][1]
    print(f"OK: {len(TARGET_NAMES)} methods found ({len(LAUNCH_METHODS)} launch + {len(TERMINATOR_METHODS)} terminator)")

    all_skip = []
    for name in LAUNCH_METHODS + TERMINATOR_METHODS:
        start, end, is_async, is_static, node = methods[name]
        all_skip.append((start, end, name, is_async, is_static, node))
        print(f"  {name}: {start}-{end} ({end-start+1}L)")
    all_skip.sort(key=lambda r: r[0])

    n_extracted = sum(e - s + 1 for s, e, *_ in all_skip)
    print(f"\nOK: {n_extracted} lines to extract")

    # ── Copy bodies verbatim ──────────────────────────────────────────
    bodies = {}
    for start, end, name, *_ in all_skip:
        bodies[name] = lines[start - 1 : end]

    # ── Build launch_pipeline.py ──────────────────────────────────────
    lp = _build_module(
        "LaunchPipeline",
        "Agent launch pipeline — worktree resolution, tmux session creation, "
        "prompt delivery, and the create/restart orchestrators. Extracted from "
        "AgentManager per design_docs/manager_py_decomposition_prompt.md.",
        LAUNCH_METHODS, bodies, methods,
        extra_imports=[
            "import asyncio", "import logging", "import shlex", "import time",
            "import uuid", "from datetime import datetime", "from pathlib import Path",
            "from typing import Any, Dict, List, Optional, Tuple", "", "import libtmux", "",
            "from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME, DESIGN_CONTEXT_SUBDIR",
            "from src.core.database import (",
            "    Agent, AgentLog, BoardConfig, DatabaseManager, Task, TaskStatus, get_db,",
            ")",
            "from src.core.simple_config import get_config",
            "from src.core.worktree_manager import WorktreeManager",
            "from src.interfaces import LaunchResult, LLMProviderInterface, get_cli_agent",
        ],
    )

    # ── Build terminator.py ───────────────────────────────────────────
    tm = _build_module(
        "Terminator",
        "Agent termination — graceful shutdown, WIP commit, tmux cleanup. "
        "Extracted from AgentManager per design_docs/manager_py_decomposition_prompt.md.",
        TERMINATOR_METHODS, bodies, methods,
        extra_imports=[
            "import logging", "import time", "from datetime import datetime",
            "from pathlib import Path", "from typing import Optional", "",
            "from src.core.constants import CONTEXT_DIR_NAME",
            "from src.core.database import (",
            "    Agent, AgentLog, DatabaseManager, Task, TaskStatus, get_db,",
            ")",
        ],
    )

    # ── Build rewritten manager.py ────────────────────────────────────
    new_mgr = list(lines[:init_end])

    new_mgr.append("")
    new_mgr.append("        # Launch-pipeline collaborator (decomposition).")
    new_mgr.append("        from src.agents.launch_pipeline import LaunchPipeline")
    new_mgr.append("        self._launch = LaunchPipeline(self)")
    new_mgr.append("")
    new_mgr.append("        # Terminator collaborator (decomposition).")
    new_mgr.append("        from src.agents.terminator import Terminator")
    new_mgr.append("        self._terminator = Terminator(self)")

    cursor = init_end
    for start, end, name, is_async, is_static, node in all_skip:
        new_mgr.extend(lines[cursor : start - 1])
        new_mgr.extend(_build_delegator(name, is_async, is_static, node, methods))
        cursor = end
    new_mgr.extend(lines[cursor:])

    # ── Syntax check ──────────────────────────────────────────────────
    for label, text in [("launch_pipeline.py", lp), ("terminator.py", tm)]:
        try:
            ast.parse(text)
        except SyntaxError as ex:
            fail(f"{label} line {ex.lineno}: {ex.msg}")
        print(f"OK: {label} valid syntax")

    mgr_text = "\n".join(new_mgr) + "\n"
    try:
        ast.parse(mgr_text)
    except SyntaxError as ex:
        fail(f"manager.py line {ex.lineno}: {ex.msg}")
    print("OK: manager.py valid syntax")

    # ── Lossless check ────────────────────────────────────────────────
    skip_lines = set()
    for start, end, *_ in all_skip:
        for i in range(start, end + 1):
            skip_lines.add(i)

    for i, line in enumerate(lines, 1):
        if i in skip_lines or i <= init_end:
            continue
        if line not in set(new_mgr):
            fail(f"lost line {i}: {line!r}")
    print(f"OK: lossless — {len(lines)} -> {len(new_mgr)}")

    print(f"\n  launch_pipeline.py: {len(lp.splitlines())} lines")
    print(f"  terminator.py: {len(tm.splitlines())} lines")
    print(f"  manager.py: {len(lines)} -> {len(new_mgr)} (delta {len(new_mgr)-len(lines):+d})")

    if not apply:
        print("\ndry run complete — re-run with --apply")
        return

    import py_compile
    for path, content in [(LAUNCH_MODULE, lp), (TERMINATOR_MODULE, tm), (MANAGER, mgr_text)]:
        path.write_text(content)
        py_compile.compile(str(path), doraise=True)
        print(f"wrote {path}")


def _build_module(class_name, docstring, method_names, bodies, method_info, extra_imports):
    """Build a collaborator module string."""
    out = []
    out.append(f'"""{docstring}"""')
    out.append("")
    for imp in extra_imports:
        out.append(imp)
    out.append("")
    out.append("logger = logging.getLogger(__name__)")
    out.append("")
    out.append("")
    out.append(f"class {class_name}:")
    out.append(f'    """{docstring}"""')
    out.append("")
    out.append("    def __init__(self, agent_manager):")
    out.append("        self._agent_manager = agent_manager")
    out.append("")

    # Lazy properties for common attributes
    for attr in ["db_manager", "config", "branch_manager", "tmux_server",
                 "llm_provider", "phase_manager", "_messenger", "_prompt_builder",
                 "_output_capture"]:
        out.append("    @property")
        out.append(f"    def {attr}(self):")
        out.append(f"        return self._agent_manager.{attr}")
        out.append("")

    # Copy method bodies, rewriting self.X -> self.X or self._agent_manager.X
    for name in method_names:
        body = bodies[name]
        # Replace self.db_manager etc with self.db_manager (property handles redirect)
        # The properties above mean self.db_manager works directly — no rewriting needed.
        out.extend(body)
        out.append("")

    return "\n".join(out) + "\n"


def _build_delegator(name, is_async, is_static, node, all_methods):
    """Build a thin delegator stub from the AST node."""
    I = "    "

    # Determine which collaborator
    coll = "launch" if name in LAUNCH_METHODS else "terminator"

    # Get the original source lines for the signature
    source_lines = MANAGER.read_text().splitlines()
    sig_lines = []
    depth = 0
    i = node.lineno - 1
    while i < len(source_lines):
        line = source_lines[i]
        sig_lines.append(line)
        for ch in line:
            if ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
        if ':' in line and depth <= 0:
            break
        i += 1

    # Build forwarding args
    args = []
    for arg in node.args.args:
        if arg.arg == 'self':
            continue
        args.append(arg.arg)
    for arg in node.args.kwonlyargs:
        default_idx = node.args.kwonlyargs.index(arg)
        if node.args.kw_defaults[default_idx] is not None:
            args.append(f"{arg.arg}={arg.arg}")
        else:
            args.append(arg.arg)
    if node.args.vararg:
        args.append(f"*{node.args.vararg.arg}")
    if node.args.kwarg:
        args.append(f"**{node.args.kwarg.arg}")
    arg_names = ", ".join(args)

    # Build delegator
    out = []
    # Copy the exact signature line(s) but keep the original indentation
    for sl in sig_lines:
        out.append(sl)

    # Forwarding call
    prefix = "await " if is_async else ""
    out.append(f"{I}    return {prefix}self._{coll}.{name}({arg_names})")
    out.append("")
    return out


if __name__ == "__main__":
    main()
