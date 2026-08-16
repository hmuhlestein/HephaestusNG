#!/usr/bin/env python3
"""One-off mechanical split of src/mcp/autopilot_api.py into the
src/mcp/autopilot/ package (design_docs/backend_module_decomposition.md §3.2).

Pure line-level relocation via ast-derived spans. Assertions enforce:
  1. every top-level span matches the exact line numbers in the design doc,
  2. the 138-name symbol -> module mapping is complete (no gaps, no extras),
  3. each span sits inside the territory range its module claims (§3.2),
  4. the cross-module dependency graph matches the reviewed expectation,
  5. extraction is lossless (every line accounted for, remainder is only
     docstring/imports/comments/blanks, no code dropped).

Usage:
  python scripts/split_autopilot_api.py             # verify only (dry run)
  python scripts/split_autopilot_api.py --apply     # write the 8 new files

Deliberately does NOT: delete autopilot_api.py, edit server.py/monitor.py or
tests. Import curation (ruff F401 trim of the superset header) and call-site
retargeting are separate manual steps per the design doc §3.3/§4.
"""

import ast
import py_compile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "mcp" / "autopilot_api.py"
OUT = ROOT / "src" / "mcp" / "autopilot"
EXPECTED_TOTAL_LINES = 5724

# Header import block: original lines 3-38 (stdlib + fastapi + pydantic +
# sqlalchemy + core constants + server + prompt loader), verbatim superset.
HEADER_START, HEADER_END = 3, 38

