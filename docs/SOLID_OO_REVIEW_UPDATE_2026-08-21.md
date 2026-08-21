# SOLID / OO Review — 2026-08-21 update

Companion to `docs/SOLID_OO_REVIEW_UPDATE_2026-08-19.md` (two days prior). That doc
re-verified the original review's 56 findings plus reported new ones; this pass
re-verifies *everything downstream of it* — the 08-19 update itself, the original
review, `docs/SOLID_REFACTOR_ADVERSARIAL_REVIEW.md`'s 22 findings,
`docs/AUTOPILOT_REFACTOR_PLAN.md`'s phase claims, and
`design_docs/phase2_solid_consolidations_findings.md` — against the code and git
history as they exist right now.

**Method:** six parallel audits, one per subsystem (mirroring the original review's
§1–§5 split, plus a sixth covering plan/process bookkeeping), each independently
re-reading the current code — not trusting any prior doc's own "done" markers — and
classifying every claim as confirmed-fixed, confirmed-still-open, stale, or
partially-fixed, with file:line evidence.

**Headline, before the detail:** the code-level fix rate across all five subsystem
audits is high — the large majority of claims in every prior doc checked out exactly
as described. The real gap this pass found is not in the code but in the *bookkeeping*:
a same-day, undocumented split moved `orchestrator/__init__.py`'s "fixed, now 3086
lines" content into a brand-new 3257-line `pipeline.py` that no doc names, and 16
same-day commits closed a `process_queue` race/arbitration bug family that
`AUTOPILOT_REFACTOR_PLAN.md` never recorded — the exact recurring bug class that plan
exists to prevent. Both are detailed in §1, not buried in the tables.

---

## 1. Gaps found this pass (read these first — not in any prior doc)

**`orchestrator/__init__.py`'s claimed 3086-line "fixed" state was superseded by an
undocumented move, not further shrunk.** The 08-19 update's priority #2 claims
`orchestrator/__init__.py` was cut from 3411 to 3086 lines via several extractions.
It is now **146 lines** — but only because commit `5bf904d` ("refactor: move
orchestrator/__init__.py's pipeline logic into pipeline.py") relocated
`run_single_workflow`/`run_continuous_pipeline`/etc. wholesale into a **new,
undocumented `src/autopilot/orchestrator/pipeline.py` (3257 lines, 25 top-level
functions)** — now the single largest file in the repository, and one no reviewed
doc names or budgets against. `__init__.py` itself is now a pure re-export surface.
The "split the god-modules" priority item is stale on its own claimed end-state: the
work moved, it wasn't finished. **Action: audit and decompose `pipeline.py` under
the same ~800-line criterion the rest of this refactor has held itself to.**

**16 undocumented commits landed today (2026-08-21), closing a `process_queue`
race/arbitration bug family — none appear in `AUTOPILOT_REFACTOR_PLAN.md`.** HEAD
is `d57b14f`; the plan doc was last touched by `b7dd1e2` (2026-08-19) and the 08-19
update doc by `f073403` (earlier today). Between those and HEAD:
`47d95a6` (stop arbitration self-perpetuation / queue-race leaks from the
`process_queue` offload), `0224543` (close a double-dispatch race the offload
introduced), `12e12ee` (stop the dispatch chain blocking the event loop), `7a42d2e`
(harden `claim_next_queued_task`, close remaining unlocked queued-task mutation
sites), `5aa2314` (`requeue_design`'s batch task reset was unlocked for queued
tasks), `963a8fd`/`6129680` (stop/pause pulling queued tasks), `ce975ca`/`e733a9e`
(termination settle-delay/polling), `14b55b2` (stuck-agent false positive on a
legitimate Monitor-tool wait), `d57b14f` (verify a delivered message actually got
queued, retry if not). `grep` for `process_queue`/`claim_next_queued_task` in
`AUTOPILOT_REFACTOR_PLAN.md` returns zero hits. Given the plan's own thesis is that
task-creation-claim races are the most-repeated bug class in this codebase's
history, a fresh same-day recurrence directly in that territory, entirely
undocumented, is worth folding into the plan before it goes stale further.

**Size-budget regression, unverified since last check.** Four route/registry files
have grown past the refactor's own ~800-line-per-module criterion since it was last
verified, all with same-day (2026-08-21) modification timestamps that postdate the
checks that cleared them: `src/mcp/server/_mcp_tool_registry.py` (980 lines, was 828
when last verified), `src/mcp/server/task_admin_routes.py` (933 lines, was 837),
`src/mcp/autopilot/project_routes.py` (1690 lines — down from a much larger
pre-split figure but still four unrelated concerns interleaved: project CRUD,
7 cost-accounting endpoints, design-file browsing, and design reordering all in one
file), `src/mcp/autopilot/feature_routes.py` (1483 lines).

**Process gap: a route-surface retirement shipped without the sign-off its own
spec required.** `design_docs/phase2_solid_consolidations_prompt.md` (sub-problem 3)
explicitly required stopping for human product sign-off before retiring either
`/api/projects/*` or `/api/autopilot/projects/*`. The retirement happened —
`src/mcp/projects_api.py` is deleted, CLI and frontend fully migrated to
`/autopilot/projects/`, confirmed correct and necessary — but
`design_docs/phase2_solid_consolidations_findings.md`'s own account shows no
recorded sign-off step. Worth a retroactive confirmation, not a revert.

**Doc-internal self-contradiction in `SOLID_OO_REVIEW_UPDATE_2026-08-19.md`.** Its
§4 priority list marks 1.18, 2.3, and 3.1 "Done"/"addressed" (2026-08-20), but its
own §2 findings table still lists 3.1 as "PARTIAL (deliberate final boundary)."
The two aren't actually in conflict — §4's "Done" means "this pass's remediation
is complete," not "fully closed" — but a reader skimming only §4 (the doc's own
stated authoritative "what's still open" section) would conclude 3.1 is fully
resolved when §2 says otherwise. Worth a wording fix in that doc, not a code
change.

