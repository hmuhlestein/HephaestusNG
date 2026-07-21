# Human-Input Intervention System — Design Document

**Status:** Ready for implementation — **depends on
`design_docs/backend_module_decomposition.md` landing first**
**Scope:** Replace the file-based human-input mailbox (`input_request_*.json` /
`input_response_*.json`) with a DB-backed `autopilot_interventions` table and
an in-process wait/notify handoff. This is Tier 2, item 1 of
`design_docs/autopilot_architecture_review.md` §11.3 — the highest-priority
remaining item, now carrying an additional, more urgent bug than when it was
originally scoped.

**Prerequisite.** The module-decomposition doc deliberately leaves
`prompt_human()` and the file-based `/input` routes untouched during its
split, specifically so this spec starts from a clean, already-modularized
tree instead of racing that refactor. **§2 below describes the codebase
*after* that split has landed** — `src/autopilot/orchestrator/policy.py`,
`src/autopilot/orchestrator/engine_client.py`, and `src/mcp/autopilot/intervention_routes.py`
are assumed to already exist. If for some reason this is being implemented
before that split, stop and do the split first; don't improvise around a
partially-modularized tree.

---

## 1. Problem statement

`src/autopilot/orchestrator/__init__.py`'s `prompt_human()` (the
module-decomposition split converts the old flat `orchestrator.py` into a
package; `prompt_human` lands in `__init__.py`'s "remaining driver" set —
see the Prerequisite note above) and the `/api/autopilot/input` routes (now
in their own `src/mcp/autopilot/intervention_routes.py`, extracted as-is by
the split) coordinate a human-in-the-loop decision entirely through JSON
files under `AUTOPILOT_STATE_DIR` (`~/.hephaestus/autopilot`,
`src/core/constants.py:6`) — a single directory, global to the whole
backend process, shared by every project.

Three concrete problems, in order of severity:

### B9 — no project/design scoping (new; makes this urgent)

Neither the request payload written by `prompt_human()` nor the
`HumanInputRequest`/`HumanInputResponse` Pydantic models (now in
`src/mcp/autopilot/intervention_routes.py`, moved there verbatim by the
split — check current line numbers, they'll differ from the pre-split
file) carry a `project_id`, `design_id`, or `workflow_id`.
`_find_pending_input()` (same file) returns "the first non-stale
`input_request_*.json`" in glob-sort order — and because filenames are
`input_request_<uuid4()[:8]>.json`, that order is effectively random.

Since `AutopilotServiceRegistry` (`src/autopilot/service.py`) now runs one
`AutopilotService` per `project_id` with a `max_concurrent_projects` cap
(§9 item 2 of the architecture review, updated 2026-07), **two different
projects' orchestrator threads can each be blocked inside `prompt_human()`
at the same time**, both writing into the same directory. `GET
/api/autopilot/input` and the frontend's `apiService.getAutopilotInput()`
(`frontend/src/services/api.ts:725`) take no `project_id` parameter at all —
confirmed by reading both the backend route and the frontend call site — so
there is no way for a user looking at Project A's dashboard to know whether
the banner they're answering belongs to Project A or Project B. Answering it
resolves whichever request the glob happened to return first, silently
unblocking the wrong project's pipeline.

This did not exist when the architecture review was written (single-project
was true then); it is a direct, unaddressed consequence of the multi-project
work that shipped afterward.

### B4 (pre-existing) — TOCTOU on the request file

`_find_pending_input()` reads a file, may unlink it if stale, and returns
its path; the caller (`get_human_input_request`) then re-reads it. Between
those steps, `prompt_human()`'s own polling loop or `dismiss_human_input`
can delete the same file. Not catastrophic (worst case: a 404 on submit,
user retries), but it's a real race with no lock.

### File IPC is generally fragile here

`prompt_human()` polls the filesystem every ~1-2s (`orchestrator.py:1815
onward`) for up to `timeout` seconds (default 600s) via a plain
`time.sleep`/`select` loop. This works, but it's the one remaining place in
the *already in-process* pipeline (per §11.1 of the architecture review,
Tier 2 "partial": `AutopilotService` runs the pipeline in-process via
`run_in_executor`, not a subprocess) that still talks to itself through
files instead of a shared in-memory/DB primitive — the last visible trace of
the old subprocess-based design.

---

## 2. Current code, post-split (read, not guessed against whatever tree you're actually on)

