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

### 10. §4.11 audit — requeue_design was unscoped across projects (high)

§4.11 is an audit item: confirm every bulk state-changing query on
Agent/Workflow/Task carries an explicit project/design/workflow scope. Result
of that sweep:

**Clean.** `pause_project_workflows` (533de2a) scopes on `project_id`;
`rerun_design` (9cb947c) resolves project -> design -> `design_wf_ids` and
scopes every mutation to it, and my §4.2 migration preserved that;
`termination_handler`'s two sites scope on `workflow_id`; `get_active_workflows`
routes through `_workflow_belongs_to_project` and all three of its call sites
pass both `project_path` and `project_id`. No unscoped bulk `.update()` on
agent/workflow/task state exists. The `concurrent-active-projects` invariant
also holds: both `is_active` write sites check `max_concurrent_projects` and
neither clears other projects' flags.

**One real gap: `requeue_design` (`queue_routes.py`).** It terminates agents
and pauses workflows -- the same class of mutation as its sibling
`rerun_design` -- but selected them with **no project filter at all**, and
matched by `filename in str(launch_params["design_document"])`, a substring
test on a path. `req_project_id` was available in the handler and used for the
queue-order file, just never applied to the workflow query.

Two independent false-match paths, both live:

- Cross-project: requeuing `design.md` in project A paused project B's
  workflow for its own `design.md`. Design filenames repeat across projects
  constantly. This is exactly the incident 9cb947c was root-caused from --
  a healthy, unrelated agent killed mid-work by another design's queue action.
- Superset names: requeuing `api.md` also matched `legacy-api.md`. 533de2a hit
  the same class of false match with a prefix compare on project directories,
  and its fix note explicitly warns against exactly this.

`rerun_design` was fixed by 9cb947c; `requeue_design` never got the same
treatment, which is precisely the "N-th independent implementation" shape this
whole phase exists to close.

Fixed: the workflow query now filters on `Workflow.project_id` when a
project_id is supplied, and matches `Path(design_doc).name == filename`.
Regression tests in `tests/test_queue_requeue_scoping.py` -- this endpoint had
no request-level coverage before. Verified non-vacuous: both tests fail
against pre-fix code (2 workflows paused instead of 1) and pass after.

### 11. §4.5 shipped only its first half — blocking calls in async routes (high)

§4.5's Current-state paragraph named two defects in `AgentCommunicationService`:
the escaping-strategy divergence, and "not async-wrapped -- reached from
`async def` routes in `agents_api.py` with no executor offload." Commit
`35f1e2c` fixed the first and left the second, and the plan carries no Done
marker for §4.5 at all, so nothing recorded the split.