---

## 2. Per-subsystem results

### §1 — MCP/API layer (`src/mcp/`)

All three structural splits the 08-19 update claimed are confirmed real:
`server.py` (was 6885 lines) no longer exists as a flat file — now a
`src/mcp/server/` package of 14 modules (`_shared.py`, `lifecycle.py`,
`_create_task_steps.py`, `_update_task_status_steps.py`, `agent_task_routes.py`,
`task_admin_routes.py`, `oauth_routes.py`, `workflow_execution_routes.py`,
`mcp_protocol.py`, `_mcp_tool_registry.py`, `background_loops.py`,
`devtools_tools.py`, `connection_broadcaster.py`, `state_bootstrap.py`). `api.py`
is deleted, split into `src/mcp/frontend/` (`DashboardService`, `TaskService`,
`PhaseService`, `AgentService` — `class FrontendAPI` no longer exists anywhere in
`src/`). `autopilot_api.py` is deleted, split into `src/mcp/autopilot/`.

Finding 1.4 (phase-ID digit-vs-UUID resolution) matches the prior doc's own
"partial" self-assessment: `src/core/phase_lookup.py:29`'s `resolve_task_phase()`
is the canonical read-path resolver; only 3 `.isdigit()` sites remain outside it
(`phase_lookup.py:46` itself, `_create_task_steps.py:95/97`,
`task_enrichment_service.py:57`), all write-path, matching the documented
exception.

Finding 1.5 (tool dispatch) — confirmed fixed. `_mcp_tool_registry.py:450/468`
defines `MCPToolSpec`/`MCP_TOOL_REGISTRY`; `mcp_protocol.py:339` dispatches via
dict lookup, falling to `_handle_devtools_tool` only for the separate devtools
namespace. Note `phase2_solid_consolidations_findings.md` still cites
`src/mcp/server.py` as the location for this fix — that file predates the Phase1c
split and no longer exists; a doc-staleness artifact, though the underlying
conclusion holds.

Finding 1.6 (`ServerState`) — still matches "partial": `_shared.py:259` directly
constructs/holds 11 concrete manager attributes. Broadcast fan-out is genuinely
extracted to `ConnectionBroadcaster`. Finding 1.16 (circular-import workaround) is
confirmed fixed — 17 files now call `get_app_state()` instead of importing
`server_state` directly.

Sub-problem 3 (project-CRUD reconciliation, `phase2_solid_consolidations_findings.md`)
— confirmed fixed: `src/mcp/projects_api.py` deleted, zero `/api/projects`
references left anywhere in `src/` or `frontend/src`. (See §1 gap above re: the
missing sign-off step.)

