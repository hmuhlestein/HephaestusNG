#!/usr/bin/env python3
"""One-off mechanical split of src/mcp/server.py into the src/mcp/server/
package (design_docs/phase_1c_server_decomposition.md).

Same methodology as scripts/split_autopilot_api.py: ast-derived top-level
spans, name -> module mapping, lossless accounting. Two differences from
that precedent, both specific to this file:

  1. server.py interleaves plain bootstrap statements (app = FastAPI(...),
     CORS setup, app.include_router(...) for already-extracted routers,
     app_context registration calls) between named defs/classes. These
     aren't captured by name-based extraction, so they're carried as four
     EXTRA_BLOCKS -- fixed line ranges anchored immediately after a named
     symbol's span and before the next one, verified to still abut those
     spans exactly (no gap, no overlap) before anything is written.
  2. server.py's routes are decorated directly on the FastAPI `app` object
     (`@app.get(...)`, `@app.post(...)`, etc.) rather than through a
     module-local `router`. Every extracted module except _shared,
     lifecycle, background_loops, and devtools_tools gets its own
     `router = APIRouter()`, and `@app.<verb>(` decorator lines (matched
     anchored at column 0, so it cannot touch `app.` references inside a
     function body) are rewritten to `@router.<verb>(`. lifecycle.py keeps
     `@app.on_event(...)` bound directly to `app` -- verified empirically
     (not assumed) that registering the same handler via
     `@router.on_event(...)` + `app.include_router(router)` double-fires it
     (FastAPI/Starlette quirk, confirmed live: startup ran twice). Routing
     lifecycle through a router would silently double-run startup/shutdown
     side effects (duplicate DB writes, duplicate agent restart
     notifications) -- a severe, easy-to-miss regression this split must
     not introduce.

Usage:
  python scripts/split_server.py             # verify only (dry run)
  python scripts/split_server.py --apply     # write the 9 new files
"""

import ast
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "mcp" / "server.py"
OUT = ROOT / "src" / "mcp" / "server"

# Original top-of-file import block, copied verbatim (superset) into every
# generated module -- same strategy as split_autopilot_api.py. Cleaned up
# per-module with `ruff check --fix` (F401) immediately after generation,
# not by hand-picking imports up front (this file has ~150 symbols; a
# by-hand per-module import list is where a script like this earns its
# keep by NOT needing to be exactly right the first time).
HEADER_START, HEADER_END = 3, 69

# name -> target module. 148 top-level names total (defs/classes/globals).
_SHARED = [
    "logger", "SELF_REVIEW_CHECKLIST_PROMPT",
    "_resolve_worktree_path", "_resolve_worktree_head_sha",
    "app", "config",
    "CreateTaskRequest", "CreateTaskResponse",
    "UpdateTaskStatusRequest", "UpdateTaskStatusResponse",
    "RegisterWorkflowDefinitionRequest", "StartWorkflowRequest",
    "ServerState", "server_state", "KNOWN_SYSTEM_AGENTS",
    "verify_agent_authentication", "_git_commit_push_already_landed",
    "_tmux_session_alive", "_build_phase_dict", "verify_agent_id",
    "_touch_agent_activity", "_resolve_agent_current_phase",
]
_LIFECYCLE = [
    "_resume_interrupted_workflows", "startup_event",
    "SAFE_RESTART_GRACE_SECONDS",
    "_notify_agents_of_restart", "_notify_and_pause_for_restart",
    "shutdown_event",
]
_BACKGROUND_LOOPS = [
    "process_queue", "background_queue_processor",
    "background_phase_advancement_sweep",
    "_LAST_BRANCH_HEAL_TIME", "_BRANCH_HEAL_INTERVAL_SECONDS",
    "_run_phase_advancement_sweep_once",
]
_AGENT_TASK_ROUTES = ["create_task", "validate_agent_id", "update_task_status"]
_TASK_ADMIN_ROUTES = [
    "get_workflows_endpoint", "pause_task_endpoint",
    "bump_task_priority_endpoint", "cancel_task_endpoint",
    "delete_task_endpoint", "complete_task_as_user",
    "cancel_queued_task_endpoint", "restart_task_endpoint",
    "get_queue_status_endpoint", "websocket_endpoint", "health_check",
    "root",
]
_OAUTH_ROUTES = [
    "oauth_server_metadata", "openid_config",
    "_auth_codes", "registered_clients", "_revoked_tokens", "_auth_lock",
    "_validate_redirect_uri", "_generate_code_challenge",
    "_rate_limit_lock", "_rate_limit_store", "RATE_LIMIT_WINDOW",
    "RATE_LIMIT_MAX", "_check_rate_limit",
    "register_client", "authorize_get", "authorize_post", "token",
    "revoke_token", "userinfo",
]
_WORKFLOW_EXECUTION_ROUTES = [
    "list_workflow_definitions", "register_workflow_definition",
    "list_workflow_executions", "start_workflow_execution",
    "get_workflow_execution", "complete_workflow_execution",
    "stop_workflow", "resume_workflow", "recover_workflows",
    "cancel_workflow",
]
_MCP_PROTOCOL = [
    "list_tools",
    "_tool_create_task", "_tool_save_memory", "_tool_search_memory",
    "_tool_get_task_status", "_tool_create_ticket", "_tool_search_tickets",
    "_tool_update_ticket_status", "_tool_broadcast_message",
    "_tool_send_message", "_tool_update_task_status",
    "_tool_complete_my_task", "_tool_submit_result",
    "_tool_submit_result_validation", "_tool_give_validation_review",
    "MCPToolSpec", "MCP_TOOL_REGISTRY", "_MCP_TOOLS", "execute_tool",
    "MCP_TOOL_NAMES", "list_resources", "get_resource", "sse_endpoint",
]
_DEVTOOLS_TOOLS = [
    "_devtools_connect", "_devtools_navigate", "_devtools_evaluate",
    "_devtools_screenshot", "_devtools_click", "_devtools_fill",
    "_devtools_get_console_errors", "_devtools_get_failed_requests",
    "_devtools_get_network_logs", "_devtools_get_performance",
    "_devtools_get_page_info", "_devtools_check_broken_images",
    "_devtools_wait_for_selector", "_devtools_get_cookies",
    "_devtools_close", "_DEVTOOLS_TOOLS", "_handle_devtools_tool",
]