# name -> (start_line, end_line) — the exact §3.2 line numbers, incl.
# decorator first line for route handlers.
EXPECTED_RANGES = {
    # _shared.py (territory 1-444, plus 5710-5724)
    "logger": (40, 40), "router": (42, 42),
    "DESIGN_QUEUE_DIR": (44, 44), "FEATURES_DIR": (45, 45),
    "_active_project_id_cache": (46, 46),
    "_queue_dir_by_project": (57, 57), "_features_dir_by_project": (58, 58),
    "ALLOWED_EXTENSIONS": (60, 60),
    "_get_active_project_id": (63, 69), "_invalidate_project_dirs": (72, 83),
    "_get_effective_queue_dir": (86, 151), "_get_effective_features_dir": (154, 212),
    "T": (217, 217), "_cache": (219, 219), "CACHE_TTL": (220, 220),
    "_cached": (223, 230), "_store": (233, 235), "_invalidate": (238, 240),
    "_safe_path": (246, 259), "_feature_status": (262, 267),
    "_extract_pr_url": (270, 301), "_get_latest_run_dir": (307, 312),
    "_read_json": (315, 319), "_read_jsonl_tail": (322, 338),
    "DesignQueueItem": (344, 349), "DesignQueueAdd": (352, 356),
    "FeatureSummary": (359, 369), "FeatureDetail": (372, 398),
    "PipelineStatus": (401, 433), "MessageItem": (436, 439),
    "configure_autopilot_api": (5715, 5724),
    # control_routes.py (territory 445-726, then 5283-5709)
    "get_pipeline_status": (445, 726),
    "start_pipeline": (5288, 5313), "_start_pipeline_reserved": (5316, 5405),
    "stop_pipeline": (5408, 5476), "cleanup_branches": (5479, 5517),
    "get_system_health": (5520, 5523), "run_health_audit": (5526, 5709),
    # queue_routes.py (territory 727-1789)
    "_get_queue_order_path": (732, 741), "_load_queue_order": (744, 751),
    "_save_queue_order": (754, 758), "list_design_queue": (761, 800),
    "QueueReorderRequest": (803, 805), "reorder_queue": (808, 827),
    "requeue_design": (830, 922), "rerun_design": (925, 1339),
    "repair_design": (1342, 1374), "spawn_repair_review_agent": (1377, 1476),
    "_run_repair": (1479, 1583), "get_repair_status": (1586, 1606),
    "DesignAddByPath": (1612, 1614), "add_design_by_path": (1617, 1728),
    "add_to_queue": (1731, 1763), "remove_from_queue": (1766, 1777),
    "get_queue_item_content": (1780, 1789),
    # project_routes.py (territory 1790-3799)
    "_ORDINAL_RE": (1794, 1794), "_design_id": (1797, 1800),
    "ProjectItem": (1803, 1813), "ProjectCreate": (1816, 1819),
    "ProjectUpdate": (1822, 1845), "CostEntryCreate": (1848, 1926),
    "DesignItem": (1929, 1936), "DesignReorderRequest": (1939, 1940),
    "DesignAddRequest": (1943, 1946),
    "_project_sync_locks": (1949, 1949), "_project_lock_guard": (1950, 1950),
    "_get_project_lock": (1953, 1957), "_get_design_queue_dir": (1960, 1965),
    "_extract_ordinal": (2010, 2018), "_sync_project_designs": (2021, 2124),
    "_validate_base_dir": (2127, 2136), "list_projects": (2139, 2162),
    "create_project": (2165, 2213), "get_project": (2216, 2236),
    "update_project": (2239, 2316), "delete_project": (2319, 2365),
    "create_cost_entry": (2371, 2444),
    "CostEntrySummary": (2450, 2462), "TaskCostSummary": (2465, 2471),
    "WorkflowCostSummary": (2474, 2480), "FeatureCostSummary": (2483, 2489),
    "DesignCostSummary": (2492, 2498), "ProjectCostSummary": (2501, 2510),
    "get_task_costs": (2513, 2565), "get_workflow_costs": (2568, 2620),
    "get_feature_costs": (2623, 2675), "get_design_costs": (2678, 2730),
    "get_project_costs": (2733, 2794),
    "sync_project_designs": (2800, 2814), "reload_project_designs": (2817, 2830),
    "list_project_designs": (2833, 2860), "add_project_design": (2863, 2917),
    "BrowseEntry": (2920, 2923), "BrowseResult": (2926, 2929),
    "browse_project_files": (2932, 2972),
    "browse_project_file_content": (2975, 2994),
    "reorder_project_designs": (2997, 3021), "remove_project_design": (3024, 3235),
    "get_project_design_content": (3238, 3255),
    "get_project_design_status": (3258, 3799),
    # feature_routes.py (territory 3800-5021; _find_archived_feature_report
    # relocated here from the project territory per the §3.2 call-site rule)
    "_find_archived_feature_report": (1968, 2007),
    "get_workflow_feature_report": (3802, 3863),
    "get_workflow_decomposition_review": (3866, 3899),
    "_scan_features": (3905, 3966), "list_features": (3969, 3971),
    "pause_feature": (3974, 4023), "resume_feature": (4026, 4095),
    "ReviewModeUpdate": (4101, 4102), "FeatureReviewRequest": (4105, 4107),
    "set_review_mode": (4110, 4123), "_review_phase0_decomposition": (4126, 4297),
    "review_feature": (4300, 4512), "delete_feature": (4515, 4641),
    "_spawn_agent_for_task": (4644, 4686), "get_feature_detail": (4689, 4766),
    "_resolve_feature_docs_base": (4769, 4785),
    "list_feature_record_docs": (4788, 4852),
    "get_feature_record_doc": (4855, 4886),
    "get_feature_record_report": (4889, 4938),
    "get_feature_report": (4941, 4950), "get_feature_doc": (4953, 4969),
    "download_feature_report": (4972, 4985),
    "list_feature_logs": (4988, 5008), "get_feature_log": (5011, 5021),
    # message_routes.py (territory 5022-5182)
    "get_messages": (5027, 5047), "get_archived_messages": (5050, 5072),
    "archive_message": (5075, 5108), "unarchive_message": (5111, 5128),
    "unarchive_all_messages": (5131, 5144), "cleanup_old_archives": (5147, 5160),
    "get_logs": (5163, 5182),
    # intervention_routes.py (territory 5183-5282)
    "STALE_INPUT_SECONDS": (5187, 5187), "HumanInputRequest": (5190, 5195),
    "HumanInputResponse": (5198, 5201), "_find_pending_input": (5204, 5226),
    "get_human_input_request": (5229, 5239),
    "submit_human_input": (5242, 5270), "dismiss_human_input": (5273, 5282),
}

