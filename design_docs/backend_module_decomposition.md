# Backend Module Decomposition — Design Document

**Status:** Ready for implementation — **do this first**
**Verified against:** `src/autopilot/orchestrator.py` @ commit `e9586a2`
(8372 lines, 117 top-level symbols) and `src/mcp/autopilot_api.py` @ commit
`b5f55f6` (3980 lines, 99 def/class + 52 routes) — every line number in
§3.1/§3.2 and every cross-file import in §4 was re-diffed against these
exact commits, not carried over from an earlier pass. (Both files moved at
least once *while this doc was being written* — a real, unrelated commit
landed on each mid-review, confirmed via diff each time to be a pure
line-shift with no symbols added, removed, or reordered. This is exactly
the scenario §3.3 already warns about: **re-run `grep`/`ast.parse()`
against the live file immediately before extracting, regardless of how
recently this doc was updated** — the symbol → module name mapping is
what's authoritative if the two ever disagree again.)
**Scope:** Convert `src/autopilot/orchestrator.py` (8372 lines, currently a
single flat file) into a `src/autopilot/orchestrator/` **package** — 8
submodules plus a driver in `__init__.py` — and split
`src/mcp/autopilot_api.py` (3980 lines) into the smaller, single-responsibility
modules `design_docs/autopilot_architecture_review.md` §7/§4.2/Tier 5.3
already proposed. This is cleanup, not a feature — no behavior change is in
scope.

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
proposed a Tier 5.3 split. As of 2026-07-21 that split **has not happened for
the backend** — both files grew instead:

| File | At review (2026-06-19) | Now (2026-07-21) |
|---|---|---|
| `src/autopilot/orchestrator.py` | ~2300 lines | **8372 lines**, 117 top-level `def`/`class` |
| `src/mcp/autopilot_api.py` | ~2560 lines | **3980 lines**, 52 route handlers |

The frontend half of the same Tier 5.3 item **did** happen —
`frontend/src/pages/Autopilot.tsx` went from ~3200 lines to 413, with 11
components extracted to `frontend/src/components/autopilot/`. That's the
existence proof this kind of split is tractable here and the tests/review
discipline supports it; the backend just never got the same treatment.

Every unrelated feature that has landed since (multi-project concurrency,
worktree isolation, the spec gate, credit/session-limit handling, arbitration,
recovery) went into these same two files because there was nowhere smaller to
put it. That's the compounding cost of not splitting: every new PR's diff
context is "one function in an 8000-line file," and unrelated logic
(design-queue scanning next to phase-transition arbitration next to HTML
report generation) sits in the same namespace with no import boundary to
signal what depends on what.

**Goal:** extract cohesive, already-visible clusters of functions into their
own modules, with zero behavior change, verified by the existing test suite
(which already imports most of these functions directly — see §4) staying
green throughout.

---

## 2. What already exists (don't recreate)

Tier 2/5 work already extracted three of the modules the original proposal
called for — confirm these exist before starting and build *around* them,
not duplicate them:

- `src/autopilot/service.py` — `AutopilotService`/`AutopilotServiceRegistry`
  (lifecycle: start/stop/status, per-project). Already thin; calls into
  `orchestrator.run_continuous_pipeline` via `run_in_executor`. That one
  import needs no change — `run_continuous_pipeline` stays in the package's
  `__init__.py`, so `from src.autopilot.orchestrator import
  run_continuous_pipeline` still resolves after the conversion. Its other
  `from src.autopilot.orchestrator import (...)` blocks (lines ~118, 148,
  170, 184, 209) import symbols that *do* move into submodules (`state.py`)
  — those need `from src.autopilot.orchestrator.state import (...)` instead
  (see §4's table). Any import left pointing at the old flat-module path
  for a moved symbol will raise `ImportError` immediately once the package
  replaces the file — a cheap, automatic completeness check for this step.
- `src/autopilot/spec.py` — the hybrid spec gate (§9.1). Already separate.
  Not touched.
- `src/autopilot/phases.py` — phase prompts/config (consolidated from the
  old `phase_1..10_*.py` files). Already separate. Not touched.
- `src/autopilot/report_generator.py` — **has a problem worth fixing in
  passing**: its `generate_feature_report()` function has **zero callers**
  anywhere in `src/` or `tests/` (confirmed by grep). `orchestrator.py`'s
  own `generate_html_feature_report()` (line 3554) independently grew a
  Jinja2-template code path (`templates/feature_report.html`) that does the
  real work today. The original architecture review's C5.2 said "converge
  with report_generator.py" — that never happened; instead one became dead
  code and the other absorbed the Jinja2 work in place. When extracting the
  reporting cluster (§3.1.6 below), delete `report_generator.py`'s dead
  `generate_feature_report()` rather than moving it — don't carry
  known-dead code into the new module structure.

---

## 3. Design

### 3.1 `src/autopilot/orchestrator.py` → `src/autopilot/orchestrator/` package (8 submodules + `__init__.py`)

This is the exhaustive symbol → module mapping, built by walking
`orchestrator.py`'s full top-level `def`/`class` list (`grep -n "^def \|^class
\|^async def " src/autopilot/orchestrator.py`, 117 symbols) one at a time —
not a line-range approximation. **Line ranges turned out to be misleading
here**: several functions are physically interleaved with a different
cluster's code (e.g. `_ensure_git_excluded`/`_run_ash_scan`, worktree
concerns, sit numerically in the middle of the phase-transition block;
`_retry_failed_tasks`, phase-transition-coupled by the real import at
`server.py:1334`, sits numerically up near the recovery-detection
functions). Use the table below, not proximity in the file, as ground
truth — it's also exactly the `dict[str, str]` the extraction script in
§3.3 needs, so it can be copied in directly.

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
(+ its module constants `_RUNNING_STATE_KEY_LEGACY`/`_RUNNING_STATE_KEY_PREFIX`),
`_workflow_belongs_to_project`. No dependency on anything else being
extracted — safe to do first. (15 symbols)

**2. `engine_client.py`** — "talk to the backend/LiteLLM"
I/O helpers: `get_litellm_config`, `file_hash`, `api_get`, `api_post`,
`update_task_status`, `increment_task_retry_count`, `terminate_agent_direct`,
`pause_workflow_direct`, `complete_workflow_direct`, `fail_workflow_direct`,
`create_agent_for_task_direct`, `_update_orchestrator_status`, `get_tasks`,
`get_agents`, `peek_agent_output`, `get_task_progress`, `get_workflow_status`,
`get_active_workflows`. The architecture review's C1.2 proposal, just
delayed. Depends only on stdlib + the DB/API layer; safe to extract second.
(18 symbols)

**3. `policy.py`** — stuck/health/credit detection and
recovery decisions: `_workflow_appears_abandoned`,
`_update_resumed_workflow_recovery_attempts`, `_escalate_stale_active_workflows`,
`attempt_recovery`, `check_api_credits`, `detect_hard_error`,
`detect_impasse`, `detect_architectural_issue`. **`prompt_human` is
deliberately not in this list** — leave it in the package's `__init__.py`
for now; it gets authored fresh, directly in this module, by
`human_input_intervention_system.md` (which lands after this split).
Moving the current file-mailbox version here first is a pointless
intermediate commit. (8 symbols)

**4. `queue.py`** — design-queue scanning/picking/status,
nothing else: `scan_design_queue`, `pick_next_design`, `_assess_run_health`,
`is_design_fully_complete`, `_update_design_status`, `_set_workflow_type`,
`_get_phase0_completion`. Purely a location change — does **not** touch the
DB-vs-file dual-store problem (Tier 3 of the architecture review, still
separately unresolved); don't conflate the two. (7 symbols)

**5. `worktree_integration.py`** — pipeline-level worktree/git
orchestration (§9.6's integration worktrees, ASH security scanning), distinct
from the generic `WorktreeManager` class in `src/core/worktree_manager.py`
(calls into it, doesn't duplicate it): `create_feature_folder`,
`copy_design_document`, `_create_integration_worktree`, `_cleanup_worktree`,
`_create_designs_folder`, `_recover_abandoned_workflows_missing_worktree`,
`_ensure_git_excluded`, `_run_ash_scan`. (8 symbols)

**6. `features.py`** — Feature-Model DB record bookkeeping
(split out of the original `reporting.py` sketch — see above):
`_create_feature_records`, `_update_feature_status`,
`_sync_stale_feature_statuses`, `_link_workflow_to_feature`,
`_relink_features_to_workflows`, `_clean_stale_assigned_tasks`,
`_validate_features_json`, `_resolve_execution_order`, `_sweep_stray_files`.
(9 symbols)

**7. `reporting.py`** — pure report/artifact generation, no DB
writes: `_report_path`, `collect_report_summaries`, `collect_files_created`,
`generate_html_feature_report`, `generate_product_validation_report`,
`_generate_design_report_html`, `_empty_report`. Delete
`report_generator.py`'s dead `generate_feature_report()` in this same PR
(§2 above) rather than leaving two unconverged report generators. (7 symbols)

**8. `phase_transitions.py`** — the actual control-loop
engine (goto/retry/continue state machine, arbitration, phase-task
creation); do this one **last**, after the other seven have proven the
process — it's the largest (28 symbols) and most externally-depended-on
cluster (`server.py:1334`'s `_run_phase_advancement_sweep_once` pulls 7 of
these symbols in one import statement):
`_retry_failed_tasks`, `_retry_exhausted_paused_workflows`,
`_update_orchestrator_max_gotos`, `_advance_phases`,
`_try_auto_resume_paused_workflow`, `_release_stale_task_creation_claims`,
`_release_pending_phases_with_done_tasks`, `_get_phase_statuses`,
`_claim_phase_task_creation`, `_release_phase_task_creation_claim`,
`_case_start_first_phase`, `_case_in_progress_no_tasks`,
`_case_completed_with_successor`, `_case_in_progress_complete`,
`_maybe_retry_failed_tasks`, `_fire_phase_transition`,
`_gather_arbitration_context`, `_build_arbitration_prompt`,
`_trigger_arbitration`, `_maybe_resolve_arbitration`,
`_read_arbitration_result`, `_resolve_arbitration_outcome`,
`_cap_out_review_phase`, `_create_phase_task`, `_create_corrective_task`,
`_wait_for_task_terminal`, `_negotiate_validation_fix`,
`_resume_stuck_workflow_tasks`. (28 symbols)

**Remaining in `src/autopilot/orchestrator/__init__.py` after all of the
above (17 symbols, roughly 1000-1500 lines):** the config/timeout helpers
`_get_workflow_timeout`, `_get_phase0_timeout`,
`_get_paused_workflow_retry_cooldown_seconds`,
`_get_paused_workflow_max_retry_cycles`; `OrchestratorLogger` (leave in
place — Tier 2.4 of the architecture review is a separate, already-tracked
item; don't fold its split into this one); `prompt_human` (see §3.1.3); and
the top-level run loop: `run_single_workflow`, `run_phase0`,
`_run_one_feature`, `run_feature_pipelines`, `run_design_aggregate`,
`_archive_and_cleanup`, `run_single_design`, `_should_stop`,
`_register_orchestrator_agent`, `run_continuous_pipeline`, `main`. Putting
the driver directly in `__init__.py` (rather than e.g. `driver.py` plus a
re-exporting `__init__.py`) means every external call site that only ever
touched these 17 symbols — `service.py`'s `run_continuous_pipeline` import
chief among them — needs **zero import changes**, since
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

**Symbol-count sanity check:** 15 + 18 + 8 + 7 + 8 + 9 + 7 + 28 + 17 = 117,
matching the full top-level symbol count. If your own `ast.parse()` pass in
the extraction script comes up with a different total, something in this
table is stale (the file has moved on since this doc was written) or your
grep pattern caught something this one didn't (e.g. a symbol defined with
an unusual decorator) — reconcile before running the extraction, don't
paper over the mismatch.

### 3.2 `src/mcp/autopilot_api.py` → `src/mcp/autopilot/` package (shared module + 6 route files)

Same discipline as §3.1: this is an exact, exhaustive symbol → file mapping
verified line-by-line against the current 3980-line file (`grep -n "^def
\|^class \|^async def \|^@router\." src/mcp/autopilot_api.py`, cross-checked
with targeted `grep`s for where ambiguous helpers are actually called from),
not the line-range approximation an earlier pass of this doc used. That
approximation had **one real gap and two proximity-misleads**, all fixed
below:

- **Gap:** `GET /status` (`get_pipeline_status`, lines 333-544) fell in the
  no-man's-land between `_shared.py`'s end (332) and the queue cluster's
  first route decorator (574) and was never assigned to any of the 6 files.
  Missing it would have silently dropped the pipeline's main status
  endpoint from the split entirely.
- **Proximity-mislead 1:** `configure_autopilot_api` sits at the very
  bottom of the file (3971-3980) but mutates `DESIGN_QUEUE_DIR`,
  `FEATURES_DIR`, `_active_project_id_cache` — module-level globals
  declared at the very top (lines 35-37). It belongs in `_shared.py` with
  the globals it owns, not in whichever route file happens to occupy the
  file's tail end.
- **Proximity-mislead 2:** `_design_id` (line 1490) sits numerically at the
  boundary of the queue cluster, but grepping its 3 call sites (1663, 1692,
  2066) shows every one is inside `project_routes.py`'s territory — it
  belongs there, not with queue.

Every route file defines its **own local** `router = APIRouter()` (not a
shared import) — the existing `@router.get(...)`/`@router.post(...)`
decorator lines then move verbatim with their function bodies and need no
rewriting, since the variable name `router` still resolves locally in each
new file. `src/mcp/autopilot/__init__.py` is the aggregator: one
`router = APIRouter(prefix="/api/autopilot", tags=["Autopilot"])` that
`include_router()`s each of the six route modules' own router instances.
`src/mcp/server.py`'s mount point changes from
`from src.mcp.autopilot_api import router as autopilot_router` to
`from src.mcp.autopilot import router as autopilot_router` (and
`configure_autopilot_api` from `src.mcp.autopilot._shared`) — this one
**does** need an import-path edit, unlike the orchestrator package's
`__init__.py` trick, because the module is being renamed
(`autopilot_api` → `autopilot`), not just relocated in place.

**`_shared.py`** (lines 1-332, plus `configure_autopilot_api` relocated
from 3971-3980): the module-level constants `ALLOWED_EXTENSIONS` (39,
used by queue/project/status routes), `PHASE0_DEFINITION_IDS` (48) and
`DESIGN_WORKFLOW_DEFINITION_IDS` (49, used by status/project/feature
routes), `DESIGN_QUEUE_DIR`/`FEATURES_DIR`/`_active_project_id_cache`
(35-37); the cross-cutting helpers `_get_active_project_id` (52),
`_invalidate_project_dirs` (61), `_get_effective_queue_dir` (73),
`_get_effective_features_dir` (111), `_cached`/`_store`/`_invalidate` (158,
168, 173 — the response-cache layer), `_safe_path` (181), `_feature_status`
(197), `_get_latest_run_dir` (208), `_read_json` (216), `_read_jsonl_tail`
(223); the Pydantic models `DesignQueueItem` (245), `DesignQueueAdd` (253),
`FeatureSummary` (259), `FeatureDetail` (272), `PipelineStatus` (301),
`MessageItem` (324); and `configure_autopilot_api` itself. Every other
route module imports from here; extract first.

**`control_routes.py`** (lines 333-544, then 3525-3970 — not contiguous,
see the gap fix above): `GET /status` → `get_pipeline_status` (333-544),
`POST /start` → `start_pipeline` (3525-3552), `_start_pipeline_reserved`
(3553-3644), `POST /stop` → `stop_pipeline` (3645-3759),
`POST /cleanup-branches` → `cleanup_branches` (3760-3775), `GET /health` →
`get_system_health` (3776-3781), `run_health_audit` (3782-3970, a ~190-line
helper — don't overlook it as "just the route body," it's a substantial
standalone health-check routine). This is the surface that calls into
`AutopilotServiceRegistry` — keep it thin where it already is, don't add
logic while moving it.

**`queue_routes.py`** (lines 545-1489, ~945 lines — the largest route
cluster): `_get_queue_order_path` (545), `_load_queue_order` (557),
`_save_queue_order` (567), `GET /queue` → `list_design_queue` (574-614),
`QueueReorderRequest` (615), `POST /queue/reorder` → `reorder_queue`
(619-640), `POST /queue/requeue` → `requeue_design` (641-720),
`POST /queue/rerun` → `rerun_design` (721-1019), `POST /queue/repair` →
`repair_design` (1020-1054), `spawn_repair_review_agent` (1055-1186),
`_run_repair` (1187-1293), `GET /queue/repair/{repair_id}` →
`get_repair_status` (1294-1319), `DesignAddByPath` (1320),
`POST /designs/add` → `add_design_by_path` (1325-1423),
`POST /queue` → `add_to_queue` (1424-1458),
`DELETE /queue/{filename}` → `remove_from_queue` (1459-1472),
`GET /queue/{filename}/content` → `get_queue_item_content` (1473-1489).
Note the repair flow (1020-1293, ~270 lines) lives here even though the
architecture review's §11.1 says it was "slimmed to workflow recovery
only" — it's still a substantial fraction of this cluster, not the small
tail the wording implies.

**`project_routes.py`** (lines 1490-2702, ~1213 lines — larger than the
original approximation suggested, because `get_project_design_status`'s
body alone runs 2293-2702): `_design_id` (1490, moved here per the
proximity-mislead fix above), `ProjectItem` (1496), `ProjectCreate` (1509),
`ProjectUpdate` (1515), `CostEntryCreate` (1522), `DesignItem` (1565),
`DesignReorderRequest` (1575), `DesignAddRequest` (1579),
`_get_project_lock` (1589), `_get_design_queue_dir` (1596),
`_extract_ordinal` (1604), `_sync_project_designs` (1615-1720),
`_validate_base_dir` (1721-1732), `GET /projects` → `list_projects`
(1733-1758), `POST /projects` → `create_project` (1759-1798),
`GET /projects/{id}` → `get_project` (1799-1821),
`PUT /projects/{id}` → `update_project` (1822-1888),
`DELETE /projects/{id}` → `delete_project` (1889-1929),
`POST /cost-entries` → `create_cost_entry` (1930-1973),
`POST /projects/{id}/sync` → `sync_project_designs` (1974-1990),
`POST /projects/{id}/designs/reload` → `reload_project_designs`
(1991-2006), `GET /projects/{id}/designs` → `list_project_designs`
(2007-2036), `POST /projects/{id}/designs` → `add_project_design`
(2037-2093), `PUT /projects/{id}/designs/reorder` →
`reorder_project_designs` (2094-2120),
`DELETE /projects/{id}/designs/{filename}` → `remove_project_design`
(2121-2271), `GET /projects/{id}/designs/{filename}/content` →
`get_project_design_content` (2272-2291),
`GET /projects/{id}/designs/{filename}/status` →
`get_project_design_status` (2292-2702).

**`feature_routes.py`** (lines 2705-3263): `GET /workflows/{id}/feature_report`
→ `get_workflow_feature_report` (2705-2733), `_scan_features` (2734-2778),
`GET /features` → `list_features` (2779-2783), `POST /features/{id}/pause`
→ `pause_feature` (2784-2835), `POST /features/{id}/resume` →
`resume_feature` (2836-2907), `_spawn_agent_for_task` (2908-2951),
`GET /features/{id}` → `get_feature_detail` (2952-3031),
`_resolve_feature_docs_base` (3032-3050),
`GET /feature-records/{id}/docs` → `list_feature_record_docs` (3051-3117),
`GET /feature-records/{id}/docs/{doc_name}` → `get_feature_record_doc`
(3118-3151), `GET /feature-records/{id}/report` →
`get_feature_record_report` (3152-3179), `GET /features/{id}/report` →
`get_feature_report` (3180-3191), `GET /features/{id}/docs/{doc_name}` →
`get_feature_doc` (3192-3208), `GET /features/{id}/download` →
`download_feature_report` (3209-3224), `GET /features/{id}/logs` →
`list_feature_logs` (3225-3247), `GET /features/{id}/logs/{log_name}` →
`get_feature_log` (3248-3263).

**`message_routes.py`** (lines 3264-3423): `GET /messages` →
`get_messages` (3264-3286), `GET /messages/archived` →
`get_archived_messages` (3287-3311), `POST /messages/archive` →
`archive_message` (3312-3347), `POST /messages/unarchive` →
`unarchive_message` (3348-3367), `POST /messages/unarchive-all` →
`unarchive_all_messages` (3368-3383), `POST /messages/cleanup-archives` →
`cleanup_old_archives` (3384-3399), `GET /logs` → `get_logs` (3400-3423 —
confirmed still colocated with messages, not control, at time of writing).

**`intervention_routes.py`** (lines 3424-3524, file-based today):
`STALE_INPUT_SECONDS` (3424), `HumanInputRequest` (3427),
`HumanInputResponse` (3435), `_find_pending_input` (3441-3465),
`GET /input` → `get_human_input_request` (3466-3478),
`POST /input` → `submit_human_input` (3479-3509),
`DELETE /input/{request_id}` → `dismiss_human_input` (3510-3524). Extract
as-is — don't rewrite internals here, that's
`human_input_intervention_system.md`'s job, landing next against this
already-extracted file.

**Line-count sanity check:** 1-332 (`_shared.py` part 1) + 333-544
(`control_routes.py` part 1) + 545-1489 (`queue_routes.py`) + 1490-2702
(`project_routes.py`) + 2703-2704 (blank lines) + 2705-3263 (`feature_routes.py`)
+ 3264-3423 (`message_routes.py`) + 3424-3524 (`intervention_routes.py`) +
3525-3970 (`control_routes.py` part 2) + 3971-3980 (`_shared.py` part 2) =
3980, the file's exact total. Every line is accounted for; if your own pass
doesn't reconcile to 3980, something drifted since this doc was written —
re-verify against the live file before extracting, the same way §3.1
verifies against 117.

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
   exactly the "remaining" set from §3.1 (config helpers,
   `OrchestratorLogger`, the top-level run loop) — nothing more, nothing
   less, because every other line was accounted for by the mapping in step
   2 — and that remainder becomes `src/autopilot/orchestrator/__init__.py`
   per step 0.
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

This turns "split two huge files" from "an agent retypes 12,000 lines
correctly" into "an agent writes and reviews one ~100-line script, plus
curates 15 short import blocks by hand" — the risk surface shrinks to
exactly the part that requires judgment.

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
| `src/autopilot/__init__.py:10` | (check current re-export list) | split per symbol per the table above |
| `src/autopilot/service.py:118,148,170,184,209` | `_get_or_create_project_id`, `_set_project_context`, `_delete_project_context`, `_get_project_context`, `_running_state_key`, `_RUNNING_STATE_KEY_LEGACY`, `_RUNNING_STATE_KEY_PREFIX`, `_resolve_project_id`, `_get_project_contexts_by_prefix` | `state.py` — change these 5 import statements to `from src.autopilot.orchestrator.state import (...)` |
| `src/autopilot/service.py:387` | `run_continuous_pipeline` | **no change** — stays in `orchestrator/__init__.py`, `from src.autopilot.orchestrator import run_continuous_pipeline` still resolves |
| `src/mcp/server.py:1308` | `OrchestratorLogger` | **no change** — stays in `orchestrator/__init__.py` (Tier 2.4, out of scope) |
| `src/mcp/server.py:1334` | `_advance_phases`, `_clean_stale_assigned_tasks`, `_maybe_resolve_arbitration`, `_recover_abandoned_workflows_missing_worktree`, `_retry_exhausted_paused_workflows`, `_retry_failed_tasks`, `_sync_stale_feature_statuses` | `phase_transitions.py` (`_advance_phases`, `_maybe_resolve_arbitration`, `_retry_exhausted_paused_workflows`, `_retry_failed_tasks`) / `worktree_integration.py` (`_recover_abandoned_workflows_missing_worktree`) / `features.py` (`_clean_stale_assigned_tasks`, `_sync_stale_feature_statuses`) — **this one import site spans 3 target modules; update it as 3 separate import statements, don't leave it importing from one module that no longer holds all of them** |
| `src/mcp/server.py:3782,3815` | `_claim_phase_task_creation`, `_release_phase_task_creation_claim` | `phase_transitions.py` |
| `src/mcp/autopilot_api.py:436` | `PersistentPipelineState` | `state.py`; this import line ends up inside `control_routes.py` after the §3.2 split (it's in `get_pipeline_status`) |
| `src/mcp/autopilot_api.py:767,889,924,930,1057` | `_resolve_project_id` (767), `_delete_project_context` (889), `_get_or_create_project_id` (924), `PersistentPipelineState` (930), `api_post`/`get_tasks` (1057) | `state.py` (all but the last) / `engine_client.py` (`api_post`/`get_tasks`); all 5 sites end up inside `queue_routes.py` after the §3.2 split — **line 1057 was missing from an earlier pass of this table entirely** (it found only 8 of the file's 9 real `from src.autopilot.orchestrator import` sites; verify with `grep -n "from src.autopilot.orchestrator import" src/mcp/autopilot_api.py` before starting, don't trust this table's count blindly either) |
| `src/mcp/autopilot_api.py:2255` | `PersistentPipelineState` | `state.py`; ends up inside `project_routes.py` |
| `src/mcp/autopilot_api.py:3516,3733` | `_get_or_create_project_id` (3516), `PersistentPipelineState` (3733) | `state.py`; both end up inside `control_routes.py` |
| `src/services/task_completion_service.py:518` | `_create_phase_task` | `phase_transitions.py` |
| `tests/` (18 files, per grep) | varies — **grep each file individually before starting**, don't assume the table above is exhaustive for tests | matches whichever module the imported symbol moved to |

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
2. Within this task: extract in the order given in §3.1 (state →
   engine_client → policy → queue → worktree_integration → features →
   reporting → phase_transitions) and §3.2 (`_shared.py` → the five smaller
   route files → `intervention_routes.py` last, matching the note above).
   Each extraction is its own commit: run the script from §3.3 for that
   module, curate its import block by hand, fix every import site from
   §4's table, run the targeted tests for that cluster, confirm green,
   commit, move to the next. Don't batch multiple modules into one commit —
   if something breaks, `git bisect` should land on one extraction, not a
   pile of them.
3. `phase_transitions.py` (orchestrator side) and the route split
   (autopilot_api.py side) are independent of each other — either order is
   fine, or do them in parallel if two agents are available, since they
   touch different files.

---

## 6. Testing

- No new tests are needed for correctness — this is a pure move, and the
  ~18 test files that already import these symbols directly are the
  regression suite. Per this repo's stated test-running preference, run
  only the targeted test files for whatever cluster you just moved, not the
  full suite, after each extraction.
- Add one new test only if you don't already have one:
  `tests/test_autopilot_api.py` (or a new file) asserting the aggregator
  `router` in `src/mcp/autopilot/__init__.py` still has the same route
  count / same paths after the split as it had before (a simple
  `len(router.routes)` or path-set comparison against a hardcoded expected
  list — the exact 52 routes enumerated in §3.2 is the baseline) — a cheap
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