- `prompt_human(reason, logger, timeout=600)` — left in
  `src/autopilot/orchestrator/__init__.py` by the split, in the "remaining
  driver" section, alongside
  `run_continuous_pipeline`. Runs on whatever thread is executing the
  pipeline (`AutopilotService.start()` drives `run_continuous_pipeline` via
  `asyncio.get_event_loop().run_in_executor(None, ...)` — confirmed
  in-process, not a subprocess, so a `threading.Event` is sufficient; no
  cross-process bridge is needed). Writes `input_request_<id>.json`
  (`id`, `reason`, `timestamp`, `options: ["c","s","q"]`, `labels`,
  `timeout_seconds`), then polls for `input_response_<id>.json` or terminal
  input. Accepts `"m"` (message-only — logs and keeps waiting) or `"c"`/`"s"`/`"q"`
  (returns the choice). Auto-continues (`"c"`) on timeout or if the request
  file is externally deleted (dismiss). **This spec deletes this
  implementation entirely and authors its replacement directly in
  `src/autopilot/orchestrator/policy.py`** (which now exists post-split, next to
  `detect_impasse`/`check_api_credits` — its natural neighbors) — see §3.4.
- Two call sites, both inside `run_continuous_pipeline` (still in
  `orchestrator/__init__.py`): the credit-exhaustion path and the impasse path (via
  `detect_impasse()`, now imported from `policy.py`). Both need an import
  added — `from src.autopilot.orchestrator.policy import prompt_human` — once §3.4 moves
  it there; check current line numbers, they've shifted from pre-split.
- `src/mcp/autopilot/intervention_routes.py` (post-split location; extracted
  as-is, file-based, by the module-decomposition work): `HumanInputRequest`/
  `HumanInputResponse` models, `_find_pending_input()`, `GET /input`,
  `POST /input`, `DELETE /input/{request_id}`, `STALE_INPUT_SECONDS = 3600`.
  This spec rewrites this file's *contents* in place — no further file-move
  needed, the split already gave it a stable home.
- `api_get`/`api_post`/`get_tasks`/`get_agents` and friends now live in
  `src/autopilot/orchestrator/engine_client.py` post-split — not directly relevant to
  this feature's implementation, but if you need to check how the pipeline
  talks to the backend elsewhere for a pattern to match, that's where to
  look now, not `orchestrator.py`.
- Frontend: `HumanInputBanner.tsx` (`frontend/src/components/autopilot/`,
  unaffected by the backend split) already takes a `projectId` prop and
  uses it in the React Query cache key (`['autopilot-input', projectId]`),
  but never sends it to the backend — `apiService.getAutopilotInput()` /
  `submitAutopilotInput()` / `dismissAutopilotInput()`
  (`frontend/src/services/api.ts:725-736`) take no project/design
  identifier.
- Options are already consistent between orchestrator and API
  (`"c"`/`"s"`/`"q"`/`"m"`) — the architecture review's **B7** ("option
  vocabularies diverge") is stale; do not re-fix what isn't broken, just
  don't regress it.
- DB migration convention for a brand-new table on existing databases:
  `Base.metadata.create_all(self.engine, tables=[Feature.__table__],
  checkfirst=True)`, used in `DatabaseManager._migrate_feature_model_columns`
  (`src/core/database.py:1701` at time of writing — this file is untouched
  by the module split, so this reference should still be close). Follow
  this pattern, not a hand-written `ALTER TABLE`.
- Existing model conventions to match: `id` = `f"{prefix}-{uuid4().hex[:8]}"`
  style primary keys, `status` as a `String` + `CheckConstraint(...)`
  (see `Feature`, `src/core/database.py:1073-1111`), FK columns typed
  `String` with `ForeignKey("table.id")`.

---

## 3. Design

### 3.1 New table: `autopilot_interventions`

Add to `src/core/database.py`, near `AutopilotDesign`/`Feature`:

```python
class AutopilotIntervention(Base):
    """A human-input request raised by the autopilot pipeline (credit
    exhaustion, impasse) and its resolution. Replaces the
    input_request_*.json / input_response_*.json file mailbox."""

    __tablename__ = "autopilot_interventions"

    id = Column(String, primary_key=True)  # Format: interv-{uuid4().hex[:8]}
    project_id = Column(String, ForeignKey("autopilot_projects.id", ondelete="CASCADE"), nullable=False)
    design_id = Column(String, ForeignKey("autopilot_designs.id"), nullable=True)
    workflow_id = Column(String, ForeignKey("workflows.id"), nullable=True)
    reason = Column(Text, nullable=False)
    options = Column(JSON, nullable=False)  # e.g. ["c", "s", "q"]
    labels = Column(JSON, nullable=False)   # e.g. {"c": "Continue", ...}
    status = Column(
        String,
        CheckConstraint("status IN ('pending', 'resolved', 'dismissed', 'timed_out')"),
        nullable=False,
        default="pending",
    )
    choice = Column(String, nullable=True)       # "c" / "s" / "q", set on resolve
    message = Column(Text, nullable=True)         # optional human note
    timeout_seconds = Column(Integer, nullable=False, default=600)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
```