New finding not in any prior doc: `_mcp_tool_registry.py` and `task_admin_routes.py`
both silently exceeded the size budget since last verified (see §1 gaps above).

### §2 — Orchestrator/pipeline (`src/autopilot/orchestrator/`, `src/phases/`, `src/workflow_engine/`)

Every specific *behavioral* claim across all five prior docs checked out — this was
the most accurate section of the whole audit. Confirmed fixed, with file:line
evidence: the two disconnected phase-retry-budget mechanisms (`phase_transitions.py`'s
`_get_phase_max_retries` at line 2204 now reads the same `eval_point.max_retries`
`WorkflowOrchestrator.evaluate` uses, and `_create_phase_task` at line 2442 calls
it instead of a hardcoded `max_phase_attempts = 5`); `_sync_stale_feature_statuses`/
`_sync_stale_design_statuses` (`features.py:251`/`293`) now route through
`derive_feature_status`/`derive_design_status`; `derive_workflow_status` wiring into
`policy.py:93` and `queue.py:72`; adversarial-review findings #1, #7, #8/#19, and
#21 all confirmed fixed with explicit `# FIX #N` comments at the cited sites;
`AUTOPILOT_REFACTOR_PLAN.md` §4.3/§4.8 (dispatch reconciliation, pause-state
primitive) both confirmed fixed.

