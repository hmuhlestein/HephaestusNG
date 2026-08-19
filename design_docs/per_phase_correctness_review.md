# Per-phase correctness review — Phases 0 → 4, in sequence

Run 2026-08-19 against `c542c22`, after the Phase 1/2 gap audit
(`phase1_phase2_gap_audit_findings.md`) closed its 24 findings. That audit
swept horizontally, by defect class. This one walks the plan **in phase
order** and asks a narrower question of each: *does the phase's own stated
exit condition actually hold in the code as it stands today?*

Baseline for the whole pass: a clean full-suite run on a quiescent tree —
**1 failed, 2548 passed, 54 skipped** (47m). The single failure is an
order-dependent pollution artefact, not a production defect: the file passes
6/6 in isolation. See §Suite at the end.

---

## Phase 0 — safety net: **fully satisfied**

All three deliverables exist, are checked in, and are green (27 tests).

| Item | Status |
|---|---|
| Route-count/path-set guardrails | All three routers pinned |
| Characterization tests | Termination invariant, worktree-removal safety, claim triad, restart-agent |
| Held-out smoke script | `scripts/smoke_run_b.sh`, 400 lines, full 10-phase pipeline |

The autopilot 52-route guardrail the plan calls for is easy to miss when
grepping `tests/*guardrail*` — it lives inside `test_autopilot_api.py`
(`TestRouteSurvival::test_no_pre_split_route_was_dropped`) with a pinned
`PRE_SPLIT_ROUTES` set, not in a dedicated file. It exists.

**One shared design property worth naming, since it is deliberate and not a
gap:** all three route guardrails assert *no route was dropped*, not *the set
matches exactly*. They stay green as the API surface grows and only fire on a
regression. That is the right call (a strict-equality guardrail goes
permanently red the first time someone legitimately adds a route, and a
permanently-red guardrail guards nothing — §4.8 already learned this the hard
way), but it does mean an accidentally *duplicated* registration is caught
only by the server guardrail, which has the extra no-duplicates assertion.

## Phase 1 / 1b / 1c — decomposition: **exit criteria hold**

- All four flat god-files are gone: `orchestrator.py`, `autopilot_api.py`,
  `api.py`, `server.py`.
- Zero stale references to the removed modules across `src/` **and** `tests/`.
- Route guardrails pass unchanged.

**Systematic symbol-drop diff, all seven splits.** This is the check that
found the one genuine silent behaviour drop during the gap audit (finding 20,
`manager.py`), so it was re-run exhaustively: for each split, every
`def`/`class` name in the pre-split file at `<commit>~1` versus the union of
names in the post-split package at HEAD.

| Split | Symbols | Dropped |
|---|---|---|
| `orchestrator.py` → `autopilot/orchestrator/` | 159 | `terminate_agent_direct` — **accounted for**: now an alias of the §4.2 primitive (an assignment, not a `def`, so the AST detector reports it) |
| `autopilot_api.py` → `mcp/autopilot/` | 130 | none |
| `api.py` → `mcp/frontend/` | 50 | none |
| `monitor.py` → 5 collaborators | 34 | none |
| `task_completion_service.py` → `task_completion/` | 12 | `fire_spec_gate_if_ready` — **accounted for**: moved into `phase_transitions.py` exactly as §3.2 directed |
| `manager.py` → agents package | 55 | none |
| `server.py` → `mcp/server/` | 106 | none |

Both apparent drops are intentional relocations, verified by locating the
symbol at its new home and confirming its callers resolve. **No unaccounted
symbol loss anywhere in Phase 1.**

**Doc drift, corrected in the plan:** §3.3 records `manager.py` at 435 lines;
it is 698. Not re-duplication — the five messaging/context methods (~244
lines) restored after the gap audit found them silently dropped.

## Phase 2 — consolidation: **primitives are real; two gaps**

Verified per-subsection that the claimed single primitive is genuinely the
only path, rather than trusting the "Done" markers.

- **§4.1** claim primitive wired at 4 sites, goto-reset at 3. ✅
- **§4.2** `agent.status = "terminated"` written in exactly one place in
  `src/`, guarded by an AST sweep. ✅
- **§4.3** `check_phase_sibling_active` guards both dispatch paths. ✅
- **§4.4** `merge_shared_branch` is the single merge primitive. ✅
- **§4.5** `send_message_to_child` routes through `AgentMessenger`. ✅
- **§4.6** three of four named targets wired; see below.
- **§4.7** `RAGSystem` constructed *after* `embedding_service` is assigned,
  and passed it — the ordering bug the §4.7 correction describes is fixed. ✅
- **§4.11** re-confirmed by running `test_queue_requeue_scoping.py`. ✅

### Fixed during this review

1. **`terminate_agent`'s `kill_tmux` parameter was accepted and silently
   ignored** (§4.2). The plan's stated target was one primitive with a
   `kill_tmux: bool` flag; what shipped was the better two-collaborator design
   (`engine_client` owns the DB invariant, `Terminator` owns tmux teardown),
   but the flag was left on the signature marked "reserved for future use" and
   read by nothing. Any caller passing `kill_tmux=True` would have gotten no
   teardown and no error. No caller ever did — a latent trap, not a live bug.
   **Parameter deleted**, docstring now points at `Terminator`.

