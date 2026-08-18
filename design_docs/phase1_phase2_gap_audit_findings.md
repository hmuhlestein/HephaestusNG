# Phase 1 / Phase 2 gap audit — findings

Audit of every Phase 1 and Phase 2 claim in `docs/AUTOPILOT_REFACTOR_PLAN.md`
against the shipped code, 2026-08-18. Every claim below was checked by reading
the current code or running the test, not by trusting a commit message or a
prior findings doc. Items marked FIXED were fixed in this pass; items marked
OPEN are left deliberately, with the reason.

## Method

- Read §3 (Phase 1) and §4.1–§4.11 (Phase 2) plus §9's sequencing block.
- Cross-checked each "Done" claim against the named symbols and call sites.
- Ran the plan's own gate tests rather than assuming they were green.
- Bisected each failing test against the commit its item claims to have shipped.

## The headline finding

`design_docs/phase2_pause_state_findings.md` reports its verification run as
"802 passed, 5 failed — all 5 confirmed pre-existing and unrelated via
`git stash` against unmodified `main`." That conclusion is wrong for four of
the five. `git stash` against `main` only establishes "not caused by the
*uncommitted* diff" — it cannot distinguish a pre-existing failure from one
introduced by an *earlier commit on main*, which is what these were. Bisecting
against the commit each item claims to have shipped is what separates them.

**Process lesson: "fails on main too" is not the same as "pre-existing."**
When a red test is one the plan itself names as a gate for an item, bisect it
against that item's own commit before classifying it.

## FIXED

### 1. §4.2 regression — the termination primitive can silently no-op (high)

`tests/test_monitor.py::TestAutoRestartResetsTask::test_resets_task_before_terminating_agent`
passes at `025d1e7~1`, fails at `025d1e7`. Root cause is structural, not a test
artifact: `engine_client.terminate_agent` wrapped its whole body in
`except Exception: logger.debug(...); return False`. The one primitive whose
purpose is making the three-field invariant unmissable failed silently, at
DEBUG level, and no caller checks its return value — so the agent row stays
`working` while the caller believes termination happened. This is exactly the
anti-pattern CLAUDE.md's design heuristics forbid ("log and raise rather than
hiding them — silent failures prevent debugging").

Fixed: `terminate_agent`, `pause_workflow`, and `resume_workflow` now log at
`logger.error(..., exc_info=True)`. The test's session mock was also missing
`.all()` support for the primitive's stray-task sweep, which is what surfaced
the swallow; completed so the test exercises the real path.

**Resolved on the second pass, once §4.2's migration made it load-bearing:**
the primitives no longer swallow when the caller supplies a session. A
caller-owned session means the caller owns the transaction, so returning
`False` into a transaction the caller then commits leaves the invariant
half-applied with nothing raised anywhere. Only the standalone path — which
owns its own transaction and can cleanly abandon it — still degrades to a
logged `False`. This had to land *before* the call-site migration: routing
twelve inline writes that previously propagated their own DB errors through a
primitive that caught everything would have introduced a silent-failure mode
rather than removing one.

### 2. §4.8 — pause cascade silently downgrades terminal features (high)

`pause_workflow`'s cascade set `feature.status = "paused"` on *every* linked
`Feature`, with no status guard. This is not recoverable:
`derive_feature_status` returns early on `PAUSED` (`status_derivation.py:56`) —
the one status it never re-derives — so nothing repairs the row, and
`resume_workflow`'s mirror cascade sends every paused feature to `"active"`.
A `completed` feature caught in one pause/resume cycle comes out as live-looking
work. That is `ce0c4a7`'s bug class ("re-paused an already-approved feature"),
re-created inside the primitive built to make it unrepresentable, and reachable
from the highest-traffic callers: `stop_workflow` (the Pause button) and
`queue_routes`' requeue/rerun paths all use the default cascade.

Fixed: the pause cascade narrows to `PENDING`/`ACTIVE`. Regression test
`test_cascade_never_pauses_a_terminal_feature`, parametrized per terminal
status, asserts a terminal feature survives a full pause→resume cycle.

### 3. §4.6 regression — unmocked DB call in `is_design_fully_complete` (medium)

`f5d0305` rewired `is_design_fully_complete` (`queue.py:69-72`) to open a real
`get_db()` session and call `derive_workflow_status`, inside a function that was
otherwise pure SDK/HTTP. Its three tests mock only the SDK layer, so they hit an
uninitialised sqlite (`no such table: workflows`). Fixed in the tests.

Worth noting for whoever touches this next, not fixed here: the call is
unguarded, and it introduces a direct-DB dependency into a function whose other
reads all go through the HTTP client.

### 4. Phase 0 route guardrail was permanently red (medium)

