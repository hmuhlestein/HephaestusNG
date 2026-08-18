# Phase 2, §4.10 — MCP tool-name single source of truth findings

## The decision (target #2): register the two (then three) missing tools, don't fix the prompts

Verified this was the right direction before writing any code: `_tool_complete_my_task` (`src/mcp/server.py`) already establishes the exact pattern needed — an MCP tool handler reads `agent_id` out of the `arguments` dict (since MCP tool calls carry no HTTP header) and calls the underlying REST-route function directly with `agent_id=` as a real keyword argument, bypassing the `Header(...)` dependency injection that only applies to actual HTTP requests. `system_prompts.yaml:203`'s documented call shape, `heph_submit_result(agent_id="{agent_id}", ...)`, is unambiguously written as an MCP-tool-shaped call (an explicit `agent_id` argument, not "make an HTTP POST"), so correcting the prompt to describe some other calling convention would have been fighting the prompt's own clear intent. Registering real tools that delegate to the existing REST routes was the lower-risk, more consistent choice, and required zero changes to any prompt text.

## A third live bug found mid-implementation, not just the two the prompt named

The prompt doc's freshness check named `heph_submit_result` and `heph_submit_result_validation` as confirmed-live bugs, scoped to `config/prompts/**/*.yaml`. Building the registry required tracing every `agent_id`-needing tool's calling convention, which led to auditing every `heph_`-prefixed reference in the codebase rather than stopping at the two named ones — and found a third: `heph_give_validation_review` (`src/validation/validator_agent.py:97`, a TASK validator's prompt, distinct from `heph_submit_result_validation`'s RESULT validator at line 140 in the same file). The real route, `POST /give_validation_review` (`src/mcp/memory_api.py:460`), existed and worked; nothing in `_MCP_TOOLS` or `/tools` ever named it. Same bug, same fix pattern, registered alongside the other two.

A full-repo `heph_[a-z_]+` grep (not scoped to YAML) confirmed these three are the *only* real tool-name references anywhere in the codebase — two other matches (`src/mcp/devtools.py`'s `window.__heph_selector_found` JS global, `src/autopilot/orchestrator/worktree_integration.py`'s local variable `heph_repo`) are unrelated identifiers that happen to share the prefix, not tool references, and were excluded from the drift-check's scope for exactly that reason (see below).

## What was built

`MCPToolSpec` (a `NamedTuple`: `name`, `description`, `input_schema`, `handler`) and `MCP_TOOL_REGISTRY: List[MCPToolSpec]` in `src/mcp/server.py`, containing all 14 non-devtools tools (the original 11 plus the 3 newly-registered ones). `list_tools()`'s `/tools` response and `_MCP_TOOLS`'s dispatch dict are now both generated from this one list — `_MCP_TOOLS = {t.name: t.handler for t in MCP_TOOL_REGISTRY}` instead of a second, independently-maintained dict. The original 11 tools' descriptions and JSON schemas were moved verbatim, not rewritten.

Three new handlers (`_tool_submit_result`, `_tool_submit_result_validation`, `_tool_give_validation_review`), each following `_tool_complete_my_task`'s established `arguments.get("agent_id")` → call the real route directly pattern, with an explicit required-args check raising `HTTPException(400, ...)` before construction (matching `_tool_create_ticket`'s style) rather than letting a bare Pydantic `ValidationError` escape.

`MCP_TOOL_NAMES: frozenset` — every name this server actually recognizes, core registry entries plus `_DEVTOOLS_TOOLS`' 15 keys — the single source the drift check (below) validates against.

## devtools — the narrower half of the same duplication, found and partially closed