2. **`pause_workflow`'s `reason` was documented as a `Literal` but never
   validated** (§4.8's own still-open list). This mattered more than it reads:
   every consumer compares `paused_by` against exact string literals —
   `resume_workflow`'s narrowing on `"system"`,
   `_wait_for_phase0_review_clearance`'s poll for `"review"`, the budget
   sweep's `"budget"` filter — so an unrecognised value raised nowhere and
   silently made *all* of those guards miss at once, leaving a workflow paused
   with no path able to resume it. **Now validated** against a module-level
   `PAUSE_REASONS` frozenset, raising before any write. All 14 live call sites
   audited first: every one passes an allowed value, so nothing can start
   raising in production. Guarded by
   `test_pause_workflow_primitive.py::TestPauseReasonValidation`.

### Reported, not fixed

3. **A fourth copy-family of the §4.1 claim logic, out of that item's scope.**
   Distinct from both the stale-fallback clear and the release, a "reopen a
   phase for a fresh cycle" write — `execution.status = <reopen state>` plus
   `task_creation_claimed_at = None` — is hand-copied at four sites:
   `phase_manager.py:892`, `:1035`, `:1603`, and `task_admin_routes.py:619`.
   Three carry comments explicitly cross-referencing the others, which is the
   N-th-hand-copy signature this whole phase exists to eliminate, and the
   failure mode each describes is silent (forget the reset → the reopened
   phase never creates a task → the pipeline hangs with no error).
   **Not consolidated here** because the four differ materially in what else
   they write, so merging them is a decision about phase-reopen semantics, not
   a mechanical dedup.

4. **§4.6's `run_design_aggregate` is still unwired — and should stay that
   way pending a real decision.** It aggregates an in-memory `feature_results`
   dict from the run it is reporting on; `derive_design_status` queries the DB.
   Same question, different inputs, by design. All four reachable outcomes were
   traced and **the two agree on every one** (all-completed → COMPLETED;
   any-failed → FAILED; partial → COMPLETED; all-skipped → the aggregate writes
   FAILED and `derive_design_status` falls through to `else: derived =
   design.status`, preserving rather than overriding it). A duplication-of-policy
   risk to watch, not a live divergence — mechanically wiring it would change
   what it measures.

5. **`paused_retry_count` remains open**, unchanged from §4.8's own note. Still
   a genuine judgement call (reset on resume risks a pause/resume loop; not
   resetting risks premature `system-exhausted`), so it stays a decision, not a
   fix.

## Phase 3 — live-bug fixes: **hold**

Spot-checked the classes most likely to regress:

- SQLAlchemy column truthiness (`Model.col is None`, `not Model.col`): **zero**
  live instances in `src/`.
- `get_agent_branch_path`'s silent main-repo fallback: returns `None`. ✅
- UTC invariant: every surviving bare `datetime.now()` in `src/` is a
  display/filename `strftime` or an events-log line — none is stored in the DB
  and later compared, which is what the invariant is about. **One exception
  found and fixed**: `state.py:436` wrote `saved_at` into DB-persisted project
  context in local time. It has zero readers today, so it was not a live bug —
  but it is a stored timestamp, the invariant covers it verbatim, and the next
  person to compare it would hit exactly the `prompt_human` failure. One-word
  fix, no consumers to break.

## Phase 4 — dead-code deletion: **not started, and three bullets are wrong**

Phase 4 is the only phase not yet executed. Its entire correctness risk is the
"confirmed zero callers" claim on each bullet — a deletion executed against a
stale claim breaks production. All twelve were re-verified. **Nine hold. Three
do not**, and all three are now corrected in the plan:

| Item | Verdict |
|---|---|
| `_archive_and_cleanup`, `MemoryIngestion`, `_should_steer_agent`, `_sweep_stray_files`, `MAX_WORKFLOW_TIME`, `MAX_PHASE0_TIME` | Genuinely zero references. Safe. |
| `FrontendAPI.get_agents`/`get_agent_output` | Confirmed dead by resolving the live route: `/api/agents` dispatches to `agents_api.list_agents`. Safe. |
| `check_executors` | Zero *production* callers (re-exported in `validation/__init__.py`, used only by 6 tests). Claim holds. |
| **`TrajectoryContext`** | ❌ **Dead state, not a dead symbol.** `monitor.py` still imports it (`:25`) and constructs it (`:208`). Genuinely dead is every *use* — `grep 'trajectory_context\.'` returns nothing. Deleting the module alone **breaks startup**; the import and construction site must go in the same commit. |
| **`api_post`** | ❌ **Has two live callers.** `queue_routes.py:734`/`:769` — the queue repair-agent path, importing it explicitly at `:695`. Deleting it breaks queue repair. Its sibling `api_get` *is* dead and safe. |
| **`EmbeddingService` hint location** | ⚠️ Stale path only: post-Phase-1c the hint is `src/mcp/server/_shared.py:245`, not `server.py:275`. |

The `api_post` bullet is the sharpest of the three: it names the wrong file
(`orchestrator.py`, which no longer exists), and the two functions it lumps
together have opposite verdicts. There is also an unrelated live
`api_get`/`api_post` pair in `src/cli/utils/__init__.py` with many CLI callers,
which a careless grep-and-delete would find first.

## Suite — the last failure, root-caused and fixed

`test_heal_orphaned_agent_branches.py::test_fast_forwards_orphaned_branch_with_no_live_worktree`
was the single failure on a clean tree, and passed 6/6 in isolation. Bisected
to one polluter — `tests/integration/test_task_deduplication_flow.py` —
reproducible in 9 seconds with just those two files.

**It was not really a pollution bug. It was an import-order bug that pollution
exposed, and the failing assertion was the least of it.**

`worktree_integration.py:23` binds the name at import time
(`from src.core.simple_config import get_config`). The victim's `config`
fixture patched only the *definition* site,
`monkeypatch.setattr("src.core.simple_config.get_config", ...)`, which never
reaches that binding. Whether the test worked came down to which import
happened first:

- **Alone:** `worktree_integration` is first imported *inside the test body*,
  after the patch is active, so line 23 binds the lambda. `wi.get_config is
  sc.get_config` → `True`. Green.
- **After anything importing `src.mcp.server`** (which pulls in
  `worktree_integration` at collection): the binding is the real function.
  `wi.get_config is sc.get_config` → `False`, and it returns the memoized
  production `Config` whose `database_path` is the real **`hephaestus.db`**.

So in a full-suite run, `heal_orphaned_agent_branches` was opening the
developer's **real database**, enumerating the **real** `AutopilotProject`
rows, and walking those real project directories attempting
`git merge --ff-only` / `update-ref` on live checkouts. It returned 0 here, so
nothing was actually merged — but the test asserting `healed == 1` was the
only reason anyone noticed, and the assertion is not what makes this serious.

This is the isolation-bypass class from §3.3 with the polarity reversed: there,
production code constructed `DatabaseManager()` wrongly, and
`test_db_test_isolation_guard.py` now catches it structurally. Here production
code is *correct* — `DbManager(str(cfg.database_path))` — and the test failed
to redirect the config lookup it reads. The existing AST guard cannot see this.

**Fixed** by patching where the name is looked up as well as where it is
defined, with the reasoning recorded in the fixture docstring. Verified green
in both orders: 13 passed with the polluter first, 6 passed alone.

### Systemic — now closed with a structural guard

Measured before writing the guard: a plugin wrapping `DatabaseManager.__init__`
and reporting any call resolving to the real `hephaestus.db`, run against the
full worktree/branch test surface (11 files) in both natural and adversarial
import order (dedup-integration test first, forcing `src.mcp.server` to be
imported before any of `worktree_integration.py`'s own test files). **Zero
other sites hit it** — `test_heal_orphaned_agent_branches.py` was the only
exposed one among these files, not a symptom of a wider outbreak.

Fixed the root cause there (patch `get_config` on the module under test, not
only at its definition site — recorded in the fixture's docstring) and added
a permanent, autouse, session-scoped guard in `tests/conftest.py`
(`_forbid_production_database`): it wraps `DatabaseManager.__init__` for the
whole test session and raises `RuntimeError` the instant any test resolves a
path to the real `hephaestus.db`, naming the fix inline. Verified it fires
exactly at the bypass point by reverting the fixture fix and re-running —
6/7 tests in that file failed via the guard's `RuntimeError`, not the
original flaky assertion — then re-verified clean with the fix restored, in
both suite orders (65 passed, 0 hits).

This is the structural half `test_db_test_isolation_guard.py` (§3.3) could
not provide: that guard checks how production code *constructs*
`DatabaseManager` (an AST sweep, static); this one checks what a test
actually *resolves* a config to at runtime, which is only observable by
running it. The two are complementary, not redundant — together they close
both directions of the same isolation contract this plan's §3.3 first found.

**Second site found by the guard itself, on the first full-suite run with it
active.** `test_cleanup_worktree_tmux_archive.py`'s three tests never mocked
`get_config` at all: `_cleanup_worktree` does
`db = DbManager(str(cfg.database_path))` before its archiving step even
runs, so every one of those tests was constructing a `DatabaseManager` against
the real `hephaestus.db`. This one was harmless in practice, not just
undetected — `WorktreeManager` itself is mocked immediately afterward and
nothing ever reads from `db` again — but it is the identical isolation gap.
Unlike the heal-branches bug, `get_config` here is imported *locally inside
the function*, so patching the definition site (not the module-under-test)
is sufficient; fixed with an autouse fixture pointing `database_path` at a
`tmp_path` DB. 3 passed.

This is exactly the outcome the guard is for: it does not just catch the one
known bypass, it surfaces every other silent one the moment it runs against
the real suite. A third full run (this time genuinely clean, or with any
remaining gaps found and fixed the same way) is what closes this out.
