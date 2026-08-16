#!/usr/bin/env python3
"""One-off AST-based extractor: src/autopilot/orchestrator.py -> src/autopilot/orchestrator/ package.

Per design_docs/backend_module_decomposition.md §3.3.
Strategy: mechanical line-level extraction with byte-lossless assertion,
then ruff-driven import generation.

Intentional, documented text changes on top of the verbatim move:
  1. OrchestratorLogger annotations quoted + TYPE_CHECKING import per submodule
  2. __file__ path depth +1 in worktree_integration._run_ash_scan and
     reporting._generate_design_report_html
  3. HEPHAESTUS_DIR +1 .parent in __init__
  4. Function-scoped imports for INIT_SCOPE_IMPORTS names
  5. Two blank lines restored between concatenated spans
"""
import ast
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

PATH = "src/autopilot/orchestrator.py"
OUT  = "src/autopilot/orchestrator"
CWD  = os.getcwd()
MARK = "# __IMPORTS__"

# ── helpers ──────────────────────────────────────────────────────────
def rel(p):
    return os.path.relpath(p, CWD)

def ruff(*args):
    """Run ruff, return JSON issues list."""
    r = subprocess.run(
        ["ruff", "check", "--no-cache", "--output-format", "json", *args],
        capture_output=True, text=True,
    )
    try:
        return json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return []

def f821_names(files):
    """Return {relpath: {name, ...}} for every F821 in the given files."""
    by = defaultdict(set)
    for iss in ruff("--select", "F821", *files):
        m = re.search(r"`([^`]+)`", iss["message"])
        if m:
            by[rel(iss["filename"])].add(m.group(1))
    return by

def read_file(path):
    return open(path).read()

def write_file(path, content):
    open(path, "w").write(content)

# ── symbol -> module mapping ─────────────────────────────────────────
M, CH = {}, {}
def assign(mod, names):
    for n in names:
        assert n not in M, f"duplicate mapping: {n}"
        M[n] = mod

