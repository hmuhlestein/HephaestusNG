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

**Fixed, 2026-08-19** (early in this same refactor pass, before this update doc's "Updated
priorities" bookkeeping caught up — row 3.7 below was left stale until this correction).
`guardian_dispatch.py:399` now reads `analysis.get("steering_message")`, verified still
correct as of 2026-08-20. This was the one-line correctness fix only; the deeper
structural fix (a `TrajectoryAnalysisResult` dataclass with canonical field names, so this
class of key-name drift becomes a type error instead of a silent `None`) remains undone.

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
| 1.13 | Broad `except Exception` (66×/35×) | **FIXED, 2026-08-20** | 141 total across `server/`+`autopilot/`+`frontend/` (was 101) at time of the 2026-08-19 update. A 3-agent parallel survey of all ~700+ `except Exception` blocks codebase-wide (not just this subset) found 23 sites matching the "silently swallows an error that should have surfaced a real state-consistency bug" shape — same pattern as every other live bug this refactor has found. Grouped into 5 themes (leaked sessions / transient-error-as-destructive-signal / data-loss risk / fictitious success / debug-level-hides-real-failures); all 5 themes fixed across 2026-08-19 to 2026-08-20 — see `design_docs/phase3_except_exception_survey_findings.md` for the full ranked list, per-site fix description, and test coverage. This does not mean every `except Exception` in the codebase is now "fixed" — 700+ blocks were surveyed, 23 were judged genuinely risky by the "silently hides a real bug" bar, and those 23 are the ones addressed; the remaining volume is a mix of legitimately defensive catches and lower-priority style debt outside this pass's scope |
| 1.14 | Duplicate `/projects` CRUD | **FIXED** | `projects_api.py` deleted; one CRUD surface remains (§4.6, commits `f5d0305`/`64d2910`) |
| 1.15 | Manual `get_session()` vs. context manager | **PARTIAL, 2026-08-19** | 45 manual vs. 3 `with` in `server/` alone. Fixed 5 sites missing `try/finally`/`try/except/finally` around a manual session (leaked connection on any mid-transaction failure): `_create_task_steps.py`'s `_persist_new_task`, `_resolve_phase_and_enrich`, `_check_for_duplicate_task`, `_handle_task_processing_failure` (4 sites, one file), and `launch_pipeline.py`'s `create_agent_for_task` stub-Agent-row block. Zero behavior change on the success path; on failure, sessions now roll back and close instead of leaking. 56 targeted tests pass. Most other manual-session sites already correctly wrap in `try/finally` — see 1.13's survey doc for the full picture |
| 1.16 | Circular-import workaround for project activation | **FIXED** | `src/core/app_context.py`'s `get_app_state()`; zero remaining `from src.mcp.server import server_state` outside `server/` itself |
| 1.17 | Validation-outcome duplicated across 4 closures | **OPEN** | Same 4 closures, relocated to `_update_task_status_steps.py`/`memory_api.py` |
| 1.18 | Three "stop a workflow" implementations | **PARTIAL, 2026-08-20** | Root-caused the divergence: the CLI's `heph workflow stop` hit `/api/workflows/{id}/stop` → `FrontendAPI.stop_workflow`, which force-failed every in-flight task; the dashboard's "Stop" button hits a *different* endpoint, `/api/workflow-executions/{id}/stop` → `workflow_execution_routes.stop_workflow`, which pauses the workflow and resets tasks to "pending" for retry instead — same verb, opposite (destructive vs. resumable) behavior. `FrontendAPI.stop_workflow` had exactly one real caller (the CLI; the dashboard never called it) plus its own offloading regression test, whose coverage is already independently provided for the canonical endpoint by `test_workflow_stop_cancel_tmux_offloading.py`. Fix: repointed the CLI's 2 call sites to `/api/workflow-executions/{id}/stop`, deleted `FrontendAPI.stop_workflow` and its route, deleted the now-subject-less `test_stop_workflow_offloading.py`, updated `test_frontend_api_routes_guardrail.py`'s pinned route set (40→39). Down to 3 implementations: the canonical user-facing `stop` (pause+resume) and `cancel` (force-fail) in `workflow_execution_routes.py`, plus `WorkflowTerminationHandler.terminate_workflow` — a genuinely distinct feature (automatic termination when a result is validated, not a user-initiated action) left untouched. 83 targeted tests pass |
| 1.19 | Duplicate trusted-agent allowlists | **FIXED (status was stale), re-verified 2026-08-20** | The duplicate is gone: `server/_shared.py` now has exactly one `KNOWN_SYSTEM_AGENTS` (`:452`), one `verify_agent_authentication`, and one `sdk-`/`mcp-` prefix check. Verified by searching for the distinctive members (`main-session-agent`, `sdk-repair-agent`, `pi-extension`) across all of `src/` rather than trusting the old line numbers. **But re-verifying it surfaced a live successor** (`bfe89b4`): `_create_task_steps.py`'s `_enforce_ticket_tracking_requirement` carried its own, much looser notion of the same question — `agent_id == "main-session-agent" or "sdk" in agent_id.lower() or "main" in agent_id.lower()` — an unanchored substring test gating the exemption from the ticket-tracking requirement. Any agent whose id merely contains "main" (`domain-expert`, `maintenance-agent`) skipped a control, not a feature. Latent rather than live, because real ids are UUID4 hex (neither "main" nor "sdk" is spellable in `[0-9a-f]`) or names like `orchestrator-*`; the next *named* agent would have been the problem. Replaced with a single shared `is_sdk_or_root_agent()`, verified to be strictly a tightening (every real system-agent identity keeps its prior classification). **The gap check then found six more of the same shape** (`3ddd25f`): `agents_api.py` guarded all six of its cross-agent endpoints with `"main" not in requesting_agent_id.lower()`, so `domain-expert`/`maintenance-agent` could view any agent's children, read their logs, message, nudge, and monitor them — the same defect on *authorization* rather than a process control. A third copy in `project_routes.py` was already correctly anchored and was consolidated as an exact equivalence, not a tightening. The canonical predicates now live in `src/core/agent_identity.py`, not `server/_shared.py`: `_shared.py` imports `agents_api.py`, so the two files that most need them cannot import from each other; `src/core` imports nothing from `src/mcp`, making the new module cycle-free by construction (verified by importing each entry point first). 35 tests |
| 1.20 | `TicketService` lazy-singleton instead of DI | **OPEN** | Unchanged, ~15 call sites |
| 4.10 | `auth_api.py` business logic in routes | **OPEN** | Untouched by this refactor; see §1's live-bug note above for a new defect in the same file |

### §2 — Orchestrator/pipeline (`src/autopilot/orchestrator/`, `src/phases/`, `src/workflow_engine/`)

| # | Finding (short) | Status | Current evidence |
|---|---|---|---|
| 2.1 | "Is this done?" reimplemented 4+ places | **PARTIAL** | 3 of 3 named `status_derivation.py` targets wired (§4.6); `run_design_aggregate` deliberately not (different inputs, verified to agree); the 5th filesystem-based source (`_shared.py:225` `_feature_status`) still live and unwired; **new**: `_sync_stale_*` sweeps are a 4th/5th independent writer (§1 above) |
| 2.2 | Two `Feature.status` write paths | **FIXED** | `_update_feature_status` is now the sole writer in `run_feature_pipelines` |
| 2.3 | `run_single_workflow`/`run_continuous_pipeline` god functions | **PARTIAL, 2026-08-20** | Both had grown (612 / 569 lines at the start of this pass) with zero test coverage — `tests/sdk/test_client_start.py` mocks `sdk.start` to raise, so it never reaches either loop. Wrote characterization suites first (`test_run_single_workflow_loop.py` 17 tests, `test_run_continuous_pipeline_loop.py` 8 tests), then decomposed: `run_single_workflow` 612→498 via `_snapshot_workflow_activity`/`_WorkflowActivity` (the agent+task count block appeared twice verbatim, pre- and post-phase-advance — the duplication was the real risk, since the second copy exists precisely because advancement can create the next task), `_log_agent_state_changes`, `_peek_active_agent_output`, `_has_unfinished_phases`, `_merge_design_branch_into_main`; `run_continuous_pipeline` 569→435 via `_build_and_start_pipeline_sdk`, `_persist_design_outcome`, `_shutdown_pipeline`. Fixed one latent bug in passing: the final-merge block referenced `_git` from a local import in a conditionally-executed branch of the caller, so a workflow whose `project_path` was already a worktree hit `UnboundLocalError` on any merge conflict, swallowed by the outer `except` as "Final merge failed" — the extracted helper imports `git` itself. Both loops' remaining bodies are still long (~200 / ~300 lines) but no longer fuse setup, observability, and business logic. Deliberately NOT extracted: `run_continuous_pipeline`'s protective gating section — its branches are interdependent and each guards against destructive cross-project behavior observed live; that needs its own pass. `_recovery_attempts` is still a dynamic, undeclared attribute. Verification went beyond "tests pass": 5 invariants (the `no_tasks_streak` two-poll confirmation, the post-advance count refresh, the credit-branch short-circuit, and both dispatch-gating branches) were individually mutated in the source and confirmed to fail exactly their covering test, before AND after the refactor — two of the first-draft tests passed against mutated code and were strengthened until they didn't |
| 2.4 | Global mutable orchestrator identity + singleton import | **PARTIAL** | `server_state` singleton import replaced by `get_app_state()` (real DIP fix); `_orchestrator_agent_id` module global unchanged, now imported by 3 modules instead of 1 |
| 2.5 | `attempt_recovery` fuses 3 strategies | **FIXED, 2026-08-20** | Four strategies by the time it was fixed (retry failed tasks / fail tasks under terminated agents / clean stale repo state / terminate dead agents). The fusion was hiding a live bug, not just poor structure: the git-cleanup step's "can't resolve a project path" guard was a bare `return` out of the *whole function*, so it silently skipped stale-agent termination — a strategy needing no project path at all. A workflow with no `working_directory` and no `PROJECT_PATH` in the environment therefore never had its dead agents reaped, leaving tasks stuck behind agents that no longer existed, which is precisely the state this same file's `_workflow_appears_abandoned`/`_escalate_stale_active_workflows` machinery then has to escalate around. Each action is now a named strategy returning what it recovered, collected in `_RECOVERY_STRATEGIES` and run in the original order; each swallows its own failures, so the early return is structurally impossible rather than merely deleted. 4 tests added (the regression test failed before the change; it and the git-failure case both fail under mutation), 297 passing across `test_orchestrator_helpers.py`/`test_orchestrator.py` whose existing `TestAttemptRecovery` coverage is unaffected. **Gap-check follow-up (`6055187`)**: extracting the stale-task strategy put it beside `features.py`'s `_clean_stale_assigned_tasks`, which does the same job — and the two had drifted. `features.py` guards the write with `if not task.failure_reason:`; the `policy.py` copy overwrote unconditionally. That is a live data-loss bug, not cosmetic: `update_task_status`' verification records exactly why a "done" claim was rejected on that same field (e.g. `"required output(s) invalid: docs/spec.md missing"`), and `_maybe_retry_failed_tasks` feeds `failure_reason` into the next attempt's prompt — so the retry lost the only feedback telling it what to fix. Guard added, 2 tests against a real DB (a `MagicMock` session accepts either write, so mocks cannot catch this), mutation-checked. **The duplication itself is reported, not consolidated**: `features.py`'s version also handles orphaned tasks in completed workflows and returns `None`, so merging them would change `attempt_recovery`'s recovered-items reporting — its own piece of work |
| 2.6 | `_create_phase_task` hardcoded retry policy + side-effecting pause | **PARTIAL** | The pause side-effect is gone (replaced by a real `_trigger_arbitration` call — an architectural improvement); retry-count magic numbers (`max_phase_attempts=5`, `max_arbitrations_per_phase=3`) are still inline, not config; see §1's live-bug note for the deeper consequence |
| 2.7 | `_fire_phase_transition` smuggles `workflow_id` via attribute mutation | **OPEN, more sites** | `pm.workflow_id = workflow_id` pattern now at 4+ call sites in `phase_transitions.py` |
| 2.8 | `_advance_phases` priority order enforced only by convention | **OPEN** | Unchanged `if result is not None: return result` chain |
| 2.9 | `_check_condition` hand-rolled regex dispatch | **PARTIAL** | if/elif → dict dispatch (OCP fixed); malformed condition still silently evaluates `False` instead of failing loudly |
| 2.10 | Condition grammar never validated at config-load time | **FIXED** | `config_validator.py` now calls `is_valid_condition_string` on every `condition["if"]` |
| 2.11 | `_phase_name_to_order` hardcodes one workflow's vocabulary | **FIXED** | Now built from real `Phase.order` DB rows; hardcoded dict survives only as a documented legacy fallback |
| 2.12 | `mark_phase_complete` 267-line copy-pasted-boilerplate function, missing `SKIP` handler | **FIXED** | Dispatch via `getattr(self, f"_handle_evaluation_{action_value}")`, `_handle_evaluation_skip` present, shared `_close_execution` helper |
| 2.13 | `_get_orchestrator` fuses DB read + decision + cache + swallowed errors | **OPEN** | Unchanged shape, now also builds `phase_order_map` inside the same fused `try/except Exception: return None` |
| 2.14 | `WorkflowTerminationHandler` non-atomic across sub-steps | **FIXED, 2026-08-20** | The finding was exactly right and the consequence was concrete: `terminate_workflow` reads as transactional (one `try/except` over four sub-steps, with a `session.rollback()`) but each sub-step committed independently, so the rollback could not undo them. A failure in step 3 or 4 left every task durably `failed` while the workflow stayed `active` — precisely the stale-active-workflow shape `run_continuous_pipeline`'s `_escalate_stale_active_workflows` exists to clean up, blocking the design queue until it fires. Sub-step commits removed; `terminate_workflow` owns the transaction and commits once. `_cancel_workflow_tasks` now `flush()`es, which is load-bearing rather than cosmetic — sessions are built `autoflush=False` and `_cleanup_workflow_resources` counts still-pending tasks straight after, so simply dropping the commit made a previously-unreachable branch live and left the phase execution `in_progress` with all its tasks failed (caught by the characterization tests, written first for exactly this reason). **Second bug found in the same function**: the cleanup record read `worktree.branch_path`, but `AgentBranch` aliases `AgentWorktree` whose column is `worktree_path` — every worktree raised `AttributeError`, was swallowed by the per-item `except`, and was reported `"success": False` while the `merge_status` write on the preceding line still committed; `cleanup_actions` reported failure 100% of the time for work that had succeeded. Scope correction from the gap check: the sole production caller (`memory_api.py`) discards the returned dict, so this corrupted the returned contract and the diagnostics rather than any live control flow. 10 tests added (this path had none — the only test referencing it patches `terminate_workflow` out entirely); both fixes mutation-checked. **Noted, not changed**: `_cleanup_workflow_resources`' `elif pending_tasks > 0` branch, commented "Keep in_progress so pending tasks can be dispatched", is unreachable — `_cancel_workflow_tasks` fails every such task first, so the count is always zero. Pre-existing dead code, pinned by a test so the comment isn't mistaken for behavior |

### §3 — Agents/monitoring (`src/agents/`, `src/monitoring/`, `src/services/`)

| # | Finding (short) | Status | Current evidence |
|---|---|---|---|
| 3.1 | `AgentManager` god class (5 responsibilities) | **PARTIAL (deliberate final boundary), re-verified 2026-08-20** | `manager.py` is 705 lines now (was 2173) — re-read the whole file fresh rather than trusting the prior "PARTIAL" note. Nearly everything is a thin one-line delegator to a named collaborator (`_launch`/`_terminator`/`_output_capture`/`_messenger`/`_prompt_builder`): `create_agent_for_task` (3.2's god function), `terminate_agent`, `restart_agent`, the whole tmux-session/transcript cluster. The genuinely-remaining fusion (`get_active_agents`, `send_recovery_keystrokes`, `broadcast_message_to_all_agents`, `send_direct_message`) each carry an explicit in-code comment explaining why: several tests patch `send_message_to_agent` on the `AgentManager` instance and assert these methods' internal loops invoked it — delegating the loop to `AgentMessenger` would call *its* `send_message_to_agent` instead, silently bypassing the mock. `manager.py` itself documents a concrete incident of exactly this risk: an earlier extraction pass silently deleted 4 working methods with 25 production call sites (`send_recovery_keystrokes` and 3 siblings), undetected until 2026-08-19 when it was restored. Given that documented history, this pass does not attempt further extraction here — the remaining fusion is a deliberate, well-reasoned boundary tied to the test suite's mocking strategy, not oversight. Un-fusing it safely would mean first changing how the test suite mocks agent messaging, a separate and larger undertaking than this session's other §3 work |
| 3.2 | `create_agent_for_task`/`restart_agent` duplicate ~85-line block, already drifted | **FIXED** | Both now call shared `_resolve_env_and_model`/`_resolve_mcp_timeout_ms`/`_build_glm_env_vars` |
| 3.3 | Per-CLI `isinstance` branching instead of polymorphism | **FIXED** | Dispatch via `cli_agent.needs_chunked_delivery`/`.format_message()`; one residual string check remains, explicitly noted |
| 3.4 | `MonitoringLoop` 2050-line god class | **PARTIAL, 2026-08-20** | Heuristics/dispatch/cleanup/diagnostics moved to 5 named collaborators; the 12-check hardcoded chain also fixed (3.5). `_monitoring_cycle`'s inline DB-querying business logic extracted to 2 named methods -- `_maybe_switch_tracked_workflow` (decides whether to switch the tracked workflow when a newer one goes active) and `_log_active_workflow_diagnostics` (per-workflow task-count logging, no decisions) -- both verbatim extractions, zero behavior change. `_monitoring_cycle` drops from ~290 to ~202 lines with zero raw DB queries left inline; still a god-*function* by line count (coordinating ~9 named phases), but no longer fuses scheduling with business logic. `monitor.py`'s overall god-class status (2050 lines originally) not re-measured this pass |
| 3.5 | 4 "is agent broken" heuristics, no shared interface | **FIXED, 2026-08-20 (scope evolved)** | The finding had grown well past its original snapshot: 12 mechanical checks now (not 4), but already unified into one collaborator class (`MechanicalRecoveryDetector`, from the 3.4 decomposition) with a uniform `async (agent) -> bool` shape — the SRP split was already done, just not the list-iteration. `monitor.py`'s `_monitoring_cycle` hardcoded a 12-call sequential if-chain (3 early-exit + 9 accumulating, an important asymmetry preserved exactly); replaced with `_EARLY_EXIT_CHECKS`/`_ACCUMULATING_CHECKS` name tuples iterated via `getattr`. No new Protocol/ABC — every check already satisfies the shared shape structurally. Deliberately scoped narrower than the original proposal: Guardian's 2 pure text-in/bool-out detectors (`detect_agent_exited`, `detect_garbled_output`) were left alone — different call path (LLM-driven analysis, not the mechanical sweep) and different input shape (raw text, not an agent). All 12 delegator methods on `MonitoringLoop` kept intact (not dead code — `tests/test_monitor.py` calls dozens of them directly); the list-based loop calls through the same delegators, not around them. 136 targeted tests pass (`test_monitor.py`, `test_monitoring_integration.py`), including the two tests that specifically assert the early-exit-vs-accumulate semantics (`TestMonitoringCycleGuardianSkip`) |
| 3.6 | Guardian LLM analysis entangled with DB reads/side effects | **PARTIAL, 2026-08-20** | `_evaluate_steering_eligibility` is a clean pure function (earlier work). `steer_agent`'s side-effecting intervention (recovery keystrokes, message send, in-memory record, DB log) extracted to `_apply_steering`, separable from the eligibility/precondition checks above it -- narrower than the original review's exact "decide_steering()/apply_steering(action)" proposal, since 2 of the 3 preconditions (task-done check, queued-message check) are themselves I/O-bound and can't be made pure without a larger pre-fetch-everything redesign. `analyze_agent_with_trajectory` already takes pre-fetched `tmux_output`/`past_summaries` as parameters (better than the original review's snapshot), but still fetches `accumulated_context`/`task`/`phase_info` internally via 3 DB-touching calls -- deliberately not touched this pass: it's this codebase's most heavily-tested trajectory-analysis method, and fully purifying it would mean changing its calling convention for every caller, a materially larger and riskier change than this session's other §3 work. 30 targeted tests pass |
| 3.7 | Guardian's key remapping unowned/duplicated | **PARTIAL, fixed 2026-08-19** | The live bug (§1 above) is fixed — `guardian_dispatch.py:399` reads the correct key. The deeper structural fix (a canonical-field-names dataclass, so this class of drift becomes a type error) remains undone |
| 3.8 | Dead code `_should_steer_agent` | **FIXED** | Deleted this session (Phase 4); zero remaining references |
| 3.9 | `Conductor` bundles unrelated QA-review op; no constructor-injected `llm_provider` | **FIXED, 2026-08-19** | QA-review bundling was already gone; `llm_provider` is now a required constructor param (matching `Guardian`'s existing pattern), passed through by `Monitor.__init__`. Both inline `get_llm_provider()` call sites removed, `self.llm_provider` used throughout `analyze_system_state`. 5 test files updated (`test_conductor.py`, `test_validation_agent_protection.py`, `test_monitoring_live.py`, `test_trajectory_monitoring.py`, `test_monitoring_integration.py`) to inject a mock instead of patching the module-level factory — this also surfaced and fixed a previously-silent bug in `test_monitoring_integration.py` where the injected mock was never actually being exercised (the old code called the real factory), which had been masking an unconfigured `AsyncMock.get_model_for_component` producing an un-awaited coroutine |
| 3.10 | Direct infra instantiation instead of DI | **PARTIAL, 2026-08-20** | Termination-duplication half is fixed (§4.2, single writer). `libtmux.Server()` DIP half now fixed: `AgentManager.__init__` takes an optional `tmux_server` param (defaults to a real `libtmux.Server()`), giving tests an injection seam instead of the previous `patch("...libtmux.Server")` + post-construction instance-attribute overwrite dance -- 3 test fixtures simplified to use it (`test_agent_manager.py`, `test_restart_agent_characterization.py`, `test_worktree_integration.py`). The `get_session()` vs. `session_scope()` imbalance is investigated but NOT fixed this pass: 24 raw `get_session()` calls remain across exactly 3 files (`launch_pipeline.py` 17, `manager.py` 4, `conductor.py` 3) -- `guardian.py`/`queue_service.py` are already fully on `session_scope()` (the original finding's file list is stale for those two), and `ticket_service.py` uses the unrelated-but-equally-safe `get_db()` context-manager pattern throughout, not this one at all. Most of the 24 sites already close their session via `try/finally` or a `with` block (not the Theme-A leak pattern), so this is a "add auto-commit/rollback consistency" task, not a leak fix -- genuinely separate, sizable work (17 sites alone in one file) deliberately not started without its own scoping decision |
| 3.11 | `TicketService.create_ticket` 470-line fused method | **FIXED, 2026-08-19** | Decomposed into 5 named `@staticmethod` helpers (`_validate_ticket_creation`, `_delete_ticket_cascade`, `_broadcast_ticket_event`, `_wait_for_ticket_approval`, `_index_new_ticket`); `create_ticket` itself is now a thin sequential orchestrator. Zero intended behavior change — every log message, exception message, and control-flow branch preserved verbatim |
| 3.12 | Duplicate cascade-delete in `TicketService` | **FIXED, 2026-08-19** | Both timeout-branch and rejection-branch cascade-deletes now call the shared `_delete_ticket_cascade(db, ticket_id, reason)` helper |
| 3.13 | Duplicate similarity thresholds | **FIXED, 2026-08-19** | `TicketSearchService` gained named class constants (`DUPLICATE_THRESHOLD=0.9`, `RELATED_THRESHOLD=0.7`, `SIMILAR_THRESHOLD=0.5`), used in `find_related_tickets`'s own classification and in `TicketService._index_new_ticket`'s duplicate-warning check (previously a second hardcoded `>= 0.9`) |
| 3.14 | `QueueService` priority-ordering duplicated 4× | **FIXED, 2026-08-20** | Extracted the byte-identical `case((Task.priority == "high", 3), ...)` expression into a module-level `_PRIORITY_ORDER_CASE` constant, exactly matching the original review's proposed fix. All 4 call sites now reference it instead of redefining it inline; each site's now-unused local `from sqlalchemy import case` import removed. Zero behavior change — verified via `git diff` line-by-line. 58 targeted tests pass (`test_queue_service.py`, `test_background_queue_processor.py`, `test_server_dispatch_endpoints.py`) |

### §4 — Core infrastructure (`src/core/`, `src/interfaces/`, `src/auth/`)

| # | Finding (short) | Status | Current evidence |
|---|---|---|---|
| 4.1 | `DatabaseManager` god class + `self.db_path` typo bug | **PARTIAL** | The specific live bug (wrong attribute name silently disabling a migration) is fixed; the god-class problem is worse — 18 hand-rolled `_migrate_*` methods now (was 3), still no schema-version table |
| 4.3 | `Config` 70-field object, untyped triple-enumeration | **PARTIAL, 2026-08-20** | The class itself is unchanged (94 attributes now, enumerated across `_apply_defaults`/`_apply_yaml_config`/`_load_env_overrides`/`to_env_dict`), and `validate()` still checks only the 3 provider/API-key pairings. Expanding `validate()` was deliberately **not** the fix: neither of this review's two live config bugs would have been caught by field validation — both were *inert* keys, which validate a value that is never consulted. The failure mode this enumeration actually produces is a key present in one enumeration site and missing or misspelled in another. Found a third instance (`d9edc5c`): `simple_config` reads `git.branch_prefix`, the shipped `hephaestus_config.yaml` wrote `git.worktree_branch_prefix`, so the key was never read and the hardcoded `"agent-"` default always won. Invisible for exactly the reason the other two were — the configured value equalled the default (`"agent-"` vs `"agent-"`, as `max_task_retries` was 5 vs 5). Fixed the key, then added `tests/test_config_keys_are_live.py`, a structural guard in the mould of `test_termination_invariant_single_writer.py`: it mutates every leaf in the shipped config and requires something observable to change in either loader (`Config` or `llm_config`, which consumes the `llm.*` subtree). Verified against the real bug — reinstating the old key name fails it. Three `ticket_tracking.embedding.*` keys are genuinely inert and allowlisted **with reasons** (that backend comes from the `EMBEDDING_BACKEND` env var, not YAML; wiring it up would change which embedding backend deployments get, and `store_factory` warns a backend/dimension mismatch corrupts an existing vector store), with a second test that fails if an allowlisted key ever starts working. Scope is stated precisely in the test: it guards `hephaestus_config.yaml` only — `workflow.yaml` has no single loader to diff against and was hand-checked instead (all 54 distinct key names appear as literals under `src/`) |
| 4.4 | `WorktreeManager.reload()` mutates global `Config` singleton | **FIXED** | Now mutates only instance-local state; docstring explicitly documents the bug this fixes |
| 4.5 | `WorktreeManager` SRP fusion + hardcoded dead conflict policy | **PARTIAL** | Merge-failure path fixed (§4.4, abort-and-preserve uniform); class-level git+DB+locking+policy fusion unchanged (1344 lines, up from ~900); config field still inert — see §1 above **Dead conflict policy fully closed, 2026-08-20**: the core `Config.conflict_resolution_strategy` field was already gone (verified absent at runtime; `worktree_manager` now returns the literal `"newest_file_wins"` with a comment saying the resolver is unconditional), and the `WORKTREE_CONFLICT_STRATEGY` env var with it. The last remnant was in the SDK (`f48004d`): `HephaestusConfig.conflict_resolution`, exported to spawned processes as a `CONFLICT_RESOLUTION` environment variable that nothing anywhere reads, with `run_continuous_pipeline` passing the resolver's only behaviour into it. Removed rather than implemented, following the resolution already applied to the core field. The class-level SRP fusion is still open. |
| 4.6 | `session_scope()` bypassed almost everywhere | **OPEN** | `WorktreeManager`: 7 manual vs. 1 `session_scope()`; `auth_api.py` unchanged |
| 4.7 | Two parallel LLM abstractions, silent LSP violation | **OPEN, one more implementer** | `AnthropicProvider`'s trajectory/coherence methods still stub hardcoded defaults; `MultiProviderLLM` is now a 3rd concrete implementer of the same ABC, not a fix |
| 4.8 | `LangChainLLMClient._create_model` 145-line if/elif dispatch | **PARTIAL, 2026-08-20** | Dispatch half fixed: `_create_model` (~134 lines) and its unmentioned twin `_initialize_models` (~76 lines of parallel if/elif over 4 *embedding* providers — same defect, same file, not in the original finding) are now registry lookups over `_MODEL_BUILDERS`/`_EMBEDDING_BUILDERS`, one builder per provider, matching the `MCP_TOOL_REGISTRY`/`mark_phase_complete` pattern already used elsewhere. 134→32 and 76→27 lines; adding a provider is a builder plus one dict entry. The "all provider packages unconditionally imported" half is deliberately NOT done and should not be: `langchain-openai`, `-groq`, `-google-genai`, `-community` are all hard, non-optional deps in `pyproject.toml`, so lazy imports would save nothing real while breaking the module-scope patch points the test suite depends on — the original finding's implied benefit doesn't exist under the current dependency declarations. Coverage was the bigger gap here: only openai/groq/openrouter had any prior test and none pinned construction arguments, so 21 tests were added (azure's fail-closed-without-endpoint path, google, the gpt-5 temperature pin, openrouter's forced-routing/reasoning-off/usage-include payloads, dispatch failure modes, chat-only embedding fallback). The 19 behavioral ones were run against the pre-refactor if/elif implementation in a clean worktree and pass identically — the actual equivalence evidence, as opposed to new tests passing on new code. **Gap-check follow-up (`8732e2d`)**: the same file also redefined `ModelAssignment`/`ProviderConfig`/`LLMConfig` alongside the canonical ones in `src/core/llm_config.py`, relying on the real ones being duck-typed in at runtime. The copies had drifted and not harmlessly — the local `ProviderConfig` lacked `api_version`, which both Azure builders read, so constructing with this module's own class would have had the field silently dropped by pydantic and hit `AttributeError` on the Azure path; the local `LLMConfig` also lacked `embedding_provider`, which is why `_initialize_models` reads it via `getattr` with a default. Nothing outside the file ever constructed them, so they were deleted in favour of importing the canonical models, plus a regression test asserting a real `ProviderConfig(api_version=...)` reaches `AzureChatOpenAI` |
| 4.9 | Duplicated CLI-output-parsing scan logic | **OPEN, partially normalized** | 4 subclasses still duplicate the scan loop; `ClaudeCodeAgent` converged onto the majority key-naming convention, narrowing the split from 2-vs-3 to 1-vs-4 |
| 4.10 | `auth_api.py` business logic in routes | **FIXED, 2026-08-19** | Extracted `AuthService` (`src/auth/auth_service.py`) with `register_user`/`authenticate`/`refresh_tokens`, matching the original review's proposed method names. Domain errors (`EmailAlreadyRegisteredError`, `UsernameAlreadyTakenError`, `AccountLockedError`, `InvalidCredentialsError`, `AccountNotActiveError`, `InvalidRefreshTokenError`, `InactiveUserError`, `WeakPasswordError`) carry `status_code`/`detail`/`headers`; the 3 routes are now `try/except AuthError` adapters that open a session and translate to `HTTPException`. `get_db_manager()` deliberately stayed in `auth_api.py` (the test suite's DB-injection seam patches it there). Zero intended behavior change — every status code, detail string/dict, and the one route with an extra `WWW-Authenticate` header preserved exactly; verified via `tests/test_authentication.py` (22 passed, 1 pre-existing unrelated failure confirmed via `git stash` isolation against the pre-refactor baseline — `test_register_success`, a test-DB-fixture issue unrelated to this change) |

---

## 3. New findings, by area

### Tooling — mypy was silently disabled repo-wide (found and fixed 2026-08-20)

`src/autopilot/spec.py` contained a prose comment beginning `# type: matches
validate_gate_result_schema's ...`. mypy parses `# type: ...` as a legacy PEP 484
annotation, fails on the prose, and reports `Invalid syntax` — an error it treats as
fatal (`errors prevented further checking`). `mypy src/`, one of the lint commands
CLAUDE.md documents, was therefore checking **nothing at all**, for as long as that
comment existed. Rewording it (`c38f143`) restored the gate; a note on the comment
records the trap, since the failure mode names neither the file's real problem nor
prose as the cause.

Two live bugs surfaced within minutes of the gate coming back, both of the
"silently swallowed, so nobody noticed" shape this review keeps finding:

- **`OrchestratorLogger.debug()` never existed** (`16ea4d1`). Three call sites already
  used it; the class defines only `log/info/warning/error/event/save_state`, has no
  `__getattr__`, and inherits from `object`, so each raised `AttributeError`. The worst
  sat in `run_single_workflow`'s handler for a failed `pipeline_metrics.json` patch —
  the `AttributeError` escaped into the enclosing `except Exception`, which logs
  "Failed to launch workflow" and returns `FAILED`, so an unreadable metrics file
  killed the whole launch and misreported why. `_pause_feature_for_review`'s site
  likewise skipped the `return` immediately following it.
- **`orchestrator.max_task_retries` was inert** (`1e9d95a`). All four retry paths read it
  via `spec.load_workflow_definition(...)`, which has never existed; every call site
  wrapped the import in `except Exception: max_retry = 5`, so the `ImportError` was
  swallowed and the hardcoded default always won. `AUTOPILOT_REFACTOR_ANALYSIS.md`
  documents both paths as reading this setting — the code did not. It looked functional
  only because the shipped config specifies exactly 5, the same as the fallback; editing
  `workflow.yaml` did nothing. Same shape as the inert worktree conflict-resolution
  config already recorded in §1. Replaced with a real `spec.get_max_task_retries()`
  modelled on its neighbour `get_max_review_runs()`.

- **Two MCP tools had never worked** (`1e4aa42`). `search_tickets` and
  `update_ticket_status` are both advertised to agents in `MCP_TOOL_REGISTRY`.
  `_tool_search_tickets` called `TicketSearchService(session)`, but that class defines
  no `__init__` and only static methods — so it raised `TypeError:
  TicketSearchService() takes no arguments` on its first line, before reaching
  `search_tickets`, which does not exist either. `_tool_update_ticket_status` called
  `TicketService.change_ticket_status`, which does not exist (it is `change_status`)
  and which requires a `comment` the tool never collected. Neither was fixable by
  rename alone: the advertised schemas omitted arguments the services require, so
  `workflow_id` (required, matching the `create_task` convention in the same registry)
  and `comment` (required) were added. `tags` stays advertised but is folded into the
  query text — only `status`/`priority`/`ticket_type` are supported filter keys, while
  `_ticket_text` indexes tags into the searchable document. 13 tests, 9 of which fail
  against the pre-fix code in a clean worktree.

- **Qdrant semantic search silently returned nothing** (`585210b`). `QdrantClient.search`
  was removed in qdrant-client 1.x (1.18 is the installed pin) in favour of
  `query_points`. The call raised `AttributeError`, the surrounding `except Exception`
  logged "Search failed in collection ..." and returned `[]` — indistinguishable to
  callers from "nothing matched", so memory/RAG lookups degraded to zero context instead
  of failing visibly. Only affects deployments using the documented
  `VECTOR_STORE_BACKEND=qdrant` fallback; turbovec is the default. The one existing
  vector-store test is an integration test gated on an OpenAI key, so this path had no
  coverage at all — 7 tests added against a mocked client.
- **`devtools.is_connected` crashed exactly when connected** (`729c167`). It read
  `self.ws.open`, which websockets removed from its asyncio `ClientConnection` (16.1
  installed). The two preceding terms short-circuit while disconnected, so the
  `AttributeError` fired only once the connection was live — the single case the property
  exists to report. Now uses `close_code`.
- **Four `AgentManager` delegators were mangled by the extraction script** (`a2f1304`).
  Each forwards to `LaunchPipeline`; the mechanical split renamed the target's real first
  parameter to `self` (`_ensure_codegraph_initialized(self: str)`,
  `_build_instructions_pointer(self: str, ...)`) and/or flattened keyword-only arguments
  into positional ones (`_build_and_send_launch_command` passing 13 positionals,
  `_resolve_worktree`, `_resolve_session_id`). None has a caller, which is why none ever
  surfaced. Repaired rather than deleted, given this class's documented history of an
  earlier extraction silently dropping four working methods.
- **Queue positions were never recalculated after unblocking a task** (`9603de4`).
  `QueueService(db_manager)` omits the required `max_concurrent_agents`, so it raised
  `TypeError` into an `except` that logged "Could not recalculate queue positions" and
  continued. Queued tasks kept stale positions after every unblock.
- **`llm_interface` misreported structured-output refusals** (`20f6884`). The OpenAI SDK
  leaves `message.parsed` as `None` on refusal; both trajectory and coherence analysis
  called `.model_dump()` on it, so a refusal surfaced as `'NoneType' object has no
  attribute 'model_dump'` three times over as the retry loop re-ran an unretryable
  condition. Control flow unchanged; the diagnostic now names the refusal.

**Baseline for whoever picks this up: 870 errors across 106 of 197 source files** (was 901
before this pass).** That
is a backlog, not a to-do list, and it should not be attacked wholesale. The
high-signal category is `[attr-defined]` (67) — the same class as both bugs above and
as the `worktree.branch_path` defect found by hand in §2.14 — and it is worth mining
first. `[union-attr]` (126) is dominated by `DatabaseManager | None` access, which is a
real nullability question rather than noise. `[arg-type]` (265) and `[assignment]` (145)
are mostly annotation debt. Every `[attr-defined]` hit flagged in this section has now been triaged to completion.
The `ConsoleEntry` hits were not a bug: `network_logs` is correctly typed, but `entry` was
bound to a `ConsoleEntry` earlier in the same function and Python scopes it function-wide,
so the NetworkEntry branches were renamed to `net_entry`. The same one-name-two-types
pattern accounts for `agents_api.py`'s `dict` / `.append` hit (`result` is a dict in one
branch and a list in another) — also not a runtime bug, and left alone. The
~35 `"None" has no attribute ...` hits across `mcp/frontend/*_routes.py` are NOT bugs —
they are the module-level `frontend_api = None` placeholder assigned at startup, i.e.
annotation debt.
`PipelineState._recovery_attempts`/`_design_branch`/`_design_worktree` are the dynamic
undeclared attributes §2.3 already records, now confirmed by the type checker.

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
**Done, 2026-08-19** — extracted verbatim into `src/autopilot/orchestrator/arbitration.py`
(671 lines); `phase_transitions.py` drops to 3000 lines. Re-exported under the original
names so existing callers/test patches keep working; the ~26 test patches targeting
`create_agent_for_task_direct`/`PhaseManager`/`build_phase_output` (called via
`arbitration.py`'s own top-level imports, not re-exported) were retargeted. Still 3000
lines — this fix narrows the god-module problem, it doesn't fully resolve it; the
remaining task-creation-claim/phase-advance-sweep/phase-task-creation/stuck-task-resume
mix is a candidate for a further split if this file keeps growing.

**`orchestrator/__init__.py` (3411 lines) — a second new god-module, mixing pipeline
execution with unrelated infrastructure.** 28 top-level functions: the real
pipeline-execution flow (`run_phase0`, `run_single_workflow`, `_run_one_feature`,
`run_feature_pipelines`, `run_single_design`, `run_continuous_pipeline`, `main`) plus config
getters, human-escalation prompting, and orchestrator-agent registration — none of which
belong to "run the pipeline." *Fix:* extract config getters to `config.py`, human-escalation
to its own module, matching the file's own package siblings.
**Partially done, 2026-08-19** — config getters (4 functions) and `prompt_human` extracted
verbatim to new `config.py`/`human_escalation.py` modules; `orchestrator/__init__.py` drops
to 3225 lines. Orchestrator-agent registration (`_register_orchestrator_agent`) and the
smaller monitored-workflow/stop-signal registries (`_register_monitored_workflow`/
`_is_workflow_monitored`, `_should_stop`/`_stop_events`) were deliberately left in place —
a plausible third "orchestrator process/runtime bookkeeping" module, but out of this pass's
scope; still open if this file keeps growing. Also found and removed an orphaned ~155-line
blank-line/stray-comment gap exposed by `prompt_human`'s extraction; other pre-existing
blank-line gaps elsewhere in the file (unrelated to this extraction) were left alone.

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

1. **Done, 2026-08-19.** The live bugs in §1 (Guardian's stale steering key, the
   disconnected retry budgets, the `_sync_stale_*` sweeps bypassing
   `status_derivation.py`, the dead `/me` auth stub, the inert worktree
   conflict-resolution config, `_complete_workflow`'s own status_derivation.py bypass
   found in a follow-up gap-check) — all fixed and tested; each was a
   silent-failure-shaped bug hiding behind a style-looking finding, exactly the pattern
   this whole refactor's bug-fix-history methodology has repeatedly found elsewhere.
2. **Split `phase_transitions.py` and `orchestrator/__init__.py`.** Both are now larger
   than the files this refactor already fixed for the same reason; leaving them
   unaddressed undercuts the refactor's own stated rationale. **Done, 2026-08-19**
   — `phase_transitions.py`'s ~620-line arbitration subsystem extracted to a new
   `arbitration.py` (671 lines), dropping it from 3539 to 3000 lines.
   `orchestrator/__init__.py`'s config getters and `prompt_human` extracted to new
   `config.py`/`human_escalation.py` modules, dropping it from 3411 to 3225 lines.
   The remaining leftover — orchestrator-agent registration and the monitored-workflow/
   stop-signal registries — extracted to new `agent_registration.py` (82 lines) and
   `runtime_registries.py` (96 lines), dropping `orchestrator/__init__.py` to 3086 lines
   (from its original 3411). `_orchestrator_agent_id` itself stayed in `__init__.py`
   deliberately: it's a mutable module global reassigned by `run_continuous_pipeline`
   (which stays) and read via deferred imports from `engine_client.py`/
   `phase_transitions.py` — only `_register_orchestrator_agent`, the side-effect-free
   function that produces the id, was safe to move. `_register_orchestrator_agent`'s
   `logger: OrchestratorLogger` parameter needed the same quoted-forward-reference +
   `TYPE_CHECKING` treatment as `human_escalation.py`'s `prompt_human`, for the same
   reason (avoiding a circular import back into `__init__.py`, where `OrchestratorLogger`
   is defined). Verified via `tests/test_orchestrator_helpers.py` (254 passed) plus
   `test_advance_phases.py`/`test_autopilot_service.py`/`test_phase_advancement_sweep.py`/
   `test_background_queue_processor.py`/`test_broadcast_scoping_round2.py` (421 more
   passed) — the full set of tests touching `_should_stop`/`_interruptible_sleep`/
   `_stop_events`/`_is_workflow_monitored`/`_register_orchestrator_agent`, either directly
   or via the deferred-import call sites in `phase_transitions.py`/`engine_client.py`/
   `background_loops.py`/`autopilot/service.py`.
3. **1.13/1.15/4.6 — the `except Exception`/manual-session patterns.** Still the largest
   volume of individual findings in the codebase (141 broad excepts, dozens of manual
   sessions); no single fix, but the original review's diagnosis (no service layer, so
   routes/methods wrap everything in one broad try/except) is still exactly right, and
   still the single highest-leverage remaining structural gap.
4. **`TicketService`/`ConductorService`/`auth_api.py`** — three areas this refactor never
   touched at all; still carry their original-review findings unchanged or worse.
   **Done, 2026-08-19** — 3.9, 3.11, 3.12, 3.13, and 4.10 all fixed (see §2 above).
5. Everything else in §3 above, roughly in the order listed. **Done, 2026-08-20** — 3.1
   (re-verified as a deliberate final boundary, not unfinished), 3.4 (`_monitoring_cycle`'s
   inline DB-query blocks extracted to 2 named methods), 3.5 (12-check hardcoded chain
   replaced with list iteration), 3.6 (`steer_agent`'s side effects split into
   `_apply_steering`), 3.10 (`libtmux.Server` DIP half fixed via constructor injection),
   3.14 (`QueueService`'s 4x-duplicated priority expression consolidated). Remaining open
   items now live entirely in §1/§2/§4 — see those tables for current status. Both
   findings the refactor had made *worse* than the original review found them are now
   addressed (2026-08-20): 1.18 (stop-workflow implementations had grown from 3 to 4,
   diverging on task force-failure — root-caused and fixed) and 2.3 (the two orchestrator
   god functions had grown to 612/569 lines with zero coverage — characterization suites
   written first, then decomposed to 498/435). See §1 and §2 above for both.