SYM2MOD: dict[str, str] = {}
for _names, _mod in [
    (_SHARED, "_shared"), (_LIFECYCLE, "lifecycle"),
    (_BACKGROUND_LOOPS, "background_loops"),
    (_AGENT_TASK_ROUTES, "agent_task_routes"),
    (_TASK_ADMIN_ROUTES, "task_admin_routes"),
    (_OAUTH_ROUTES, "oauth_routes"),
    (_WORKFLOW_EXECUTION_ROUTES, "workflow_execution_routes"),
    (_MCP_PROTOCOL, "mcp_protocol"), (_DEVTOOLS_TOOLS, "devtools_tools"),
]:
    for _n in _names:
        SYM2MOD[_n] = _mod

EXPECTED_TOTAL_NAMES = 118

# module -> whether it needs its own `router = APIRouter()` and @app.->@router.
# decorator rewriting. lifecycle keeps @app.on_event bound to the real `app`
# (see module docstring). background_loops/devtools_tools have no routes.
NEEDS_ROUTER = {
    "_shared": False, "lifecycle": False, "background_loops": False,
    "agent_task_routes": True, "task_admin_routes": True,
    "oauth_routes": True, "workflow_execution_routes": True,
    "mcp_protocol": True, "devtools_tools": False,
}

DOCSTR = {
    "_shared": "Cross-cutting helpers, request/response models, and server state for the MCP server.",
    "lifecycle": "Startup, shutdown, and restart-notification handlers.",
    "background_loops": "Background queue processor and phase-advancement sweep.",
    "agent_task_routes": "Agent-facing task lifecycle routes: create_task, update_task_status.",
    "task_admin_routes": "User/dashboard task operations, health check, websocket, and root routes.",
    "oauth_routes": "OAuth 2.0 / OIDC authorization server routes.",
    "workflow_execution_routes": "Workflow-definition and workflow-execution routes.",
    "mcp_protocol": "MCP tool + resource protocol surface.",
    "devtools_tools": "Devtools-bridge tool handlers, referenced by mcp_protocol's tool dispatch.",
}

MODULE_ORDER = [
    "_shared", "lifecycle", "background_loops", "agent_task_routes",
    "task_admin_routes", "oauth_routes", "workflow_execution_routes",
    "mcp_protocol", "devtools_tools",
]

