---
type: architecture_design_report
---

# Architecture: OpenCode Cost Collector

**Feature ID:** des-91c8-opencode-collector
**Status:** Ready for implementation (scope_review verdict: PROCEED_TO_ARCHITECTURE_DESIGN, `docs/scope_review/scope_review_result.json`)
**Input:** `docs/requirements_analysis.md` (FR1-FR5, NFRs, Open Questions)

This document is scoped strictly to what `docs/requirements_analysis.md` authorized: make `collect_task_cost()` produce correct `CostEntry` rows for `cli_type == "opencode"` tasks by reading OpenCode's own SQLite DB. No schema changes, no new call sites, no UI changes.

---

## 1. System Architecture

No new components. This feature replaces the body of one existing branch (`cost_collection_service.py:482-485`) and one existing class (`OpenCodeCollector`), and adds one new private helper. Everything else in the cost pipeline — `record_cost()`, `SessionCostCheckpoint`, the rollup chain, the UI — is consumed unchanged, per requirements NFR "No new tables/columns."

```
Task completion
      │
      ▼
collect_task_cost(task_id)                    [cost_collection_service.py:404]
      │
      ├─ cli_type == "pi"          → _discover_session_file()      (unchanged)
      ├─ cli_type == "claude_code" → inline .claude/projects lookup (unchanged)
      ├─ cli_type == "opencode"    → _discover_opencode_session()  (NEW)
      │                                   │
      │                                   ▼
      │                          opens ~/.local/share/opencode/opencode.db
      │                          read-only, queries `session` table by
      │                          directory + time window
      │                                   │
      │                                   ▼
      │                          returns (db_path, session_row_id) or None
      │
      ▼
collector.collect(...)  →  OpenCodeCollector (REWRITTEN)
      │  opens same opencode.db read-only, re-selects the row by
      │  session_row_id, maps columns → entry dict
      ▼
record_cost() + SessionCostCheckpoint update           (unchanged)
```

The existing `if not session_file: ... return` guard at line 489 is the natural join point: OpenCode's discovery step returns something usable there, everything downstream (collector dispatch, `record_cost`, checkpoint write, logging) requires zero changes.

### 1.1 Why the collector interface doesn't change

`CostCollector.collect()`'s signature takes `session_file: Path`. For OpenCode this isn't a session transcript — it's the OpenCode `opencode.db` path plus a row identifier. Two options were considered:

- **(Rejected) Add a new method to the `ABC`** (e.g. `collect_db(db_path, row_id, ...)`) — requires stub overrides on `PiJsonlCollector`/`ClaudeCodeCollector`/`CodexStubCollector` for a method they'll never use, violates "no new abstraction for one additional source" (NFR, Technology Constraints).
- **(Chosen) Reuse `session_file` as the DB path, and thread the matched row's rowid through `checkpoint`** — `checkpoint` is currently `int` (`lines_processed`), and OpenCode has no line concept at all (FR5). `_discover_opencode_session()` returns `(db_path: Path, session_row_id: str)`. `session_file` becomes `db_path`. The matched `session_row_id` is passed to the collector via a new optional constructor field on `OpenCodeCollector`, set by `collect_task_cost()` right before calling `.collect()` — mirroring the existing pattern where each collector is a fresh instance per call (`collectors = {...}` dict is rebuilt every invocation, line 494-499).

This keeps the `ABC` contract untouched and confines OpenCode-specific plumbing to the OpenCode branch and class, matching "Touch only what you must."

---

## 2. Component Interfaces

### 2.1 `_discover_opencode_session(cwd: str, agent_created_at: datetime) -> Optional[Tuple[Path, str]]`

New private helper in `cost_collection_service.py`, placed after `_discover_session_file()` (mirrors its shape and security pattern per requirements §5).

```python
def _discover_opencode_session(cwd: str, agent_created_at: datetime) -> Optional[Tuple[Path, str]]:
    """Discover the OpenCode session row matching an agent's cwd and launch time.

    OpenCode assigns no deterministic session ID Hephaestus controls (unlike
    pi's --session-id / Claude Code's uuid5). Correlation is by directory
    (session.directory is a literal, unsanitized path) and a time window
    bounded by [agent_created_at, now].

    Returns (db_path, session.id) for the most recent in-window match, or
    None if the DB doesn't exist or no session matches.
    """
```