# name -> target module (138 names: 123 def/class + 15 module constants)
SYM2MOD = {}
for _n in ["logger", "router", "DESIGN_QUEUE_DIR", "FEATURES_DIR",
           "_active_project_id_cache", "_queue_dir_by_project",
           "_features_dir_by_project", "ALLOWED_EXTENSIONS", "T", "_cache",
           "CACHE_TTL", "_get_active_project_id", "_invalidate_project_dirs",
           "_get_effective_queue_dir", "_get_effective_features_dir",
           "_cached", "_store", "_invalidate", "_safe_path", "_feature_status",
           "_extract_pr_url", "_get_latest_run_dir", "_read_json",
           "_read_jsonl_tail", "DesignQueueItem", "DesignQueueAdd",
           "FeatureSummary", "FeatureDetail", "PipelineStatus", "MessageItem",
           "configure_autopilot_api"]:
    SYM2MOD[_n] = "_shared"
for _n in ["get_pipeline_status", "start_pipeline", "_start_pipeline_reserved",
           "stop_pipeline", "cleanup_branches", "get_system_health",
           "run_health_audit"]:
    SYM2MOD[_n] = "control_routes"
for _n in ["_get_queue_order_path", "_load_queue_order", "_save_queue_order",
           "list_design_queue", "QueueReorderRequest", "reorder_queue",
           "requeue_design", "rerun_design", "repair_design",
           "spawn_repair_review_agent", "_run_repair", "get_repair_status",
           "DesignAddByPath", "add_design_by_path", "add_to_queue",
           "remove_from_queue", "get_queue_item_content"]:
    SYM2MOD[_n] = "queue_routes"
for _n in ["_ORDINAL_RE", "_design_id", "ProjectItem", "ProjectCreate",
           "ProjectUpdate", "CostEntryCreate", "DesignItem",
           "DesignReorderRequest", "DesignAddRequest", "_project_sync_locks",
           "_project_lock_guard", "_get_project_lock", "_get_design_queue_dir",
           "_extract_ordinal", "_sync_project_designs", "_validate_base_dir",
           "list_projects", "create_project", "get_project", "update_project",
           "delete_project", "create_cost_entry", "CostEntrySummary",
           "TaskCostSummary", "WorkflowCostSummary", "FeatureCostSummary",
           "DesignCostSummary", "ProjectCostSummary", "get_task_costs",
           "get_workflow_costs", "get_feature_costs", "get_design_costs",
           "get_project_costs", "sync_project_designs",
           "reload_project_designs", "list_project_designs",
           "add_project_design", "BrowseEntry", "BrowseResult",
           "browse_project_files", "browse_project_file_content",
           "reorder_project_designs", "remove_project_design",
           "get_project_design_content", "get_project_design_status"]:
    SYM2MOD[_n] = "project_routes"
for _n in ["get_workflow_feature_report", "get_workflow_decomposition_review",
           "_scan_features", "list_features", "pause_feature", "resume_feature",
           "ReviewModeUpdate", "FeatureReviewRequest", "set_review_mode",
           "_review_phase0_decomposition", "review_feature", "delete_feature",
           "_find_archived_feature_report", "_spawn_agent_for_task",
           "get_feature_detail", "_resolve_feature_docs_base",
           "list_feature_record_docs", "get_feature_record_doc",
           "get_feature_record_report", "get_feature_report", "get_feature_doc",
           "download_feature_report", "list_feature_logs", "get_feature_log"]:
    SYM2MOD[_n] = "feature_routes"
for _n in ["get_messages", "get_archived_messages", "archive_message",
           "unarchive_message", "unarchive_all_messages",
           "cleanup_old_archives", "get_logs"]:
    SYM2MOD[_n] = "message_routes"
