# Tech Debt Survey — 2026-08-22

A fresh sweep beyond `docs/GOD_FUNCTION_DECOMPOSITION_CANDIDATES.md` (function-level
size) and `docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md` (file-level size, except-block
silence). This one covers what those two didn't: type-checking debt, TODO markers,
frontend god-components, and residual items from earlier passes that were tracked
but never closed. Evidence-based — every number below came from actually running
the tool, not estimating.

**Re-verified 2026-08-22, after 11 more commits landed.** mypy 1003→1005 and the
pass-only except count ~145→148 — both within normal noise, findings unchanged.
`phase_transitions.py`'s two deferred functions and `DesignQueuePanel.tsx`'s size
are byte-for-byte unchanged (neither file was touched by the 11 commits). One
real drift: §3 below.

---

## Status: executed 2026-08-23

**§1 mypy — plugin trial reverted (measured), scoped union-attr triage done.**
The "cheap win" was tested exactly as this section suggested — re-running the
count after adding `plugins = ["sqlalchemy.ext.mypy.plugin"]`. Result: it did
NOT clear database.py's 108; the plugin expects SQLAlchemy 2.0 `Mapped[...]`
typing, this codebase uses 1.x-style `relationship()`/`Column()` without
annotations, so the plugin instead emitted 160 "please specify
`Mapped[<type>]`" errors in that same file (1008 → 1131 total). Reverted;
`pyproject.toml` now carries a NOTE explaining the measurement and that the
plugin only becomes a win as part of a `Mapped[]` annotation migration. The
`union-attr` triage was done for the named file, `workflow_execution_routes.py`
(14 sites, 8 handlers): every site is provably non-None on the request path —
`ServerState.startup()` assigns `db_manager`/`phase_manager`/`queue_service`
unconditionally before the app accepts requests, so none was a silent gap and
no speculative guards were added. The debt was paid with ten `assert ... is not
None` narrowings (two inside the queued-task loops, where an empty queue means
the attribute was never touched — placed loop-internal so a test fixture with
an unset `queue_service` and empty queue keeps its 200). mypy 1008 → 994;
`test_workflow_stop_cancel_tmux_offloading.py` + queue/guardrail suites 79/79.

**§2 auth audit-log cluster — DONE.** `AuthService.authenticate` now takes
`ip_address`/`user_agent` (defaulted, backward compatible) and the `/login`
endpoint extracts them (first `X-Forwarded-For` hop else peer address, plus
`User-Agent`); the six empty-string TODO sites (login-attempt ×2, session) and
the login audit-log row now carry the real values. A new `_load_user_roles`
replaces both `roles=[]` TODOs with the user's active role names (a grant
whose `expires_at` has passed is not active) in both `authenticate` and
`refresh_tokens`. Characterized by a new test in `tests/test_authentication.py`
(`test_login_records_request_metadata_and_roles` — posts through the real
router with headers, asserts all three row types + the JWT `roles` claim);
suite 25/25.

**§2 smaller items — one fixed, two deferred with rationale.**
`ticket_service.py`'s list-view `comment_count`/`commit_count` placeholders are
now real bulk group-by counts (two queries, not N+1); ticket suites 47 passed.
Deferred: logout token blacklisting (`auth_api.py`) — it needs a design choice
(access-token blacklist table vs revoking the user's refresh tokens, the latter
wrong for multi-device) and a middleware check; that's a security-semantics
decision, not a drive-by. And `tickets_api.py`'s `has_more=False` TODO — the
route returns the full result set and has no pagination parameters, so
`has_more=False` is correct for the current contract; "implement pagination" is
an API-feature addition, not a placeholder bug.

**§3 frontend — deferred, per the doc's own caveat.** `DesignQueuePanel.tsx`
(1321 lines) was modified minutes before this execution started and
`TaskDetailModal.tsx` (1425) is in the same active dark-mode workstream of the
concurrent session — the doc says "re-check the line count before acting on
it, it's a moving target." Restructuring either mid-edit by another session is
the same write-conflict risk that deferred `phase_transitions.py`'s two retry
functions. Additionally the `window.confirm`/`alert` replacement is a deliberate
UX change (native dialogs → the app's modal system), a design decision the
original finding itself flagged — the Playwright recipe removes the
*verification* blocker, not the *decision*.

**§4 residuals — unchanged by this execution**, still tracked in their home
docs (the two deferred `phase_transitions.py` functions remain deferred for
the reasons recorded there).

---

## 1. mypy: 1003 errors across 118 files — configured but not enforced

`pyproject.toml`'s `[tool.mypy]` section is real (confirmed working in
`docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md`'s audit — it produces genuine type
errors, not a silent no-op). But nobody has looked at what it actually reports:
**1003 errors in 118 of 219 checked source files**. mypy runs; its output isn't
acted on. That gap is itself the debt — a type checker nobody reads catches
regressions no faster than no type checker at all.

