# SOLID / OO Review — 2026-08-19 update

Companion to `docs/SOLID_OO_REVIEW.md` (the original review, referenced below as "the
original review"). That review predates almost all of `docs/AUTOPILOT_REFACTOR_PLAN.md`'s
work — `server.py` (6885 lines), the `api.py` `FrontendAPI` monolith, the flat
`orchestrator.py`, and the 2173-line `AgentManager`/2455-line `MonitoringLoop` god
classes it describes have all since been split, decomposed, or partially fixed. This
document re-verifies every one of the original review's 56 findings against the code as
it exists right now, then reports newly-found violations — both ones the original review
missed and ones this session's own refactor introduced or worsened.

**Method:** four parallel audits, one per the original review's four backend sections,
each re-reading the actual current files (not trusting old line numbers or this session's
own prior "done" claims) and independently hunting for new violations in its area. Findings
below are their re-verified output, lightly edited for consistency; file:line citations
were read by the audits, not inferred. Frontend (§5 of the original review) was not
re-audited this pass — out of scope, unchanged by the backend refactor.

**Headline, before the detail:** two genuinely new god-modules were produced *by* this
refactor itself (`phase_transitions.py` at 3539 lines, `orchestrator/__init__.py` at 3411
lines — the two largest files in the repo, neither ever held to Phase 1c's own "~800
lines per module" criterion). One real, live bug was found in passing: Guardian's
steering-key remapping now silently breaks `GuardianAnalysis.steering_recommendation`
(always `None`). Both are detailed below, not buried in the tables.

---

## 1. Live bugs found in passing (not style — read these first)

**Guardian's `steering_recommendation`/`steering_message` key drift now silently breaks a
DB column.** `guardian.py:262-264` renames the LLM's `steering_recommendation` key to
`steering_message` before returning its analysis dict. `guardian_dispatch.py:247-249`
correctly reads `steering_message` back out (with a comment flagging the rename). But
`guardian_dispatch.py:395` independently reads `analysis.get("steering_recommendation")`
to populate `GuardianAnalysis.steering_recommendation` — a key that no longer exists on
the dict, so that DB column has been silently `None` on every write since whichever
change introduced the rename. Two call sites in the *same file* disagree on the key name.
This is the original review's finding 3.7 ("Guardian's return-dict key remapping is
unowned and duplicated at the call site") having actually gone wrong, not just being
ugly. **Fix:** `guardian_dispatch.py:395` should read `analysis.get("steering_message")`,
one-line; the deeper fix (a `TrajectoryAnalysisResult` dataclass with canonical field
names, per the original review's finding 3.7) prevents the next instance.

**Two disconnected phase-retry-budget mechanisms can disagree.** `_create_phase_task`
(`phase_transitions.py:2838-2859`) counts `Task` rows with `action in ("retry", "goto")`
against a hardcoded `max_phase_attempts = 5`, then triggers arbitration once exceeded.
Independently, `WorkflowOrchestrator.evaluate` (`workflow_engine/orchestrator.py:271-294`)
tracks its own in-memory `phase_retry_counts` against `eval_point.max_retries`, a
config-driven value from `workflow.yaml`, and decides `RETRY` vs. `ARBITRATE` on its own.
Neither system knows about the other's count or threshold — if `workflow.yaml` sets
`max_retries=10` but the DB-row count hits 5 first, the phase force-arbitrates 5 retries
early; if the reverse, the DB count never binds and the config value governs alone. This
is exactly the "N-th independent implementation of a budget/count" bug class Phase 2
of this refactor was built to eliminate — between two systems this session touched
repeatedly but never reconciled with each other. See §3 (new findings, orchestrator) for
the fix.

**`_sync_stale_feature_statuses`/`_sync_stale_design_statuses` bypass `status_derivation.py`
entirely — contradicting this plan's own §4.6 "wiring complete" claim.**
`features.py:133` documents `_update_feature_status` as "the single write path for
Feature.status from the orchestrator." `_sync_stale_feature_statuses` (`features.py:161-249`)
and `_sync_stale_design_statuses` (`:252-305`) — background self-heal sweeps whose entire
purpose is closing exactly the "status never caught up" bug class `status_derivation.py`
exists to prevent — write `feature.status`/`design.status` directly (`:243`, `:301`),
calling neither `_update_feature_status` nor `derive_feature_status`/`derive_design_status`.
§4.6's plan-doc completion note (this session, `docs/AUTOPILOT_REFACTOR_PLAN.md`) says
`status_derivation.py`'s wiring is "all three done" for its three named targets — true for
those three, but these two sweeps are a 4th/5th independent "is this done" writer the plan
doc never named and this session never checked. See §3 for the fix.

**`auth_api.py`'s `/me` endpoint is a dead 501 stub shadowing a working implementation.**
`auth_api.py:461-463` — `get_current_user` under `/me` unconditionally raises
`HTTPException(501, "not yet implemented")`. `auth_middleware.py:56-90` already has a
fully working `get_current_user` (verifies the JWT, loads the `User` row) used as a
`Depends()` throughout the app. The name collision between the two functions is a real
hazard for a future edit that wires the wrong one in. `/me` has been permanently broken
since whichever commit added the real one.

**A worktree-conflict-resolution config field actively lies about being honored.**
`config.conflict_resolution_strategy` (settable via `WORKTREE_CONFLICT_STRATEGY` env var,
`simple_config.py:323`) is read at exactly one site (`worktree_manager.py:576`) — only to
be echoed into a result dict, never branched on. `_resolve_conflicts` always runs
newest-file-wins regardless of what the operator configured. Setting
`WORKTREE_CONFLICT_STRATEGY=manual_review` silently does nothing. Not new — the original
review's finding 4.5 already flagged this as "dead configuration implying pluggable
strategies" — but worth restating as a live footgun, not just a style complaint: an
operator reading the config schema has no way to discover the setting is inert short of
reading `_resolve_conflicts`'s source.

---

## 2. Original review's 56 findings — current status

Legend: **FIXED** (problem gone) · **PARTIAL** (some of it fixed) · **OPEN** (unchanged
or relocated, same problem) · **WORSE** (regressed since the original review) · **STALE**
(structure changed enough the finding needs restating, noted inline).

### §1 — MCP/API layer (`src/mcp/`)

| # | Finding (short) | Status | Current evidence |
|---|---|---|---|
| 1.1 | `server.py` god module | **FIXED** | Split into `src/mcp/server/` package (9 files, each <850 lines), `__init__.py` is composition-root-only |
| 1.2 | `update_task_status` 451-line handler | **FIXED** | `agent_task_routes.py` thin wrapper → `_update_task_status_steps.py`'s 9 named functions |
| 1.3 | `create_task`/`process_queue` duplicate enrichment | **FIXED** | Both call `TaskEnrichmentService.resolve_phase_id()`/`.enrich()` |
| 1.4 | Phase-ID order/UUID resolution duplicated 12+× | **PARTIAL** | Canonical resolver exists and is used at the original sites, but 9 more duplicate `.isdigit()` sites found (`agents_api.py`, `frontend/_shared.py`, `prompts/assembler.py`, `launch_pipeline.py`) — more remain than were centralized |
| 1.5 | Tool dispatch string-branching | **FIXED** | `server/_mcp_tool_registry.py`'s `MCPToolSpec`/`MCP_TOOL_REGISTRY` |
| 1.6 | `ServerState` god singleton | **PARTIAL** | Still owns 9+ managers + migration + broadcast (`server/_shared.py:233-434`); the DIP/circular-import symptom (1.16) is fixed, the class itself isn't decomposed |
| 1.7 | `FrontendAPI` 84-method ISP violation | **PARTIAL** | Routing split into 4 routers, but all delegate into one still-monolithic 2759-line, 41-method class underneath |
| 1.8 | `get_project_design_status` 300-line ad hoc handler | **OPEN** | Moved to `project_routes.py:1555`, same shape, no service extracted |
| 1.9 | Ticket endpoints/models split across 2 files | **PARTIAL** | Duplicate-model bug fixed; endpoints now split across 2 *different* files (`tickets_api.py` + new `messaging_api.py`) |
| 1.10 | Task/agent serialization duplicated 6× | **OPEN** | No serializer class; ~34 hand-rolled sites now (`frontend/_shared.py` ~22, `agents_api.py` ~12) |
| 1.11 | Repair/rerun orchestration in API layer | **OPEN** | `queue_routes.py`, still constructs its own `DatabaseManager`/`WorktreeManager` |
| 1.12 | Ad hoc `DatabaseManager()`/`WorktreeManager()` construction | **OPEN, more sites** | 5 call sites now (`control_routes.py`, `queue_routes.py`, `feature_routes.py`, `frontend/phase_routes.py`) |
| 1.13 | Broad `except Exception` (66×/35×) | **OPEN, worse** | 141 total across `server/`+`autopilot/`+`frontend/` (was 101) |
| 1.14 | Duplicate `/projects` CRUD | **FIXED** | `projects_api.py` deleted; one CRUD surface remains (§4.6, commits `f5d0305`/`64d2910`) |
| 1.15 | Manual `get_session()` vs. context manager | **OPEN** | 45 manual vs. 3 `with` in `server/` alone |
| 1.16 | Circular-import workaround for project activation | **FIXED** | `src/core/app_context.py`'s `get_app_state()`; zero remaining `from src.mcp.server import server_state` outside `server/` itself |
| 1.17 | Validation-outcome duplicated across 4 closures | **OPEN** | Same 4 closures, relocated to `_update_task_status_steps.py`/`memory_api.py` |
| 1.18 | Three "stop a workflow" implementations | **OPEN, worse** | 4 divergent implementations now, differing on whether tasks get force-failed |
| 1.19 | Duplicate trusted-agent allowlists | **OPEN** | Byte-for-byte unchanged, same file (`server/_shared.py:454-463` vs. `:639-648`) |
| 1.20 | `TicketService` lazy-singleton instead of DI | **OPEN** | Unchanged, ~15 call sites |
| 4.10 | `auth_api.py` business logic in routes | **OPEN** | Untouched by this refactor; see §1's live-bug note above for a new defect in the same file |

### §2 — Orchestrator/pipeline (`src/autopilot/orchestrator/`, `src/phases/`, `src/workflow_engine/`)

| # | Finding (short) | Status | Current evidence |
|---|---|---|---|
| 2.1 | "Is this done?" reimplemented 4+ places | **PARTIAL** | 3 of 3 named `status_derivation.py` targets wired (§4.6); `run_design_aggregate` deliberately not (different inputs, verified to agree); the 5th filesystem-based source (`_shared.py:225` `_feature_status`) still live and unwired; **new**: `_sync_stale_*` sweeps are a 4th/5th independent writer (§1 above) |
| 2.2 | Two `Feature.status` write paths | **FIXED** | `_update_feature_status` is now the sole writer in `run_feature_pipelines` |
| 2.3 | `run_single_workflow`/`run_continuous_pipeline` god functions | **OPEN, worse** | 591 lines (was 465) / 547 lines (was 430); `_recovery_attempts` still a dynamic, undeclared attribute |
| 2.4 | Global mutable orchestrator identity + singleton import | **PARTIAL** | `server_state` singleton import replaced by `get_app_state()` (real DIP fix); `_orchestrator_agent_id` module global unchanged, now imported by 3 modules instead of 1 |
| 2.5 | `attempt_recovery` fuses 3 strategies | **OPEN** | `policy.py:168-314`, same fusion, just relocated |
| 2.6 | `_create_phase_task` hardcoded retry policy + side-effecting pause | **PARTIAL** | The pause side-effect is gone (replaced by a real `_trigger_arbitration` call — an architectural improvement); retry-count magic numbers (`max_phase_attempts=5`, `max_arbitrations_per_phase=3`) are still inline, not config; see §1's live-bug note for the deeper consequence |
| 2.7 | `_fire_phase_transition` smuggles `workflow_id` via attribute mutation | **OPEN, more sites** | `pm.workflow_id = workflow_id` pattern now at 4+ call sites in `phase_transitions.py` |
| 2.8 | `_advance_phases` priority order enforced only by convention | **OPEN** | Unchanged `if result is not None: return result` chain |
| 2.9 | `_check_condition` hand-rolled regex dispatch | **PARTIAL** | if/elif → dict dispatch (OCP fixed); malformed condition still silently evaluates `False` instead of failing loudly |
| 2.10 | Condition grammar never validated at config-load time | **FIXED** | `config_validator.py` now calls `is_valid_condition_string` on every `condition["if"]` |
| 2.11 | `_phase_name_to_order` hardcodes one workflow's vocabulary | **FIXED** | Now built from real `Phase.order` DB rows; hardcoded dict survives only as a documented legacy fallback |
| 2.12 | `mark_phase_complete` 267-line copy-pasted-boilerplate function, missing `SKIP` handler | **FIXED** | Dispatch via `getattr(self, f"_handle_evaluation_{action_value}")`, `_handle_evaluation_skip` present, shared `_close_execution` helper |
| 2.13 | `_get_orchestrator` fuses DB read + decision + cache + swallowed errors | **OPEN** | Unchanged shape, now also builds `phase_order_map` inside the same fused `try/except Exception: return None` |
| 2.14 | `WorkflowTerminationHandler` non-atomic across sub-steps | **OPEN** | Byte-identical to the original finding |

### §3 — Agents/monitoring (`src/agents/`, `src/monitoring/`, `src/services/`)

| # | Finding (short) | Status | Current evidence |
|---|---|---|---|
| 3.1 | `AgentManager` god class (5 responsibilities) | **PARTIAL** | Tmux delivery/prompt-formatting genuinely extracted (`messenger.py`, `prompt_builder.py`); broadcast/messaging/DB-scattered-persistence deliberately kept on `AgentManager` (tests patch at that level) |
| 3.2 | `create_agent_for_task`/`restart_agent` duplicate ~85-line block, already drifted | **FIXED** | Both now call shared `_resolve_env_and_model`/`_resolve_mcp_timeout_ms`/`_build_glm_env_vars` |
| 3.3 | Per-CLI `isinstance` branching instead of polymorphism | **FIXED** | Dispatch via `cli_agent.needs_chunked_delivery`/`.format_message()`; one residual string check remains, explicitly noted |
| 3.4 | `MonitoringLoop` 2050-line god class | **PARTIAL** | Heuristics/dispatch/cleanup/diagnostics moved to 5 named collaborators; `monitor.py` down to 850 lines, but `_monitoring_cycle` (~290 lines) still fuses scheduling with inline DB-querying business logic |
| 3.5 | 4 "is agent broken" heuristics, no shared interface | **OPEN** | Same two-class split, still no `StuckDetector` protocol |
| 3.6 | Guardian LLM analysis entangled with DB reads/side effects | **PARTIAL** | `_evaluate_steering_eligibility` is now a clean pure function; `analyze_agent_with_trajectory`/`steer_agent` still entangle LLM calls, DB reads, and side effects |
| 3.7 | Guardian's key remapping unowned/duplicated | **OPEN, now a live bug** | See §1 above |
| 3.8 | Dead code `_should_steer_agent` | **FIXED** | Deleted this session (Phase 4); zero remaining references |
| 3.9 | `Conductor` bundles unrelated QA-review op; no constructor-injected `llm_provider` | **PARTIAL** | QA-review bundling is gone; constructor-injection bypass (`get_llm_provider()` called inline, duplicated in 2 branches) is still open |
| 3.10 | Direct infra instantiation instead of DI | **PARTIAL** | Termination-duplication half is fixed (§4.2, single writer); `libtmux.Server()` direct instantiation and raw `get_session()` vs. `session_scope()` imbalance persist in `launch_pipeline.py`/`manager.py`/`conductor.py` |
| 3.11 | `TicketService.create_ticket` 470-line fused method | **OPEN, worse** | Now ~501 lines, all originally-cited pieces still present |
| 3.12 | Duplicate cascade-delete in `TicketService` | **OPEN** | Unchanged, two ~32-line verbatim-duplicate blocks |
| 3.13 | Duplicate similarity thresholds | **OPEN** | Unchanged |
| 3.14 | `QueueService` priority-ordering duplicated 4× | **OPEN** | Unchanged |

### §4 — Core infrastructure (`src/core/`, `src/interfaces/`, `src/auth/`)

| # | Finding (short) | Status | Current evidence |
|---|---|---|---|
| 4.1 | `DatabaseManager` god class + `self.db_path` typo bug | **PARTIAL** | The specific live bug (wrong attribute name silently disabling a migration) is fixed; the god-class problem is worse — 18 hand-rolled `_migrate_*` methods now (was 3), still no schema-version table |
| 4.3 | `Config` 70-field object, untyped triple-enumeration | **OPEN** | Unchanged; `validate()` still checks only 3 of dozens of fields |
| 4.4 | `WorktreeManager.reload()` mutates global `Config` singleton | **FIXED** | Now mutates only instance-local state; docstring explicitly documents the bug this fixes |
| 4.5 | `WorktreeManager` SRP fusion + hardcoded dead conflict policy | **PARTIAL** | Merge-failure path fixed (§4.4, abort-and-preserve uniform); class-level git+DB+locking+policy fusion unchanged (1344 lines, up from ~900); config field still inert — see §1 above |
| 4.6 | `session_scope()` bypassed almost everywhere | **OPEN** | `WorktreeManager`: 7 manual vs. 1 `session_scope()`; `auth_api.py` unchanged |
| 4.7 | Two parallel LLM abstractions, silent LSP violation | **OPEN, one more implementer** | `AnthropicProvider`'s trajectory/coherence methods still stub hardcoded defaults; `MultiProviderLLM` is now a 3rd concrete implementer of the same ABC, not a fix |
| 4.8 | `LangChainLLMClient._create_model` 145-line if/elif dispatch | **OPEN, worse** | ~134 lines (grew); all 5 provider packages still unconditionally imported |
| 4.9 | Duplicated CLI-output-parsing scan logic | **OPEN, partially normalized** | 4 subclasses still duplicate the scan loop; `ClaudeCodeAgent` converged onto the majority key-naming convention, narrowing the split from 2-vs-3 to 1-vs-4 |
| 4.10 | `auth_api.py` business logic in routes | **OPEN** | See §1 above for a new defect (`/me` stub) in the same file |

---

## 3. New findings, by area

### MCP/API layer

**`project_routes.py` (2098 lines) is a new god-file bundling 4 unrelated domains.**
`src/mcp/autopilot/project_routes.py:354-688` project CRUD; `:689-1099` seven cost-accounting
endpoints; `:1100-1554` design-file browsing/reorder/sync; `:1555+` design-status
aggregation (= finding 1.8). None share a reason to change together — a byproduct of the
`autopilot_api.py` split concentrating unrelated concerns under one router name instead of
one router per bounded context. *Fix:* extract `CostAccountingService`/routes and
`ProjectDesignBrowserService`/routes, leaving this file strictly project CRUD + activation.

**`DatabaseManager(None)` duplicated within `src/auth/` itself, not just against `src/mcp/`.**
`auth_api.py:79-81` defines `get_db_manager()` wrapping `DatabaseManager(None)`, but
`auth_middleware.py:86` constructs `DatabaseManager(None)` inline instead of calling its own
sibling. Same correctness risk as finding 1.12 (two independent engines against one SQLite
file), now confirmed inside `src/auth/` too. *Fix:* route both through one shared accessor.

### Orchestrator/pipeline

**`phase_transitions.py` (3539 lines) — the single largest file in the repo, produced by
this session's own decomposition, never held to its own size criterion.** 36 top-level
functions fusing 4+ unrelated responsibilities: task-creation-claim primitives, the
phase-advance dispatch/self-heal sweep, a ~700-line arbitration subsystem, phase-task
creation, and stuck-task resume. Ironic given Phase 1c's own stated exit criterion ("no
module over ~800 lines") was written specifically to prevent this shape. *Fix:* split
arbitration (`_gather_arbitration_context`/`_build_arbitration_prompt`/`_trigger_arbitration`/
`_maybe_resolve_arbitration`/`_resolve_arbitration_outcome`) into its own `arbitration.py`.

**`orchestrator/__init__.py` (3411 lines) — a second new god-module, mixing pipeline
execution with unrelated infrastructure.** 28 top-level functions: the real
pipeline-execution flow (`run_phase0`, `run_single_workflow`, `_run_one_feature`,
`run_feature_pipelines`, `run_single_design`, `run_continuous_pipeline`, `main`) plus config
getters, human-escalation prompting, and orchestrator-agent registration — none of which
belong to "run the pipeline." *Fix:* extract config getters to `config.py`, human-escalation
to its own module, matching the file's own package siblings.

**Two disconnected phase-retry-budget mechanisms** — see §1, live bug. *Fix:*
`_create_phase_task`'s DB-row-count bound should read from `eval_point.max_retries` (the
same config-driven value `WorkflowOrchestrator.evaluate` already uses), or be deleted
entirely in favor of the config-driven one.

**`_sync_stale_feature_statuses`/`_sync_stale_design_statuses` bypass the single-writer
contract they exist to enforce** — see §1, live bug. *Fix:* route both through
`derive_feature_status(db, feature.id, write_back=True)`/`derive_design_status(...)`.

### Agents/monitoring

**A shared tmux-session-lookup helper exists but 9 call sites across 6 files don't use it.**
`output_capture.py:544-551`'s `_find_tmux_session` (exposed on `AgentManager`) is
independently reimplemented in `manager.py`, `messenger.py`, `terminator.py`,
`launch_pipeline.py`, `orphan_reaper.py` (×3), `mechanical_recovery.py` (×2) — a direct
side effect of splitting `AgentManager` into collaborators that each hold a reference to
`agent_manager` but never converged on the one helper it already exposes. *Fix:* delete
the local copies, call `agent_manager._find_tmux_session`.

**`MechanicalRecoveryDetector` reaches back into `monitor.py` via dynamic `getattr` import
instead of a real dependency.** `mechanical_recovery.py:29-47` pulls regex patterns and
constants out of `monitor.py` via `import ... as _mod; getattr(_mod, name)`, explicitly to
dodge a circular import — the "extracted collaborator" now depends on its own orchestrator
through a back-channel invisible to static analysis, the opposite of the inversion the
decomposition was meant to produce. *Fix:* move the shared regexes/constants to a
lower-level `src/monitoring/patterns.py` both files import normally.

**Two unrelated methods are both named `restart_agent`, doing incompatible things, called
from the same file.** `AutoRestart.restart_agent` terminates the agent and requeues its
task for a *different* agent to pick up later; `AgentManager.restart_agent` kills the tmux
session and relaunches in place, reusing the same row/worktree. `guardian_dispatch.py`
calls both, for genuinely different scenarios. *Fix:* rename one, e.g.
`AutoRestart.requeue_and_terminate` vs. `AgentManager.relaunch_in_place`.

### Core infrastructure

**`status_derivation.py`'s self-heal write-back block is duplicated 4× within the module
that bills itself as "the single source of truth."** `derive_feature_status` (:200-207),
`derive_design_status` (:329-354), and `derive_workflow_status` (two copies: :419-428 and
:487-494) each independently repeat the same
`if write_back and derived != X.status: log; X.status = derived; db.commit()` shape. *Fix:*
extract `_apply_derived_status(db, entity, derived, entity_label, entity_id, write_back,
on_change=None)`, called from all three; `derive_design_status`'s extra `design.error`
clearing becomes the `on_change` hook.

**`HephaestusSDK` (`src/sdk/client.py`, 1202 lines) is a god-object facade mixing 4 hats.**
Process lifecycle (spawn/health/watchdog), phase/workflow-definition loading from
YAML-or-objects, the actual client CRUD API, and child-agent orchestration, all on one
class. `_start_headless` alone is 110 lines mixing Qdrant health-checking, subprocess
spawning, polling, and watchdog-thread startup. *Fix:* split into
`HephaestusProcessSupervisor`, `WorkflowDefinitionLoader`, and a thin client composing both.

**Worktree conflict-resolution config field is unreachable** — see §1, live footgun.
*Fix:* either delete the config field/env var, or implement the strategy interface the
original review already proposed.

**`langchain_llm_client.py` unconditionally imports all 5 provider packages at module load,
even for a deployment using only one.** A provider package merely being uninstalled (not
just unconfigured) breaks the whole module at import time. *Fix:* move each `import`
inside its own `elif` branch — mechanical once the factory-registry refactor (finding 4.8)
lands.

---

## 4. Updated priorities

The original review's "suggested order of attack" is mostly obsolete (items 1, 3 there are
now fixed or well underway). Re-ranked given what's actually still open, weighted toward
correctness risk over pure style:

1. **The two live bugs in §1** (Guardian's stale steering key, the disconnected retry
   budgets, the `_sync_stale_*` sweeps bypassing `status_derivation.py`) — each is a
   silent-failure-shaped bug hiding behind a style-looking finding, exactly the pattern
   this whole refactor's bug-fix-history methodology has repeatedly found elsewhere.
2. **Split `phase_transitions.py` and `orchestrator/__init__.py`.** Both are now larger
   than the files this refactor already fixed for the same reason; leaving them
   unaddressed undercuts the refactor's own stated rationale.
3. **1.13/1.15/4.6 — the `except Exception`/manual-session patterns.** Still the largest
   volume of individual findings in the codebase (141 broad excepts, dozens of manual
   sessions); no single fix, but the original review's diagnosis (no service layer, so
   routes/methods wrap everything in one broad try/except) is still exactly right, and
   still the single highest-leverage remaining structural gap.
4. **`TicketService`/`ConductorService`/`auth_api.py`** — three areas this refactor never
   touched at all; still carry their original-review findings unchanged or worse.
5. Everything else in §3 above, roughly in the order listed.