# (module, insert_after_name, start_line, end_line) -- verified against the
# CURRENT file below before use; these four ranges are bootstrap statements
# (not simple name = value assigns) that sit between two named spans.
EXTRA_BLOCKS = [
    ("_shared", "_resolve_worktree_head_sha", "app", 111, 113),
    ("_shared", "app", "config", 119, 120),
    ("_shared", "config", "CreateTaskRequest", 122, 160),
    ("_shared", "ServerState", "server_state", 470, 472),
    ("_shared", "server_state", "KNOWN_SYSTEM_AGENTS", 474, 485),
    ("background_loops", "process_queue", "background_queue_processor", 1588, 1596),
    ("oauth_routes", "openid_config", "_auth_codes", 3821, 3827),
    ("oauth_routes", "_auth_lock", "_validate_redirect_uri", 3832, 3833),
]


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    apply = "--apply" in sys.argv
    if not SRC.exists():
        print("src/mcp/server.py no longer exists -- the split was already "
              "applied. See design_docs/phase_1c_server_decomposition.md.")
        return
    lines = SRC.read_text().splitlines()
    tree = ast.parse("\n".join(lines))

    # ── 1. derive top-level spans ─────────────────────────────────────
    spans: dict[str, tuple[int, int]] = {}
    order: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            spans[node.name] = (start, node.end_lineno)
            order.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    spans[t.id] = (node.lineno, node.end_lineno)
                    order.append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            spans[node.target.id] = (node.lineno, node.end_lineno)
            order.append(node.target.id)

    if len(spans) != EXPECTED_TOTAL_NAMES:
        fail(f"found {len(spans)} top-level names, expected {EXPECTED_TOTAL_NAMES}")
    if set(spans) != set(SYM2MOD):
        fail(f"name set mismatch: only-in-file={set(spans) - set(SYM2MOD)} "
             f"only-in-mapping={set(SYM2MOD) - set(spans)}")
    print(f"OK: {len(spans)} top-level spans match the mapping exactly")

    # ── 2. verify the four extra blocks abut their anchors exactly ─────
    for mod, after_name, before_name, s, e in EXTRA_BLOCKS:
        after_end = spans[after_name][1]
        before_start = spans[before_name][0]
        if s != after_end + 1:
            fail(f"extra block for {mod} starts at {s}, expected {after_end + 1} "
                 f"(right after {after_name} ends at {after_end})")
        if e != before_start - 1:
            fail(f"extra block for {mod} ends at {e}, expected {before_start - 1} "
                 f"(right before {before_name} starts at {before_start})")
    print(f"OK: all {len(EXTRA_BLOCKS)} extra blocks abut their anchor spans exactly")

    # ── 3. lossless check across the whole file ─────────────────────────
    claimed: set[int] = set()
    for (s, e) in spans.values():
        for i in range(s, e + 1):
            if i in claimed:
                fail(f"line {i} claimed twice (span)")
            claimed.add(i)
    for (_mod, _a, _b, s, e) in EXTRA_BLOCKS:
        for i in range(s, e + 1):
            if i in claimed:
                fail(f"line {i} claimed twice (extra block)")
            claimed.add(i)
    remainder = [i for i in range(1, len(lines) + 1) if i not in claimed]
    for i in remainder:
        l = lines[i - 1]
        ok = (not l.strip()
              or l.lstrip().startswith(("#", "import ", "from ", '"""'))
              or l.startswith(" ")  # import continuation lines
              or l == ")"
              or i <= 2)
        if not ok:
            fail(f"remainder line {i} is code, not header/comment/blank: {l!r}")
    n_extracted = sum(e - s + 1 for s, e in spans.values())
    n_extra = sum(e - s + 1 for (_m, _a, _b, s, e) in EXTRA_BLOCKS)
    if len(remainder) != len(lines) - n_extracted - n_extra:
        fail("remainder/extracted line accounting mismatch")
    print(f"OK: lossless -- {n_extracted} named + {n_extra} extra-block + "
          f"{len(remainder)} header/comment/blank remainder = {len(lines)}")

    # ── 4. cross-module dependency derivation ───────────────────────────
    top_nodes: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top_nodes[t.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top_nodes[node.target.id] = node

    # module -> source_module -> set of names it needs imported from there.
    # A locally-scoped `from ... import X` inside a function body shadows an
    # outer name of the same X for that function's whole scope (Python hoists
    # local bindings) -- e.g. several handlers do a deferred
    # `from src.autopilot.orchestrator.engine_client import resume_workflow`
    # to reach a same-named-but-different function, not the sibling route
    # handler `resume_workflow` this split also extracts. Treating that as a
    # cross-module dependency is wrong and, worse, can fabricate an import
    # cycle that doesn't really exist. Any name locally bound anywhere in a
    # top-level node (import alias or assignment target) is excluded from
    # that node's cross-module reference collection.
    cross_deps: dict[str, dict[str, set[str]]] = {m: {} for m in MODULE_ORDER}
    for name, node in top_nodes.items():
        owner = SYM2MOD[name]
        locally_bound: set[str] = set()
        for sub in ast.walk(node):
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for alias in sub.names:
                    locally_bound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        locally_bound.add(t.id)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id == "logger" or sub.id in locally_bound:
                    continue  # own logger (never cross-imported) / locally shadowed
                if sub.id in SYM2MOD and SYM2MOD[sub.id] != owner:
                    src_mod = SYM2MOD[sub.id]
                    cross_deps[owner].setdefault(src_mod, set()).add(sub.id)
    for mod, by_src in cross_deps.items():
        for src_mod, names in by_src.items():
            print(f"  {mod} <- {src_mod}: {sorted(names)}")

    # ── 5. build module files ───────────────────────────────────────────
    superset = lines[HEADER_START - 1:HEADER_END]

    # name -> ordered list of (line_start, kind, payload) items per module,
    # where kind is "span" (a name) or "extra" (a fixed block), so extra
    # blocks are emitted in their correct position relative to named spans.
    items_by_mod: dict[str, list[tuple[int, str, object]]] = {m: [] for m in MODULE_ORDER}
    for name, (s, e) in spans.items():
        items_by_mod[SYM2MOD[name]].append((s, "span", name))
    for (mod, _a, _b, s, e) in EXTRA_BLOCKS:
        items_by_mod[mod].append((s, "extra", (s, e)))
    for mod in items_by_mod:
        items_by_mod[mod].sort(key=lambda t: t[0])

    contents: dict[str, list[str]] = {}
    for mod in MODULE_ORDER:
        out: list[str] = [f'"""{DOCSTR[mod]}\n\nExtracted from src/mcp/server.py (design_docs/phase_1c_server_decomposition.md).\n"""', ""]
        out += superset
        out.append("")
        if NEEDS_ROUTER[mod]:
            out.append("from fastapi import APIRouter")
            out.append("")
        for src_mod in sorted(cross_deps[mod]):
            names = ", ".join(sorted(cross_deps[mod][src_mod]))
            out.append(f"from src.mcp.server.{src_mod} import {names}")
        if cross_deps[mod]:
            out.append("")
        if mod != "_shared":
            out.append(f'logger = logging.getLogger("src.mcp.server.{mod}")')
            out.append("")
        if NEEDS_ROUTER[mod]:
            out.append("router = APIRouter()")
            out.append("")
        for _start, kind, payload in items_by_mod[mod]:
            if kind == "span":
                name = payload
                s, e = spans[name]
                block = lines[s - 1:e]
            else:
                s, e = payload
                block = lines[s - 1:e]
            if NEEDS_ROUTER[mod]:
                block = [
                    (ln.replace("@app.", "@router.", 1) if ln.startswith("@app.") else ln)
                    for ln in block
                ]
            out += block
            out.append("")
        while out and out[-1] == "":
            out.pop()
        contents[mod] = out

    init_lines = [
        '"""MCP server package -- FastAPI app assembly (design_docs/phase_1c_server_decomposition.md)."""',
        "",
        "from src.mcp.server import (",
    ]
    for mod in MODULE_ORDER:
        if mod != "_shared":
            init_lines.append(f"    {mod},")
    init_lines.append(")")
    init_lines.append("from src.mcp.server._shared import app  # noqa: F401 -- re-exported for uvicorn/tests")
    init_lines.append("")
    for mod in MODULE_ORDER:
        if NEEDS_ROUTER[mod]:
            init_lines.append(f"app.include_router({mod}.router)")
    init_lines.append("")

    # syntax-check every output before writing anything
    for mod, out in list(contents.items()) + [("__init__", init_lines)]:
        text = "\n".join(out) + "\n"
        try:
            ast.parse(text)
        except SyntaxError as ex:
            fail(f"{mod}: extracted content is not valid syntax: {ex}")
    print("OK: all 10 outputs parse as valid Python")

    for mod in MODULE_ORDER:
        n_syms = sum(1 for n in SYM2MOD if SYM2MOD[n] == mod)
        print(f"  {mod}.py: {len(contents[mod])} lines, {n_syms} symbols")
    print(f"  __init__.py: {len(init_lines)} lines (aggregator)")

    if not apply:
        print("\ndry run complete -- re-run with --apply to write files")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    for mod, out in contents.items():
        p = OUT / f"{mod}.py"
        p.write_text("\n".join(out) + "\n")
        py_compile.compile(str(p), doraise=True)
    (OUT / "__init__.py").write_text("\n".join(init_lines) + "\n")
    py_compile.compile(str(OUT / "__init__.py"), doraise=True)
    print(f"\nwrote {len(contents) + 1} files under {OUT}/ (py_compile clean)")


if __name__ == "__main__":
    main()