`project_id` is required (every intervention originates from one project's
pipeline run); `design_id`/`workflow_id` are nullable because credit
exhaustion can fire before a design has started processing. This is what
fixes **B9**: every row is unambiguously scoped, so two concurrent projects
can never collide, and the UI can filter `WHERE project_id = ?` directly
instead of guessing.

Register the table via the existing new-table migration pattern
(`Base.metadata.create_all(self.engine, tables=[AutopilotIntervention.__table__],
checkfirst=True)`) in a new `_migrate_autopilot_interventions_table` method,
called from `create_tables()` alongside the other `_migrate_*` calls.

### 3.2 In-process wait/notify (replaces file polling, not `asyncio.Condition`)

The architecture review's original §4.4 proposed `asyncio.Condition`, written
when it assumed the orchestrator might still be a separate process. It's
now confirmed in-process, but `prompt_human()` runs on a
`run_in_executor` **thread**, not the asyncio event loop — a raw
`asyncio.Condition` isn't safely awaitable from there. Use a plain
`threading.Event`, which is safe to `set()` from the FastAPI async handler
(any thread) and `wait(timeout=...)` on the pipeline thread:

- A process-wide `dict[str, threading.Event]`, keyed by intervention `id`,
  holds the event for each currently-pending intervention. Created when the
  row is created, popped when resolved. **Lives in a new
  `src/autopilot/interventions.py`** (see §3.3) — not in
  `intervention_routes.py`. `prompt_human()` (`src/autopilot/orchestrator/policy.py`)
  and the route handlers (`src/mcp/autopilot/intervention_routes.py`) both
  need to reach the same dict, and `src/autopilot/*` must not import from
  `src/mcp/*` (wrong dependency direction — the mcp layer depends on the
  autopilot layer everywhere else in this codebase, never the reverse). A
  small module under `src/autopilot/` that both sides import is the only
  placement consistent with that direction.
- `prompt_human()` creates the DB row (via a small helper — see 3.3),
  registers the event, then `event.wait(timeout=timeout)` instead of the
  current `while time.time() - start < timeout: ...` sleep loop. On wake,
  re-read the row for the final `choice`/`status`. **On timeout (the
  `event.wait()` call returns `False`), the row must be actively
  transitioned to `status='timed_out'`** — not just left as `pending` and
  re-read as-is. Leaving it `pending` is a real bug, not a cosmetic one:
  `get_pending_intervention(project_id)` would keep returning this same
  stale row indefinitely after `prompt_human` has already given up and the
  pipeline has moved on, so the UI would show a phantom intervention
  forever, and a human answering it late via `POST /input` would silently
  succeed against a request nothing is listening for anymore. This is also
  why the `AutopilotIntervention.status` `CheckConstraint` includes
  `'timed_out'` as a real value (§3.1) — it must actually get used, not
  sit in the constraint unreachable.
- `POST /input` sets the row's `choice`/`status`/`resolved_at` and calls
  `event.set()`.
- If the backend restarts mid-wait, the in-memory event dict is empty on
  the next poll — a `pending` row not in the dict has no event to wait on.
  This is a smaller regression than it looks: the *old* file mailbox had
  the identical failure mode (a restarted orchestrator subprocess forgot
  every in-flight request too, since state lived only in that process's
  loop variables). Document it as a known gap, matching the doc's existing
  §8.1 "no backend lifecycle integration" note — do not try to solve
  cross-restart durability in this change.

### 3.3 New module: `src/autopilot/interventions.py`

Houses the DB helpers and the event registry from §3.2 together — both
`create_intervention`/`resolve_intervention`/`dismiss_intervention`/
`get_pending_intervention` and the `dict[str, threading.Event]` belong to
the same "intervention lifecycle" concern, and keeping them in one module
means `policy.py` and `intervention_routes.py` each have exactly one new
import to add, not two:

- `create_intervention(project_id, reason, options, labels, timeout_seconds, design_id=None, workflow_id=None) -> AutopilotIntervention` — inserts the row, registers its `threading.Event`, returns the row.
- `resolve_intervention(intervention_id, choice, message=None) -> bool` — updates status/choice/resolved_at, `event.set()`s and pops its entry; returns `False` and makes no change if the row's current status is anything other than `'pending'` (i.e. already `resolved`, `dismissed`, *or* `timed_out` — not just the `resolved` case; this is also what keeps the §3.2 timeout-vs-late-resolve race in §5's tests resolving in favor of whichever transition actually lands first).
- `dismiss_intervention(intervention_id) -> bool` — **new, not in the original sketch of this section; needed because §3.5's `DELETE /input/{id}` has no `choice` to hand `resolve_intervention` and dismissal is a distinct action, not a fourth option value.** Same shape as `resolve_intervention` but sets `status='dismissed'`, `resolved_at=now()`, no `choice`/`message`; same `event.set()` + pop + "only from `pending`" idempotency guard. This is what makes `dismiss_human_input`'s old file-delete behavior (which let `prompt_human`'s polling loop notice the file was gone and auto-continue) actually reachable in the new design — without this function, nothing ever sets `status='dismissed'` or wakes a blocked `prompt_human()` on dismiss, silently breaking today's dismiss-to-continue UX.
- `get_pending_intervention(project_id) -> Optional[AutopilotIntervention]` — the DB-backed replacement for `_find_pending_input()`, scoped by `project_id` (fixes B9 directly).
- `wait_for_resolution(intervention_id, timeout) -> AutopilotIntervention` — blocks on that intervention's `threading.Event` via `event.wait(timeout)`. If it returns `True` (the row is `resolved` or `dismissed` — either `resolve_intervention` or `dismiss_intervention` called `event.set()`), re-fetch and return the row as-is. **If it returns `False` (timed out), update the row to `status='timed_out'`, `resolved_at=now()` before returning it** — same idempotency guard as `resolve_intervention` (only transition if still `pending`; a race where `POST /input`/`DELETE /input/{id}` resolves or dismisses it in the same instant it times out should let that real transition win, not get clobbered by the timeout path). Pop the event dict entry either way. This is the one place in the module that actually writes `'timed_out'`; used by `prompt_human()` (§3.4) instead of it managing the event directly.

### 3.4 `prompt_human()` rewrite — authored in `src/autopilot/orchestrator/policy.py`

Delete the current implementation from `orchestrator/__init__.py` entirely (don't
leave it as a fallback — matches this codebase's "delete dead code, no
compat shims" convention; see `CLAUDE.md` "Forbidden" section and this
repo's own precedent of deleting `_summary_from_output_artifact` rather
than patching it). Author the replacement in `policy.py`, next to
`detect_impasse`/`check_api_credits`, its two callers.

Signature grows one required parameter: `prompt_human(reason, logger,
project_id, design_id=None, workflow_id=None, timeout=600)`. Both call
sites are in `run_continuous_pipeline` (still in `orchestrator/__init__.py`
post-split — confirm current line numbers, they've shifted) and already run inside a
function with `project_id` in scope (`run_continuous_pipeline` is called
per-project via `AutopilotService`) — thread it through rather than
inferring it. Add `from src.autopilot.orchestrator.policy import prompt_human` at both
call sites.

Body: `create_intervention(...)` → `row = wait_for_resolution(intervention.id,
timeout)` → `return row.choice if row.status == "resolved" else "c"`. The
`else` branch covers both `dismissed` and `timed_out` — both match today's
external behavior exactly (dismiss and timeout both currently
auto-continue), but now via two distinct, honest terminal DB states instead
of one row silently rotting as `pending` forever. `prompt_human`'s own
return type/contract to its callers is unchanged — always a plain string,
never the row itself.

### 3.5 API routes — rewrite `src/mcp/autopilot/intervention_routes.py` in place

The split already gave this its own file with the three file-based routes
moved in verbatim; this task replaces their *bodies*, not their location.
Delete `_find_pending_input()`, `STALE_INPUT_SECONDS`, and all
`Path(AUTOPILOT_STATE_DIR)` file handling from this file — none of it is
needed once the routes call into `src/autopilot/interventions.py`:

- `GET /input?project_id=...` (project_id now **required** — this is the
  actual B9 fix on the read side) → `get_pending_intervention(project_id)`,
  returns `None` if nothing pending.
- `POST /input` → body includes `intervention_id` (renamed from
  `request_id` for clarity, or keep `request_id` if the frontend diff would
  rather not rename — implementer's call, but be consistent) → validates
  choice is in the row's own `options` (not a hardcoded tuple — a row could
  in principle carry a different option set later) → `resolve_intervention(...)`.
- `DELETE /input/{intervention_id}` → `dismiss_intervention(intervention_id)`
  (§3.3) — same as today conceptually (mark `dismissed`, don't require a
  choice, and — new versus the old behavior — actually wake a blocked
  `prompt_human()` immediately via the row's `event.set()` rather than
  requiring it to notice a deleted file on its next poll) but as a status
  update, not a file unlink.
- Keep the existing `_invalidate("status")` cache-bust calls on both submit
  and dismiss — `_invalidate` now lives in `src/mcp/autopilot/_shared.py`
  post-split; import it from there.

### 3.6 Frontend

`HumanInputBanner.tsx` already receives `projectId` as a prop — the fix is
almost entirely in `frontend/src/services/api.ts`:
`getAutopilotInput(projectId)`, `submitAutopilotInput(interventionId, choice, projectId, message?)`,
`dismissAutopilotInput(interventionId, projectId)` — thread `projectId`
through to the actual HTTP call (as a query param for GET, body field for
POST/DELETE). No visual/UX change needed; this is purely closing the gap
between the prop that's already there and the network call that ignores it.

---

## 4. Out of scope (explicitly deferred)

- The WS/SSE event stream (§11.3 Tier 2 item 2 of the architecture review).
  This change makes interventions DB-backed and poll-free on the
  orchestrator side; the UI can keep its existing 5s `refetchInterval`
  poll (`HumanInputBanner.tsx:19`) for now. Don't build a stream as a
  side effect of this task.
- Persisting `PipelineState`/messages/events to DB (Tier 2 item 3) —
  unrelated table, separate item.
- Splitting `OrchestratorLogger` (Tier 2 item 4) — unrelated.
- Cross-restart durability of in-flight interventions (see §3.2) — matches
  existing, already-documented limitation elsewhere in the service.

---

## 5. Testing

Follow this repo's git-stash-verify discipline: write the test against the
old file-based behavior first (should fail once the old code is removed,
confirming the test isn't vacuous), then implement, then confirm green.

- `tests/test_autopilot_interventions.py` (new — mirrors whatever naming
  convention the split's own new-module tests landed on, e.g.
  `tests/test_autopilot_policy.py` if one exists by now; match it):
  table creation via the migration helper on a fresh DB;
  `create_intervention`/`resolve_intervention`/`get_pending_intervention`/
  `wait_for_resolution` round-trip; two interventions with different
  `project_id` don't leak into each other's `get_pending_intervention`
  result (this is the direct regression test for **B9** — construct two
  pending rows for two different `project_id`s and assert each project
  only ever sees its own); **`wait_for_resolution` on a genuine timeout
  (no `resolve_intervention` call, short `timeout_seconds`) transitions the
  row to `status='timed_out'` — assert `get_pending_intervention(project_id)`
  no longer returns it afterward.** This is the regression test for the
  stale-`pending`-row bug §3.2/§3.3 call out explicitly — without the
  timeout-path status write, this assertion is exactly what would fail.
  Also test the race explicitly: call `resolve_intervention` at
  approximately the same moment `wait_for_resolution`'s timeout elapses and
  assert the row ends up `resolved` (the real answer), never clobbered back
  to `timed_out`.
- `tests/test_autopilot_policy.py` (or wherever the split's own tests for
  `policy.py` ended up — check first, don't create a second file for the
  same module): `prompt_human` resolves correctly when
  `resolve_intervention` is called on another thread before timeout
  (event-driven, not sleep-driven — assert it returns well before
  `timeout_seconds` elapses, proving it isn't just polling); returns `"c"`
  on timeout with no response.
- `tests/test_autopilot_api.py` or `tests/test_intervention_routes.py`
  (whichever the split's route-file split settled on for this route group
  — check first): `GET /input` requires `project_id` (400 or 422 without
  it); `POST /input` rejects a choice not in the row's own `options`;
  double-submit on an already-resolved intervention is rejected or is a
  no-op (idempotent), not a second state transition.
- Frontend: no new test framework needed if none exists for this component
  today — confirm via `npx tsc --noEmit` that the `apiService` signature
  changes don't break other call sites, and manually smoke-test the banner
  against a live dev server per this repo's "test the golden path in a
  browser" convention for UI changes.

---

## 6. Rollout note for whoever (human or agent) implements this

This document is written to be handed to the autopilot pipeline as the next
design to self-host, per `design_docs/autopilot_architecture_review.md`
§11.3. Read that document's §9.2-§9.4 (isolation model, human-in-the-loop
autonomy decisions) before starting — the intervention system this spec
describes *is* the impasse-escalation surface those sections assume exists;
don't re-litigate whether impasses should exist, only how they're plumbed.