The prompt doc scoped `devtools_*` tools as out-of-scope "unless your own freshness check finds they share the exact same duplication problem." They do, but a narrower version of it: `/tools`'s 15 devtools entries each hand-type a `"required"` list, duplicating `_DEVTOOLS_TOOLS`' `(required_args, handler)` tuples — the actual dispatch-time enforcement (`_handle_devtools_tool`'s `missing = [k for k in required if k not in arguments]`). Comparing the two side by side found the duplication wasn't just redundant, it was **wrong, systematically, for all 15 tools**: every hand-written schema listed `session_id` as required, but `_handle_devtools_tool` always defaults it (`arguments.get("session_id", "default")`) before the required-args check ever runs — `_DEVTOOLS_TOOLS`' real `required_args` never includes it. `/tools` has been telling agents `session_id` is mandatory for every devtools tool since these were first written; it never actually was.

Fixed the "required" half of the duplication: `list_tools()` now overwrites each devtools entry's `input_schema["required"]` from `_DEVTOOLS_TOOLS[name][0]` after building the (still hand-written) list, rather than trusting a second hand-typed copy. The full JSON-schema `description`/`properties` for each devtools tool were left as hand-written literals — `_DEVTOOLS_TOOLS` has no equivalent data to derive them from, and building that out (property-level descriptions matching what each `_devtools_*` handler actually reads from `arguments`) would be a second, larger item in its own right. Documented as a deliberate partial fix, not silently left as a fully-closed gap.

## Verification

New `tests/test_mcp_tool_registry.py` (16 tests):
- Structural consistency: registry names match `_MCP_TOOLS` keys and handlers exactly; `/tools`'s core section order matches the registry; every devtools `/tools` entry's `required` matches `_DEVTOOLS_TOOLS`; `MCP_TOOL_NAMES` covers both.
- A direct regression test for the `session_id`-never-actually-required finding (`devtools_connect`'s real `required_args` is `[]`).
- The drift check itself (`TestPromptToolNameDriftCheck`): scans `config/prompts/**/*.yaml`, `config/workflows/**/*.yaml`, and `src/validation/*.py` for `heph_<name>` references and asserts every name is in `MCP_TOOL_NAMES` — plus a self-check confirming the scan actually finds the three historically-live names (so a scope/regex regression in the test itself can't silently make it pass trivially).
- Dispatch tests for all three new tools, including the literal regression case: calling `execute_tool({"tool": "heph_submit_result", ...})` (and the other two) now succeeds instead of 400ing with `"Unknown tool: submit_result"`.

**Scope note on the drift check**: scoped to the three locations a full-repo grep confirmed are the only ones with real tool references, not all of `src/`+`config/` — a broader scan would flag `devtools.py`'s JS global and `worktree_integration.py`'s local variable as false positives. If a fourth prompt-generating location is added later, it needs adding to this test's scan list by hand; this isn't itself fully automatic, just far narrower than "nothing checks this at all."

## Follow-up: bare-name coverage was missing, found re-reading this item's own prompt doc

The prompt doc explicitly named the surface as "16 files... reference one of the 11 real tool names (by bare name, with or without a `heph_` prefix)." The drift check as first written only matched `heph_`-prefixed references — a real gap, since a genuine bare-name usage pattern exists: `complete_my_task(`, `save_memory(`, `search_memory(`, `search_tickets(`, `create_task(`, and `update_task_status(` all appear unprefixed across several workflow YAMLs (shorthand step labels like `call: complete_my_task(...)`, not the fully-instructed `heph_`-prefixed form).

Tried the obvious fix first — a fully generic `\bname\(` scan — and it was unusable: it matched dozens of unrelated identifiers from code examples embedded in the prompts (`add(`, `error(`, `process(`, `run(`, `str(`, `subtract(`, etc., none of them tool calls). `heph_` is a reliable "this is a tool call" signal; a bare identifier followed by `(` is not, in files that also contain example code.

Landed on a bounded middle ground instead: check bare-call-shaped mentions of the *current* core registry names specifically (a fixed tuple in the test, `_CORE_NAMES_AT_WRITE_TIME`), rather than an unbounded identifier scan. This can't catch a brand-new, never-registered bare name (nothing to match it against), but it does catch the more likely real drift — one of these six names getting renamed in the registry while an existing bare reference to the old name is left behind, silently no longer resolving. New `test_bare_name_references_are_still_registered` confirms all six currently found (`complete_my_task`, `create_task`, `save_memory`, `search_memory`, `search_tickets`, `update_task_status`) resolve.

**Correction on why bare names matter, from the user directly**: bare and `heph_`-prefixed aren't "real" vs. "informal" shorthand — different agentic CLIs' MCP adapters disagree on whether they prepend the server name before presenting a tool to an agent, so which shape a given agent actually sends depends on which CLI it's running in, not on how carefully a prompt was written. Both are first-class. The asymmetric *test coverage* (full for `heph_`-prefixed, bounded for bare) is about what a static scan can reliably detect without false positives, not a judgment that bare calls matter less — updated `TestPromptToolNameDriftCheck`'s docstring to say this correctly.

Given that reframing, added `TestDispatchAcceptsBothCallShapesForEveryTool` — two new tests confirming `137f12b`'s dispatch-time strip actually delivers on this flexibility for *every* registered tool (all 14 core + all 15 devtools, 29 total), not just the 3 newly-registered ones the individual wrapper tests happened to spot-check. Each tool is dispatched once bare and once `heph_`-prefixed, asserting both resolve to the same handler. This is the more direct verification of what "being flexible is good" actually requires — the drift check only catches a prompt referencing an *unregistered* name; this new pair confirms dispatch itself never depends on which shape the agent sends.

Full regression run across every test file depending on `src/mcp/server.py` (24 files): 18 (new file) + 167 + 191 passed. 8 failures observed in one large combined batch run were individually investigated: `tests/test_mcp_results_endpoint.py` (2) and `tests/test_mcp_server_tickets.py` (1) confirmed pre-existing and unrelated via `git stash` on just `src/mcp/server.py` (identical failures with and without this item's changes — unrelated fixture/background-task issues, not MCP dispatch); `tests/test_update_task_status_ordering.py` (5) confirmed order-dependent/cross-file interference, not a real regression (all 11 tests in that file pass when run alone). Zero regressions attributable to this item. ruff clean (3 pre-existing E402 findings, confirmed via `git show HEAD` — unchanged count, same lines this session's other work already found).

## Explicitly out of scope, left for a follow-up

- Fully closing devtools' duplication (per-tool `description`/`properties` schemas) — see above.
- `_DEVTOOLS_TOOLS` itself joining `MCP_TOOL_REGISTRY`'s exact shape — genuinely different dispatch mechanism (browser-session precondition, special-cased `devtools_connect`/`devtools_close` handler signatures in `_handle_devtools_tool`), not forced together.
- Any other Phase 2 item (§4.11 onward).

No commits — left in the working tree for review.