**Behavior:**
1. Resolve `db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"`. Resolve-and-verify-under-base the same way `_discover_session_file` does for its sessions dir (NFR "Path safety") — defensive even though this path has no user-supplied component, for consistency with the sibling branches.
2. If `db_path` doesn't exist, return `None` (NFR "Graceful absence").
3. Open with `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)` (NFR "Read-only access").
4. Query:
   ```sql
   SELECT id, time_created FROM session
   WHERE directory = ?
     AND time_created >= ?
     AND time_created <= ?
   ORDER BY time_created DESC
   ```
   Params: `cwd`, `int(agent_created_at.timestamp() * 1000)` (epoch-ms, per requirements §1's confirmed schema), `int(datetime.utcnow().timestamp() * 1000)`.
5. Zero rows → log at debug, return `None` (FR2 "log and skip").
6. One or more rows → take the first (`ORDER BY time_created DESC` already puts the most recent first) — this is the explicit tie-break policy for FR2's multiple-match case and Open Question 2: **most recent `time_created` in-window wins.** Log at debug when `len(rows) > 1` noting the discarded candidate IDs, so a misattribution is traceable after the fact.
7. Return `(db_path, row["id"])`.
8. Any `sqlite3.Error` → log and return `None` (matches every other collector's "never raise into `collect_task_cost()`'s caller").

`cwd` comes from the existing `_get_agent_cwd(db, agent, task)` helper, reused as-is (requirements §5) — called from `collect_task_cost()` exactly like the `pi`/`claude_code` branches already do.

### 2.2 `OpenCodeCollector` (rewritten)

Replaces the current stdout-JSON body (`cost_collection_service.py:263-323`) entirely — it is dead code today (requirements §1) and its shape doesn't fit `-i` mode.

```python
class OpenCodeCollector(CostCollector):
    """Collector for OpenCode sessions via opencode.db.

    OpenCode's `session` table stores pre-aggregated cost/token totals
    per session row -- no per-turn tailing needed. Each agent launch
    mints exactly one fresh session row (OpenCodeAgent never resumes),
    so checkpoint is a simple 0/1 "already collected" guard, not a
    line count.
    """

    def __init__(self, session_row_id: Optional[str] = None):
        self.session_row_id = session_row_id

    def collect(
        self,
        session_id: str,
        task_id: str,
        workflow_id: str,
        agent_id: Optional[str],
        session_file: Path,   # repurposed: path to opencode.db
        checkpoint: int,      # repurposed: 0 = not yet collected, 1 = collected
    ) -> Tuple[List[dict], int]:
        entries: List[dict] = []

        if checkpoint >= 1:
            return entries, checkpoint  # already collected this session

        if not self.session_row_id:
            return entries, checkpoint

        try:
            conn = sqlite3.connect(f"file:{session_file}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT cost, tokens_input, tokens_output, tokens_reasoning, "
                "tokens_cache_read, tokens_cache_write, model FROM session "
                "WHERE id = ?",
                (self.session_row_id,),
            ).fetchone()
            conn.close()
        except sqlite3.Error as e:
            logger.error(f"Error reading opencode.db for session {self.session_row_id}: {e}")
            return entries, checkpoint

        if not row or (row["cost"] or 0) <= 0:
            return entries, 1

        model = None
        try:
            model_info = json.loads(row["model"]) if row["model"] else {}
            model = model_info.get("id")
        except json.JSONDecodeError:
            model = row["model"]

        entries.append(
            {
                "id": f"cost-{uuid.uuid4().hex[:8]}",
                "task_id": task_id,
                "agent_id": agent_id,
                "workflow_id": workflow_id,
                "source": "opencode",
                "model": model,
                "input_tokens": row["tokens_input"] or 0,
                "output_tokens": row["tokens_output"] or 0,
                "cache_read_tokens": row["tokens_cache_read"] or 0,
                "cache_write_tokens": row["tokens_cache_write"] or 0,
                "reasoning_tokens": row["tokens_reasoning"] or 0,
                "cost_usd": row["cost"],
                "recorded_at": datetime.utcnow(),
                "raw_usage": dict(row),
            }
        )

        return entries, 1
```

Column mapping is a direct match against the real schema confirmed in requirements §1 (`cost`, `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`, `model` as a JSON string with `id`/`providerID`). No new `CostEntry` columns — `source="opencode"` already exists.

`sqlite3` import needs adding to `cost_collection_service.py`'s import block (stdlib, no new dependency, per Technology Constraints).

### 2.3 `collect_task_cost()` opencode branch (rewritten)

Replaces `cost_collection_service.py:482-485`:

```python
elif cli_type == "opencode":
    cwd = _get_agent_cwd(db, agent, task)
    if cwd:
        result = _discover_opencode_session(cwd, agent.created_at)
        if result:
            db_path, session_row_id = result
            session_file = db_path
            opencode_session_row_id = session_row_id
```

`opencode_session_row_id` is a new local variable in `collect_task_cost()`, initialized to `None` alongside `session_file = None` at line 450. It's read when instantiating the collector:

```python
collectors = {
    "pi": PiJsonlCollector(),
    "claude_code": ClaudeCodeCollector(),
    "opencode": OpenCodeCollector(session_row_id=opencode_session_row_id),
    "codex": CodexStubCollector(),
}
```

No change to the `if not session_file: ... return` guard, the collector-dispatch block, `record_cost()` call, or checkpoint write — all reused as-is.

---

## 3. Data Flow

1. Task completes → existing task-completion handler calls `collect_task_cost(task_id)` (unchanged call site, per requirements Integration Points).
2. `collect_task_cost` loads `Task`, `Agent`; `agent.cli_type == "opencode"`.
3. `_get_agent_cwd(db, agent, task)` → literal cwd string (existing helper, unchanged).
4. `_discover_opencode_session(cwd, agent.created_at)` opens `~/.local/share/opencode/opencode.db` read-only, matches `session.directory == cwd` within `[agent.created_at, now]`, returns the most recent match's `(db_path, id)`.
5. `OpenCodeCollector(session_row_id=id).collect(..., session_file=db_path, checkpoint=<0 or 1 from SessionCostCheckpoint>)` re-queries the same row by `id`, maps its pre-aggregated columns to a single `CostEntry` dict, returns checkpoint `1`.
6. `record_cost()` writes the `CostEntry` row and triggers the existing rollup chain (Task → Feature → AutopilotDesign → AutopilotProject `cost_total_usd`) — unchanged.
7. `SessionCostCheckpoint` row is created/updated with `lines_processed=1`, keyed by Hephaestus's own internal `session_id` (from `_extract_session_id`, unrelated to OpenCode's `session.id` — this is the existing tmux-session-name-derived ID used uniformly across all four `cli_type`s for checkpoint bookkeeping only). This prevents a second `collect_task_cost()` call for the same Hephaestus session (e.g. a retry) from double-recording the same OpenCode session's total.

**No polling, no timer** — same single trigger point as every other collector (NFR "No timer-based collection").

---

## 4. Infrastructure Requirements

None beyond what's already present. `sqlite3` is stdlib. No new environment variables, no new config keys, no new files under `config/`. The one external dependency is `~/.local/share/opencode/opencode.db` existing on the host running the agent — a file this feature only ever opens read-only, never creates or migrates.

---

## 5. Task Breakdown

Tasks are ordered by blocking dependency. All tasks operate on files already identified in requirements §5; no new files.

### Task 1 — Resolve Open Question 1: test `opencode -s <id>` session-creation semantics
**Blocks:** Task 2, Task 3 (their design depends on the outcome)
**Not blocking:** none upstream

Run a real `opencode -s <caller-chosen-id> run "..."` invocation (or `-i` equivalent) against a scratch directory and inspect `~/.local/share/opencode/opencode.db`'s `session` table afterward.

**Acceptance criteria:**
- Determine definitively: does `-s <id>` (a) mint a **new** session row with `id == <id>` when no such session exists, or (b) error/no-op when the ID doesn't already exist (i.e. resume-only)?
- Record the finding in a code comment at the top of `_discover_opencode_session()` (or, if (a), skip directly to implementing Task 2's deterministic-ID variant instead of time-window matching) and in the memory saved at the end of this phase.
- If (a) is true: implement `OpenCodeAgent.get_session_args()` override passing `-s {session_id}` (mirroring `ClaudeCodeAgent`'s uuid5 pattern at `cli_interface.py:403-411`), and change `_discover_opencode_session()` to look up `session.id = ?` directly instead of directory+time-window matching — this is strictly simpler and eliminates the multiple-match ambiguity in FR2 entirely. Re-scope Tasks 2-3 below accordingly before implementing them.
- If (b): proceed with Tasks 2-3 as specified (time-window matching).

### Task 2 — Implement `_discover_opencode_session()`
**Blocks:** Task 3, Task 4
**Blocked by:** Task 1

Add the helper described in §2.1 to `cost_collection_service.py`, placed after `_discover_session_file()` (~line 402).

**Acceptance criteria:**
- Function signature and behavior match §2.1 exactly (or the Task-1-conditional deterministic-ID variant if `-s` supports create).
- Resolves `~/.local/share/opencode/opencode.db`, verifies the resolved path stays under `~/.local/share/opencode/` before opening (NFR "Path safety"), matching the resolve-and-startswith pattern at lines 376-383 and 470-481.
- Opens with `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)` — never opens read-write.
- Returns `None` (not an exception) when: the DB file doesn't exist, the query returns zero rows, or any `sqlite3.Error`/`OSError` occurs.
- On multiple in-window matches, selects the most recent `time_created` and logs a debug line naming the discarded candidate IDs.
- Unit test with a temp SQLite file containing a `session` table (matching the real schema's column names from requirements §1) covering: no DB file, empty result, single match, multiple matches (tie-break verification), directory mismatch, time-window boundary (session just before `agent_created_at`, just after "now").

### Task 3 — Rewrite `OpenCodeCollector`
**Blocks:** Task 4
**Blocked by:** Task 1, Task 2

Replace `cost_collection_service.py:263-323` with the class in §2.2.

**Acceptance criteria:**
- Constructor accepts `session_row_id: Optional[str] = None`.
- `collect()` treats `checkpoint` as a 0/1 collected-flag, `session_file` as the `opencode.db` path — not a transcript.
- Queries the `session` row by `id = self.session_row_id`, maps `cost`→`cost_usd`, `tokens_input`→`input_tokens`, `tokens_output`→`output_tokens`, `tokens_reasoning`→`reasoning_tokens`, `tokens_cache_read`→`cache_read_tokens`, `tokens_cache_write`→`cache_write_tokens`.
- Parses `model` (JSON string) for `id`; falls back to the raw string if `json.loads` fails; falls back to `None` if the column is empty.
- Returns `([], checkpoint)` unchanged when `checkpoint >= 1`, `session_row_id` is `None`, the row is missing, or `cost <= 0`.
- Returns `([], 1)` when the row exists but cost is `<= 0` (matches existing `PiJsonlCollector`/`ClaudeCodeCollector` "skip zero-cost, still don't error" convention) — checkpoint still advances to 1 so a genuinely zero-cost session isn't re-queried every completion.
- Never raises out of `collect()` — catches `sqlite3.Error` and logs.
- Unit test with a temp SQLite `session` table: normal row, zero-cost row, missing row, malformed `model` JSON, `session_row_id=None`.

### Task 4 — Wire the `collect_task_cost()` opencode branch
**Blocks:** Task 5
**Blocked by:** Task 2, Task 3

Replace `cost_collection_service.py:482-485` per §2.3; add `opencode_session_row_id = None` initialization near line 450; pass it into `OpenCodeCollector(...)` at the collectors-dict construction (line 494-499); add `import sqlite3` to the top-level imports.

**Acceptance criteria:**
- `cli_type == "opencode"` branch calls `_get_agent_cwd` then `_discover_opencode_session`, sets both `session_file` and `opencode_session_row_id` on success, leaves both `None` on any failure (falls through to the existing `if not session_file: ... return` guard — no new early-return path introduced).
- `codex` branch and all other branches are untouched (byte-for-byte, aside from the shared `opencode_session_row_id = None` init line if placed adjacently).
- Integration test: seed a fake `Agent(cli_type="opencode", created_at=...)`, `Task`, `Workflow(working_directory=...)`, and a temp `opencode.db` with a matching `session` row; call `collect_task_cost(task_id)`; assert a `CostEntry` row is written with `source="opencode"` and the expected `cost_usd`/token values, and a `SessionCostCheckpoint` row exists with `lines_processed=1`.
- Integration test: call `collect_task_cost(task_id)` a second time for the same task/session; assert no second `CostEntry` row is written (checkpoint guard holds).
- Integration test: no `opencode.db` present on the filesystem at all; assert `collect_task_cost` returns without raising and writes no `CostEntry`.

### Task 5 — Regression pass on existing collectors
**Blocks:** none (final task)
**Blocked by:** Task 4

**Acceptance criteria:**
- Existing `pi` and `claude_code` collection tests still pass unmodified (targeted run: `pytest` on whatever test module(s) cover `cost_collection_service.py` — do not run the full suite).
- Confirm the `collectors` dict change (constructing `OpenCodeCollector` with a kwarg instead of `OpenCodeCollector()`) doesn't break the `pi`/`claude_code`/`codex` entries, which remain no-arg constructions.

---

## 6. Explicitly Out of Scope

Per requirements §2/§6 and the design doc's Codex stub status:
- Codex collection (`CodexStubCollector` untouched).
- Any UI surfacing of OpenCode-specific data — `source="opencode"` is already a first-class value in every UI component.
- `AutopilotProject.cli_tool` UI exposure (`ProjectSettingsModal.tsx`) — configuring OpenCode as a project's CLI is a separate, unrelated feature; this collector works regardless of how `cli_type` gets set.
- OpenCode DB schema versioning/migration handling — graceful-failure-on-unexpected-shape only (NFR), no version-detection logic.
