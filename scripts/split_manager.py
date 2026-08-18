#!/usr/bin/env python3
"""Scripted extraction of the output-capture cluster from src/agents/manager.py.

Parses manager.py via ast, extracts 9 output-capture methods by verified
line range, generates src/agents/output_capture.py, and rewrites the
original methods as thin delegators.  Follows the methodology of
scripts/split_autopilot_api.py — all method bodies are copied VERBATIM.

Usage:
  python scripts/split_manager.py              # verify only (dry run)
  python scripts/split_manager.py --apply      # write files

Deliberately does NOT: run ruff, retarget test call sites, or commit.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANAGER = ROOT / "src" / "agents" / "manager.py"
OUT_MODULE = ROOT / "src" / "agents" / "output_capture.py"

# Methods to extract: (name, is_static)
OUTPUT_CAPTURE_METHODS = [
    ("get_agent_output", False),
    ("_resolve_tmux_transcript_dir", False),
    ("_read_transcript_log", False),
    ("_find_tmux_session", False),
    ("_capture_pane_lines", False),
    ("_append_lines", True),
    ("_poll_stable_transcript", False),
    ("_flush_stable_transcript", False),
    ("_get_orchestrator_output", False),
]
TARGET_NAMES = {n for n, _ in OUTPUT_CAPTURE_METHODS}


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def collect_class_methods(tree: ast.Module, class_name: str):
    """Return {name: (start, end, is_async, is_static)} for methods on class_name."""
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            methods = {}
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    is_static = any(
                        isinstance(d, ast.Name) and d.id == "staticmethod"
                        for d in child.decorator_list
                    )
                    start = (
                        child.decorator_list[0].lineno
                        if child.decorator_list
                        else child.lineno
                    )
                    methods[child.name] = (
                        start,
                        child.end_lineno,
                        isinstance(child, ast.AsyncFunctionDef),
                        is_static,
                    )
            return methods
    return {}


def main() -> None:
    apply = "--apply" in sys.argv

    if not MANAGER.exists():
        fail(f"target not found: {MANAGER}")

    source = MANAGER.read_text()
    lines = source.splitlines()
    tree = ast.parse(source)
    methods = collect_class_methods(tree, "AgentManager")

    # ── 1. Verify every target method exists ──────────────────────────
    missing = TARGET_NAMES - set(methods)
    if missing:
        fail(f"methods not found on AgentManager: {missing}")

    init_range = methods.get("__init__")
    if not init_range:
        fail("__init__ not found on AgentManager")
    init_end = init_range[1]

    print(f"OK: all {len(TARGET_NAMES)} target methods found")
    print(f"OK: __init__ at lines {init_range[0]}-{init_end}")

    skip_ranges = []
    for name, is_static in OUTPUT_CAPTURE_METHODS:
        start, end, is_async, _ = methods[name]
        skip_ranges.append((start, end, name, is_async, is_static))
        print(f"  {name}: lines {start}-{end} ({end - start + 1} lines)")

    n_extracted = sum(e - s + 1 for s, e, *_ in skip_ranges)
    print(f"\nOK: {n_extracted} total lines in output-capture cluster")
    skip_ranges.sort(key=lambda r: r[0])

    # ── 2. Copy extracted method bodies VERBATIM ──────────────────────
    extracted_bodies = {}
    for start, end, name, *_ in skip_ranges:
        extracted_bodies[name] = lines[start - 1 : end]

    # ── 3. Build output_capture.py ────────────────────────────────────
    oc = []
    oc.append(
        '"""Agent output capture — reading tmux transcripts and stable-clean logs.'
    )
    oc.append("")
    oc.append(
        "Extracted from AgentManager, which mixed this concern in with tmux session"
    )
    oc.append(
        "lifecycle, prompt construction, DB persistence, and messaging — see"
    )
    oc.append("design_docs/manager_py_decomposition_prompt.md.  AgentManager still exposes")
    oc.append(
        "get_agent_output (many callers depend on that public API) but delegates to"
    )
    oc.append(
        "an AgentOutputCapture instance instead of implementing the transcript"
    )
    oc.append("plumbing itself.")
    oc.append('"""')
    oc.append("")
    oc.append("import logging")
    oc.append("from pathlib import Path")
    oc.append("from typing import Any, Dict, List, Optional")
    oc.append("")
    oc.append("from src.core.constants import AUTOPILOT_STATE_DIR, CONTEXT_DIR_NAME")
    oc.append("from src.core.database import Agent, AgentLog")
    oc.append("")
    oc.append("logger = logging.getLogger(__name__)")
    oc.append("")
    oc.append("")
    oc.append("class AgentOutputCapture:")
    oc.append(
        '    """Reads and filters tmux output for agents — transcripts, clean logs,'
    )
    oc.append("    and live capture-pane snapshots.")
    oc.append("")
    oc.append("    Constructor args are intentionally minimal: only the state this")
    oc.append("    collaborator actually reads (db_manager, tmux_server).")
    oc.append('    """')
    oc.append("")
    oc.append("    _STABILITY_CONFIRMATIONS = 3")
    oc.append("")
    oc.append("    def __init__(self, db_manager, tmux_server):")
    oc.append("        self.db_manager = db_manager")
    oc.append("        self.tmux_server = tmux_server")
    oc.append("        self._transcript_filter_cache: Dict[str, Any] = {}")
    oc.append("        self._pane_stability_cache: Dict[str, Dict[str, Any]] = {}")
    oc.append("")

    for name, is_static in OUTPUT_CAPTURE_METHODS:
        oc.extend(extracted_bodies[name])
        oc.append("")

    # ── 4. Build rewritten manager.py ─────────────────────────────────
    new_mgr = []
    new_mgr.extend(lines[:init_end])

    # Composition import + lazy accessor
    new_mgr.append("")
    new_mgr.append(
        "        # Output-capture collaborator (decomposition) — reads tmux"
    )
    new_mgr.append(
        "        # transcripts, clean logs, and live capture-pane snapshots."
    )
    new_mgr.append("        from src.agents.output_capture import AgentOutputCapture")
    new_mgr.append("")
    new_mgr.append(
        "        self._output_capture = AgentOutputCapture(db_manager, self.tmux_server)"
    )
    new_mgr.append("")
    new_mgr.append("    def _get_output_capture(self):")
    new_mgr.append('        """Lazy accessor for the output-capture collaborator.')
    new_mgr.append("")
    new_mgr.append("        Tests that bypass __init__ via __new__ set db_manager")
    new_mgr.append(
        "        and tmux_server manually — this creates the collaborator"
    )
    new_mgr.append(
        "        on first access so those tests don't hit AttributeError."
    )
    new_mgr.append('        """')
    new_mgr.append('        oc = getattr(self, "_output_capture", None)')
    new_mgr.append("        if oc is None:")
    new_mgr.append("            import libtmux")
    new_mgr.append("")
    new_mgr.append(
        "            from src.agents.output_capture import AgentOutputCapture"
    )
    new_mgr.append(
        '            tmux = getattr(self, "tmux_server", None) or libtmux.Server()'
    )
    new_mgr.append("            oc = AgentOutputCapture(self.db_manager, tmux)")
    new_mgr.append("            self._output_capture = oc")
    new_mgr.append("        return oc")

    cursor = init_end
    for idx, (start, end, name, is_async, is_static) in enumerate(skip_ranges):
        new_mgr.extend(lines[cursor : start - 1])
        new_mgr.extend(_delegator(name, is_static))
        cursor = end
    new_mgr.extend(lines[cursor:])

    # ── 5. Syntax check ───────────────────────────────────────────────
    oc_text = "\n".join(oc) + "\n"
    try:
        ast.parse(oc_text)
    except SyntaxError as ex:
        fail(f"output_capture.py syntax error: {ex}")
    print("OK: output_capture.py parses as valid Python")

    mgr_text = "\n".join(new_mgr) + "\n"
    try:
        ast.parse(mgr_text)
    except SyntaxError as ex:
        fail(f"rewritten manager.py syntax error: {ex}")
    print("OK: rewritten manager.py parses as valid Python")

    # ── 6. Lossless reassembly ────────────────────────────────────────
    skip_lines = set()
    for start, end, *_ in skip_ranges:
        for i in range(start, end + 1):
            skip_lines.add(i)

    for i, line in enumerate(lines, 1):
        if i in skip_lines or i <= init_end:
            continue
        if line not in set(new_mgr):
            fail(f"lost line {i}: {line!r}")

    if not any("_get_output_capture().get_agent_output" in l for l in new_mgr):
        fail("get_agent_output delegator not found")

    print(f"OK: lossless reassembly — {len(lines)} -> {len(new_mgr)} lines")

    # ── 7. Stats ──────────────────────────────────────────────────────
    print(f"\n  output_capture.py: {len(oc)} lines")
    print(
        f"  manager.py: {len(lines)} -> {len(new_mgr)} lines "
        f"(delta {len(new_mgr) - len(lines):+d})"
    )

    if not apply:
        print("\ndry run complete — re-run with --apply to write files")
        return

    # ── 8. Write ──────────────────────────────────────────────────────
    import py_compile

    OUT_MODULE.write_text(oc_text)
    py_compile.compile(str(OUT_MODULE), doraise=True)
    print(f"\nwrote {OUT_MODULE}")

    MANAGER.write_text(mgr_text)
    py_compile.compile(str(MANAGER), doraise=True)
    print(f"wrote {MANAGER}")


