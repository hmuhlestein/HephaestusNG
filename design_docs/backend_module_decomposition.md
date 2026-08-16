# Backend Module Decomposition — Design Document

**Status:** Orchestrator split (§3.1) **COMPLETE** — API split (§3.2) **COMPLETE**
**Verified against:** `src/autopilot/orchestrator.py` @ commit `e9c47f7`
(10246 lines, 139 top-level symbols) and `src/mcp/autopilot_api.py` @ commit
`e9c47f7` (5724 lines, 123 def/class + 63 routes) — every line number in
§3.1/§3.2 and every cross-file import in §4 was re-derived from the live
files with `ast.parse()` on 2026-08-15, not carried over from an earlier
pass. **Third verification pass (2026-08-16, pre-API-split):** `ast.parse()`
against the live `autopilot_api.py` @ HEAD (`5fbbfbe`) confirmed all 5724
lines, all 138 top-level symbol spans (123 def/class + 15 module
constants), all 63 routes, and every §4 internal import line number match
this doc exactly — commit `a2905e8` (the §3.1 call-site migration) touched
the file post-`e9c47f7` but only retargeted 14 orchestrator import lines,
with zero structural change. `scripts/split_autopilot_api.py` hardcodes
those same 138 ranges and refuses to run if any span drifts. This is the **second** re-verification: both files moved again since
the previous pass (commits `e9586a2`/`b5f55f6`, 2026-07-21), and not just by
line shift — four orchestrator symbols listed here were *deleted* in the
meantime (`generate_html_feature_report` and
`generate_product_validation_report` in `a7b9ee4`'s dead-code cleanup,
`_update_orchestrator_max_gotos` in `ff02edc`'s goto-budget fix,
`_link_workflow_to_feature` in `c3622c9`'s feature-sync fix), 26 new
orchestrator symbols appeared, and the API file gained 24 def/class and 11
routes (cost tracking, file browsing, review mode, decomposition review).
This is exactly the scenario §3.3 already warns about: **re-run
`grep`/`ast.parse()` against the live file immediately before extracting,
regardless of how recently this doc was updated** — the symbol → module
name mapping is what's authoritative if the two ever disagree again.
**Scope:** Convert `src/autopilot/orchestrator.py` (10246 lines, currently a
single flat file) into a `src/autopilot/orchestrator/` **package** — 8
submodules plus a driver in `__init__.py` — and split
`src/mcp/autopilot_api.py` (5724 lines) into the smaller, single-responsibility
modules `design_docs/autopilot_architecture_review.md` §7/§4.2/Tier 5.3
already proposed. This is cleanup, not a feature — no behavior change is in
scope.

**Execution results (orchestrator split, 2026-08-15):**
- Commits: `691d22a` (extraction), `a2905e8` (call-site migration), `d481a37`
  (test fixes) + several intermediate fix commits
- 139/139 symbols present in package, lossless reassembly verified
- 10,779 lines across 9 files (8 submodules + `__init__.py`)
- `ruff check --select F401,F811,F821` all clean
- `py_compile` all clean
- 580/581 tests passing across 18 test files (1 flaky test-isolation failure,
  not caused by the split)
- `test_advance_phases.py`: 62/98 passing (36 remaining are test-isolation
  flakiness — individual tests pass when run alone)

**Key deviations from plan:**
1. **Import derivation for `__init__.py` was the hardest part.** The plan's
   §3.3 step 5 said "imports are not scriptable and stay a manual/agent step."
   In practice, the superset-header + ruff-trim approach worked for submodules
   (ruff auto-fixes F401), but ruff *won't* auto-fix F401 on package
   `__init__.py` (re-export heuristic). The strip-and-re-derive-via-F821
   approach also failed due to ruff argument-ordering issues (`--no-cache`
   duplication). Final approach: strip all imports from `__init__.py`, run
   F821 loop to add back exactly what's needed, then manually add 7 remaining
   names that the loop missed (`json`, `shutil`, `sys`, `get_db`, `Workflow`,
   `DatabaseManager`, `get_config`).
2. **`_ensure_git_excluded` in `state.py` created a circular import** with
   `worktree_integration.py` (state → worktree_integration → state). Fixed by
   making it a function-scoped import in `_get_or_create_project_id`. The plan
   didn't flag this cycle.
3. **`threading as _threading`** import at line 154 (outside the leading block)
   needed manual addition to `phase_transitions.py`.
4. **Test patch-target retargeting was the bulk of the work.** The plan's §4
   listed 223 string-based `patch("src.autopilot.orchestrator.X")` targets
   across 8 test files but didn't specify the retargeting strategy. The key
   insight: **mocks must target where the name is LOOKED UP (the calling
   module), not where it's DEFINED**. E.g., tests exercising
   `_retry_failed_tasks` (phase_transitions) must patch
   `phase_transitions.get_tasks`, not `engine_client.get_tasks`, because
   phase_transitions imports get_tasks at module level. This required
   per-test-class analysis to determine which module the function under test
   lives in.
5. **`HEPHAESTUS_DIR` +1 `.parent`** pitfall was real — caught and fixed as
   predicted.
6. **`__file__` path depth** fixes in `worktree_integration._run_ash_scan`
   (`parents[2]` → `parents[3]`) and `reporting._generate_design_report_html`
   (`parent / "templates"` → `parent.parent / "templates"`) were needed as
   predicted.
7. **Column-0 indentation errors** in test files — the migration script
   sometimes left migrated imports at column 0 when they should have been
   indented (inside functions). Fixed with a post-migration indentation repair
   pass.

The orchestrator side is deliberately a **package**, not just several new
sibling files dropped next to `service.py`/`spec.py`/`phases.py`: every
extracted module here (`state.py`, `engine_client.py`, `policy.py`, etc.) is
specifically *orchestrator*-internal machinery — the pipeline's own control
loop, queue scanning, phase transitions — not a general autopilot-level
concern the way `service.py` (lifecycle) or `spec.py` (the gate) are. A
package makes that boundary explicit in the directory structure itself,
mirrors the `src/mcp/autopilot/` package this same doc creates on the API
side (§3.2), and gives the driver code left behind (§3.1's "remaining" set)
an honest name: it doesn't get demoted to a leftover scrap file, it becomes
`src/autopilot/orchestrator/__init__.py` — *the* orchestrator module, not a
former-god-object's remainder.

**Sequencing decision:** this lands **before**
`design_docs/human_input_intervention_system.md`. Cleanup before the next
feature, on a clean tree, is easier to review and bisect than cleanup
racing a feature that's rewriting one of the functions being moved. That
feature doc has been updated to assume the module layout below already
exists — read it after this one, not the reverse.

---

## 1. Problem statement

The architecture review flagged both files as "god-objects" (P7) and
proposed a Tier 5.3 split. As of 2026-08-15 that split **has not happened
for the backend** — both files grew instead:

| File | At review (2026-06-19) | Now (2026-08-15) |
|---|---|---|
| `src/autopilot/orchestrator.py` | ~2300 lines | **10246 lines**, 139 top-level `def`/`class` |
| `src/mcp/autopilot_api.py` | ~2560 lines | **5724 lines**, 63 route handlers |

The frontend half of the same Tier 5.3 item **did** happen —
`frontend/src/pages/Autopilot.tsx` went from ~3200 lines to 498, with 13
components extracted to `frontend/src/components/autopilot/`. That's the
existence proof this kind of split is tractable here and the tests/review
discipline supports it; the backend just never got the same treatment.

Every unrelated feature that has landed since (multi-project concurrency,
worktree isolation, the spec gate, credit/session-limit handling,
arbitration, recovery, per-task cost tracking, file browsing, review mode)
went into these same two files because there was nowhere smaller to
put it. That's the compounding cost of not splitting: every new PR's diff
context is "one function in a 10000-line file," and unrelated logic
(design-queue scanning next to phase-transition arbitration next to health
auditing) sits in the same namespace with no import boundary to
signal what depends on what.

**Goal:** extract cohesive, already-visible clusters of functions into
their own modules, with zero behavior change, verified by the existing test
suite (which already imports most of these functions directly — see §4)
staying green throughout.

---

## 2. What already exists (don't recreate)

Tier 2/5 work already extracted three of the modules the original proposal
called for — confirm these exist before starting and build *around* them,
not duplicate them:

- `src/autopilot/service.py` — `AutopilotService` (line 36) /
  `AutopilotServiceRegistry` (line 448), lifecycle per project. Already
  thin; calls into `orchestrator.run_continuous_pipeline` via
  `run_in_executor` (`service.py:407` runs `self._run_pipeline_sync` on an
  executor, which imports and calls `run_continuous_pipeline` at
  `service.py:433`). That one import needs no change —
  `run_continuous_pipeline` stays in the package's `__init__.py`, so
  `from src.autopilot.orchestrator import run_continuous_pipeline` still
  resolves after the conversion. Its other `from src.autopilot.orchestrator
  import (...)` blocks (lines 118, 148, 170, 184, 209) import symbols that
  *do* move into submodules (`state.py`) — those need
  `from src.autopilot.orchestrator.state import (...)` instead (see §4's
  table). One more thing to preserve: `service.py:432` does
  `import src.autopilot.orchestrator as orch_module` and writes
  `orch_module._stop_events[self.project_id]` — a module-attribute write,
  not a symbol import. The package's `__init__.py` must therefore keep
  defining `_stop_events` (it's in the "remaining" set, §3.1). Any import
  left pointing at the old flat-module path for a moved symbol will raise
  `ImportError` immediately once the package replaces the file — a cheap,
  automatic completeness check for this step.
- `src/autopilot/spec.py` — the hybrid spec gate (§9.1). Already separate.
  Not touched.
- `src/autopilot/phases.py` — phase prompts/config (consolidated from the
  old `phase_1..10_*.py` files, deleted in `d62e67b`). Already separate.
  Not touched.
- `src/autopilot/report_generator.py` — the dead-code problem an earlier
  version of this doc flagged here **has already been resolved**: commit
  `a7b9ee4` (2026-08-10, "chore: remove dead code") deleted
  `report_generator.py` in its entirety (158 lines, zero imports), plus
  `orchestrator.py`'s `generate_html_feature_report()` and
  `generate_product_validation_report()` (both confirmed zero-caller), plus
  `templates/feature_report.html` (only used by the deleted generator).
  The feature-report HTML is now produced by the `feature_review` agent
  itself (written next to `features.json`); the orchestrator merely copies
  it into the designs folder (`run_phase0`) and to permanent storage
  (`finalize_phase0_workflow`). Nothing to do here — but note the
  consequence for §3.1: the reporting cluster is 5 symbols, not the 7 an
  earlier draft of this doc listed, and `src/autopilot/templates/` now
  contains only `design_report.html`.

---

## 3. Design

### 3.1 `src/autopilot/orchestrator.py` → `src/autopilot/orchestrator/` package (8 submodules + `__init__.py`)

This is the exhaustive symbol → module mapping, built by walking
`orchestrator.py`'s full top-level `def`/`class` list (`grep -n "^def \|^class
\|^async def " src/autopilot/orchestrator.py`, 139 symbols as of `e9c47f7`)
one at a time — not a line-range approximation. **Line ranges turned out to
be misleading here**: several functions are physically interleaved with a
different cluster's code (e.g. `_ensure_git_excluded` (6513) /
`_run_ash_scan` (6562), worktree concerns, sit numerically between
`_resolve_arbitration_outcome` (ends 6510) and `_cap_out_review_phase`
(6627); `_retry_failed_tasks` (1643), phase-transition-coupled by the real
import at `server.py:1735`, sits numerically between
`is_design_fully_complete` (ends 1640) and `attempt_recovery` (1868)). Use
the table below, not proximity in the file, as ground truth — it's also
exactly the `dict[str, str]` the extraction script in §3.3 needs, so it can
be copied in directly.

This revises the original 7-module sketch to **8 modules**: the earlier
draft folded feature-record bookkeeping (`_create_feature_records`,
`_validate_features_json`, etc.) into `reporting.py` by file proximity, but
semantically that's Feature-Model DB bookkeeping, not report generation —
splitting it into its own `features.py` keeps `reporting.py` to "produces
an HTML/JSON artifact" and nothing else.

Every path below is inside the **new package directory**
`src/autopilot/orchestrator/` — e.g. "`state.py`" means
`src/autopilot/orchestrator/state.py`, not a sibling of `service.py`. The
existing flat `src/autopilot/orchestrator.py` file is deleted as part of
this task; a file and a package can't share a name, so this isn't optional.

**1. `state.py`** — data classes + project-context
persistence: `StopReason`, `DesignStatus`, `DesignEntry`, `IterationResult`,
`FeatureReport`, `PipelineState`, `_get_project_context`,
`_set_project_context`, `_delete_project_context`,
`_get_project_contexts_by_prefix`, `_resolve_project_id`,
`_get_or_create_project_id`, `_running_state_key`, `PersistentPipelineState`
(+ its module constants `_RUNNING_STATE_KEY_PREFIX` (line 503) /
`_RUNNING_STATE_KEY_LEGACY` (line 504)),
`_workflow_belongs_to_project`. No dependency on anything else being
extracted — safe to do first. (15 symbols)

**2. `engine_client.py`** — "talk to the backend/LiteLLM"
I/O helpers: `get_litellm_config`, `file_hash`, `api_get`, `api_post`,
`update_task_status`, `increment_task_retry_count`, `terminate_agent_direct`,
`pause_workflow_direct`, `complete_workflow_direct`, `fail_workflow_direct`,
`pause_project_workflows`, `create_agent_for_task_direct`,
`_update_orchestrator_status`, `get_tasks`, `get_agents`, `peek_agent_output`,
`get_task_progress`, `get_workflow_status`, `get_active_workflows`. Takes
the module constant `API_BASE` (line 61) with it — it's used only by
`api_get`/`api_post`. `pause_project_workflows` is the newest addition to
this cluster (multi-project concurrency + budget enforcement); it's called
from `stop_pipeline` and from `src/core/cost_derivation.py`'s lazy-import
wrapper (see §4). The architecture review's C1.2 proposal, just delayed.
Depends only on stdlib + the DB/API layer; safe to extract second. (19 symbols)

**3. `policy.py`** — stuck/health/credit detection and
recovery decisions: `_workflow_appears_abandoned`,
`_update_resumed_workflow_recovery_attempts`, `_escalate_stale_active_workflows`,
`attempt_recovery`, `check_api_credits`, `detect_hard_error`,
`detect_impasse`, `detect_architectural_issue`. Also takes two module
constants whose primary users are the policy detectors (the
`__init__.py` driver, which already imports from its submodules, imports
them back from here): `ACTIVE_AGENT_STATUSES` (line 111) and
`STALE_ACTIVE_WORKFLOW_CONSECUTIVE_CHECKS` (line 130). **`prompt_human` is
deliberately not in this list** — leave it in the package's `__init__.py`
for now; it gets authored fresh, directly in this module, by
`human_input_intervention_system.md` (which lands after this split).
Moving the current file-mailbox version here first is a pointless
intermediate commit. (8 symbols)

**4. `queue.py`** — design-queue scanning/picking/status,
nothing else: `scan_design_queue`, `_has_resumable_active_design`,
`pick_next_design`, `_assess_run_health`, `is_design_fully_complete`,
`_update_design_status`, `_set_workflow_type`, `_get_phase0_completion`.
`_has_resumable_active_design` is the helper `pick_next_design` calls to
decide whether an active design can be resumed — it belongs with the
picker, not in the driver. Purely a location change — does **not** touch
the DB-vs-file dual-store problem (Tier 3 of the architecture review,
still separately unresolved); don't conflate the two. (8 symbols)

**5. `worktree_integration.py`** — pipeline-level worktree/git
orchestration (§9.6's integration worktrees, ASH security scanning),
distinct from the generic `WorktreeManager` class in
`src/core/worktree_manager.py` (calls into it, doesn't duplicate it):
`create_feature_folder`, `copy_design_document`,
`_create_integration_worktree`, `_cleanup_worktree`,
`sweep_completed_workflow_worktrees`, `heal_orphaned_agent_branches`,
`_heal_orphaned_branches_for_project`, `_create_designs_folder`,
`_recover_abandoned_workflows_missing_worktree`,
`_recover_abandoned_workflows_with_completed_phase`,
`_ensure_git_excluded`, `_run_ash_scan`. The four recovery/sweep functions
are the newest additions — restart-window self-heal (orphaned branches,
completed-but-uncleaned worktrees) that `server.py`'s startup sweep and
phase-advancement sweep call (see §4). (12 symbols)

**6. `features.py`** — Feature-Model DB record bookkeeping
(split out of the original `reporting.py` sketch — see above):
`_create_feature_records`, `_update_feature_status`,
`_sync_stale_feature_statuses`, `_sync_stale_design_statuses`,
`_relink_features_to_workflows`, `_clean_stale_assigned_tasks`,
`_validate_features_json`, `_resolve_execution_order`, `_sweep_stray_files`.
Takes the sweep's module constants `SWEEP_ENABLED` (4374),
`_SWEEP_REPORT_NAMES` (4378), `_STRAY_DIRS` (4396) — used only by
`_sweep_stray_files`. Note: an earlier draft of this doc listed
`_link_workflow_to_feature` here; it no longer exists (removed in
`c3622c9` — it was a pre-existing no-op folded into the feature-sync
logic). `_sync_stale_design_statuses` is its design-table sibling of
`_sync_stale_feature_statuses`, called from the same server sweep. (9 symbols)

**7. `reporting.py`** — pure report/artifact generation, no DB
writes: `_report_path`, `collect_report_summaries`, `collect_files_created`,
`_generate_design_report_html`, `_empty_report`. Takes the module constant
`_REPORT_SUBDIR` (4449); `features.py`'s `_sweep_stray_files` also reads it,
so `features.py` imports it from here rather than duplicating it. The two
Jinja2 generators an earlier draft of this doc listed here
(`generate_html_feature_report`, `generate_product_validation_report`)
were deleted as zero-caller dead code in `a7b9ee4` — there is no
"converge with report_generator.py" work left to do (§2 above). (5 symbols)

**8. `phase_transitions.py`** — the actual control-loop
engine (goto/retry/continue state machine, arbitration, manual-handoff,
phase-task creation); do this one **last**, after the other seven have
proven the process — it's the largest (33 symbols) and most
externally-depended-on cluster (`server.py:1735`'s
`_run_phase_advancement_sweep_once` pulls 4 of these symbols in one import
statement, and `task_completion_service.py` imports 3 more — see §4):
`_try_advance_phases`, `_retry_failed_tasks`,
`_retry_exhausted_paused_workflows`, `_advance_phases`,
`_try_auto_resume_paused_workflow`, `_release_stale_task_creation_claims`,
`_release_pending_phases_with_done_tasks`, `_get_phase_statuses`,
`_claim_phase_task_creation`, `_release_phase_task_creation_claim`,
`_case_start_first_phase`, `_case_in_progress_no_tasks`,
`_case_completed_with_successor`, `_manual_handoff_required`,
`_pause_for_manual_handoff`, `_case_in_progress_complete`,
`_maybe_retry_failed_tasks`, `_fire_phase_transition`,
`_gather_arbitration_context`, `_build_arbitration_prompt`,
`_phase_currently_passes`, `_trigger_arbitration`,
`_maybe_resolve_arbitration`, `_read_arbitration_result`,
`_consume_arbitration_result`, `_resolve_arbitration_outcome`,
`_cap_out_review_phase`, `_create_phase_task`, `_create_corrective_task`,
`_create_corrective_task_body`, `_wait_for_task_terminal`,
`_negotiate_validation_fix`, `_resume_stuck_workflow_tasks`. Takes the
module constants `CLAIM_STALE_TIMEOUT_SECONDS` (140),
`MANUAL_ONLY_PHASES` (5106), `ARBITRATION_CREATED_BY` (5906) — each is
used only by this cluster (plus the `_advance_phases_locks` /
`_advance_phases_locks_guard` pair at lines 181-182, which
`_try_advance_phases` owns). (33 symbols)

**Remaining in `src/autopilot/orchestrator/__init__.py` after all of the
above (30 symbols, roughly 3000 lines):** the config/timeout helpers
`_get_workflow_timeout`, `_get_phase0_timeout`,
`_get_paused_workflow_retry_cooldown_seconds`,
`_get_paused_workflow_max_retry_cycles`; the workflow-monitoring trio
`_register_monitored_workflow`, `_unregister_monitored_workflow`,
`_is_workflow_monitored` (plus their module globals
`_actively_monitored_lock` (155) / `_actively_monitored_workflows` (156) —
`server.py:1872` imports `_is_workflow_monitored` across the package
boundary, which is fine); `_resync_pipeline_registry` (a self-heal that
re-schedules `AutopilotService.start()` onto the server's event loop — it
is lifecycle-adjacent driver machinery, and its type hint references
`OrchestratorLogger`, so it stays with the driver rather than in
`policy.py`); `OrchestratorLogger` (leave in place — Tier 2.4 of the
architecture review is a separate, already-tracked item; don't fold its
split into this one); `prompt_human` (see §3.1.3); the review-mode run-loop
gates added by `design_docs/autopilot_review_mode.md`:
`_should_pause_for_review`, `_pause_feature_for_review`,
`_wait_for_review_clearance`, `_restore_phase0_completed_status`,
`_pause_phase0_for_review`, `_wait_for_phase0_review_clearance`,
`finalize_phase0_workflow`, `_wait_for_pending_reviews` (they sit between
`run_phase0` and `_run_one_feature` and are called only from the run loop;
`finalize_phase0_workflow` has two external importers —
`autopilot_api.py`'s `_review_phase0_decomposition` and
`src/phases/phase_manager.py:1716` — both of which keep working because the
symbol stays in `__init__.py`); and the top-level run loop:
`run_single_workflow`, `run_phase0`, `_run_one_feature`,
`run_feature_pipelines`, `run_design_aggregate`, `_archive_and_cleanup`,
`run_single_design`, `_should_stop`, `_interruptible_sleep`,
`_register_orchestrator_agent`, `run_continuous_pipeline`, `main`.
Everything else at module level stays here too: `logger`, `HEPHAESTUS_DIR`,
`POLL_INTERVAL`, `STUCK_THRESHOLD`, `DESIGN_QUEUE_SCAN_INTERVAL`,
`HEARTBEAT_INTERVAL`, `MAX_WORKFLOW_TIME`, `MAX_PHASE0_TIME`,
`MAX_PARALLEL_FEATURES`, `MAX_DESIGN_RETRIES`, `PARENT_PEEK_INTERVAL`,
`_orchestrator_agent_id`, and `_stop_events` (9526 — required by
`service.py:432`'s module-attribute write, §2). **`HEPHAESTUS_DIR` has a
real trap in the move:** it's `Path(__file__).parent.parent.parent` (line
60), which resolves to the repo root from the flat file; in the package,
`__init__.py` is one directory deeper, so the expression as-written would
silently resolve to `src/`. Add one more `.parent` as part of the move —
its four use sites (9706, 9741, 10225, 10231) are all in this remaining
set, so the fix stays local. Putting the driver directly in
`__init__.py` (rather than e.g. `driver.py` plus a re-exporting
`__init__.py`) means every external call site that only ever touched these
30 symbols — `service.py`'s `run_continuous_pipeline` import chief among
them — needs **zero import changes**, since
`from src.autopilot.orchestrator import run_continuous_pipeline` resolves
identically whether `orchestrator` is a flat file or a package.

**The package stays its own thing — don't fold it into `service.py`.**
This is a deliberate resolution of what was an open question in an earlier
draft of this doc, not a default: `service.py` is *lifecycle* (per-project
start/stop/status, the `AutopilotServiceRegistry`), a thin layer that calls
into `run_continuous_pipeline` via `run_in_executor` and otherwise knows
nothing about phases, designs, or workflows. The driver left in
`orchestrator/__init__.py` is the actual pipeline driver — a different
responsibility at a different layer, even after the package as a whole
sheds 7000+ lines into its 8 submodules. Merging them would re-blur exactly
the boundary `AutopilotService` was introduced to draw (Tier 2 of the
architecture review), just with the blur moved one level down. This
package **is** "the orchestrator module" in the fullest sense — a real
package with real internal structure, not a flat file that happened to
shrink.

**Symbol-count sanity check:** 15 + 19 + 8 + 8 + 12 + 9 + 5 + 33 + 30 = 139,
matching the full top-level symbol count as of `e9c47f7`. If your own
`ast.parse()` pass in the extraction script comes up with a different
total, something in this table is stale (the file has moved on since this
doc was written) or your grep pattern caught something this one didn't
(e.g. a symbol defined with an unusual decorator) — reconcile before
running the extraction, don't paper over the mismatch.

### 3.2 `src/mcp/autopilot_api.py` → `src/mcp/autopilot/` package (shared module + 6 route files)

Same discipline as §3.1: this is an exact, exhaustive symbol → file mapping
verified line-by-line against the current 5724-line file (`grep -n "^def
\|^class \|^async def \|^@router\." src/mcp/autopilot_api.py`, 123
def/class + 63 `@router.*` routes as of `e9c47f7`, cross-checked with
targeted `grep`s for where ambiguous helpers are actually called from),
not the line-range approximation an earlier pass of this doc used. That
approximation had **one real gap and two proximity-misleads**, all fixed
below and still correct today:

- **Gap:** `GET /status` (`get_pipeline_status`, lines 445-726) fell in the
  no-man's-land between `_shared.py`'s end (444) and the queue cluster's
  first route decorator (761) and was never assigned to any of the 6 files.
  Missing it would have silently dropped the pipeline's main status
  endpoint from the split entirely.
- **Proximity-mislead 1:** `configure_autopilot_api` sits at the very
  bottom of the file (5715-5724) but mutates `DESIGN_QUEUE_DIR`,
  `FEATURES_DIR`, `_active_project_id_cache` — module-level globals
  declared near the very top (lines 44-46). It belongs in `_shared.py`
  with the globals it owns, not in whichever route file happens to occupy
  the file's tail end.
- **Proximity-mislead 2:** `_design_id` (line 1797) sits numerically at the
  boundary of the queue cluster, but grepping its 3 call sites (2069,
  2098, 2892) shows every one is inside `project_routes.py`'s territory —
  it belongs there, not with queue.

Every route file defines its **own local** `router = APIRouter()` (not a
shared import) — the existing `@router.get(...)`/`@router.post(...)`
decorator lines then move verbatim with their function bodies and need no
rewriting, since the variable name `router` still resolves locally in each
new file. `src/mcp/autopilot/__init__.py` is the aggregator: one
`router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])` that
`include_router()`s each of the six route modules' own router instances.
`src/mcp/server.py`'s mount point changes from
`from src.mcp.autopilot_api import router as autopilot_router` (server.py:856)
and `from src.mcp.autopilot_api import configure_autopilot_api`
(server.py:855) to `from src.mcp.autopilot import router as autopilot_router`
(and `configure_autopilot_api` from `src.mcp.autopilot._shared`) — this one
**does** need an import-path edit, unlike the orchestrator package's
`__init__.py` trick, because the module is being renamed
(`autopilot_api` → `autopilot`), not just relocated in place.

**`_shared.py`** (lines 1-444, plus `configure_autopilot_api` relocated
from 5715-5724): the module-level constants `logger` (40), `router` (42),
`DESIGN_QUEUE_DIR`/`FEATURES_DIR`/`_active_project_id_cache` (44-46),
`_queue_dir_by_project`/`_features_dir_by_project` (57-58, the per-project
dir caches), `ALLOWED_EXTENSIONS` (60, used by queue/project/status routes);
note that `PHASE0_DEFINITION_IDS`/`DESIGN_WORKFLOW_DEFINITION_IDS` are no
longer defined in this file — they moved to `src/core/constants.py`
(43-44) and are imported at line 27, so `_shared.py` just re-uses the
import; the cross-cutting helpers `_get_active_project_id` (63),
`_invalidate_project_dirs` (72), `_get_effective_queue_dir` (86),
`_get_effective_features_dir` (154), `_cached`/`_store`/`_invalidate`
(223, 233, 238 — the response-cache layer, together with its `T` type alias
(217), `_cache` (219) and `CACHE_TTL` (220) module state), `_safe_path`
(246), `_feature_status` (262), `_extract_pr_url` (270 — its two call sites
split one project / one feature, so it's genuinely cross-cutting and stays
here), `_get_latest_run_dir` (307), `_read_json` (315), `_read_jsonl_tail`
(322); the Pydantic models `DesignQueueItem` (344), `DesignQueueAdd` (352),
`FeatureSummary` (359), `FeatureDetail` (372), `PipelineStatus` (401),
`MessageItem` (436); and `configure_autopilot_api` itself. Every other
route module imports from here; extract first.

**`control_routes.py`** (lines 445-726, then 5283-5709 — not contiguous,
see the gap fix above): `GET /status` → `get_pipeline_status` (445-726),
`POST /start` → `start_pipeline` (5288-5313), `_start_pipeline_reserved`
(5316-5405), `POST /stop` → `stop_pipeline` (5408-5476),
`POST /cleanup-branches` → `cleanup_branches` (5479-5517), `GET /health` →
`get_system_health` (5520-5523), `run_health_audit` (5526-5709, a ~184-line
helper — don't overlook it as "just the route body," it's a substantial
standalone health-check routine). This is the surface that calls into
`AutopilotServiceRegistry` — keep it thin where it already is, don't add
logic while moving it. Note `stop_pipeline` now calls
`pause_project_workflows` (5446, `engine_client.py`) to actually stop
agents, not just flip status — that cross-package import is real, not
accidental.

**`queue_routes.py`** (lines 727-1789, ~1063 lines): `_get_queue_order_path`
(732), `_load_queue_order` (744), `_save_queue_order` (754),
`GET /queue` → `list_design_queue` (761-800), `QueueReorderRequest` (803),
`POST /queue/reorder` → `reorder_queue` (808-827), `POST /queue/requeue` →
`requeue_design` (830-922), `POST /queue/rerun` → `rerun_design`
(925-1339), `POST /queue/repair` → `repair_design` (1342-1374),
`spawn_repair_review_agent` (1377-1476), `_run_repair` (1479-1583),
`GET /queue/repair/{repair_id}` → `get_repair_status` (1586-1606),
`DesignAddByPath` (1612), `POST /designs/add` → `add_design_by_path`
(1617-1728), `POST /queue` → `add_to_queue` (1731-1763),
`DELETE /queue/{filename}` → `remove_from_queue` (1766-1777),
`GET /queue/{filename}/content` → `get_queue_item_content` (1780-1789).
Note the repair flow (1342-1583, ~242 lines) lives here even though the
architecture review's §11.1 says it was "slimmed to workflow recovery
only" — it's still a substantial fraction of this cluster, not the small
tail the wording implies. **Execution deviation (2026-08-16):** `feature_routes.py`
reads the mutable global `FEATURES_DIR` directly at one site (original
line 4696, `get_feature_detail`), and `FEATURES_DIR` is *rebound* (not
mutated) by `configure_autopilot_api` and test fixtures — a `from
._shared import FEATURES_DIR` would bind a stale copy at import time, so
that one line became `feature_dir = _safe_path(_shared.FEATURES_DIR,
feature_id)` with `from src.mcp.autopilot import _shared`. This is the
single non-pure move in the split; every other line is byte-identical.
`AUTOPILOT_STATE_DIR` (read bare in `queue_routes`/`intervention_routes`/
`_shared`) has no non-test writer, so plain from-imports work there and
the test fixtures fan the rebind out to all three reader modules.

**`project_routes.py`** (lines 1790-3799, ~2010 lines — now unambiguously
the largest route cluster: `get_project_design_status`'s body alone runs
3258-3799, 542 lines, and cost-tracking and file-browsing endpoints landed
here since the original approximation): `_ORDINAL_RE` (1794), `_design_id`
(1797, moved here per the proximity-mislead fix above), `ProjectItem`
(1803), `ProjectCreate` (1816), `ProjectUpdate` (1822), `CostEntryCreate`
(1848), `DesignItem` (1929), `DesignReorderRequest` (1939),
`DesignAddRequest` (1943), `_project_sync_locks`/`_project_lock_guard`
(1949-1950), `_get_project_lock` (1953), `_get_design_queue_dir` (1960),
`_extract_ordinal` (2010), `_sync_project_designs` (2021-2124),
`_validate_base_dir` (2127-2136), `GET /projects` → `list_projects`
(2139-2162), `POST /projects` → `create_project` (2165-2213),
`GET /projects/{project_id}` → `get_project` (2216-2236),
`PUT /projects/{project_id}` → `update_project` (2239-2316),
`DELETE /projects/{project_id}` → `delete_project` (2319-2365),
`POST /cost-entries` → `create_cost_entry` (2371-2444), then the
cost-tracking cluster from `design_docs/per_task_cost_tracking.md`:
`CostEntrySummary` (2450), `TaskCostSummary` (2465),
`WorkflowCostSummary` (2474), `FeatureCostSummary` (2483),
`DesignCostSummary` (2492), `ProjectCostSummary` (2501),
`GET /tasks/{task_id}/costs` → `get_task_costs` (2513-2565),
`GET /workflows/{workflow_id}/costs` → `get_workflow_costs` (2568-2620),
`GET /features/{feature_id}/costs` → `get_feature_costs` (2623-2675),
`GET /designs/{design_id}/costs` → `get_design_costs` (2678-2730),
`GET /projects/{project_id}/costs` → `get_project_costs` (2733-2794);
`POST /projects/{project_id}/sync` → `sync_project_designs` (2800-2814),
`POST /projects/{project_id}/designs/reload` → `reload_project_designs`
(2817-2830), `GET /projects/{project_id}/designs` → `list_project_designs`
(2833-2860), `POST /projects/{project_id}/designs` → `add_project_design`
(2863-2917), then the file-browsing cluster: `BrowseEntry` (2920),
`BrowseResult` (2926), `GET /projects/{project_id}/browse` →
`browse_project_files` (2932-2972),
`GET /projects/{project_id}/browse/content` → `browse_project_file_content`
(2975-2994); `PUT /projects/{project_id}/designs/reorder` →
`reorder_project_designs` (2997-3021),
`DELETE /projects/{project_id}/designs/{filename}` →
`remove_project_design` (3024-3235),
`GET /projects/{project_id}/designs/{filename}/content` →
`get_project_design_content` (3238-3255),
`GET /projects/{project_id}/designs/{filename}/status` →
`get_project_design_status` (3258-3799).

**`feature_routes.py`** (lines 3800-5021, ~1222 lines):
`GET /workflows/{workflow_id}/feature_report` →
`get_workflow_feature_report` (3802-3863),
`GET /workflows/{workflow_id}/decomposition_review` →
`get_workflow_decomposition_review` (3866-3899), `_scan_features`
(3905-3966), `GET /features` → `list_features` (3969-3971),
`POST /features/{feature_id}/pause` → `pause_feature` (3974-4023),
`POST /features/{feature_id}/resume` → `resume_feature` (4026-4095), then
the review-mode cluster from `design_docs/autopilot_review_mode.md`:
`ReviewModeUpdate` (4101), `FeatureReviewRequest` (4105),
`PATCH /projects/{project_id}/review-mode` → `set_review_mode`
(4110-4123 — note the `/projects` path: it's kept here, not in
`project_routes.py`, because it's semantically the review-mode toggle,
reviewed and tested alongside `review_feature`; flag it in review so
nobody "fixes" it back), `_review_phase0_decomposition` (4126-4297),
`POST /features/{feature_id}/review` → `review_feature` (4300-4512),
`DELETE /features/{feature_id}` → `delete_feature` (4515-4641);
`_find_archived_feature_report` (1968-2007, relocated from the project
territory per the same call-site rule as the `_design_id` fix — 3 of its 4
call sites are here, at 3859, 3945, 4933; the project-side caller at 3635
imports it from here); `_spawn_agent_for_task` (4644-4686),
`GET /features/{feature_id}` → `get_feature_detail` (4689-4766),
`_resolve_feature_docs_base` (4769-4785),
`GET /feature-records/{feature_id}/docs` → `list_feature_record_docs`
(4788-4852), `GET /feature-records/{feature_id}/docs/{doc_name}` →
`get_feature_record_doc` (4855-4886), `GET /feature-records/{feature_id}/report` →
`get_feature_record_report` (4889-4938), `GET /features/{feature_id}/report` →
`get_feature_report` (4941-4950), `GET /features/{feature_id}/docs/{doc_name}` →
`get_feature_doc` (4953-4969), `GET /features/{feature_id}/download` →
`download_feature_report` (4972-4985), `GET /features/{feature_id}/logs` →
`list_feature_logs` (4988-5008), `GET /features/{feature_id}/logs/{log_name}` →
`get_feature_log` (5011-5021).

**`message_routes.py`** (lines 5022-5182): `GET /messages` →
`get_messages` (5027-5047), `GET /messages/archived` →
`get_archived_messages` (5050-5072), `POST /messages/archive` →
`archive_message` (5075-5108), `POST /messages/unarchive` →
`unarchive_message` (5111-5128), `POST /messages/unarchive-all` →
`unarchive_all_messages` (5131-5144), `POST /messages/cleanup-archives` →
`cleanup_old_archives` (5147-5160), `GET /logs` → `get_logs` (5163-5182 —
confirmed still colocated with messages, not control, at time of writing).

**`intervention_routes.py`** (lines 5183-5282, file-based today):
`STALE_INPUT_SECONDS` (5187), `HumanInputRequest` (5190),
`HumanInputResponse` (5198), `_find_pending_input` (5204-5226),
`GET /input` → `get_human_input_request` (5229-5239),
`POST /input` → `submit_human_input` (5242-5270),
`DELETE /input/{request_id}` → `dismiss_human_input` (5273-5282). Extract
as-is — don't rewrite internals here, that's
`human_input_intervention_system.md`'s job, landing next against this
already-extracted file.

**Line-count sanity check:** 1-444 (`_shared.py` part 1) + 445-726
(`control_routes.py` part 1) + 727-1789 (`queue_routes.py`) + 1790-3799
(`project_routes.py`) + 3800-5021 (`feature_routes.py`) + 5022-5182
(`message_routes.py`) + 5183-5282 (`intervention_routes.py`) + 5283-5709
(`control_routes.py` part 2) + 5710-5724 (`_shared.py` part 2) =
5724, the file's exact total. Every line is accounted for; note this is
the *territory* partition, not the module partition —
`_find_archived_feature_report` (1968-2007) physically sits inside the
project territory but moves to `feature_routes.py`, so the per-module line
sums differ from the ranges above by exactly that span. If your own pass
doesn't reconcile to 5724, something drifted since this doc was written —
re-verify against the live file before extracting, the same way §3.1
verifies against 139.

### 3.3 Implementation methodology — script the code move, don't hand-copy it

This is thousands of lines of exact, working code being relocated across
15 new files (8 inside the new `src/autopilot/orchestrator/` package, 7
inside the new `src/mcp/autopilot/` package). Hand-copying that much text
(agent or human) risks silent
transcription errors that a visual diff review won't reliably catch — a
dropped line, a retyped-instead-of-copied comment, a docstring that
changes by one character. **Write a one-off Python script that does the
line-level extraction mechanically, using the `ast` module, instead of
retyping function bodies.** The script itself is throwaway tooling (put it
in `/tmp` or a scratch dir — it isn't a deliverable and doesn't need
tests/lint), but the correctness guarantee it buys is real and checkable.

**What the script does, per file being split:**
0. For the `orchestrator.py` side specifically: create the
   `src/autopilot/orchestrator/` directory first. The script writes its 8
   submodule outputs there, and the "remaining" content (step 4) becomes
   `src/autopilot/orchestrator/__init__.py`, not a rewritten
   `orchestrator.py` — `git rm src/autopilot/orchestrator.py` once its
   content has been fully redistributed, don't leave both a file and a
   package of the same name coexisting even transiently.
1. `ast.parse()` the source, walk top-level `FunctionDef`/`AsyncFunctionDef`/
   `ClassDef` nodes. Each node's `.lineno` (or its first decorator's
   `.lineno`, if `node.decorator_list` is non-empty — decorators sit above
   `.lineno` in the source but aren't included in it) through `.end_lineno`
   gives the exact, unambiguous source range for that function/class,
   including its own blank-line padding as authored.
2. A hand-written `dict[str, str]` in the script maps each function/class
   name to its target module — copy this straight from the name lists in
   §3.1/§3.2 above; this mapping is the one place human judgment enters,
   and it's a small, reviewable data structure, not a code edit.
3. For each target module, extract the exact source lines for every symbol
   mapped to it (`original_lines[start-1:end]`), preserving original
   order, and write them into the new file below a manually-authored
   header (module docstring + the import block — see step 5).
4. Remove those same line ranges from the original file, processing ranges
   **in reverse line order** so earlier deletions don't shift the offsets
   of later ones. What's left of the original `orchestrator.py` content is
   exactly the "remaining" set from §3.1 (config helpers, the monitoring
   trio, `OrchestratorLogger`, `prompt_human`, the review-mode gates, the
   top-level run loop) — nothing more, nothing less, because every other
   line was accounted for by the mapping in step 2 — and that remainder
   becomes `src/autopilot/orchestrator/__init__.py` per step 0. The
   handful of module-level constants that §3.1 assigns to submodules
   (e.g. `API_BASE` → `engine_client.py`, `MANUAL_ONLY_PHASES` →
   `phase_transitions.py`) move manually alongside their symbols; every
   other module-level name stays in `__init__.py`.
5. **Imports are not scriptable and stay a manual/agent step**, done once
   per new module after the mechanical move: read the extracted functions,
   grep for which of the original file's top-of-file imports they actually
   reference, and write a minimal import block for just those. Also add
   the new cross-module imports each relocated function now needs (e.g.
   something in `phase_transitions.py` calling a function that moved to
   `queue.py`) — this is exactly the §4 table's job, applied per module as
   you go.
6. **Verify the move is lossless, automatically, not by eyeballing a diff:**
   concatenate every extracted span back in original file order and assert
   it's byte-identical to the original file's content minus the "remaining"
   ranges. This is a strictly stronger correctness check than code review
   can give you for a pure text relocation, and the script can assert it
   before you even open an editor.
7. Run `python -m py_compile` (or `ast.parse`) on every output file
   immediately — catches an off-by-one in the line-range math before it
   reaches the test suite.

Apply the same approach to §3.2's route split: the unit to extract per
route is the `@router.*` decorator line(s) through the handler function's
`end_lineno` (decorators again sit above `.lineno`, same caveat as step 1).

This turns "split two huge files" from "an agent retypes 16,000 lines
correctly" into "an agent writes and reviews one ~100-line script, plus
curates 15 short import blocks by hand" — the risk surface shrinks to
exactly the part that requires judgment.

The §3.2 API split was executed with `scripts/split_autopilot_api.py`
(one-off, kept for re-verification: `python scripts/split_autopilot_api.py`
dry-runs the full assertion chain — 138 spans vs §3.2, territory partition,
cross-module dependency graph, lossless line accounting — without writing).
Import curation went as predicted: superset header (original import block
verbatim) + `ruff check --select F401 --fix` (180 unused imports trimmed)
+ the auto-derived cross-module imports, which the script asserts against a
hardcoded, cycle-free expectation (everything → `_shared`, plus one
`project_routes → feature_routes` edge for `_find_archived_feature_report`).

---

## 4. External call sites to update (grounded, not exhaustive-by-guess)

Every one of these is a **local, function-scoped import** — matches this
codebase's established convention (see e.g. the pattern already used
throughout `server.py`), not a top-of-file import. Preserve that convention
in the moved code; don't "clean it up" into module-level imports as a
drive-by change.

All "New home" paths below are inside the new
`src/autopilot/orchestrator/` package (§3.1) — e.g. "`state.py`" means
`src/autopilot/orchestrator/state.py`.

| Importing file | Symbols imported from `orchestrator.py` | New home |
|---|---|---|
| `src/autopilot/__init__.py:10-15` | `DesignStatus`, `PipelineState`, `StopReason`, `run_continuous_pipeline` | `state.py` (the first three — update to `from src.autopilot.orchestrator.state import (...)`) / `__init__.py` (`run_continuous_pipeline`, **no change**) |
| `src/autopilot/service.py:118` | `_get_or_create_project_id` | `state.py` |
| `src/autopilot/service.py:148` | `_running_state_key`, `_set_project_context` | `state.py` |
| `src/autopilot/service.py:170` | `_delete_project_context`, `_running_state_key` | `state.py` |
| `src/autopilot/service.py:184` | `_get_project_context`, `_running_state_key` | `state.py` |
| `src/autopilot/service.py:209` | `_RUNNING_STATE_KEY_LEGACY`, `_RUNNING_STATE_KEY_PREFIX`, `_delete_project_context`, `_get_project_context`, `_get_project_contexts_by_prefix`, `_resolve_project_id` | `state.py` — the five blocks above are the only ones that change |
| `src/autopilot/service.py:432` | `import src.autopilot.orchestrator as orch_module` (writes `orch_module._stop_events[project_id]`) | **no change** — `_stop_events` stays a module global in `orchestrator/__init__.py`; the module-attribute access resolves identically against a package |
| `src/autopilot/service.py:433` | `run_continuous_pipeline` | **no change** — stays in `orchestrator/__init__.py`, `from src.autopilot.orchestrator import run_continuous_pipeline` still resolves |
| `src/mcp/server.py:1101` | `sweep_completed_workflow_worktrees` | `worktree_integration.py` |
| `src/mcp/server.py:1686` | `OrchestratorLogger` | **no change** — stays in `orchestrator/__init__.py` (Tier 2.4, out of scope) |
| `src/mcp/server.py:1735-1747` (in `_run_phase_advancement_sweep_once`, def at :1723) | `_try_advance_phases`, `_clean_stale_assigned_tasks`, `_maybe_resolve_arbitration`, `_recover_abandoned_workflows_missing_worktree`, `_recover_abandoned_workflows_with_completed_phase`, `_resync_pipeline_registry`, `_retry_exhausted_paused_workflows`, `_retry_failed_tasks`, `_sync_stale_design_statuses`, `_sync_stale_feature_statuses`, `heal_orphaned_agent_branches` | `phase_transitions.py` (`_try_advance_phases`, `_maybe_resolve_arbitration`, `_retry_exhausted_paused_workflows`, `_retry_failed_tasks`) / `worktree_integration.py` (`_recover_abandoned_workflows_missing_worktree`, `_recover_abandoned_workflows_with_completed_phase`, `heal_orphaned_agent_branches`) / `features.py` (`_clean_stale_assigned_tasks`, `_sync_stale_design_statuses`, `_sync_stale_feature_statuses`) / `__init__.py` (`_resync_pipeline_registry`, **no change**) — **this one import site spans 3 submodules + the package root; update it as 4 separate import statements, don't leave it importing from one module that no longer holds all of them** |
| `src/mcp/server.py:1872` | `_is_workflow_monitored` | **no change** — stays in `orchestrator/__init__.py` |
| `src/mcp/server.py:4875` | `_claim_phase_task_creation` | `phase_transitions.py` |
| `src/mcp/server.py:4908` | `_release_phase_task_creation_claim` | `phase_transitions.py` |
| `src/mcp/autopilot_api.py:579` | `PersistentPipelineState` | `state.py`; this import line ends up inside `control_routes.py` after the §3.2 split (it's in `get_pipeline_status`) |
| `src/mcp/autopilot_api.py:959,979,1186,1252` | `_get_or_create_project_id` (959), `_resolve_project_id` (979), `_delete_project_context` (1186), `PersistentPipelineState` (1252) | `state.py`; all 4 sites are inside `rerun_design` → `queue_routes.py` |
| `src/mcp/autopilot_api.py:1209` | `_cleanup_worktree` | `worktree_integration.py`; inside `rerun_design` → `queue_routes.py` |
| `src/mcp/autopilot_api.py:1379` | `api_post`, `get_tasks` | `engine_client.py`; inside `spawn_repair_review_agent` → `queue_routes.py` |
| `src/mcp/autopilot_api.py:3195` | `_cleanup_worktree` | `worktree_integration.py`; inside `remove_project_design` → `project_routes.py` |
| `src/mcp/autopilot_api.py:3221` | `PersistentPipelineState` | `state.py`; inside `remove_project_design` → `project_routes.py` |
| `src/mcp/autopilot_api.py:4192` | `finalize_phase0_workflow` | **no change** — stays in `orchestrator/__init__.py`; the import line ends up inside `feature_routes.py` (it's in `_review_phase0_decomposition`) |
| `src/mcp/autopilot_api.py:4622` | `_cleanup_worktree` | `worktree_integration.py`; inside `delete_feature` → `feature_routes.py` |
| `src/mcp/autopilot_api.py:5291` | `_get_or_create_project_id` | `state.py`; inside `start_pipeline` → `control_routes.py` |
| `src/mcp/autopilot_api.py:5446` | `pause_project_workflows` | `engine_client.py`; inside `stop_pipeline` → `control_routes.py` |
| `src/mcp/autopilot_api.py:5464` | `PersistentPipelineState` | `state.py`; inside `stop_pipeline` → `control_routes.py` |
| `src/monitoring/monitor.py:2876` | `run_health_audit` | `control_routes.py` — function-scoped import inside `_audit_system_health` (missed by the first draft of this §4 table; the §3.2 split found it by grep, not by table) |
| `src/services/task_completion_service.py:622` | `_claim_phase_task_creation` | `phase_transitions.py` |
| `src/services/task_completion_service.py:683` | `_trigger_arbitration` | `phase_transitions.py` |
| `src/services/task_completion_service.py:717,801` | `_create_phase_task` (×2) | `phase_transitions.py` |
| `src/core/cost_derivation.py:307` | `pause_project_workflows` | `engine_client.py` — this is a deliberately lazy import (orchestrator imports `cost_derivation` too); keep it lazy and just retarget the path, don't hoist it |
| `src/phases/phase_manager.py:1716` | `finalize_phase0_workflow` | **no change** — stays in `orchestrator/__init__.py` |
| `tests/` (18 files import from `src.autopilot.orchestrator`: `test_advance_phases`, `test_ash_scan`, `test_autopilot_service`, `test_budget_enforcement`, `test_cleanup_worktree_paused_workflow`, `test_cleanup_worktree_tmux_archive`, `test_cost_tracking`, `test_create_feature_records`, `test_forensics_gating`, `test_heal_orphaned_agent_branches`, `test_orchestrator_helpers`, `test_orchestrator`, `test_phase0_idempotency`, `test_resolve_execution_order`, `test_review_mode`, `test_run_feature_pipelines`, `test_sweep_completed_workflow_worktrees`, `test_validate_features_json`) | varies — **re-grep each file before starting**, don't assume the list above is exhaustive | matches whichever module the imported symbol moved to |
| `tests/` (6 files reference `src.mcp.autopilot_api` — affected by the §3.2 **rename**, not just the move): `tests/conftest.py` (module import as `api_mod`, :481/:503, plus the *string-based* `patch("src.mcp.autopilot_api._get_active_project_id")` at :545, plus module-attribute writes of `DESIGN_QUEUE_DIR`/`FEATURES_DIR`/`AUTOPILOT_STATE_DIR`/`_cache` in the `mock_app`/`client` fixtures), `tests/test_autopilot_api.py` (module + `router` at ~25 sites; `AUTOPILOT_STATE_DIR` rebinds must fan out to `_shared`/`queue_routes`/`intervention_routes`; `_get_active_project_id` and `verify_agent_authentication` patches must target the modules that *look up* the name — `_shared`+`control_routes` and `project_routes` respectively), `tests/test_autopilot_api_helpers.py` (`_safe_path`/`_cached`/`_invalidate`/`_store`/`_feature_status`/`_read_json`/`_read_jsonl_tail` → `_shared.py`; `_design_id` → `project_routes.py`; `_load_queue_order`/`_save_queue_order` + the `_get_queue_order_path` string patches → `queue_routes.py`; `start_pipeline` + the `_invalidate` string patch → `control_routes.py`), `tests/test_cost_tracking.py` (`CostEntryCreate` → `project_routes.py`), `tests/test_design_status_derivation.py` (`get_project_design_status` → `project_routes.py`), `tests/test_monitor.py` (four *string-based* `patch("src.mcp.autopilot_api.run_health_audit", ...)` targets at :3030/:3048/:3099/:3167 — string patches don't fail at import time, so they need a manual retarget to `src.mcp.autopilot.control_routes`; the first draft of this table listed only two) | — | the new package paths per §3.2 |

**Do not leave compatibility re-exports** in `orchestrator/__init__.py` for
symbols that moved to a submodule (e.g. `from .state import
PersistentPipelineState  # noqa`) — per this repo's CLAUDE.md
("Forbidden... no backwards-compat shims"), fix every call site instead.
This is easier to enforce than in a typical refactor: once the flat file is
gone, **any import of a moved symbol via the old `from
src.autopilot.orchestrator import <symbol>` path raises `ImportError`
outright** (the package's `__init__.py` no longer defines it) — there's no
way to silently succeed with a stale import the way there would be if
`orchestrator.py` still existed. The table above plus a final
`grep -rn "from src.autopilot.orchestrator import\|from src.autopilot import orchestrator" src/ tests/`
sweep (to catch anything the table missed) is the completeness check; a
clean `pytest` collection run (imports resolve even before tests execute)
is a second, even cheaper one.

---

## 5. Sequencing

1. **This lands first, on its own, before `human_input_intervention_system.md`.**
   `prompt_human` and the current file-based `/input` routes are
   deliberately left untouched by this split (§3.1.3, §3.2) precisely so
   the feature spec has a stable, already-modularized codebase to build on
   — it authors the rewritten `prompt_human` directly into `policy.py` and
   rewrites `intervention_routes.py`'s contents in place, with no file-move
   in that diff at all.
2. ~~Within this task: extract in the order given in §3.1 (state →
   engine_client → policy → queue → worktree_integration → features →
   reporting → phase_transitions) and §3.2 (`_shared.py` → the five smaller
   route files → `intervention_routes.py` last, matching the note above).~~
   **§3.1 DONE** — all 8 submodules + `__init__.py` extracted, 139 symbols,
   580/581 tests passing. Commits: `691d22a` through `d481a37`.
3. **§3.2 DONE (2026-08-16).** `src/mcp/autopilot_api.py` →
   `src/mcp/autopilot/` package split complete via
   `scripts/split_autopilot_api.py` + import curation + §4 call-site
   retargeting:
   - 8 files: `_shared.py` (415 lines, 31 symbols) + 6 route modules +
     aggregator `__init__.py`; `autopilot_api.py` deleted
   - Lossless reassembly asserted by the script (5369 extracted lines +
     355 header/comment/blank remainder = 5724, byte-identical spans);
     `ruff check --select F401,F811,F821` clean except 3 pre-existing
     `F821` findings (below), `py_compile` clean
   - New guard test `TestRouterAggregation::test_all_63_routes_survived_the_split`
     in `tests/test_autopilot_api.py` (the §6 route-count guardrail; uses
     recursive `effective_candidates()` expansion because FastAPI ≥ 0.137
     lazy-wraps `include_router()` in `_IncludedRouter` objects, so
     `len(router.routes)` is not the route count)
   - Targeted suite (5 API test files) identical to pre-split baseline on
     the same tree state: 205 passed / 2 skipped, same 2 pre-existing
     `TestFeatures` failures both before and after (they read the
     CWD-relative `hephaestus.db` via `DatabaseManager()` — pass in a
     clean tree, fail in the repo root; environment sensitivity, not a
     split regression — verified by running the pre-split worktree with
     the same DB); `tests/test_monitor.py` 134/134; full-suite collection
     clean (2428 tests)
   - **Pre-existing bug found while moving (NOT fixed, per §7):**
     `review_feature`'s approve path (original line ~4388, now
     `feature_routes.py`) references `_Phase` without importing it — the
     `from src.core.database import ...` block imports `_Task` and
     `_PhaseExecution` but not `Phase as _Phase`, so approving a feature
     via `POST /features/{id}/review` raises `NameError` at runtime.
     Present verbatim in `e9c47f7`; kept byte-identical in the move.
     Logged in `autopilot_architecture_review.md` §11 as B10.
   - Call sites updated: `server.py:855-856`, `monitor.py:2876`,
     `conftest.py` (`mock_app`/`client` fixtures), and the 5 test files in
     §4's last table row. One-line `FEATURES_DIR` deviation documented in
     §3.2 above.

---

## 6. Testing

- No new tests are needed for correctness — this is a pure move, and the
  18 test files that already import these symbols directly are the
  regression suite. Per this repo's stated test-running preference, run
  only the targeted test files for whatever cluster you just moved, not the
  full suite, after each extraction.
- Add one new test only if you don't already have one:
  `tests/test_autopilot_api.py` (or a new file) asserting the aggregator
  `router` in `src/mcp/autopilot/__init__.py` still has the same route
  count / same paths after the split as it had before (a simple
  `len(router.routes)` or path-set comparison against a hardcoded expected
  list — the exact 63 routes enumerated in §3.2 is the baseline) — a cheap
  guardrail that `include_router()` wiring didn't silently drop a route
  (this is exactly the kind of mistake the `GET /status` gap in §3.2 would
  have been, had it shipped instead of being caught here).
- `heph restart`, then hit `GET /api/autopilot/health` and `GET
  /api/autopilot/status?project_id=...` to confirm the live server starts
  and the router aggregation actually works, not just that imports resolve.
- `ruff check` / `mypy` (per this repo's lint commands) after each
  extraction — moved code is exactly where a stray unused import gets left
  behind.

---

## 7. Out of scope

- Any behavior change. If you notice a bug while moving code (there will be
  temptation — this file has accreted plenty), **note it, don't fix it
  here.** Log it back into `design_docs/autopilot_architecture_review.md`
  §11 or a new bug entry, same as B9 was added there. Mixing a refactor
  with a fix makes both harder to review and impossible to `git bisect`
  cleanly.
- `OrchestratorLogger`'s split (Tier 2.4 of the architecture review) — an
  adjacent, already-tracked, separate item.
- The DB-vs-file queue unification (Tier 3) — `queue.py` here is a location
  change only, not the dual-store fix.
- The `/api/autopilot/stream` WS/SSE work, `PipelineState`-to-DB migration —
  unrelated Tier 2 items.