Only the *bookkeeping* claim (line counts / where the god-module ended up) was
stale — see the `pipeline.py` finding in §1 above, and note `_advance_phases`
itself is genuinely down to ~159 lines, distinct from the 498/435-line figures the
08-19 update cites for `run_single_workflow`/`run_continuous_pipeline` (easy to
conflate across docs — they're different functions).

### §3 — Agents/monitoring/services (`src/agents/`, `src/monitoring/`, `src/services/`)

God-class claims: `src/agents/manager.py` is 762 lines (48 methods),
`src/monitoring/monitor.py` is 782 lines (56 methods) — matching the 08-19 update's
705/778 re-measurements (not the original review's 2173/2455 figures), both now
thin delegator facades over named collaborators. Guardian's steering-key bug
(`guardian_dispatch.py:417`) confirmed still fixed. All 22 adversarial-review
findings (#2–#22) independently re-verified in code, not just re-read from the
doc — every one confirmed fixed, including the three that postdate the 08-19
update itself (`MechanicalRecoveryDetector`'s dynamic-`getattr` import →
static import via new `src/monitoring/patterns.py`; the two unrelated
`restart_agent` methods → renamed to `requeue_and_terminate`). The termination
invariant (every `status="terminated"` write also clears `current_task_id` and
sets `terminated_at`) is enforced by an AST-sweep test,
`tests/test_termination_invariant_single_writer.py`.

One item from the 08-19 update confirmed still open: the shared `_find_tmux_session`
helper remains unused at its originally-cited call sites across `messenger.py`,
`terminator.py`, `launch_pipeline.py` (5 sites), `orphan_reaper.py`,
`mechanical_recovery.py`.

New finding not in any prior doc: `_find_tmux_session` is independently *defined*
twice — `src/agents/manager.py:440` and `src/agents/output_capture.py:544` — not
just unused elsewhere as the existing finding frames it. Worth confirming the two
implementations agree, then consolidating one to call the other.

### §4 — Core infrastructure (`src/core/`, `src/interfaces/`, `src/auth/`)

`auth_api.py`'s `/me` 501 stub — confirmed fixed: `auth_api.py:147-172` now
depends on `auth_middleware.get_current_user`, which verifies the JWT and loads
the `User` row via `session_scope()`. The worktree conflict-resolution config field
— confirmed fixed by deletion, not implementation: `conflict_resolution_strategy`
no longer exists anywhere in `simple_config.py`/the YAML;
`worktree_manager.py:492,527-534` hardcodes `newest_file_wins` with a comment
documenting the removal. mypy — confirmed re-enabled and running for real: a live
`[tool.mypy]` section in `pyproject.toml:61-65` produces genuine type errors on
`src/autopilot/spec.py`, not the fatal parse-abort the original review described.

Dead/mismatched config confirmed still open, by design (tracked, not fixed):
`src/sdk/config.py` exports `MAX_HEALTH_FAILURES`/`TASK_DEDUPLICATION_ENABLED`/
`PROJECT_ROOT` into spawned-process env, while `src/core/simple_config.py` only
reads differently-named `MAX_HEALTH_CHECK_FAILURES`/`TASK_DEDUP_ENABLED`/
`PROJECT_PATH` — none of the three match, and both
`tests/test_config_keys_are_live.py` and `tests/test_exported_env_vars_are_consumed.py`
exist tracking this as a deliberate per-setting owner decision, not an oversight.

One doc-only inaccuracy found: `AUTOPILOT_REFACTOR_PLAN.md:221` and
`SOLID_REFACTOR_ADVERSARIAL_REVIEW.md:601` both claim adversarial finding #21 was
fixed via a new method `_update_feature_status_by_key` — no such function exists
anywhere in `src/`. The underlying type-contract problem it describes
(`feature_id: Optional[str]` plus a separate `feature_key` param) is nonetheless
genuinely gone — current `_update_feature_status`
(`src/autopilot/orchestrator/features.py:126`) takes a required `feature_id: str`
with no `feature_key` split. The symptom is fixed; the docs' description of *how*
is wrong. Worth a doc correction, not a code change.

### §5 — Frontend (`frontend/src/`)

The 08-19 update states frontend "was not re-audited this pass — out of scope,
unchanged by the backend refactor." That note is itself stale: a same-day
(2026-08-21) session already re-audited and fixed all five original §5 findings,
independently reconfirmed here against current code:

- 5.1 `TaskDetailModal` — partially fixed (as the doc itself claims): now 1290
  lines, `useTaskDetails`/`useDisclosure` hooks extracted and shared with
  `AgentDetailModal.tsx`/`TicketDetailModal.tsx` (a third duplicate site the
  original finding missed, now also consolidated). `window.confirm`/`alert` calls
  and JSX-splitting deliberately left — real UX changes, not verifiable
  without a browser.
- 5.2 Per-row polling + status-config maps — confirmed fixed: no per-row
  `setInterval` remains in `DesignQueuePanel.tsx`; a merged
  `DESIGN_FEATURE_STATUS_CONFIG` replaces the duplicated maps.
- 5.3 `MessageCenter.getMessageActions` — confirmed fixed: delegates to a pure,
  rule-array-driven `deriveMessageActions`; the `TODO: Implement retry` stub is
  gone.
- 5.4 Duplicated markdown config — confirmed fixed: `Results.tsx` now uses the
  shared `MarkdownRenderer`.
- 5.5 `Graph.tsx` dagre re-layout on hover — confirmed fixed: the layout
  `useEffect` no longer depends on hover state; highlighting is applied via
  `onNodeMouseEnter`/`onNodeMouseLeave` patching already-laid-out elements.

New finding, still open: three independent `StatusBadge` component definitions —
`components/StatusBadge.tsx:51`, `pages/Autopilot.tsx:501`,
`components/autopilot/DesignQueuePanel.tsx:461`. The 08-19 update already names
this as found-but-deliberately-out-of-scope; confirmed still unaddressed, and a
larger blast radius (3 files, dozens of call sites) than the config-map fix that
did ship.

### §6 — Plan/process bookkeeping

`AUTOPILOT_REFACTOR_PLAN.md`'s own internal bookkeeping is unusually
self-correcting — nearly every "Done" claim in it carries a later
"Verified/Corrected" annotation that re-checks itself against shipped code (e.g.
§3.1 Exception 2 is explicitly marked NOT honored despite being planned; §3.3's
exit criteria explicitly flag the "no behavior-changing diff" bullet as "only half
met"). No internal contradictions found. `docs/AUTOPILOT_REFACTOR_ANALYSIS.md`'s
citations into specific commits check out verbatim; its file:line citations for
anything post-decomposition are ~1 week stale, but the plan's own §4.6 handoff
prompt (`design_docs/phase2_solid_consolidations_prompt.md:11`) already flags this
itself ("every file path and line number in it is stale") — not a new problem.

See §1 above for this section's two substantive findings: the undocumented
`process_queue` commit batch, and the missing sign-off record on the project-route
retirement.

---

## 3. Updated priorities

1. **New, highest leverage.** Fold today's 16 `process_queue` race-family commits
   into `AUTOPILOT_REFACTOR_PLAN.md` (or a dedicated note) before the plan doc's
   account of this bug class falls further out of sync with the actual fix
   history — this is precisely the "N-th independent implementation of a
   race-prone primitive" pattern the plan was built to track.
2. **New.** Audit and decompose `src/autopilot/orchestrator/pipeline.py` (3257
   lines) under the same ~800-line criterion already applied to
   `phase_transitions.py`/`orchestrator/__init__.py` — it is now the largest file
   in the repo and structurally is exactly the god-module the 08-19 update's
   priority #2 believed it had already closed.
3. **New, low-effort.** Re-check size budgets on `_mcp_tool_registry.py` (980
   lines), `task_admin_routes.py` (933 lines), `project_routes.py` (1690 lines),
   `feature_routes.py` (1483 lines) — all grew past their last-verified sizes with
   same-day modification timestamps.
4. **Carried forward, unchanged priority.** 1.13/1.15/4.6 — the `except
   Exception`/manual-session patterns remain the single highest-leverage
   remaining structural gap (141 broad excepts, dozens of manual sessions); no
   subsystem audit this pass found this materially improved or worsened since
   08-19.
5. **Low-effort cleanup, newly found.** Consolidate the two independent
   `_find_tmux_session` definitions (`src/agents/manager.py:440`,
   `src/agents/output_capture.py:544`); the frontend's three independent
   `StatusBadge` definitions remain the open item both this pass and the 08-19
   update agree on.
6. **Doc hygiene, no code change.** Fix `SOLID_REFACTOR_ADVERSARIAL_REVIEW.md:601`
   / `AUTOPILOT_REFACTOR_PLAN.md:221`'s reference to a nonexistent
   `_update_feature_status_by_key` method (the underlying fix is real, the
   description of it is not); reword `SOLID_OO_REVIEW_UPDATE_2026-08-19.md`'s §4
   "Done" language for 3.1 so it doesn't read as contradicting its own §2 table;
   retroactively confirm (or note the absence of) the product sign-off
   `design_docs/phase2_solid_consolidations_prompt.md` required before the
   `/api/projects/*` retirement.

## 5. Follow-up: priorities 1-3 and 5 actioned, 2026-08-21

Items 1-3 above are done — see `docs/AUTOPILOT_REFACTOR_PLAN.md` §4.12 (queue-
dispatch claim primitive), §4.8's 2026-08-21 follow-up (pause-state extended to
queued tasks), Tier 4 (the three unrelated live bugs), §4.13 (`pipeline.py`,
partial — one safe extraction landed, the rest deliberately deferred, same
"deliberate final boundary" pattern already used for `AgentManager`/
`MonitoringLoop`), and §4.14 (`project_routes.py` split three ways;
`_mcp_tool_registry.py`/`task_admin_routes.py` reviewed and deliberately not
split, each for a documented reason rather than left silently undone).

**Item 5's own framing was corrected while acting on it.** `manager.py:440`'s
`_find_tmux_session` is not an independent duplicate — it's a one-line
delegator to `output_capture.py:544`'s real implementation, the same thin-
facade pattern already used by its three neighboring methods (verified by
reading both, not just grepping the name). The real finding is the 9 call
sites that hand-roll the same has-session-then-iterate logic instead of
calling the shared helper — but of those, only `launch_pipeline.py`'s
restart-path kill-session block turned out to be a clean, low-risk
consolidation candidate (no diagnostic logging, no same-day fragility, already
holds a reference to the collaborator that owns the real method): fixed.
`messenger.py`'s and `terminator.py`'s copies sit in message-delivery/
termination hot paths patched twice today for unrelated live incidents
(`d57b14f`, `e733a9e`/`ce975ca`) — deliberately left alone rather than risk a
regression in code stabilized hours earlier for a cosmetic DRY win, and
`messenger.py`'s version also carries debug logging that looks deliberately
added to diagnose a past incident, not safe to silently collapse.
`orphan_reaper.py`/`mechanical_recovery.py` don't actually duplicate the
pattern at all — they iterate `.sessions` directly without a `has_session`
precheck, a different (arguably better) shape. See
`docs/AUTOPILOT_REFACTOR_PLAN.md`'s commit history (`2c41a37`) for the fix and
full reasoning.