assign("state.py", [
    "StopReason", "DesignStatus", "DesignEntry", "IterationResult", "FeatureReport",
    "PipelineState", "_get_project_context", "_set_project_context", "_delete_project_context",
    "_get_project_contexts_by_prefix", "_resolve_project_id", "_get_or_create_project_id",
    "_running_state_key", "PersistentPipelineState", "_workflow_belongs_to_project",
])
assign("engine_client.py", [
    "get_litellm_config", "file_hash", "api_get", "api_post", "update_task_status",
    "increment_task_retry_count", "terminate_agent_direct", "pause_workflow_direct",
    "complete_workflow_direct", "fail_workflow_direct", "pause_project_workflows",
    "create_agent_for_task_direct", "_update_orchestrator_status", "get_tasks", "get_agents",
    "peek_agent_output", "get_task_progress", "get_workflow_status", "get_active_workflows",
])
assign("policy.py", [
    "_workflow_appears_abandoned", "_update_resumed_workflow_recovery_attempts",
    "_escalate_stale_active_workflows", "attempt_recovery", "check_api_credits",
    "detect_hard_error", "detect_impasse", "detect_architectural_issue",
])
assign("queue.py", [
    "scan_design_queue", "_has_resumable_active_design", "pick_next_design", "_assess_run_health",
    "is_design_fully_complete", "_update_design_status", "_set_workflow_type", "_get_phase0_completion",
])
assign("worktree_integration.py", [
    "create_feature_folder", "copy_design_document", "_create_integration_worktree", "_cleanup_worktree",
    "sweep_completed_workflow_worktrees", "heal_orphaned_agent_branches",
    "_heal_orphaned_branches_for_project", "_create_designs_folder",
    "_recover_abandoned_workflows_missing_worktree", "_recover_abandoned_workflows_with_completed_phase",
    "_ensure_git_excluded", "_run_ash_scan",
])
assign("features.py", [
    "_create_feature_records", "_update_feature_status", "_sync_stale_feature_statuses",
    "_sync_stale_design_statuses", "_relink_features_to_workflows", "_clean_stale_assigned_tasks",
    "_validate_features_json", "_resolve_execution_order", "_sweep_stray_files",
])
assign("reporting.py", [
    "_report_path", "collect_report_summaries", "collect_files_created",
    "_generate_design_report_html", "_empty_report",
])
assign("phase_transitions.py", [
    "_try_advance_phases", "_retry_failed_tasks", "_retry_exhausted_paused_workflows", "_advance_phases",
    "_try_auto_resume_paused_workflow", "_release_stale_task_creation_claims",
    "_release_pending_phases_with_done_tasks", "_get_phase_statuses", "_claim_phase_task_creation",
    "_release_phase_task_creation_claim", "_case_start_first_phase", "_case_in_progress_no_tasks",
    "_case_completed_with_successor", "_manual_handoff_required", "_pause_for_manual_handoff",
    "_case_in_progress_complete", "_maybe_retry_failed_tasks", "_fire_phase_transition",
    "_gather_arbitration_context", "_build_arbitration_prompt", "_phase_currently_passes",
    "_trigger_arbitration", "_maybe_resolve_arbitration", "_read_arbitration_result",
    "_consume_arbitration_result", "_resolve_arbitration_outcome", "_cap_out_review_phase",
    "_create_phase_task", "_create_corrective_task", "_create_corrective_task_body",
    "_wait_for_task_terminal", "_negotiate_validation_fix", "_resume_stuck_workflow_tasks",
])
assign("__init__.py", [
    "_get_workflow_timeout", "_get_phase0_timeout", "_get_paused_workflow_retry_cooldown_seconds",
    "_get_paused_workflow_max_retry_cycles", "_register_monitored_workflow",
    "_unregister_monitored_workflow", "_is_workflow_monitored", "_resync_pipeline_registry",
    "OrchestratorLogger", "prompt_human", "_should_pause_for_review", "_pause_feature_for_review",
    "_wait_for_review_clearance", "_restore_phase0_completed_status", "_pause_phase0_for_review",
    "_wait_for_phase0_review_clearance", "finalize_phase0_workflow", "_wait_for_pending_reviews",
    "run_single_workflow", "run_phase0", "_run_one_feature", "run_feature_pipelines",
    "run_design_aggregate", "_archive_and_cleanup", "run_single_design", "_should_stop",
    "_interruptible_sleep", "_register_orchestrator_agent", "run_continuous_pipeline", "main",
])

# module-level constants -> home module
CH = {
    "HEPHAESTUS_DIR": "__init__.py", "API_BASE": "engine_client.py",
    "POLL_INTERVAL": "phase_transitions.py", "STUCK_THRESHOLD": "__init__.py",
    "DESIGN_QUEUE_SCAN_INTERVAL": "__init__.py", "HEARTBEAT_INTERVAL": "__init__.py",
    "MAX_WORKFLOW_TIME": "__init__.py", "ACTIVE_AGENT_STATUSES": "policy.py",
    "PARENT_PEEK_INTERVAL": "__init__.py", "MAX_PHASE0_TIME": "__init__.py",
    "MAX_PARALLEL_FEATURES": "__init__.py", "MAX_DESIGN_RETRIES": "queue.py",
    "STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS": "policy.py",
    "CLAIM_STALE_TIMEOUT_SECONDS": "phase_transitions.py",
    "_orchestrator_agent_id": "__init__.py", "_actively_monitored_lock": "__init__.py",
    "_actively_monitored_workflows": "__init__.py",
    "_advance_phases_locks": "phase_transitions.py", "_advance_phases_locks_guard": "phase_transitions.py",
    "_RUNNING_STATE_KEY_PREFIX": "state.py", "_RUNNING_STATE_KEY_LEGACY": "state.py",
    "SWEEP_ENABLED": "features.py", "_SWEEP_REPORT_NAMES": "features.py", "_STRAY_DIRS": "features.py",
    "_REPORT_SUBDIR": "reporting.py", "MANUAL_ONLY_PHASES": "phase_transitions.py",
    "ARBITRATION_CREATED_BY": "phase_transitions.py", "_stop_events": "__init__.py",
}