def _delegator(name: str, is_static: bool) -> list[str]:
    """Build a thin delegator — each one forwards to _get_output_capture()."""
    I = "    "

    # _append_lines is only called internally by other extracted methods;
    # no delegator needed on AgentManager.
    if name == "_append_lines":
        return []

    if name == "get_agent_output":
        return [
            f"{I}def get_agent_output(self, agent_id: str, lines: int = 200) -> str:",
            f'{I}    """Get recent output from agent\'s tmux session or stored output for terminated agents.',
            f"{I}",
            f"{I}    Args:",
            f"{I}        agent_id: Agent ID",
            f"{I}        lines: Number of lines to retrieve",
            f"{I}",
            f"{I}    Returns:",
            f"{I}        Recent output text",
            f'{I}    """',
            f"{I}    return self._get_output_capture().get_agent_output(agent_id, lines)",
            "",
        ]

    # Map method name to (def-indent, return-expr)
    delegators = {
        "_resolve_tmux_transcript_dir": (
            "    ",  # class-level indent
            "def _resolve_tmux_transcript_dir(self, agent) -> Optional[Path]:",
            '"""Find the .hephaestus/tmux/ directory this agent\'s transcript files live in."""',
            "self._get_output_capture()._resolve_tmux_transcript_dir(agent)",
        ),
        "_read_transcript_log": (
            "    ",
            "def _read_transcript_log(self, agent, lines: int) -> str:",
            '"""Read output from the pipe-pane transcript log file."""',
            "self._get_output_capture()._read_transcript_log(agent, lines)",
        ),
        "_find_tmux_session": (
            "    ",
            "def _find_tmux_session(self, session_name: str):",
            '"""Look up a live libtmux.Session by name, or None."""',
            "self._get_output_capture()._find_tmux_session(session_name)",
        ),
        "_capture_pane_lines": (
            "    ",
            "def _capture_pane_lines(self, session_name: str) -> Optional[List[str]]:",
            '"""capture-pane the full available scrollback as a list of lines."""',
            "self._get_output_capture()._capture_pane_lines(session_name)",
        ),
        "_poll_stable_transcript": (
            "    ",
            "def _poll_stable_transcript(self, session_name: str, clean_path: Path) -> None:",
            '"""Append whatever\'s newly stable since the last poll to clean_path."""',
            "self._get_output_capture()._poll_stable_transcript(session_name, clean_path)",
        ),
        "_flush_stable_transcript": (
            "    ",
            "def _flush_stable_transcript(self, session_name: str, clean_path: Path) -> None:",
            '"""Final, unconditional flush before killing a session."""',
            "self._get_output_capture()._flush_stable_transcript(session_name, clean_path)",
        ),
        "_get_orchestrator_output": (
            "    ",
            "def _get_orchestrator_output(self, agent, lines: int) -> str:",
            '"""Return the orchestrator\'s run log as human-readable text."""',
            "self._get_output_capture()._get_orchestrator_output(agent, lines)",
        ),
    }

    indent, sig, doc, ret = delegators[name]
    return [
        f"{indent}{sig}",
        f"{indent}    {doc}",
        f"{indent}    return {ret}",
        "",
    ]


if __name__ == "__main__":
    main()