`test_all_63_routes_survived_the_split` asserted a bare count of 63; the live
count is 66. Diffing the route set against `105589c` confirms **nothing was
dropped** — three multi-project routes were added (`GET /projects/active`,
`POST /projects/{id}/activate`, `POST /projects/{id}/deactivate`).

A permanently-red guardrail guards nothing: the next reader sees a known
failure and moves on, which is precisely when a real drop slips through.
Fixed by pinning the pre-split 63-route `(method, path)` set and asserting no
*drop*, so the test stays green as the surface grows and still catches the
failure mode it exists for. Renamed to `test_no_pre_split_route_was_dropped`.

### 5. §4.7 goal (a) was never achieved (medium)

The plan claims `src/mcp/server.py` "wires it to the same fastembed instance
already shared by `TaskSimilarityService`/`TicketSearchService`." It did not:
`RAGSystem` was constructed with
`embedding_provider=getattr(self, 'embedding_service', None)` *before*
`self.embedding_service` was assigned — it is initialised to `None` in
`__init__` and only set ~20 lines later, inside the `task_dedup_enabled` branch.
The argument was always `None`.

Goal (b), the dimension fix, worked anyway because `RAGSystem` falls back to
`create_embedding_provider()` — which is why nothing surfaced — but a third
copy of the model loaded on every startup. Fixed by moving the `RAGSystem`
construction below the embedding block and passing `self.embedding_service`
directly. The `None` fallback is retained deliberately: RAG must not be
disabled by the task-dedup toggle.

### 6. Live `utc-only` invariant violation in `orphan_reaper.py` (medium)

Found while reading `auto_restart.py`, not part of any Phase 2 item.
`orphan_reaper.py:112` compared `agent.last_activity` — stamped with
`datetime.utcnow()` at all eight of its write sites — against a
`datetime.now()` cutoff. West of UTC the difference is negative, so the
`< 30` "agent reported in recently, don't reap" guard matched
unconditionally and this reaper path never reaped. This is the exact bug shape
CLAUDE.md's `utc-only` invariant documents from a prior incident. Fixed to
`datetime.utcnow()`.

`orphan_reaper.py:132`'s `datetime.now()` is left alone: it is only ever
compared against `self.last_check_time`, stamped the same way in the same
process, so it is self-consistent.

### 7. Stale "DO NOT FIX here" comment in `auto_restart.py` (low)

The module docstring still declared a termination-invariant violation
("never sets `terminated_at` … Logged for Phase 3; DO NOT FIX here") that
§4.2 closed — the module now calls the shared primitive. A future reader would
either re-fix a non-bug or trust a stale warning. Corrected.

### 8. Plan-doc status was stale for §4.1, §4.2, §9 (high risk of duplicated work)

