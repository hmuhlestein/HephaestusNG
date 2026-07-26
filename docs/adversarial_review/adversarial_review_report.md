---
type: adversarial_review_result
feature_id: des-91c8-pi-extension
blocker_count: 2
warning_count: 3
nit_count: 1
---

# Adversarial Review: Pi Cost Tracker Extension

**Scope:** This feature's own diff (`extensions/hephaestus-cost-tracker/README.md`,
1 line) is trivially correct — verified independently against
`index.ts:123` and `autopilot_api.py:2144`, matching the architectural
review's finding. A 1-line doc fix has no exception paths, concurrency, or
connection handling to attack.

The task instructions require tracing exception propagation, concurrency,
error handling, defaults, and DB/connection lifecycles for the feature
under review. "Pi Cost Tracker Extension" is not the README alone — it is
the full pipeline the README documents and this feature's FR-2/FR-3 are
meant to verify: `extensions/hephaestus-cost-tracker/src/index.ts` →
`POST /api/autopilot/cost-entries` (`src/mcp/autopilot_api.py`) →
`src/core/cost_derivation.py` → the JSONL fallback in
`src/services/cost_collection_service.py`. That pipeline predates this
feature (per `docs/architecture.md` §1), but this feature's own README
makes explicit correctness claims about it ("SessionCostCheckpoint
prevents double-counting") that are in scope to verify. One of those
claims is false, and it's a real data-integrity bug — reported below.

## BLOCKER findings

### B-1: Real-time pi extension costs and the JSONL fallback collector are not mutually exclusive — every turn is double-counted whenever the extension is working

**Claim being disproven:** `extensions/hephaestus-cost-tracker/README.md:47-52`
states: "If the extension is not loaded, Hephaestus falls back to JSONL
tailing... `SessionCostCheckpoint` prevents double-counting." This
describes the two paths as an either/or fallback. The code does not
implement that — both paths run unconditionally and independently.

**Exact failure sequence:**
1. `HEPHAESTUS_AGENT_ID`/`TASK_ID`/`WORKFLOW_ID` are set, `pi` is
   installed with the extension built and loaded — the intended, normal
   case, not a degraded one.
2. On every turn, `index.ts:78-117` (`turn_end`) reads
   `message.usage.cost.total` and calls `postCost()` (`index.ts:122-137`),
   which `POST`s to `/api/autopilot/cost-entries`. The endpoint
   (`autopilot_api.py:2144-2197`) calls `record_cost()`
   (`cost_derivation.py:37-115`), which creates a `CostEntry` row with
   `source="pi"` — one row per turn, in real time.
3. When the task completes, `task_completion_service.py:924-926`
   unconditionally calls `collect_task_cost(task_id)`
   (`cost_collection_service.py:404`) — there is no check anywhere for
   "did the extension already post costs for this session." Confirmed by
   grep: no flag such as `cost_source`/`extension_active` exists in
   `Agent`, `Task`, or `SessionCostCheckpoint`.
4. `collect_task_cost` reads `SessionCostCheckpoint.lines_processed`
   for this `session_id` (`cost_collection_service.py:444-446`). This
   checkpoint has never been touched by the extension's HTTP path — the
   `/cost-entries` endpoint never writes to `SessionCostCheckpoint` at
   all — so it is `0` on the first (and normally only) task completion.
5. `PiJsonlCollector.collect()` (`cost_collection_service.py:63-139`)
   therefore tails the **entire** session JSONL file from line 1,
   re-extracting `message.usage.cost.total` for every assistant turn —
   the exact same turns already POSTed in step 2 — and returns them as
   new entries.
6. `collect_task_cost` writes every one of these as a second `CostEntry`
   row via `record_cost()` (`cost_collection_service.py:516-531`).
   `CostEntry` (`database.py:1230-1257`) has no unique constraint on
   `(task_id, source, recorded_at, ...)` — nothing rejects or merges the
   duplicate.
7. `record_cost()` triggers `derive_task_cost`/`derive_workflow_cost`
   (`cost_derivation.py:110-113`), which roll further into
   `derive_feature_cost`/`derive_design_cost`/`derive_project_cost`
   (`cost_derivation.py:174-179`) — every level of the cost hierarchy is
   now ~2x the true spend.
8. `derive_project_cost` calls `_check_budget_enforcement`
   (`cost_derivation.py:284-286`), which pauses the project
   (`pause_project_workflows`) once `cost_total_usd >= cost_limit_usd`
   (`cost_derivation.py:298-307`). With costs inflated ~2x, a project with
   a real spend of $50 against a $100 budget gets paused as if it had
   spent $100 — silent, premature work stoppage with no error, just a
   `logger.warning` line.

**Why it's not a clean, at-least-detectable 2x:** the extension's POST is
fire-and-forget and any failure (network blip, API restart, 401, etc.) is
swallowed with only `console.warn` (`index.ts:109-112`) — that turn is
never retried. So a session's turns are inconsistently double-counted:
turns where the real-time POST succeeded get counted twice, turns where it
failed get counted once (via the JSONL tail only). The resulting total is
neither the true cost nor a clean multiple of it, so it can't be corrected
after the fact by dividing by two — the corruption is unrecoverable from
the stored data alone.

**Recommended fix:** pick one source of truth per session, not both.
Simplest correct option: have `collect_task_cost` initialize
`SessionCostCheckpoint.lines_processed` to the current line count of the
session file at the point the pi extension is confirmed active (e.g. when
`initialize()` fires and posts a marker, or by having the extension itself
report the line count it started tailing from), so the JSONL collector
only ever picks up turns the extension didn't already cover. Alternative:
have the JSONL collector skip any turn whose cost already exists as a
`source="pi"` `CostEntry` for that `task_id` within the same time window
(requires making individual turns identifiable, e.g. by transcript line
number, which `CostEntry` does not currently store). Either fix requires a
schema or checkpoint-semantics change — flag for architecture, this is not
a doc-only fix.

### B-2: A single bad entry silently discards an entire task's cost data, permanently, with no retry

**Exact failure sequence:**
1. `collect_task_cost` (`cost_collection_service.py:404-545`) collects a
   list of entries from the collector, then loops and calls
   `record_cost()` for each one (`cost_collection_service.py:516-531`),
   all inside one `with get_db() as db:` block.
2. `get_db()` (`database.py:2072-2086`) commits once, after the whole
   block exits; on any exception it rolls back the entire transaction and
   re-raises.
3. If `record_cost()` raises for entry N (e.g. `ValueError` from
   `cost_usd < 0`, `cost_derivation.py:77-78` — reachable if a future
   collector or a malformed `raw_usage` payload produces a negative value
   that a collector's own `<= 0` guard doesn't catch, since
   `PiJsonlCollector` only guards its own extraction, not `record_cost`'s
   input generically), the whole `with` block's rollback discards every
   `CostEntry` already `db.add()`-ed in that loop (entries 1..N-1) **and**
   the `SessionCostCheckpoint` update (`cost_collection_service.py:533-543`)
   never happens, since it runs after the loop, inside the same block.
4. The exception propagates out of `collect_task_cost` to its only caller,
   `TaskCompletionService.collect_cost_on_completion`
   (`task_completion_service.py:912-928`), which catches `Exception`
   broadly and logs `logger.warning(...)` — no re-raise, no alert, no
   dead-letter record of the failed batch.
5. Because the checkpoint was never advanced, and `collect_cost_on_completion`
   is only invoked once, on task completion (`task_completion_service.py:916`),
   there is no later retry path that would pick this session back up. The
   entire task's cost — not just the one bad entry — is permanently lost,
   silently, with only a log line no one is watching in the failure path.

**Recommended fix:** wrap each `record_cost()` call in its own
try/except inside the loop so one bad entry is skipped (logged loudly,
e.g. `logger.error`) without discarding the rest of the batch, and advance
the checkpoint past whatever was successfully processed rather than
all-or-nothing.

## WARNING findings

### W-1: The same $1000/turn anomaly is handled two different silent ways depending on ingestion path

`CostEntryCreate.validate_cost_usd` (`autopilot_api.py:1688-1696`) **rejects**
`cost_usd > 1000` with a `ValueError` → the API call fails, and since the
extension only `console.warn`s on failure (`index.ts:109-112`) and never
retries, that turn's cost is dropped entirely from the real-time path. But
`record_cost()` itself (`cost_derivation.py:77-81`), used by the JSONL
fallback path, **silently caps** the same value to exactly `$1000` and
proceeds. Same anomalous input, two different corruption modes (data
dropped vs. data silently truncated), neither surfaced to an operator
beyond a log line. Recommend making both paths agree — either both reject
and require a documented manual-entry recovery, or both cap and emit a
higher-severity signal (not just `logger.warning`) so a genuine cost spike
isn't quietly hidden by the cap.

### W-2: No validation that `task_id`/`workflow_id` refer to real rows — bad IDs produce permanently uncounted orphan `CostEntry` rows

`record_cost()` (`cost_derivation.py:89-104`) writes the `CostEntry` with
whatever `task_id`/`workflow_id` it's given — there's no existence check
before insert (only an existence check for the auto-derive-workflow-from-task
lookup, `cost_derivation.py:84-87`, which just leaves `workflow_id=None` if
the task isn't found, it doesn't reject the write). `derive_task_cost`
and `derive_workflow_cost` (`cost_derivation.py:133-136`, `160-163`) both
silently `return 0.0` when the referenced row doesn't exist. Net effect: a
malformed or stale `task_id` (e.g. `HEPHAESTUS_TASK_ID` left set in an
agent's env after its task actually completed and a new one started under
a different ID, or simply a typo/attacker-supplied header value) produces
a `CostEntry` row that is billed to nothing, rolls up to nothing, and is
invisible in every cost report — the money is spent but silently
unaccounted anywhere in the hierarchy.

### W-3: Composition — `collect_task_cost` embeds per-CLI-type, low-level session-file-discovery logic instead of pushing it into the `CostCollector` subclasses

`collect_task_cost` (`cost_collection_service.py:404-550`) is the
high-level orchestrator (task → agent → session → collector → persist),
but lines 448-487 contain a 40-line `if cli_type == "pi": ... elif
cli_type == "claude_code": <path sanitization, traversal checks, glob
matching> ... elif cli_type == "opencode": pass ... elif cli_type ==
"codex": pass` conditional chain doing CLI-specific, low-level file-path
resolution directly in the orchestration function. `CostCollector` is
already an `ABC` with a `collect()` method (`cost_collection_service.py:28-53`)
— this is exactly the seam where a `discover_session_file(session_id, cwd)`
method on each subclass belongs, so the orchestrator just calls
`collector.discover_session_file(...)` polymorphically instead of
branching on `cli_type` twice (once here, again at
`cost_collection_service.py:494-499` for collector selection). As written,
adding a 5th CLI type means editing this conditional in the high-level
function rather than adding a new subclass — the opposite of what the ABC
is for.

## NIT findings

### N-1: Fire-and-forget POST failures are invisible outside the pi TUI's own log

`index.ts:109-112` logs failed cost posts via `console.warn` inside the pi
process. Nothing forwards this to Hephaestus — an operator watching the
dashboard has no signal that cost tracking degraded for a session unless
they're tailing that specific pi process's stdout. Low severity since the
JSONL fallback (mostly) recovers the data (modulo B-1), but worth a
follow-up to at least count/expose failed-post occurrences somewhere
queryable.

## Gate recommendation

**FAIL — 2 BLOCKERs.** Both are pre-existing defects in the cost-tracking
pipeline this feature's README documents and claims (incorrectly, in B-1's
case) is correct, not defects introduced by this feature's 1-line diff.
Recommend: file B-1 and B-2 as their own follow-up tickets against
`cost_collection_service.py`/`cost_derivation.py` rather than blocking
this feature's trivial README fix — but do not let the README continue to
assert "prevents double-counting" (line 51) without qualification while
B-1 is open, since that's now a documented false claim about data
integrity. Minimum bar to keep the README honest: soften or remove that
line until B-1 is fixed.
