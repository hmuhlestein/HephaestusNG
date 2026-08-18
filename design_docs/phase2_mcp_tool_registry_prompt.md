# Prompt: Phase 2, §4.10 — MCP tool-name single source of truth

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.10 of `docs/AUTOPILOT_REFACTOR_PLAN.md`. Tenth item in this session's Phase 2 sequence — §4.1 through §4.9 are done; read their findings docs for the established rigor and format before starting.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.10 (full text, short). Note its own inline correction: the plan originally named four duplicated surfaces (including `src/mcp/mcp_client.py`), but that file doesn't exist in this repo — there are three real surfaces. Trust the corrected version, but re-verify everything below yourself; this handoff's own checks are current as of this prompt's writing, not guaranteed current by the time you start.

## Freshness check — confirmed as of this handoff, re-verify

- **`_MCP_TOOLS`** (`src/mcp/server.py:5502`) — an 11-entry `Dict[str, Any]` mapping bare tool name → async handler (`create_task`, `save_memory`, `search_memory`, `get_task_status`, `update_task_status`, `complete_my_task`, `create_ticket`, `search_tickets`, `update_ticket_status`, `broadcast_message`, `send_message`). Dispatched from `/tools/execute` (`server.py:5517`), which strips a leading `heph_` if present (`137f12b`'s fix, still intact — see below) before the dict lookup.
- **`/tools`** (`server.py:4194`, `list_tools()`) — a ~300-line hand-written JSON response with one dict per tool: `name`, `description`, `input_schema` (full JSON Schema, `properties`/`required`). Confirmed the same 11 bare names as `_MCP_TOOLS`, in the same order, currently in sync — this pass is not about fixing a drift between these two, it's about preventing the next one, and separately fixing a drift that's already live in the third surface below.
- **`devtools_*` tools** — a genuinely separate registry, `_DEVTOOLS_TOOLS` (`server.py:5639`), already consolidated (SOLID review 1.5, same as `_MCP_TOOLS` itself) with a different shape (`(required_args, handler)` tuples, a browser-session precondition). Not part of the `/tools` JSON-schema listing the same way the 11 tools above are. **Out of scope** — don't fold it into whatever you build unless you find it shares the exact same duplication problem; on this handoff's read it doesn't (no separate hand-written schema listing to keep in sync).
- **The prompt/YAML surface — confirmed live drift, two real bugs**:
  - `config/prompts/system_prompts.yaml:203` tells every non-phase agent to call `heph_submit_result(agent_id="{agent_id}", ...)`. There is no `submit_result` entry in `_MCP_TOOLS`, and `/tools/execute`'s dispatch never falls through to anything else — calling this exactly as instructed gets HTTP 400 `"Unknown tool: submit_result"`. The real thing that exists is a REST route, `POST /submit_result` (`src/mcp/memory_api.py:670`, `WorkflowResultService.submit_result`) — a completely different call shape (HTTP POST to a REST endpoint, not an MCP tool invocation) from what the prompt tells the agent to do.
  - `src/validation/validator_agent.py:140` tells a validator agent to use `heph_submit_result_validation` — same bug, same shape. The real thing is `POST /submit_result_validation` (`memory_api.py:833`).
  - Both are confirmed still live as of this handoff — re-verify they haven't been separately patched since.
  - 16 files under `config/prompts/` and `config/workflows/` reference one of the 11 real tool names (by bare name, with or without a `heph_` prefix) — this is the real surface area a registry-driven check needs to cover, not just `system_prompts.yaml`. Enumerate them fresh; don't assume this handoff's count is exhaustive or still accurate.
- **The historical flip-flop** (why this needs a structural fix, not another one-off patch): six commits over five weeks each fixed exactly one of these surfaces after it was caught producing "Unknown tool" or double-prefixed errors (`heph_heph_create_task`) — `bede479` (register a missing tool), `ef438e8`/`8e4105d` (add `hephaestus_` prefix, two different surfaces, same day), `d50ebd8` (fix remaining bare references in prompts), `e44689c` (remove the `heph_` prefix entirely), `137f12b` (give up trying to keep prefixes in sync — strip defensively instead, in code, at dispatch time). **`137f12b`'s defensive strip is the one fix from this history you should keep, not replace** — it's a legitimate belt-and-suspenders normalization (MCP adapters prepending a server-name prefix is a real, permanent fact of the protocol, not a bug), sitting alongside whichever single-source-of-truth mechanism you build, not superseded by it.

## Target

One declaration — a registry (a `Dict[str, ToolSpec]`-shaped structure, or a decorator-driven manifest, your call, see below) that both:

1. **Generates `/tools`'s listing and `_MCP_TOOLS`'s dispatch dict from the same data.** This is the safe, mechanical part: define each tool once (name, description, input_schema, handler) and have both `list_tools()` and `/tools/execute`'s dispatch read from that one structure instead of two independently-maintained ones. Given `_MCP_TOOLS` and `/tools` are currently in sync, this consolidation is low-risk — a straightforward extraction, not a behavior change, and the existing 11 entries and their exact JSON schemas should transfer verbatim (don't rewrite the descriptions/schemas while you're in there).

2. **Closes the drift on the prompt/YAML surface.** This is the part with a real design decision, and the plan explicitly says to resolve it, not assume an answer: decide whether `heph_submit_result`/`heph_submit_result_validation` should become real registered MCP tools (thin wrappers delegating to `WorkflowResultService.submit_result`/the validation equivalent, matching the calling convention every other MCP tool already uses), or whether the two prompt references should be corrected to describe the actual REST-call mechanism instead. Whichever you pick, also decide how the registry prevents this exact class of drift from recurring — a build-time/test-time check that every tool-name string appearing in `config/prompts/**/*.yaml` and `config/workflows/**/*.yaml` exists in the registry is the most direct fix and matches "one edit instead of a grep-and-pray"; a decorator/codegen approach that literally rewrites the YAML files is heavier and probably not warranted just for this. Pick the lighter approach unless you find a concrete reason not to, and say so either way in findings.

**Naming convention**: keep bare names as the registry's canonical form (`create_task`, not `heph_create_task` or `hephaestus_create_task`) — that's what `137f12b` already settled on and what both current surfaces already agree on. Don't reopen that question; this item is about eliminating the duplicate-declaration problem, not re-relitigating the six-commit prefix argument that already resolved.

## Verification

- Characterization test asserting `/tools`'s listing and `_MCP_TOOLS`'s dispatch dict are generated from (or derived from / equal to) the same single source — this should be true immediately after the mechanical consolidation in target (1), and should be the kind of test that would have caught any of the six historical drift commits had it existed then.
- A test (or a startup/CI check, your call) that fails if any tool-name string referenced in `config/prompts/**/*.yaml` or `config/workflows/**/*.yaml` isn't in the registry — this should **fail today** for `heph_submit_result` and `heph_submit_result_validation` before you fix them, confirming the check actually catches the live bug, and pass once you've resolved target (2)'s design decision one way or the other.
- Whichever fix you choose for the two live bugs, verify it end-to-end: if you register them as real tools, a call through `/tools/execute` with the exact prompt-text invocation must succeed; if you correct the prompts instead, verify the corrected instruction actually reaches the real `/submit_result`/`/submit_result_validation` REST routes correctly.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.9).
- Any other Phase 2 item (§4.11 onward). Log anything found belonging to that one.
- `_DEVTOOLS_TOOLS`/`devtools_*` tools, unless your own freshness check finds they share the exact same three-surface duplication (this handoff's read says they don't).
- Re-opening the bare-vs-`heph_`-vs-`hephaestus_` naming argument — `137f12b` already settled it; keep bare names and keep the defensive strip.
- Fixing every prompt/YAML file's tool-name references by hand as a one-off sweep instead of building the registry-driven check — the point of this item is that the check does that job going forward, not a single manual pass that drifts again next month.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions or this prompt's own freshness-check guesses (re-verify all of the above yourself). `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>`. Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Findings doc (`design_docs/phase2_mcp_tool_registry_findings.md` or similar) — lead with the explicit decision you made for target (2) (register the two missing tools, or fix the two prompts) and why, since that's the one part of this item that isn't just mechanical extraction. No commits — leave everything in the working tree for review.