Three `async def` routes in `agents_api.py` called synchronous methods inline:
`get_children` (blocking DB), `get_children_status_summary` (blocking DB plus
per-child tmux inspection), and `get_child_logs` -- which shells out to
`tmux capture-pane` over up to 2000 lines of scrollback. Each stalls the whole
event loop for its duration. This is a named entry in CLAUDE.md's `<forbidden>`
list ("Do not use synchronous blocking calls in async endpoints without thread
pool"). Fixed by wrapping all three in `asyncio.to_thread`, matching the
convention already used in `embedding_factory.py`.

**The wider sweep is the real finding.** An AST pass over `src/` for blocking
calls (`subprocess.run`, `time.sleep`, `requests.*`) inside `async def` bodies
returns **18 instances across 7 files**. Ranked by how long they hold the loop:

- `queue_routes.rerun_design` -- `for _ in range(10): time.sleep(0.5)` plus a
  further 0.5s after SIGKILL: up to **5.5 seconds** of total server freeze on
  every rerun, blocking dashboard reads, agent check-ins, and task-completion
  callbacks alike. Fixed here (`await asyncio.sleep`), because the same
  function already does exactly that 300 lines further down, with a comment
  explaining why -- so the correct pattern was established in-file and this
  loop simply predates or missed it.
- `agents/terminator.terminate_agent` -- three `subprocess.run` calls with 3-5s
  timeouts plus a `time.sleep(1)`, on the awaited agent-termination path.
- `tickets_api.get_commit_diff_endpoint` -- three inline `git` subprocesses.
- `frontend/_shared`, `feature_routes`, `project_routes`, `server.py` -- shorter
  `subprocess.run` calls (3-5s timeouts) and 0.05-0.15s retry sleeps.

The remaining 15 are left unfixed deliberately: several sit in files another
session is actively editing, and converting termination's SIGINT/SIGKILL
sequence to async is a real change to shutdown semantics rather than a
mechanical swap. Sized as its own item, not a drive-by.

### 12. Regression I introduced in ba202c0, found by full-suite bisection (high)

Running the full suite at `ba202c0` and at its parent, both in worktrees
pinned to those commits, isolates my commit's effect exactly: **80 failed vs
79, one new failure, nothing fixed.**

    FAILED tests/test_resume_interrupted_workflows.py::
      TestResumeInterruptedWorkflowsGitCommitPushRecovery::
      test_marks_done_instead_of_restarting_when_branch_already_merged

The §4.2 migration of `server.py`'s resume/auto-recovery site broke it, and my
own code comment there asserted the opposite ("the task was just marked done
above, so the primitive's stray-task sweep correctly leaves it alone").

**Root cause, and it generalizes.** This project's sessions are built with
`autoflush=False` (`database.py:1395`). `terminate_agent(session=...)`'s
stray-task query therefore reads the *database*, not the caller's uncommitted
in-memory changes. The recovery path sets `task.status = "done"` and then calls
the primitive in the same session; the query still saw `in_progress`, matched,
and reset the task to `"pending"` -- clobbering the very completion that code
path exists to record. Fixed with a `session.flush()` before the call, which
preserves the prior semantics exactly (task stays `done`, `assigned_agent_id`
retained for attribution).

This is a trap for every future caller, so it is now documented in
`terminate_agent`'s docstring: with a caller-supplied session, either flush
first or set the task's terminal state *after* the primitive returns.

I re-audited the other eleven migrated sites against this hazard. All are safe,
for one of two reasons: they call the primitive *before* writing task state
(`launch_pipeline` ×3, `feature_routes`, `queue_routes` requeue, `server.py`
stop/cancel, `pause_project_workflows`), or they write only values identical to
what the primitive would set (`queue_routes` rerun, which pre-resets tasks to
pending with a null agent). `orchestrator/__init__.py` and `terminator.py`
touch no task at all.

**Process note.** The targeted runs (690 passed, then 168 more) did not catch
this: the test lives in `test_resume_interrupted_workflows.py`, which is not
one of the files I touched and does not obviously exercise a "migrated path" by
name. Only the full-suite bisection found it. For a change that touches twelve
call sites across six files, blast-radius-scoped testing was the wrong
instrument, and I reported green on it before running the full suite.

### 13. §4.4 verified; but Phase 4's deletion list is wrong about one target

§4.4's consolidation is real: `merge_shared_branch` exists
(`worktree_manager.py:925`) and `cleanup_all_stale_branches`'s inline
`_merge_and_delete` now calls it (`:1134`) rather than carrying its own merge
strategy. Abort-and-preserve is the single strategy. That claim holds.

What does not hold is the disposal note §4.4 hands to Phase 4: "delete the
other two, including the now-fully-superseded `WorktreeManager.merge_to_main`/
`cleanup_worktree` if confirmed unreachable after this consolidation." Checked:

- `merge_to_main` -- reachable only through `merge_to_parent`, which has **zero
  production callers** (5 references, all in `tests/test_worktree_integration.py`).
  Genuinely dead in production; deletable, but the tests go with it.
- `cleanup_worktree` -- **live, not superseded.** Reached via `discard_agent`
  (`worktree_manager.py:923`), which `launch_pipeline.py:1851` calls on the
  agent-creation failure path. Deleting it per the Phase 4 note would break
  failed-agent cleanup: the "failed work never touches main" path.
- `_archive_and_cleanup` -- zero production callers, so "confirmed dead code"
  is right, but it has live tests (`test_orchestrator_helpers.py:2917/2942`)
  that must be removed in the same commit or the deletion goes red.

Phase 4 should re-derive reachability per target at deletion time rather than
trusting this list. Two of its three entries are wrong as written.

### 14. Correction to §4.3's findings: the cooldown was not lost in the split

`design_docs/phase2_dispatch_findings.md` (and §4.3's Done note in the plan)
records that the in-memory per-phase dispatch cooldown "was silently dropped
during Phase 1b's `MonitoringLoop` collaborator extraction (`7619936`) -- never
migrated to any of the 5 new collaborators, not deleted deliberately, just lost
in the move," and offers it as the plan's own evidence that decomposition
silently drops behavior.

Measured, not inferred:

    9a2fd48   (added it)     "30 s cooldown" present  -> 1
    7619936~1 (pre-split)    "30 s cooldown" present  -> 0
    7619936   (post-split)                            -> 0

It was already gone *before* the split. `git log -S` identifies the actual
removal: **`a5c8236` "fix: address all gap review items" (2026-06-29)**, which
deleted the cooldown block outright. The decomposition four weeks later moved
code that no longer contained it.

The conclusion §4.3 drew from this is still worth keeping -- a state-based
phase-sibling guard is better than a 30s in-memory timer, and the guard audit
was the right thing to run. But the specific claim, and the "decomposition
loses behavior" lesson attached to it, rests on a misattribution.

**The broader check that claim invited comes back clean.** I ran symbol-level
and instance-state diffs across the four Phase 1b decompositions
(`7619936` MonitoringLoop, `a69d47d` api.py, `e161be4` task_completion_service,
`87a221f` AgentManager), comparing every `def`/`class` name and every
`self.<attr>` assignment before the split against the union across the
resulting modules. **Zero symbols and zero instance attributes were lost.** The
one apparent loss, `fire_spec_gate_if_ready`, is the deliberate relocation to
`phase_transitions.py` that §3.2 called for, verified present there.

So the decompositions were, as far as these two measures reach, genuinely
lossless -- and the one cited counter-example was not caused by them. What the
decompositions *did* break, repeatedly, is the tests that pointed at the old
shape (finding 15).

### 15. §4.6's two unverified bullets, and §4.10, are largely resolved

The plan's §4.6 carries three bullets; only the `status_derivation` one had been
checked. Result of checking the other two:

- **Registries replacing string-keyed if/elif dispatch.** Substantially done,
  partly by work landing right now. Condition evaluation already routes through
  the shared `CONDITION_PATTERN`/`CONDITION_OPERATORS` grammar
  (`workflow_engine/orchestrator.py:26-30`) that §4.6 named as "the template to
  extend, not reinvent" -- nothing to do there. MCP tool dispatch now has
  `MCP_TOOL_REGISTRY` generating both the `/tools` listing (`server.py:4217`)
  and the `_MCP_TOOLS` dispatch dict from one declaration. Phase-action
  handling is down to two `action == "..."` branches in `phase_transitions.py`
  -- too small to justify a registry; converting it would be ceremony.

- **Reconciling the two parallel project-CRUD route surfaces.** Already
  resolved: `projects_api.py` no longer exists, `/api/projects/*` is gone
  entirely, and the only surviving surface is `/api/autopilot/projects/*`.
  `frontend/src/services/api.ts` calls that surface 16 times and the retired
  prefix zero times, so the frontend-coordination risk §4.6 flagged for this
  item ("not backend-internal like the rest of this plan") never materialized.
  The item can be closed rather than scheduled.

**§4.10's live instance is being closed too, by the concurrent session.** The
`heph_submit_result` drift I reported (prompts instruct agents to call a tool
that returns HTTP 400) is fixed by the same `MCP_TOOL_REGISTRY` work: the
registry now carries 14 tools including `submit_result` and
`submit_result_validation`, with `_tool_submit_result` bridging to
`POST /submit_result` and a comment citing `system_prompts.yaml`'s own usage.
That is the right fix of the two options I flagged -- expose the tools rather
than edit the prompts -- and it is theirs, not mine; noted here only so the
finding is not actioned twice.

### 16. Phase 1's "every god-object is now decomposed" is true as scoped, and misleading

§3.3's closing criterion reads: "This closes out Phase 1 in full -- every
god-object this plan named across both `backend_module_decomposition.md` and
Phase 1b is now decomposed." Every *named* target was indeed decomposed. But
the naming, not the decomposing, is where the gap is.

**`src/mcp/server.py` is 5,787 lines -- the largest module in the codebase --
and was never in scope.** It is larger than `src/mcp/api.py` (3,225) and larger
than `src/mcp/autopilot_api.py` (5,724), both of which this plan declared
god-objects worth splitting. Measured across the refactor it did not shrink;
it grew slightly (5,752 -> 5,787). Phase 1 decomposed everything around it.

**The `api.py` split met its literal goal but left a 2,872-line class.**
`a69d47d` turned a 3,225-line module into:

    _shared.py           2,872 lines,  0 routes  (one class: FrontendAPI)
    dashboard_routes.py    177 lines, 17 routes
    phase_routes.py        182 lines, 15 routes
    task_routes.py          59 lines,  6 routes
    agent_routes.py         41 lines,  4 routes

The route clustering §3.2 asked for genuinely happened -- all 42 routes are
distributed correctly, and the thin route modules are the intended shape. What
moved wholesale was the implementation: `FrontendAPI` is now a single
2,872-line class in `_shared.py`. That is larger than `MonitoringLoop`
(~2,050 lines), which this same plan flagged as a god-object requiring
decomposition. By its own threshold, the split produced a new instance of the
thing it was closing.

Neither of these is a bug and neither needs fixing now. The point is narrower:
"every god-object is decomposed" is a claim about a list, and the list was
drawn before `server.py` was the biggest file and before `FrontendAPI` existed
at its current size. A size-based re-derivation at the end of Phase 1 -- rather
than a checklist walk -- would have caught both.

**Actioned:** this is now **Phase 1c**, added to the plan at §3.4 with a risk-
register row, a slot in §9's sequencing graph (after Phase 2, before Phase 4),
and a full execution plan in `design_docs/phase_1c_server_decomposition.md` --
nine-module symbol-to-module mapping written to `backend_module_decomposition.md`'s
standard. Its exit criteria add the size threshold Phase 1 never had (**no module
over ~800 lines**), precisely so a split cannot pass by relocating mass the way
`api.py`'s did.

Two hazards found while mapping the file, both recorded there:

- **A duplicated rate-limit subsystem.** `_rate_limit_store`, `RATE_LIMIT_WINDOW`,
  `RATE_LIMIT_MAX` and `_check_rate_limit` are each defined twice (L1363-1378,
  L3867-3886). The first `_check_rate_limit` has no lock and **zero callers**;
  all three live OAuth call sites follow the second definition and bind the
  thread-safe copy. Correct today by definition order alone -- and splitting the
  file makes thread safety depend on which module a route imports from. Step 0
  of Phase 1c deletes the unlocked copy.
- **Two god-functions inside the god-file:** `create_task` (601 lines) and
  `update_task_status` (423). Each is larger than three of the four modules the
  `api.py` split produced; moving them verbatim reproduces that outcome.

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