# Names submodules may need from __init__ -- must use function-scoped imports (circular)
INIT_SCOPE_IMPORTS = {
    "_orchestrator_agent_id", "_get_paused_workflow_retry_cooldown_seconds",
    "_get_paused_workflow_max_retry_cycles", "_should_pause_for_review", "_should_stop",
}

SUBMODULES = ["state.py", "engine_client.py", "policy.py", "queue.py",
              "worktree_integration.py", "features.py", "reporting.py", "phase_transitions.py"]
ALL_FILES = [f"{OUT}/{f}" for f in SUBMODULES + ["__init__.py"]]

DOCSTRINGS = {
    "state.py":              '"""Orchestrator state: data classes, project-context persistence, pipeline state."""',
    "engine_client.py":      '"""Orchestrator <-> backend/LiteLLM I/O helpers."""',
    "policy.py":             '"""Stuck/health/credit detection and recovery decisions."""',
    "queue.py":              '"""Design-queue scanning, picking, and status."""',
    "worktree_integration.py": '"""Pipeline-level worktree/git orchestration and security scanning."""',
    "features.py":           '"""Feature-Model DB record bookkeeping."""',
    "reporting.py":          '"""Pure report/artifact generation (no DB writes)."""',
    "phase_transitions.py":  '"""Control-loop engine: goto/retry/continue state machine, arbitration, phase-task creation."""',
}

# ── collect spans ────────────────────────────────────────────────────
src_text = open(PATH).read()
src_lines = src_text.splitlines(keepends=True)
tree = ast.parse(src_text)

sym_spans, const_spans = [], []
for n in tree.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        assert n.name in M, f"unmapped symbol: {n.name}"
        start = (n.decorator_list[0].lineno if n.decorator_list else n.lineno) - 1
        sym_spans.append((n.name, start, n.end_lineno - 1))
    elif isinstance(n, ast.Assign):
        ts = [t.id for t in n.targets if isinstance(t, ast.Name)]
        if ts and ts[0] != "logger":
            assert ts[0] in CH, f"unmapped const: {ts[0]}"
            const_spans.append((ts[0], n.lineno - 1, n.end_lineno - 1))
    elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
        assert n.target.id in CH, f"unmapped const: {n.target.id}"
        const_spans.append((n.target.id, n.lineno - 1, n.end_lineno - 1))

all_spans = sym_spans + const_spans
owner = [None] * len(src_lines)
for nm, s, e in all_spans:
    for i in range(s, e + 1):
        assert owner[i] is None, f"line {i+1}: double-owned by {nm} and {owner[i]}"
        owner[i] = nm
assert len(M) == 139, f"expected 139 symbols, got {len(M)}"

def home(nm):
    return M.get(nm, CH.get(nm))

line_home = [None] * len(src_lines)
for nm, s, e in all_spans:
    h = home(nm)
    for i in range(s, e + 1):
        line_home[i] = h

# leading contiguous import block (after docstring)
lead = []
for n in tree.body:
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        lead.append(n)
    elif (not lead and isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)
          and isinstance(n.value.value, str)):
        continue  # docstring
    else:
        break
LEAD_S, LEAD_E = lead[0].lineno - 1, lead[-1].end_lineno - 1

# Build: bound_name -> original import statement text (single-name form)
name_to_stmt = {}
for n in tree.body:
    if isinstance(n, ast.Import):
        for a in n.names:
            k = a.asname or a.name.split(".")[0]
            stmt = f"import {a.name}" + (f" as {a.asname}" if a.asname else "")
            name_to_stmt[k] = stmt
    elif isinstance(n, ast.ImportFrom):
        for a in n.names:
            k = a.asname or a.name
            stmt = f"from {n.module} import {a.name}" + (f" as {a.asname}" if a.asname else "")
            name_to_stmt[k] = stmt

# ── PART 1: write files + lossless check ────────────────────────────
os.makedirs(OUT, exist_ok=True)

def span_text(s, e):
    return "".join(src_lines[s:e+1])