for _n in ["STALE_INPUT_SECONDS", "HumanInputRequest", "HumanInputResponse",
           "_find_pending_input", "get_human_input_request",
           "submit_human_input", "dismiss_human_input"]:
    SYM2MOD[_n] = "intervention_routes"

# (module, territory_start, territory_end) — the §3.2 line partition
TERRITORIES = [
    ("_shared", 1, 444),
    ("control_routes", 445, 726),
    ("queue_routes", 727, 1789),
    ("project_routes", 1790, 3799),
    ("feature_routes", 3800, 5021),
    ("message_routes", 5022, 5182),
    ("intervention_routes", 5183, 5282),
    ("control_routes", 5283, 5709),
    ("_shared", 5710, 5724),
]

# name -> territory override (relocated symbol)
TERRITORY_OVERRIDE = {"_find_archived_feature_report": "feature_routes"}

# module -> sorted list of (name, source_module) cross-module references
EXPECTED_CROSS_DEPS = {
    "_shared": [],
    "control_routes": sorted([
        ("ALLOWED_EXTENSIONS", "_shared"), ("PipelineStatus", "_shared"),
        ("_cached", "_shared"), ("_get_active_project_id", "_shared"),
        ("_get_effective_queue_dir", "_shared"), ("_get_latest_run_dir", "_shared"),
        ("_invalidate", "_shared"), ("_read_json", "_shared"),
        ("_read_jsonl_tail", "_shared"), ("_store", "_shared"),
    ]),
    "queue_routes": sorted([
        ("ALLOWED_EXTENSIONS", "_shared"), ("DesignQueueAdd", "_shared"),
        ("DesignQueueItem", "_shared"), ("_cached", "_shared"),
        ("_get_effective_queue_dir", "_shared"), ("_invalidate", "_shared"),
        ("_safe_path", "_shared"), ("_store", "_shared"),
    ]),
    "project_routes": sorted([
        ("ALLOWED_EXTENSIONS", "_shared"), ("_cached", "_shared"),
        ("_extract_pr_url", "_shared"),
        ("_find_archived_feature_report", "feature_routes"),
        ("_invalidate", "_shared"), ("_safe_path", "_shared"), ("_store", "_shared"),
    ]),
    "feature_routes": sorted([
        ("FEATURES_DIR", "_shared"), ("FeatureDetail", "_shared"),
        ("FeatureSummary", "_shared"), ("_cached", "_shared"),
        ("_extract_pr_url", "_shared"), ("_feature_status", "_shared"),
        ("_get_effective_features_dir", "_shared"), ("_invalidate", "_shared"),
        ("_read_json", "_shared"), ("_safe_path", "_shared"), ("_store", "_shared"),
    ]),
    "message_routes": sorted([
        ("MessageItem", "_shared"), ("_cached", "_shared"),
        ("_get_latest_run_dir", "_shared"), ("_read_jsonl_tail", "_shared"),
        ("_store", "_shared"),
    ]),
    "intervention_routes": [("_invalidate", "_shared")],
}