**By error class:**

| Class | Count | What it usually means |
|---|---|---|
| `arg-type` | 254 | Optional passed where required, or vice versa |
| `attr-defined` | 175 | attribute access mypy can't resolve on the inferred type |
| `assignment` | 138 | type mismatch on assignment |
| `union-attr` | 132 | attribute access on an `X \| None` without a null check first |
| `no-any-return` | 110 | function declared to return a concrete type, actually returns `Any` |
| `return-value` | 41 | return type mismatch |
| `misc` | 41 | — |
| `valid-type` | 39 | mostly the SQLAlchemy `Base` issue below |

**By file (top 10):**

`database.py` 108, `interfaces/llm_interface.py` 79, `interfaces/langchain_llm_client.py`
57, `monitoring/mechanical_recovery.py` 51, `orchestrator/pipeline.py` 48,
`phases/phase_manager.py` 45, `sdk/client.py` 38, `monitoring/guardian.py` 36,
`agents/launch_pipeline.py` 34, `mcp/server/task_admin_routes.py` 31.

**One cheap, high-leverage fix found: `database.py`'s 108 errors are almost
entirely one root cause.** Every ORM model class trips `Variable "Base" is not
valid as a type` / `Invalid base class "Base"` — a well-known mypy limitation
against SQLAlchemy's dynamically-created `declarative_base()` return value,
solved by SQLAlchemy's own mypy plugin, not present in `pyproject.toml`:

```toml
[tool.mypy]
plugins = ["sqlalchemy.ext.mypy.plugin"]
```

Adding this one line plausibly clears most of `database.py`'s 108 and likely a
share of the `union-attr`/`attr-defined` counts elsewhere that stem from ORM
column types mypy currently can't resolve. Not verified against the full 1003 —
worth re-running the count after adding the plugin before assuming what's left.

**The `union-attr` 132 are the category worth real attention, not just
configuration.** Unlike the SQLAlchemy noise, "attribute access on `X | None`"
is exactly the shape of bug this project's own incident history keeps finding
(see `docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md`'s and this session's own
`except Exception: pass` sweep — both found real silent-failure bugs by reading
past a plausible-looking type signature). `workflow_execution_routes.py` alone
has over a dozen: `PhaseManager | None`, `DatabaseManager | None`,
`QueueService | None` accessed without a guard in more than one route handler.
Whether each is a real live-bug risk (the attribute is accessed on a path where
the value is provably never `None`) or a genuine gap needs per-site judgment —
same discipline as the except-block sweep, not a mechanical fix.

**Not attempted here** — this is a survey, not a plan. 1003 errors is too large
to triage in one pass; flagging the shape (one cheap config fix + one real
bug-risk category + a long tail of annotation noise) rather than the whole list.

---

## 2. TODO cluster: `auth_service.py`'s audit log never got IP/user-agent/roles wired up

```
src/auth/auth_service.py:278,279,291,292,318,319  ip_address="", user_agent="" (×6)
src/auth/auth_service.py:302,386                  roles=[] (×2)
```

Eight `# TODO` markers (corrected count, re-verified 2026-08-22 — the
original pass under-counted this by 2), all in the same file, all the same
shape: `AuthService`'s
login/session functions construct audit-log entries with `ip_address`/
`user_agent` hardcoded to empty strings and `roles` hardcoded to an empty list,
rather than reading them from the actual request/user. If these audit log rows
are ever relied on for a real security investigation or compliance need, they
currently can't answer "from where" or "as what role" — the columns exist and
get written, just always empty. Scoped, single-file, plausibly a half-day fix
(thread the request object through to where these are constructed) — but not
attempted here since it touches auth request-handling, worth a deliberate look
rather than a drive-by.