def module_body(mod):
    items = sorted([x for x in all_spans if home(x[0]) == mod], key=lambda x: x[1])
    text = "\n\n".join(span_text(s, e) for _, s, e in items)
    # intentional text changes
    text = re.sub(r'(: |-> )OrchestratorLogger\b', r'\1"OrchestratorLogger"', text)
    if mod == "worktree_integration.py":
        text = text.replace(
            'Path(__file__).resolve().parents[2]',
            'Path(__file__).resolve().parents[3]  # one .parent deeper: now a package module',
        )
    if mod == "reporting.py":
        text = text.replace(
            'Path(__file__).parent / "templates"',
            'Path(__file__).parent.parent / "templates"  # one .parent deeper: now a package module',
        )
    return text

# write submodules with marker placeholder
for mod in SUBMODULES:
    body = module_body(mod)
    write_file(f"{OUT}/{mod}", DOCSTRINGS[mod] + "\n\n" + MARK + "\n\n" + body)

# write __init__ with marker placeholder
init_lines = [src_lines[i] for i in range(len(src_lines)) if line_home[i] in (None, "__init__.py")]
rem = "".join(init_lines)
rem = rem.replace(
    "HEPHAESTUS_DIR = Path(__file__).parent.parent.parent",
    "HEPHAESTUS_DIR = Path(__file__).parent.parent.parent.parent  # one .parent deeper: now a package __init__",
)
assert "parent.parent.parent.parent" in rem
rl = rem.splitlines(keepends=True)
rl[LEAD_S:LEAD_E + 1] = [MARK + "\n"]
write_file(f"{OUT}/__init__.py", "".join(rl))

# lossless reassembly check
recon, sp = [], 0
ss = sorted(all_spans, key=lambda x: x[1])
for i in range(len(src_lines)):
    if owner[i] is None:
        recon.append(src_lines[i])
    else:
        while sp < len(ss) and i > ss[sp][2]:
            sp += 1
        if i == ss[sp][1]:
            recon.append(span_text(ss[sp][1], ss[sp][2]))
assert "".join(recon) == src_text, "LOSSLESS REASSEMBLY FAILED"
print("LOSSLESS REASSEMBLY: OK")

# ── PART 2: superset headers ────────────────────────────────────────
orig_hdr = "".join(src_lines[LEAD_S:LEAD_E + 1]).rstrip("\n")

def names_in(mod):
    return sorted(
        set(n for n, h in M.items() if h == mod)
        | set(n for n, h in CH.items() if h == mod)
    )

def render_submodule_header(mod):
    cross = []
    for other in SUBMODULES:
        if other == mod:
            continue
        ns = names_in(other)
        if ns:
            cross.append(
                f"from src.autopilot.orchestrator.{other[:-3]} import (\n"
                + "\n".join(f"    {n}," for n in ns)
                + "\n)"
            )
    h = orig_hdr + "\n"
    if cross:
        h += "\n" + "\n".join(cross) + "\n"
    h += "\nfrom typing import TYPE_CHECKING\n\n"
    h += "if TYPE_CHECKING:\n    from src.autopilot.orchestrator import OrchestratorLogger\n"
    h += "\nimport logging\nlogger = logging.getLogger(__name__)\n"
    return h

def render_init_header():
    cross = []
    for mod in SUBMODULES:
        ns = names_in(mod)
        if ns:
            cross.append(
                f"from src.autopilot.orchestrator.{mod[:-3]} import (\n"
                + "\n".join(f"    {n}," for n in ns)
                + "\n)"
            )
    h = orig_hdr + "\n"
    if cross:
        h += "\n" + "\n".join(cross) + "\n"
    return h

# replace marker in each file
for mod in SUBMODULES:
    p = f"{OUT}/{mod}"
    content = read_file(p).replace(MARK, render_submodule_header(mod))
    write_file(p, content)

p = f"{OUT}/__init__.py"
content = read_file(p).replace(MARK, render_init_header())
write_file(p, content)

# ── PART 3: trim unused imports ──────────────────────────────────────
print("\n=== Trimming unused imports ===")

# Submodules: ruff --fix works fine
r = subprocess.run(
    ["ruff", "check", "--select", "F401,F811", "--fix", "--no-cache", *[f"{OUT}/{f}" for f in SUBMODULES]],
    capture_output=True, text=True,
)
print(f"  submodules: {'trimmed' if r.stdout.strip() else 'clean'}")