§4.1 and §4.2 shipped as `4d6010f` and `025d1e7`, but the plan still carried
"**Correction, verified 2026-08-16: none of this work happened in Phase 1**"
with no Done marker, and §9's sequencing block still described §4.1 as fully
open. Anyone picking up the plan would have redone both. Corrected, with the
§4.2 note recording that the item is only *partially* done (see OPEN #2).

Also corrected in the plan:
- §3.3's exit criterion "the import sweep returns nothing outside the new
  packages themselves" is unmeetable and was never true — the package
  `__init__` is now the legitimate import surface (161 such imports). The
  measurable criterion is zero references to the *removed* modules
  (`src.mcp.autopilot_api`, `src.mcp.api`), both verified clean.
- §4.10 names `mcp/mcp_client.py` as one of four tool-name surfaces; that file
  does not exist. Three surfaces, not four.

### 9. §4.2 completed — the invariant now has exactly one writer (high)

The item shipped as `025d1e7` with the primitive built but only ~4 call sites
routing through it. Twelve raw sites still hand-rolled the three fields behind
a copied `# Invariant: all three fields together` comment — precisely the
outcome §4.2's own Verification paragraph warned would amount to "an 8th
independent fix of the same recurring bug" rather than a structural guarantee.

All twelve are now migrated: `launch_pipeline.py` ×3, `server.py` ×3,
`feature_routes.py`, `queue_routes.py` ×2, `project_routes.py`,
`orchestrator/__init__.py`, plus a twelfth the original sweep missed because it
lives inside the primitive's own module (`engine_client.pause_project_workflows`).
`Terminator.terminate_agent` — the `kill_tmux=True` full-teardown path — now
delegates its DB half to the same primitive, with cost collection kept ahead of
it since `collect_task_cost` needs `current_task_id` before the invariant
clears it. `agent.status = "terminated"` is written in exactly one place in
`src/`.

Two migrations fixed real dangling state as a side effect:
`feature_routes.py`'s `pause_feature` left `assigned_agent_id` pointing at the
agent it had just terminated, and `launch_pipeline.py`'s fallback-retry path
left the task in whatever state the dead agent had it in rather than releasing
it for the retry to claim.

The no-swallow change earned its keep immediately: it turned three
previously-hidden test-mock inadequacies into visible failures. Four tests in
`test_monitor.py` (`TestDetectCreditExhausted` ×2, `TestSessionLimitPause` ×2)
were passing only because `pause_workflow` caught a `TypeError` from their
incomplete session mocks -- an unconfigured `Mock`'s `.all()` is not iterable --
logged it at DEBUG, returned `False`, and let the caller continue as if the
pause had succeeded. The tests then asserted on a workflow object the mock had
mutated by other means. Fixed by configuring the `Feature` branch of both
session-mock helpers. No production impact (a real session returns a list), but
it is a clean demonstration of what the swallow was concealing.

Guarded by `tests/test_termination_invariant_single_writer.py`: an AST sweep
asserting no raw `.status = "terminated"` assignment exists outside the
primitive, plus a second test that fails if the allowlisted writer ever
disappears (so the guard cannot silently become vacuous). Verified non-vacuous
by running the detector against the pre-migration file — it reports exactly the
lines that were migrated.

## OPEN — deliberately not fixed

### 1. Should the state primitives raise instead of returning `False`?

`terminate_agent`/`pause_workflow`/`resume_workflow` now log at ERROR, but they
still swallow. Raising is the CLAUDE.md-correct answer and would make §4.2's
"structurally impossible to violate" claim true, but several callers are sweep
loops (`policy.py:307`, `orchestrator/__init__.py:818`/`1267`/`1291`) that would
abort mid-iteration. Needs its own scoped change.


### 2. `resume_workflow` never resets `paused_retry_count`

The only site that manages that counter is `_retry_exhausted_paused_workflows`,
deliberately left unmigrated per `phase2_pause_state_findings.md`. A workflow
resumed through the primitive keeps a stale count, so on its next `system`
pause it can trip `paused_retry_count >= max_cycles`
(`phase_transitions.py:451`) immediately and go permanently `system-exhausted`
without a single retry. Whether a manual resume should reset the counter is a
product decision, so it is flagged rather than assumed.

Related, same function: it writes a fifth `paused_by` value,
`"system-exhausted"` (`phase_transitions.py:453`), outside the primitive and
without refreshing `paused_at`; and `pause_workflow`'s `reason` is documented as
a `Literal` but never validated.

### 3. §4.10 has a live instance right now

`config/prompts/system_prompts.yaml:203` instructs every non-phase agent to call
`heph_submit_result(...)`. That is not in `_MCP_TOOLS` — it exists only as a
REST route (`src/mcp/memory_api.py:670`). `/tools/execute` strips the `heph_`
prefix, misses the dispatch dict, does not match `devtools_*`, and returns
HTTP 400 "Unknown tool: submit_result". `heph_submit_result_validation`
(`src/validation/validator_agent.py:140`) has the same shape.

The two *code* surfaces are currently in sync (11 tools, identical name sets) —
the prompt/YAML surface is the one that drifted. Not fixed because the choice
between "expose these two as real MCP tools" and "correct the prompts" is a
product call §4.10 should make.

### 4. `FeatureStatus.VALIDATED` is unwritable

`FeatureStatus.VALIDATED = "validated"` is declared and is in
`FeatureStatus.ALL`, and `status_derivation.py:298` branches on it — but the
`features` table's CHECK constraint (`database.py:1121`) allows only
`pending/active/completed/failed/skipped/paused`. Any write raises
`IntegrityError`; the derivation branch is unreachable. Found by a parametrized
test attempting to create such a row. Pre-existing and outside Phase 1/2 scope;
the fix is either widening the constraint (a migration) or dropping the member,
which is a judgement call.

## Verified accurate — no action

- Both flat god-files are gone; all Phase 1 and Phase 1b packages exist.
- Bare `DatabaseManager()` is at **0** in `src/`. Two remain outside it:
  `tests/test_monitoring_live.py:26` and `scripts/migrate_add_worktrees.py:18`.
- §4.1's consolidation is real and correctly wired — the sweep-vs-inline design
  choice was resolved explicitly via `repair_status=`, not defaulted.
- §4.6's `status_derivation` wiring landed at every named site, including
  `review_feature`, `is_design_fully_complete`, and `_workflow_appears_abandoned`.
- §4.3's `check_phase_sibling_active` reaches both `launch_pipeline` and
  `validator_agent`.
- `src.mcp.server` imports clean; ruff clean on every production file touched here.
