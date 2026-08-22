# Tech Debt Survey — 2026-08-22

A fresh sweep beyond `docs/GOD_FUNCTION_DECOMPOSITION_CANDIDATES.md` (function-level
size) and `docs/SOLID_OO_REVIEW_UPDATE_2026-08-21.md` (file-level size, except-block
silence). This one covers what those two didn't: type-checking debt, TODO markers,
frontend god-components, and residual items from earlier passes that were tracked
but never closed. Evidence-based — every number below came from actually running
the tool, not estimating.

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
src/auth/auth_service.py:278-319  ip_address="", user_agent="" (×4)
src/auth/auth_service.py:302,386  roles=[] (×2)
```

Six `# TODO` markers, all in the same file, all the same shape: `AuthService`'s
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

- **`TaskDetailModal.tsx` — 1305 lines.** Still has 3 `window.confirm`/`alert`
  calls (native browser dialogs, not this app's own modal system — visually
  inconsistent, and the original finding's own note says this "deliberately
  not fixed (real UX change, unverifiable without a browser)". **That
  blocker is gone now** — `TESTING.md`'s Playwright recipe (added this
  session, §5) makes this verifiable in a real browser without installing
  anything new. JSX-splitting into smaller components also never happened.
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

## Not flagged

Checked and found reasonably sized, not god-functions/files by the same bar
used in `GOD_FUNCTION_DECOMPOSITION_CANDIDATES.md`: `phases/phase_manager.py`
(2721 lines, but 45 methods with no single dominant outlier — largest is
`start_execution` at 242 lines, 9% of the file), `services/ticket_service.py`,
`mcp/tickets_api.py`, `mcp/frontend/dashboard_service.py`, `core/worktree_manager.py`,
`sdk/client.py`, `interfaces/cli_interface.py` — all similarly "many
reasonably-sized methods," not one-function-doing-everything.