# __init__: ruff won't auto-fix (re-export heuristic) -- remove unused manually
def trim_init_f401():
    """Remove unused imports from __init__.py by re-deriving what the driver needs."""
    ip = f"{OUT}/__init__.py"
    # Strategy: strip ALL imports, then let F821 tell us what's needed
    text = read_file(ip)
    t = ast.parse(text)
    imports = [n for n in t.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if not imports:
        return
    # Remove each import line individually
    lines = text.split("\n")
    to_remove = set()
    for n in imports:
        for ln in range(n.lineno - 1, n.end_lineno):
            to_remove.add(ln)
    new_lines = [l for i, l in enumerate(lines) if i not in to_remove]
    write_file(ip, "\n".join(new_lines))
    print(f"  Stripped {len(imports)} import nodes ({len(to_remove)} lines)")
    # Now F821 loop will add back exactly what's needed (in PART 4)

trim_init_f401()


# ── PART 4: resolve remaining F821 ──────────────────────────────────
print("\n=== Resolving F821 ===")

def add_import_at_top(fname, stmt):
    """Insert an import statement after the last existing import in the file."""
    text = read_file(fname)
    if stmt in text:
        return False
    t = ast.parse(text)
    last = max(
        (n.end_lineno for n in t.body if isinstance(n, (ast.Import, ast.ImportFrom))),
        default=12,
    )
    ll = text.split("\n")
    ll.insert(last, stmt)
    write_file(fname, "\n".join(ll))
    return True

# Iteratively resolve F821 across all files
emitted = defaultdict(set)  # fname -> {stmt, ...}
for iteration in range(30):
    undefs = f821_names(ALL_FILES)
    if not any(undefs.values()):
        print(f"  All F821 clean after {iteration} iterations")
        break

    total_fixed = 0
    for fname, names in undefs.items():
        if not names:
            continue
        for name in sorted(names):
            # _threading special case
            if name == "_threading" and "import threading as _threading" not in read_file(fname):
                text = read_file(fname)
                ll = text.split("\n")
                for i, l in enumerate(ll):
                    if l.strip() == "import threading":
                        ll.insert(i + 1, "import threading as _threading")
                        write_file(fname, "\n".join(ll))
                        total_fixed += 1
                        break
                continue

            # INIT_SCOPE_IMPORTS: handled in function-scoped step
            if name in INIT_SCOPE_IMPORTS:
                continue

            # logger special case
            if name == "logger":
                stmt = "import logging\nlogger = logging.getLogger(__name__)"
                if add_import_at_top(fname, stmt):
                    emitted[fname].add(stmt)
                    total_fixed += 1
                continue

            # Try original header import
            if name in name_to_stmt:
                stmt = name_to_stmt[name]
                if stmt not in emitted[fname] and add_import_at_top(fname, stmt):
                    emitted[fname].add(stmt)
                    total_fixed += 1
                    continue

            # Try cross-module import
            target = M.get(name) or CH.get(name)
            if target:
                mod_short = fname.split("/")[-1]
                if target != mod_short and target != "__init__.py":
                    stmt = f"from src.autopilot.orchestrator.{target[:-3]} import {name}"
                    if stmt not in emitted[fname] and add_import_at_top(fname, stmt):
                        emitted[fname].add(stmt)
                        total_fixed += 1
                        continue

            print(f"  MANUAL {fname}: {name}")

    if total_fixed == 0:
        # Fallback: try adding imports for remaining names using name_to_stmt
        for fname2, names2 in undefs.items():
            if not names2: continue
            for name in sorted(names2):
                if name in INIT_SCOPE_IMPORTS: continue
                if name == "_threading": continue
                # Try name_to_stmt
                if name in name_to_stmt:
                    stmt = name_to_stmt[name]
                    already = stmt in read_file(fname2)
                    if add_import_at_top(fname2, stmt):
                        emitted[fname2].add(stmt)
                        total_fixed += 1
                        continue
                # Try cross-module
                target = M.get(name) or CH.get(name)
                if target:
                    mod_short = fname2.split("/")[-1]
                    if target != mod_short and target != "__init__.py":
                        stmt = f"from src.autopilot.orchestrator.{target[:-3]} import {name}"
                        if add_import_at_top(fname2, stmt):
                            emitted[fname2].add(stmt)
                            total_fixed += 1
                            continue
                print(f"  MANUAL {fname2}: {name}")
        if total_fixed == 0:
            break
    print(f"  iteration {iteration}: fixed {total_fixed}")

# ── PART 5: function-scoped imports for INIT_SCOPE_IMPORTS ──────────
print("\n=== Function-scoped imports ===")
for frel in ALL_FILES:
    undefs = f821_names([frel])
    fs_names = {n for n in undefs.get(frel, set()) if n in INIT_SCOPE_IMPORTS}
    if not fs_names:
        continue

    # Find which function each name appears in
    iss = ruff("--select", "F821", frel)
    func_needs = defaultdict(set)
    for i in iss:
        m = re.search(r"`([^`]+)`", i["message"])
        if not m or m.group(1) not in fs_names:
            continue
        row = i["location"]["row"]
        t = ast.parse(read_file(frel))
        for n in t.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.lineno <= row <= n.end_lineno:
                func_needs[n.name].add(m.group(1))
                break

    if not func_needs:
        continue

    # Inject bottom-up to preserve line offsets
    text = read_file(frel)
    t = ast.parse(text)
    funcs = [
        (n.lineno, n.name, n.body)
        for n in t.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in func_needs
    ]
    funcs.sort(reverse=True)

    for _, func_name, body in funcs:
        names_list = sorted(func_needs[func_name])
        stmt = f"from src.autopilot.orchestrator import {', '.join(names_list)}"
        ll = text.split("\n")
        ins = body[0].lineno - 1  # 0-indexed first body line
        # skip past docstring if present
        if (isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            q = '"""' if '"""' in ll[ins] else "'''"
            if ll[ins].strip().count(q) >= 2:
                ins += 1
            else:
                for j in range(ins + 1, len(ll)):
                    if q in ll[j]:
                        ins = j + 1
                        break
        # match indentation of the function body
        body_indent = len(ll[ins]) - len(ll[ins].lstrip()) if ins < len(ll) else 4
        ll.insert(ins, " " * body_indent + stmt)
        text = "\n".join(ll)
        print(f"  {frel}: {func_name}() <- {stmt}")
    write_file(frel, text)

# ── PART 6: E302 spacing fix ────────────────────────────────────────
print("\n=== Spacing fix ===")
subprocess.run(
    ["ruff", "check", "--select", "E302,E303", "--fix", "--no-cache", *ALL_FILES],
    capture_output=True,
)
print("  done")

# ── PART 7: fixup remaining edge cases ──────────────────────────────
# These names are used inside functions but the F821 loop missed them
# (they're in the original header but the loop's name_to_stmt lookup failed)
ip = f"{OUT}/__init__.py"
fixup_imports = [
    "import json",
    "import shutil",
    "import sys",
    "from src.core.database import get_db",
    "from src.core.database import Workflow",
    "from src.core.database import DatabaseManager",
    "from src.core.simple_config import get_config",
]
text_init = read_file(ip)
for stmt in fixup_imports:
    if stmt not in text_init:
        added = add_import_at_top(ip, stmt)
        if added:
            text_init = read_file(ip)  # re-read after write
            print(f"  fixup: added {stmt}")
        else:
            print(f"  fixup: {stmt} already present or failed")

# ── FINAL ────────────────────────────────────────────────────────────
print("\n=== FINAL ===")
iss = ruff("--select", "F401,F811,F821,E302", *ALL_FILES)
if iss:
    from collections import Counter
    counts = Counter(i["code"] for i in iss)
    print(f"  {dict(counts)}")
    for i in iss[:10]:
        m = re.search(r"`([^`]+)`", i["message"])
        print(f"    {i['code']} {rel(i['filename']).split('/')[-1]}:{i['location']['row']} {m.group(1) if m else '?'}")
    if len(iss) > 10:
        print(f"    ... +{len(iss) - 10} more")
else:
    print("  ALL CLEAN ✓")

total = sum(1 for f in ALL_FILES for _ in open(f))
print(f"\n  Total lines: {total}")