Two smaller ones, lower urgency: `auth_api.py:144` (no token blacklisting/session
termination on logout — a token stays valid until its own expiry even after
explicit logout) and `mcp/tickets_api.py:1023`/`services/ticket_service.py:1299-1300`
(pagination and comment/commit counts hardcoded to placeholder values in ticket
stats — cosmetic, not correctness-affecting).

---

## 3. Frontend: two god-components, tracked since the original SOLID review, still open

Both were found in `docs/SOLID_OO_REVIEW.md`'s original pass, marked
"partially fixed" in a later session (dark-mode/hook extraction landed; the
size itself didn't), and are unchanged as of this survey:

- **`TaskDetailModal.tsx` — 1305 → 1425 lines as of 2026-08-22, and still
  growing.** Grew via 3 commits since this doc was first written (most
  recently `192f389`, dark-mode theme-token work), and there's a further
  131-line uncommitted diff sitting in the working tree right now from an
  active concurrent session doing more dark-mode work on this same file —
  re-check the line count before acting on it, it's a moving target. Still
  has `window.confirm`/`alert` calls (native browser dialogs, not this
  app's own modal system — visually inconsistent, and the original
  finding's own note says this "deliberately not fixed (real UX change,
  unverifiable without a browser)". **That blocker is gone now** —
  `TESTING.md`'s Playwright recipe (added this session, §5) makes this
  verifiable in a real browser without installing anything new.
  JSX-splitting into smaller components also never happened.
- **`DesignQueuePanel.tsx` — 1266 lines.** Not previously flagged for size on
  its own (the original finding was about per-row polling and duplicated
  status-config maps, both since fixed) — but it's the second-largest
  frontend file in the repo and hasn't had a structural look since.

Neither reviewed in depth for this survey (no `ast`-equivalent triage tool run
against `.tsx` — this entry is a pointer, not a finding with the same rigor as
§1/§2).

---

## 4. Residual items from earlier passes, still technically open

Cross-referencing rather than re-finding — these were already documented
elsewhere and never closed, so a reader of *this* doc should know they're not
duplicates of anything above:

- **`phase_transitions.py`'s two deferred god-functions** —
  `_retry_failed_tasks` (316 lines) and `_maybe_retry_failed_tasks` (270 lines).
  See `docs/GOD_FUNCTION_DECOMPOSITION_CANDIDATES.md`'s "Status" section for why
  they were deferred (they call each other; the file was being actively edited
  by a concurrent session when the other 3 passes landed).
- **~145 pass-only `except` blocks**, classified in bulk as legitimate
  (idempotent guards, expected-outcome passes) by
  `docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md` §6/gap-review, with 16 individual
  sites found genuinely silent and fixed. The remaining ~145 were classified by
  *pattern*, not each individually re-derived from first principles — a
  different reviewer doing a fully independent per-site pass could plausibly
  find one or two more the pattern-matching missed, the same way the gap-review
  pass found `worktree_manager.py`'s stash-pop failure after the bulk sweep
  called it done.
- **`sdk/config.py`/`simple_config.py` env-var name mismatch** —
  `MAX_HEALTH_FAILURES`/`TASK_DEDUPLICATION_ENABLED`/`PROJECT_ROOT` exported by
  the SDK don't match `MAX_HEALTH_CHECK_FAILURES`/`TASK_DEDUP_ENABLED`/
  `PROJECT_PATH` read by `simple_config.py` — confirmed still mismatched as of
  this survey. Tracked deliberately (per-setting owner decision, not an
  oversight — see `docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md` §2 core-infra
  findings), not a new discovery, just re-confirmed still-open.

---

---

## 5. Follow-up sweep, 2026-08-24 — four more findings

Different angles than §1-4: deprecated API usage, a duplicated construction
pattern with a known fix already proven in one corner of the codebase, N+1
query candidates found the same way `ticket_service.py`'s were (§2, now
fixed), and untested high-blast-radius modules.

### 5a. `datetime.utcnow()`: 182 call sites across 58 files, deprecated since Python 3.12