DOCTSTR = {
    "_shared": "Shared constants, cross-cutting helpers, and Pydantic models for the Autopilot API.",
    "control_routes": "Pipeline control routes: status, start, stop, cleanup, health.",
    "queue_routes": "Design-queue routes: listing, reorder, requeue, rerun, repair, add/remove.",
    "project_routes": "Project routes: CRUD, cost tracking, design management, file browsing.",
    "feature_routes": "Feature routes: reports, review mode, pause/resume, docs, logs.",
    "message_routes": "Message and log routes.",
    "intervention_routes": "Human-input intervention routes (file-based).",
}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def main() -> None:
    apply = "--apply" in sys.argv
    if not SRC.exists():
        print("src/mcp/autopilot_api.py no longer exists — the split was "
              "already applied. This script is a one-off tool for that split; "
              "see design_docs/backend_module_decomposition.md §3.2/§5 for "
              "the resulting module layout.")
        return
    lines = SRC.read_text().splitlines()
    if len(lines) != EXPECTED_TOTAL_LINES:
        fail(f"file is {len(lines)} lines, expected {EXPECTED_TOTAL_LINES}")
    tree = ast.parse("\n".join(lines))

    # ── 1. derive top-level spans ─────────────────────────────────────
    spans: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            spans[node.name] = (start, node.end_lineno)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    spans[t.id] = (node.lineno, node.end_lineno)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                spans[node.target.id] = (node.lineno, node.end_lineno)

    if len(spans) != 138:
        fail(f"found {len(spans)} top-level names, expected 138")
    if set(spans) != set(EXPECTED_RANGES):
        fail(f"name set mismatch: only-in-file={set(spans) - set(EXPECTED_RANGES)} "
             f"only-in-doc={set(EXPECTED_RANGES) - set(spans)}")
    for name, (s, e) in spans.items():
        if EXPECTED_RANGES[name] != (s, e):
            fail(f"{name}: ast span {s}-{e} != doc range {EXPECTED_RANGES[name]}")
    for name, (s, e) in spans.items():
        if set(SYM2MOD) != set(spans):
            fail("SYM2MOD name set != file name set")
    print(f"OK: 138 spans match doc §3.2 exactly")

    # ── 2. territory check ────────────────────────────────────────────
    for name, (s, e) in sorted(spans.items(), key=lambda kv: kv[1][0]):
        mod = SYM2MOD[name]
        if name in TERRITORY_OVERRIDE:
            # relocated symbol: physically sits in its source territory, moves
            # to a different module — only assert it's inside *some* territory
            ok = any(ts <= s and e <= te for _m, ts, te in TERRITORIES)
        else:
            ok = any(m == mod and ts <= s and e <= te for m, ts, te in TERRITORIES)
        if not ok:
            fail(f"{name} ({s}-{e}) not inside a {mod} territory")
    # partition must be exact
    parts = [(a, b) for _m, a, b in TERRITORIES]
    total = sum(b - a + 1 for a, b in parts)
    if total != EXPECTED_TOTAL_LINES:
        fail(f"territory partition sums to {total}, expected {EXPECTED_TOTAL_LINES}")
    print(f"OK: territory partition exact ({total} lines)")

    # ── 3. cross-module dependency derivation ─────────────────────────
    top_nodes = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top_nodes[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    top_nodes[t.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            top_nodes[node.target.id] = node

    deps: dict[str, set[tuple[str, str]]] = {m: set() for m in SYM2MOD.values()}
    for name, node in top_nodes.items():
        m = SYM2MOD[name]
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id in SYM2MOD and SYM2MOD[sub.id] != m and sub.id not in ("logger", "router"):
                    deps[m].add((sub.id, SYM2MOD[sub.id]))
    for m in deps:
        if sorted(deps[m]) != EXPECTED_CROSS_DEPS[m]:
            fail(f"cross-deps for {m}: derived={sorted(deps[m])} "
                 f"expected={EXPECTED_CROSS_DEPS[m]}")
    print("OK: cross-module dependency graph matches expectation (no cycles)")

    # ── 4. build module files ─────────────────────────────────────────
    superset = lines[HEADER_START - 1:HEADER_END]
    depth = 0
    for i, h in enumerate(superset, HEADER_START):
        stripped = h.strip()
        if depth == 0 and stripped and not stripped.startswith(("import ", "from ", "#")):
            fail(f"unexpected non-import header line {i}: {h!r}")
        depth += h.count("(") - h.count(")")
    if depth != 0:
        fail(f"unbalanced parens in import header (depth {depth})")

    contents: dict[str, list[str]] = {}
    for mod in DOCTSTR:
        out: list[str] = [f'"""{DOCTSTR[mod]} — extracted from src/mcp/autopilot_api.py (backend_module_decomposition.md §3.2)."""', ""]
        out += superset
        out.append("")
        by_src: dict[str, list[str]] = {}
        for (nm, src_mod) in sorted(EXPECTED_CROSS_DEPS[mod]):
            if src_mod == "_shared" and nm == "FEATURES_DIR":
                continue  # mutable global: handled via `_shared.FEATURES_DIR`
            by_src.setdefault(src_mod, []).append(nm)
        for src_mod in sorted(by_src):
            names = ", ".join(by_src[src_mod])
            out.append(f"from src.mcp.autopilot.{src_mod} import {names}")
        if mod == "feature_routes":
            out.append("from src.mcp.autopilot import _shared")
        out.append("")
        if mod != "_shared":
            out.append("logger = logging.getLogger(__name__)")
            out.append("")
            out.append("router = APIRouter()")
            out.append("")
        for nm, (s, e) in sorted(
                ((n, sp) for n, sp in spans.items() if SYM2MOD[n] == mod),
                key=lambda kv: kv[1][0]):
            out += lines[s - 1:e]
            out.append("")
        while out and out[-1] == "":
            out.pop()
        contents[mod] = out

    init = [
        '"""Aggregator router for the Autopilot API package (backend_module_decomposition.md §3.2)."""',
        "",
        "from fastapi import APIRouter",
        "",
        "from src.mcp.autopilot import (",
        "    control_routes,",
        "    feature_routes,",
        "    intervention_routes,",
        "    message_routes,",
        "    project_routes,",
        "    queue_routes,",
        ")",
        "",
        'router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])',
        "",
        "router.include_router(control_routes.router)",
        "router.include_router(queue_routes.router)",
        "router.include_router(project_routes.router)",
        "router.include_router(feature_routes.router)",
        "router.include_router(message_routes.router)",
        "router.include_router(intervention_routes.router)",
    ]

    # ── 5. lossless check ─────────────────────────────────────────────
    claimed: set[int] = set()
    for (s, e) in spans.values():
        for i in range(s, e + 1):
            if i in claimed:
                fail(f"line {i} claimed twice")
            claimed.add(i)
    remainder = [i for i in range(1, len(lines) + 1) if i not in claimed]
    import re
    for i in remainder:
        l = lines[i - 1]
        if not (not l.strip()
                or l.lstrip().startswith(("#", "import ", "from "))
                or l.startswith(" ")  # import continuation lines
                or l == ")"  # closing paren of multi-line import
                or i <= 2):
            fail(f"remainder line {i} is code, not header/comment/blank: {l!r}")
    n_extracted = sum(e - s + 1 for s, e in spans.values())
    if len(remainder) != len(lines) - n_extracted:
        fail("remainder/extracted line accounting mismatch")
    print(f"OK: lossless — {n_extracted} extracted lines + {len(remainder)} "
          f"header/comment/blank remainder lines = {len(lines)}")

    # syntax-check every output before writing anything
    for mod, out in list(contents.items()) + [("__init__", init)]:
        text = "\n".join(out) + "\n"
        try:
            ast.parse(text)
        except SyntaxError as ex:
            fail(f"{mod}: extracted content is not valid syntax: {ex}")
    print("OK: all 8 outputs parse as valid Python")

    for mod, out in contents.items():
        print(f"  {mod}.py: {len(out)} lines, "
              f"{sum(1 for n, sp in spans.items() if SYM2MOD[n] == mod)} symbols")
    print(f"  __init__.py: {len(init)} lines (aggregator)")

    if not apply:
        print("\ndry run complete — re-run with --apply to write files")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    for mod, out in contents.items():
        p = OUT / f"{mod}.py"
        p.write_text("\n".join(out) + "\n")
        py_compile.compile(str(p), doraise=True)
    (OUT / "__init__.py").write_text("\n".join(init) + "\n")
    py_compile.compile(str(OUT / "__init__.py"), doraise=True)
    print(f"\nwrote {len(contents) + 1} files under {OUT}/ (py_compile clean)")
    print("next: ruff F401 trim of superset headers, feature_routes FEATURES_DIR")
    print("edit, then §4 call-site retargeting (server.py, monitor.py, tests)")


if __name__ == "__main__":
    main()