Every test run this whole session has printed this warning dozens of times;
never previously counted or written down. This one is genuinely tricky, not
a find-and-replace: `CLAUDE.md`'s own critical invariant mandates
`datetime.utcnow()` specifically over `datetime.now()`, root-caused to a real
incident (a staleness check comparing a `utcnow()`-stamped value against a
`now()`-based cutoff, silently never firing). The correct migration is
`datetime.now(timezone.utc)`, not a blind revert to `datetime.now()` — but
that produces a **timezone-aware** datetime, and this codebase's DB columns
and comparisons are written assuming naive UTC throughout. Swapping one call
site at a time risks a naive/aware comparison `TypeError` at the boundary
between migrated and un-migrated code. Not attempted here — flagging the
scale (58 files) and the specific hazard (naive vs. aware, not just a
deprecation warning) rather than a mechanical fix.

### 5b. `DatabaseManager(None)` constructed independently at 14 more sites — the fix pattern already exists, just not generalized

`src/auth/auth_db.py`'s own docstring documents this exact problem and its
own fix: "each file independently constructed its own `DatabaseManager(None)`
... fragile in exactly the way `SOLID_OO_REVIEW_UPDATE_2026-08-19.md`
found" — and solves it with one shared `get_db_manager()` accessor, but only
for `src/auth/`'s own two call sites. The same raw `DatabaseManager(None)`
construction still exists at 14 other sites (verified by direct grep, not
estimated):
`prompts/assembler.py` (×2), `autopilot/spec.py` (×4),
`orchestrator/agent_registration.py`, `orchestrator/phase_transitions.py`
(×3), `orchestrator/pipeline.py`, `orchestrator/arbitration.py` (×3),
`services/task_blocking_service.py`. **Not a performance bug** —
`DatabaseManager.__init__` caches engines by resolved path
(`_engines`/`_sessions` class dicts), so repeated construction is cheap, not
N duplicate connection pools. It's the same "N-th independent
implementation of a primitive" shape this whole refactor effort has spent
weeks closing elsewhere (task-creation-claim, agent-termination, pause-state
— see `AUTOPILOT_REFACTOR_PLAN.md` §4), just never extended past `src/auth/`
to the rest of the codebase.

### 5c. Two real N+1 query candidates, same shape as `ticket_service.py`'s (now fixed in `1e72c4ef`)

- `src/services/agent_communication.py:58-59` — fetches a workflow's
  child agents in one bulk query, then queries `Task` individually per
  agent in a loop (`session.query(Task).filter_by(id=child.current_task_id)`)
  instead of one `Task.id.in_(...)` bulk query. A workflow with many
  concurrent agents pays one query per agent instead of one query total.
- `src/services/ticket_service.py:273-275` — a *different* site than the one
  already fixed today: iterating `blocked_by_ticket_ids` and querying
  `Ticket` once per ID. Lower urgency (blocking-ticket lists are typically
  small, single digits), but the exact same fixable shape.

Not fixed here — flagged as two more instances of a pattern this session
already has a proven fix template for (§2's bulk `group_by` rewrite).

### 5d. Two high-blast-radius modules with zero dedicated test coverage

- **`src/prompts/assembler.py` (596 lines) — genuinely zero test coverage,
  not even indirect.** No test file imports it, references
  `assemble_prompt`, or exercises it through an integration path. This is
  the module that builds the actual prompt text sent to every dispatched
  agent — arguably the single highest-blast-radius untested file in the
  repo, since a bug here doesn't crash anything, it just silently changes
  or corrupts what every agent is told to do.
- **`src/mcp/frontend/dashboard_service.py` (1418 lines) — one loose test
  reference, no dedicated test file.** Backs the main dashboard's data;
  `get_results` (233 lines) and `get_graph_data` (199 lines) — both
  previously checked in §"Not flagged" below and found not to be
  god-functions — have no direct test coverage either.

Neither reviewed for *what* to test (that needs domain understanding of
each function's contract) — this is a coverage-gap finding, not a
test-writing plan.

---

## Not flagged

Checked and found reasonably sized, not god-functions/files by the same bar
used in `GOD_FUNCTION_DECOMPOSITION_CANDIDATES.md`: `phases/phase_manager.py`
(2721 lines, but 45 methods with no single dominant outlier — largest is
`start_execution` at 242 lines, 9% of the file), `services/ticket_service.py`,
`mcp/tickets_api.py`, `mcp/frontend/dashboard_service.py`, `core/worktree_manager.py`,
`sdk/client.py`, `interfaces/cli_interface.py` — all similarly "many
reasonably-sized methods," not one-function-doing-everything.
