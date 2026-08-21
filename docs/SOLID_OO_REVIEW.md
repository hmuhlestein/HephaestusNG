# SOLID / OO Maintainability Review

Scope: full backend (`src/`, ~57k lines) + the largest frontend components. This review is
class/function-level: for each finding — where the logic *actually lives* today, why that hurts
maintainability, and where it *should* live. Findings are grouped by subsystem, ordered
roughly by severity/impact within each group. A cross-cutting summary is at the bottom.

Companion document: [`docs/ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md) covers runtime/flow
issues (DetachedInstanceError, process supervision, etc.) that were fixed in a prior pass. This
review is about **class/function design**, not runtime bugs — some items here are adjacent to
that doc's H-3 (status derivation) and are cross-referenced below.

---

## 1. MCP / API layer (`src/mcp/server.py`, `api.py`, `autopilot_api.py`, `tickets_api.py`, `devtools.py`)

This is the highest-concentration problem area in the codebase. `server.py` alone is 6885 lines
and has no service layer beneath its routes — DB access, git operations, LLM calls, and HTTP
concerns are all interleaved directly in route handlers.

### 1.1 `server.py` is a router + business-logic + DB-access + git + OAuth god module
**Location:** `src/mcp/server.py` (whole file — e.g. task lifecycle at 1793-2984, OAuth at
5357-5516, workflow execution at 6063-6475, git commit logic at 2846-2943)

**Smell:** SRP violated at the module level. One file owns: 20+ Pydantic models, the `ServerState`
composition root, FastAPI init/CORS, agent auth, rate limiting, the task lifecycle state machine,
ticket-clarification/approval endpoints (partially duplicating `tickets_api.py`), a full OAuth 2.0
authorization server (`/oauth/*`, `/.well-known/*`), workflow CRUD, and raw `GitPython` calls.

**Why it hurts:** No one engineer can hold this file's responsibilities in their head. Unrelated
features (OAuth, ticket approval, workflow execution) constantly collide in the same file, causing
merge conflicts. Importing `server.py` for its Pydantic models drags in `RAGSystem`,
`WorktreeManager`, `git.Repo`, embeddings, and OAuth — the whole stack.

**Proposed fix:** Split by domain into routers under `src/mcp/`: `oauth_api.py` (5357-5516),
`workflow_api.py` (6063-6475), `agent_management_api.py` (4866-5327). Keep `server.py` as the
composition root only (assembles `ServerState`, mounts routers, owns startup/shutdown).

### 1.2 `update_task_status` — 451-line handler fusing five unrelated concerns
**Location:** `src/mcp/server.py:2535-2984`

**Smell:** God function. In one handler: auth check, memory/embedding persistence, output-artifact
file validation, spec-gate evaluation (constructs `PhaseManager` and calls `mark_phase_complete`
inline), validator-agent spawning (nested `async def spawn_validation_async`), raw git
commit-and-push (`Repo(wt_path).git.commit(...)`), ticket auto-linking, and websocket broadcast.

**Why it hurts:** A bug in "auto-link commit to ticket" requires reading and reasoning about all
451 lines, since task/session state mutates incrementally throughout. Testing the auth-check path
alone requires standing up a DB, a git repo, a worktree, and a ticket.

**Proposed fix:** Extract `TaskCompletionService` (`src/services/`) with discrete methods:
`record_learnings()`, `verify_output_artifact()`, `fire_spec_gate_if_ready()`, `spawn_validation()`,
`commit_and_link_ticket()`. Route handler becomes: authenticate → call service → return response.

### 1.3 `create_task` (711 lines) and `process_queue` (557 lines) independently duplicate the same enrichment pipeline
**Location:** `src/mcp/server.py:1793-2504` (`create_task`, with an inner ~280-line
`process_task_async` closure) and `src/mcp/server.py:1159-1715` (`process_queue`)

**Smell:** Duplicated logic + god functions. Both independently do phase-ID-to-UUID resolution,
RAG memory retrieval, project-context building, LLM enrichment, and near-duplicate-task detection
via embeddings — with different variable names and log prefixes (`[QUEUE_ENRICHMENT]` vs. the
`create_task` version) but the same underlying steps, including the same defensive
`isinstance(enriched_desc, dict)` normalization repeated 3+ times.

**Why it hurts:** A bug fix applied to one enrichment path must be manually reapplied to the
other; the code's own "BUG FIX" comments show this has already happened. Neither path is
unit-testable without a live DB and mocked LLM/embedding services — there's no
`TaskCreationService.create()` to call directly.

**Proposed fix:** Extract `TaskCreationService`/`TaskEnrichmentService` with
`resolve_phase()`, `check_duplicate_task()`, `enrich_and_persist()`, `maybe_queue_or_assign()`.
Both `create_task` and `process_queue` call the same methods instead of re-deriving the pipeline.

### 1.4 Phase-ID "order number vs. UUID" resolution duplicated 12+ times
**Location:** `src/mcp/server.py:1229, 1477, 1900, 1903, 2141, 4940`;
`src/mcp/api.py:267, 340, 528, 870, 2625`

**Smell:** The pattern `if phase_id.isdigit(): lookup by order else: lookup by UUID` is
copy-pasted at 12+ call sites. The surrounding code comments explicitly reference "BUG FIX" for
this exact resolution logic more than once — i.e. it has already broken and been patched
piecemeal rather than centralized.

**Why it hurts:** Any change to the ID encoding scheme (e.g. switching to ULIDs, which also start
with digits) requires updating all 12+ sites; missing even one reintroduces a bug class that's
already been fixed once.

**Proposed fix:** Extract one `PhaseResolver.resolve(phase_id_or_order, workflow_id) -> Phase` in
`src/services/phase_resolver.py`; replace all call sites.

### 1.5 Tool dispatch (`execute_tool`, `_handle_devtools_tool`, `list_tools`) — string-branching instead of a registry (OCP)
**Location:** `src/mcp/server.py:6476-6641` (`execute_tool`, 9 `elif` branches),
`6644-6791` (`_handle_devtools_tool`, 13 `elif` branches),
`5547-6063` (`list_tools`, 516-line function whose body is a hand-written tool-schema literal)

**Smell:** Three separate hand-rolled "tool name → behavior" mechanisms, none sharing an
abstraction. Every new MCP tool requires editing three places: the schema in `list_tools`, the
dispatch branch in `execute_tool`, and (for devtools tools) another branch in
`_handle_devtools_tool` — with inconsistent per-branch error handling (some raise `HTTPException`
for missing args, some don't).

**Why it hurts:** Triples the chance of drift between what's advertised and what's implemented; a
tool can be documented in `list_tools` and silently missing from `execute_tool`, or vice versa.

**Proposed fix:** One `TOOL_REGISTRY: Dict[str, ToolSpec]` (name, JSON schema, handler,
required-args), populated via `@register_tool(...)` decorators co-located with each handler.
`list_tools`, `execute_tool`, and `_handle_devtools_tool` collapse into registry lookups.

### 1.6 `ServerState` — DI container + service locator + one-off migration runner + pub/sub hub, as a global singleton
**Location:** `src/mcp/server.py:447-626`

**Smell:** DIP violation. `ServerState` directly constructs and holds 9+ concrete managers
(`DatabaseManager`, `AgentManager`, `RAGSystem`, `PhaseManager`, `WorktreeManager`,
`ResultValidatorService`, `EmbeddingService`, `TaskSimilarityService`, `QueueService`) as public
mutable attributes, plus owns DB migration logic (`_migrate_is_active_column`), active-project
bootstrapping (`_load_active_project`), and WebSocket/SSE broadcast fan-out (`broadcast_update`) —
four unrelated responsibilities on one class, instantiated once as `server_state` and imported as
a global from `tickets_api.py`, `autopilot_api.py`, `projects_api.py`,
`src/autopilot/orchestrator.py`, and `src/services/ticket_service.py` (4 separate import sites).

**Why it hurts:** `TicketService` — nominally a domain service — imports `server_state` from the
**web server module** just to call `.broadcast_update(...)`, making it untestable without
partially booting `server.py`, and forcing the "lazy import to avoid circular deps" workarounds
visible in comments throughout `tickets_api.py`/`autopilot_api.py`. Nothing in the codebase can
substitute a fake `AgentManager` in tests without monkeypatching the global.

**Proposed fix:** Split into `AppServices` (pure DI container), `ProjectBootstrapper` (migration +
active-project load, run once and discarded), and a standalone `EventBus`/`BroadcastHub` (no
FastAPI import) injected into services that need to publish. Introduce a narrow `Broadcaster`
protocol so `TicketService`/orchestrator depend on an interface, not the server module.

### 1.7 `FrontendAPI` — 84 methods spanning 9+ unrelated domains (ISP)
**Location:** `src/mcp/api.py:34-2703` (`class FrontendAPI`)

**Smell:** One class mixes dashboard stats, task/agent queries, memory search, graph
visualization, workflow/phase info, guardian/conductor analyses, results/validation downloads,
blocked-task tracking, and phase-prompt versioning (create/publish/restore/diff/preview — 8
methods alone). A consumer needing only `get_dashboard_stats()` still depends on the same object
that owns prompt-diffing logic.

**Why it hurts:** Changes to prompt-versioning risk breaking unrelated dashboard-stats tests that
share fixtures/mocks of this one class; the class name gives no hint what it actually does.

**Proposed fix:** Split by bounded context: `DashboardService`, `AgentQueryService`,
`PhasePromptVersionService`, `TaskPromptOverrideService`, `ResultsService`,
`GuardianAnalysisService`. The file already has thin 1:1 passthrough router functions
(2705-3125) — the split point already exists structurally, it just needs to target the new
services instead of one god class.

### 1.8 `get_project_design_status` — 300-line handler doing ad hoc aggregation instead of using a service
**Location:** `src/mcp/autopilot_api.py:1901-2195`

**Smell:** Manually builds `phase_map`/`agents_map`, deduplicates agent lists via linear scan,
computes "overall status" via a manual priority cascade, searches the filesystem for a matching
feature folder by substring match, and calls `derive_feature_status` inline — none of it reusable
outside the HTTP layer.

**Proposed fix:** Extract `DesignStatusService.get_status(project_id, filename) -> DesignStatusDTO`.

### 1.9 Ticket endpoints split across two files; duplicate Pydantic models
**Location:** `src/mcp/server.py:3865-4130` (`request_ticket_clarification_endpoint`,
`approve_ticket_endpoint`, `reject_ticket_endpoint`) vs. `src/mcp/tickets_api.py`
(create/update/status/comment/search/stats/resolve/link-commit). `RequestTicketClarificationRequest`/
`Response` and `ApproveTicketResponse`/`RejectTicketResponse` are defined **in both files**
(`server.py:317-358` and `tickets_api.py:345-390`).

**Smell:** `tickets_api.py`'s own docstring says it was "extracted from server.py for better
modularity," but three endpoints and their models were left behind, half-finishing the split.

**Why it hurts:** A developer looking for "where do I change ticket approval" has to know to
check `server.py`, not the obviously-named `tickets_api.py`. The duplicate model definitions can
silently diverge.

**Proposed fix:** Finish the extraction — move the 3 remaining endpoints into `tickets_api.py`'s
router, delete the duplicate models from `server.py`.

### 1.10 Task/agent serialization duplicated across 6 call sites
**Location:** `src/mcp/api.py:264-280, 338-369, 522-541, 867-885`; `src/mcp/server.py:4876-5000`

**Smell:** The "ORM row → response dict" mapping (phase resolution, description fallback, UTC `Z`
suffixing) is hand-rolled independently at ~6 sites with slightly different field subsets.

**Proposed fix:** `TaskSerializer.to_summary_dict()`/`to_detail_dict()`,
`AgentSerializer.to_dict()` used everywhere instead of inline dict literals.

### 1.11 Repair/rerun orchestration lives in the API module instead of the orchestration layer
**Location:** `src/mcp/autopilot_api.py:618-812` (`rerun_design`), `847-998`
(`spawn_repair_review_agent`), `999-1120` (`_run_repair`)

**Smell:** These functions construct `DatabaseManager()`/`WorktreeManager()` directly and spawn
subprocess/tmux-backed agents — materially the same job `AgentManager`/`orchestrator.py` already
do, reimplemented a third time in the API layer.

**Proposed fix:** Move into `src/autopilot/` next to `orchestrator.py`, expose
`RepairService.spawn_review()`/`.rerun()`, called from the (thin) route handler.

### 1.12 Ad hoc `DatabaseManager()`/`WorktreeManager()` construction bypassing the shared instance
**Location:** `src/mcp/autopilot_api.py:705-706, 2973-2974, 2988, 2997` (`run_health_audit`
defaults to constructing its own `DatabaseManager()` when not given one); `src/mcp/api.py:3041`
(hardcodes `DatabaseManager("hephaestus.db")`)

**Why it hurts:** Two independent SQLAlchemy engines against the same SQLite file risk
locking/consistency surprises — this is a correctness risk, not just style.

**Proposed fix:** Require `db_manager` as a parameter everywhere (no default construction); replace
the hardcoded path literal with `get_config().database_path`.

### 1.13 Pervasive broad `except Exception` (66× in server.py, 35× in autopilot_api.py) masking missing service boundaries
**Smell:** Not a SOLID letter violation directly, but the direct symptom of 1.1-1.3: because
business logic isn't isolated into services with typed failure modes, route handlers wrap huge
try-blocks around 5-10 unrelated operations with one `except Exception: log/500`, hiding which
operation actually failed. Debugging relies on grep-ing hundreds of manually placed
`logger.info(f"[TAG] ...")` calls instead of typed exceptions.

**Proposed fix:** A byproduct of 1.2/1.3/1.8/1.11 — once each concern is its own method, exceptions
can be narrowly typed (`EnrichmentFailedError`, `ValidationSpawnError`) and the route's
try/except becomes a thin HTTP-status translation layer.

### 1.14 Duplicate `/projects` CRUD implemented independently in two files
**Location:** `src/mcp/projects_api.py` (whole file, 288 lines) vs.
`src/mcp/autopilot_api.py:1329-1691`

**Smell:** DRY violation. Both files independently redeclare `ProjectItem`/`ProjectCreate`/
`ProjectUpdate` and separate `list_projects`/`create_project`/`update_project`/`delete_project`
handlers over the same `AutopilotProject` table — with **different logic**: `autopilot_api.py`'s
version calls `_sync_project_designs`, `projects_api.py`'s doesn't; the "delete active project →
reassign new active" fallback differs subtly between the two (`projects_api.py:189-226` vs.
`autopilot_api.py:1653-1690`).

**Why it hurts:** A fix applied to one implementation is easy to miss in the other — this is the
same failure mode as 2.1/2.2 (multiple writers, one concept) applied to project CRUD.

**Proposed fix:** Single `ProjectService` in `src/services/`; both routers delegate to it if both
URL prefixes must remain live.

### 1.15 ~60 of 61 DB sessions in `server.py` use manual `get_session()` instead of a context manager
**Location:** 60 occurrences of `session = server_state.db_manager.get_session()` vs. 1 use of
`with ...get_session()` (grep-verified). Concretely leaky in `update_task_status`: the session is
acquired at line 2551 with no enclosing `try` until several lines later.

**Why it hurts:** Any exception between session acquisition and the `try:` leaks a DB connection;
at 60 call sites this is a real connection-pool-exhaustion risk under load, and mirrors the
`session_scope()`-bypass problem already flagged for `WorktreeManager`/`auth_api.py` in 4.6.

**Proposed fix:** Standardize on `with db_manager.get_session() as session:` (already used
correctly via `get_db()` in `autopilot_api.py`/`projects_api.py`) or inject via
`Depends(get_db_session)`.

### 1.16 Circular-import workarounds signal project-activation logic has no home
**Location:** `autopilot_api.py:1680` (`from src.mcp.projects_api import _apply_active_project`)
and `projects_api.py:271` (`from src.mcp.server import server_state`) — a real import cycle:
`server.py → projects_api.py → server.py`.

**Why it hurts:** These deferred/local imports exist purely to dodge circular-import errors — a
strong signal that project-activation logic belongs to neither router but to a service layer both
could depend on without cycling (same root cause as 1.6/3.11).

**Proposed fix:** Move activation logic into `ProjectActivationService` in `src/services/`,
imported normally by both routers.

### 1.17 Validation-outcome handling duplicated across 4 near-identical "terminate + advance queue" closures
**Location:** `give_validation_review` (`server.py:3286-3473`) and `submit_result_validation`
(`server.py:3634-3750`), each defining its own nested closure:
`terminate_and_process_queue`, `terminate_both_and_process_queue`,
`terminate_validator_and_process_queue`, `terminate_result_validator_and_process_queue`.

**Why it hurts:** Every validation-outcome branch must remember to call `process_queue()`
manually — a forgotten call silently stalls the queue. Worktree-commit logic
(`git.add`/`git.commit`) is independently duplicated in both outer functions too.

**Proposed fix:** `QueueService.release_agent_and_advance(agent_id)` as one atomic operation,
called from all branches instead of four ad hoc closures.

### 1.18 Three independent "stop a workflow" implementations with three different terminal states
**Location:** `src/mcp/api.py:2934` (`/api/workflows/{id}/stop`), `src/mcp/server.py:6359`
(`/api/workflow-executions/{id}/stop`), `src/mcp/server.py:6455` (`cancel_workflow`) — one sets
status to `"failed"`, another `"paused"`, the third `"failed"` again, via three separate tmux
teardown implementations.

**Why it hurts:** Callers can't know which of two same-named-concept endpoints to use; a
tmux-cleanup bug fix needs three separate edits.

**Proposed fix:** `WorkflowLifecycleService.stop(workflow_id, terminal_status)`; deprecate one of
the duplicate endpoints.

### 1.19 Duplicate trusted-agent allowlists that can silently diverge
**Location:** `KNOWN_SYSTEM_AGENTS` at `server.py:635-643` vs. a near-identical inline
`known_system_ids` set at `server.py:1095-1103` (inside `verify_agent_id`).

**Why it hurts:** Adding a new trusted system-agent ID requires editing two hardcoded sets in the
same file; missing one produces inconsistent auth behavior between endpoints that should share
one trust policy.

**Proposed fix:** Single `AgentTrustPolicy.is_system_agent(agent_id)` used by both
`verify_agent_authentication` and `verify_agent_id`.

### 1.20 `TicketService` accessed via a hand-rolled lazy-singleton getter instead of DI
**Location:** `src/mcp/tickets_api.py:22-32` (`_ticket_service` module global +
`_get_ticket_service()`), called by all ~10 route handlers in the file.

**Why it hurts:** Impossible to substitute a fake service for tests without monkeypatching the
module global — the same service-locator smell as `server_state` (1.6), scoped to one file.

**Proposed fix:** `Depends(get_ticket_service)` FastAPI provider instead of the manual global.

---

## 2. Orchestrator / autopilot pipeline (`src/autopilot/orchestrator.py`, `src/workflow_engine/`, `src/phases/phase_manager.py`)

### 2.1 "Is this workflow/design done?" is independently reimplemented in 4+ places, bypassing `status_derivation.py`
**Location:** `src/core/status_derivation.py` (the centralized module, added this session — see
`ARCHITECTURE_REVIEW.md` H-3) vs. `src/autopilot/orchestrator.py:646-764`
(`is_design_fully_complete`), `:3092-3125` (inline check in `run_single_workflow`'s poll loop),
`:3742-3757` (`run_design_aggregate`'s boolean rollup), and `src/phases/phase_manager.py:1066-1076`
/ `:1183-1194` (`_complete_workflow`, `_populate_feature_folder` — unconditional status writes with
no rollup check at all).

**Smell:** Duplicate business rule / shotgun logic. `status_derivation.py`'s own docstring calls
it "the single source of truth," but a repo-wide grep confirms only `src/mcp/autopilot_api.py`
actually calls `derive_feature_status`/`derive_design_status`/`derive_workflow_status`.
`orchestrator.py` and `phase_manager.py` — the two largest orchestration files — never call it.
Each has its own independent definition of "done," using different signals: task-status sets
(status_derivation.py) vs. pending/failed/agent/git-branch/phase-count checks
(`is_design_fully_complete`) vs. yet another `all_completed`/`any_failed` chain
(`run_design_aggregate`) vs. "last phase finished → mark completed, full stop" with zero
sibling-feature check (`phase_manager.py:1183-1194`).

**Why it hurts:** These four+ definitions of "done" can and do disagree — this is precisely the
bug class visible in recent git history (`fix: respect DB 'paused' status in feature status
derivation`, `fix: derive feature status from task statuses instead of DB value`): each fix
patched exactly one of the N duplicate implementations, leaving the others still wrong.
`phase_manager.py:1183-1194` in particular is the most severe instance — it can mark a
multi-feature design "completed" the moment the *first* feature's workflow finishes populating its
folder, directly violating the "any ACTIVE feature → design ACTIVE" rule the centralized function
enforces elsewhere in the same system.

**Additionally**, a fifth, structurally different status source exists:
`src/mcp/autopilot_api.py:196-201`'s `_feature_status(metrics)` derives feature status from a
filesystem `metrics.json` (`product_validated`/`stop_reason`), a parallel pipeline not driven by
DB tasks at all.

**Proposed fix:** Make `is_design_fully_complete`, `run_design_aggregate`, and
`phase_manager.py`'s two write sites delegate to `derive_workflow_status`/`derive_design_status`
for the core answer, layering only orchestrator-specific extras (unmerged git branches,
active-agent checks) on top. Remove the inline recomputation in the poll loop. This is the
single highest-leverage fix in the whole review — it collapses a proven, recurring bug class.

### 2.2 Two competing write paths for `Feature.status`, neither going through `status_derivation.py`
**Location:** `src/autopilot/orchestrator.py:1652-1719` (`_update_feature_status`) vs.
`:3646-3660` (`run_feature_pipelines` sets `Feature.status = "skipped"` inline, populating fewer
fields than the helper it should have called)

**Why it hurts:** Two independent writers can race on the same row with different opinions about
status — a split-brain, compounded by `status_derivation.py`'s own self-healing write-back
running as a third, unrelated writer whenever `autopilot_api.py` calls it.

**Proposed fix:** Route all feature-status writes through one function; fix the inline block at
3646-3660 to call `_update_feature_status(...)`.

### 2.3 `run_single_workflow` — 465-line god function; `run_continuous_pipeline` — 430-line god function with a dynamically-bolted-on field
**Location:** `src/autopilot/orchestrator.py:2792-3257` (`run_single_workflow`);
`:4022-4487` (`run_continuous_pipeline`, `state._recovery_attempts` added via dynamic attribute
access around line 4254 rather than a declared `PipelineState` field)

**Smell:** `run_single_workflow` fuses git worktree setup, the polling loop, phase-transition
dispatch, agent-state-change logging, credit-exhaustion detection, stuck/impasse detection,
human-escalation, and final git-merge logic — all sharing loop-local mutable state.
`run_continuous_pipeline` bootstraps the SDK, registers a DB agent record, and runs a 200+ line
`while True` loop; `_recovery_attempts` being a dynamic (undeclared) attribute means no type
checker catches a typo in the name.

**Why it hurts:** A bug in the final-merge block can't be exercised without running the entire
polling/credit/impasse machinery first. `_recovery_attempts` can silently break the
recover->escalate path if renamed elsewhere without a matching rename here.

**Proposed fix:** Extract a `WorkflowPoller` class (injected agent/task gateways, merge
coordinator, impasse detector) with `_setup_worktree()`/`_poll_loop()`/
`_handle_workflow_completion()`/`_handle_stuck_state()`. Add `recovery_attempts: int = 0` as a
declared `PipelineState` field; extract a `ContinuousPipelineRunner` class.

### 2.4 Global mutable orchestrator identity + singleton import (DIP)
**Location:** `src/autopilot/orchestrator.py:69-70` (module-level `_orchestrator_agent_id`),
`:488-505`, `:4128-4157` (mutations), `:441-485` (`create_agent_for_task_direct` importing
`from src.mcp.server import server_state`)

**Why it hurts:** Two orchestrator runs in one process (tests, future multi-tenancy) cannot
coexist — one run's global leaks into the other. Untestable without booting the real
`src.mcp.server` singleton (same root cause as 1.6).

**Proposed fix:** Wrap identity/status and DB/agent-manager references in an
`OrchestratorContext` dataclass, constructed once and threaded explicitly through
`run_single_design`/`run_single_workflow`/helpers.

### 2.5 `attempt_recovery` — three unrelated recovery strategies fused into one function
**Location:** `src/autopilot/orchestrator.py:767-914`

**Smell:** DB task-retry logic, four sequential raw `subprocess.run(["git", ...])` calls
(merge --abort, checkout main, clean -fd, reset --hard), and stale-agent termination, all in one
148-line function — with an inline aliased re-import of `get_db`/`Task`/`Agent` (line 811)
duplicating imports the module already has at the top.

**Why it hurts:** The three actions have no independent success/failure signaling; a caller can't
retry just the git cleanup. Business logic (which tasks are worth retrying) is interleaved with
pure infra (raw git subprocess calls), so retry-count policy can't be unit tested without a real
git repo on disk.

**Proposed fix:** Extract `_retry_failed_tasks()`, `_clean_stale_git_state()`,
`_terminate_stale_agents()` as independently callable/testable units.

### 2.6 `_create_phase_task` — hardcoded retry policy buried in task-creation code, with a side-effecting workflow pause
**Location:** `src/autopilot/orchestrator.py:2659-2789` (`MAX_PHASE_ATTEMPTS = 3` at line 2701,
flips `wf.status = "paused"` at 2716-2719 as a buried side effect)

**Why it hurts:** Changing retry policy means spelunking inside task-creation code instead of
editing a config object. The failure-cleanup path (2759-2768) opens a **second, disconnected**
DB session rather than continuing the original transaction, leaving a window where pollers can
observe an orphaned "pending" task with no agent.

**Proposed fix:** Move `MAX_PHASE_ATTEMPTS` to a module/config constant; extract
`_check_retry_bound()` as a separate policy check; wrap task+agent creation in one
compensating-transaction helper.

### 2.7 `_fire_phase_transition` smuggles `workflow_id` into `PhaseManager` via post-construction attribute mutation (DIP)
**Location:** `src/autopilot/orchestrator.py:2598-2656`, specifically:
```python
pm = PhaseManager(DatabaseManager())
pm.workflow_id = workflow_id
result = pm.mark_phase_complete(phase_id, ...)
```

**Why it hurts:** `PhaseManager` already caches orchestrators keyed by `workflow_id`
(`phase_manager.py:85-86`) — if it ever adds construction-time caching keyed on that value, this
smuggled-attribute pattern will silently produce wrong results. A fresh `PhaseManager` and fresh
`DatabaseManager()` are constructed on every single call, with no injection seam for tests.

**Proposed fix:** Make `workflow_id` a required parameter of `mark_phase_complete(workflow_id,
phase_id, ...)`; inject a shared `PhaseManager` instance into the orchestrator instead of
constructing one ad hoc per call.

### 2.8 `_advance_phases` — good decomposition, but "priority order" is enforced only by convention
**Location:** `src/autopilot/orchestrator.py:2321-2377` (dispatcher),
case handlers at `2426-2596`

**Smell:** This is the best-structured code in the file — `_case_start_first_phase`,
`_case_in_progress_no_tasks`, `_case_completed_with_successor`, `_case_in_progress_complete` are
each properly extracted — but "evaluated in priority order" (per the docstring) is enforced only
by the literal sequence of `if result is not None: return result` statements; nothing prevents a
future edit from silently reordering or inserting a case.

**Proposed fix:** Replace the `if` chain with a declared, ordered list
(`PHASE_ADVANCE_RULES = [...]`) iterated by the dispatcher, so the order is data that can be
inspected/tested independently. **Use this file's own pattern as the template for 2.3/2.5/2.6.**

### 2.9 `WorkflowOrchestrator._check_condition` — hand-rolled expression language via regex (OCP)
**Location:** `src/workflow_engine/orchestrator.py:441-514`

**Smell:** Condition strings like `"score < 0.6"` are parsed via one regex then dispatched
through a hardcoded `if operator == "<": ... elif operator == "<=": ...` chain. A malformed
condition just logs a warning and evaluates to `False` — silent, not a startup-time failure.

**Cross-file gap (finding 2.10):** `src/workflow_engine/config_validator.py:296-337` validates
that `condition["action"]` and `condition["target"]` are well-formed, but **never** validates that
`condition["if"]` actually matches the regex `_check_condition` requires — the validator's own
purpose ("catch errors before the pipeline runs") is defeated for this exact field. A YAML typo
like `if: "score between 0.5 and 0.9"` passes validation cleanly and silently no-ops at runtime.

**Proposed fix:** Replace the if/elif chain with an `{"<": operator.lt, ...}` dict dispatch.
Export the condition grammar as a reusable validator function and have `config_validator.py` call
it on every `condition["if"]` value.

### 2.11 `WorkflowOrchestrator._phase_name_to_order` hardcodes one workflow's phase vocabulary into a supposedly generic engine (LSP-adjacent)
**Location:** `src/workflow_engine/orchestrator.py:293-323`

**Smell:** `WorkflowOrchestrator` is documented as generic, config-driven ("configurable per
workflow via orchestrator_config"), but this method hardcodes a dict of exactly the autopilot
pipeline's phase names (`"product_requirements": 1, "architecture_design": 2, ...`). Any other
workflow definition using numeric `after_phase` references gets silently wrong resolution
(falls back to `0`) with no error.

**Proposed fix:** Derive the name→order mapping from actual `Phase.order` DB values (already
available at `phase_manager.py`'s call sites) and pass it into `evaluate()` rather than baking one
caller's vocabulary into the engine.

### 2.12 `PhaseManager.mark_phase_complete` — 267-line function with copy-pasted DB-write boilerplate across 5 branches
**Location:** `src/phases/phase_manager.py:607-896`

**Smell:** Branches on `force_action` (2 cases) then on `OrchestrationAction` (5 cases,
746-878), each inlining its own commit/logging/return-dict construction. The
`execution.status = "completed"; ...; session.commit()` block is near-verbatim copy-pasted 3
times (671-674, 709-712, 746-750, 788-791). The `SKIP` action (already defined in the
`OrchestrationAction` enum at `workflow_engine/orchestrator.py:27`) is **never handled here** —
it silently falls through to the generic `{"action": "continue", ...}` default, losing "skip"
semantics entirely.

**Proposed fix:** Extract `_handle_continue`/`_handle_retry`/`_handle_goto`/`_handle_arbitrate`/
`_handle_fail` dispatched via a `Dict[OrchestrationAction, Callable]`, plus a shared
`_close_execution(execution, summary)` helper for the repeated boilerplate — this also turns the
missing `SKIP` handler into a `KeyError` (visible) instead of a silent fallthrough (invisible).

### 2.13 `PhaseManager._get_orchestrator` — DB read + business decision + cache mutation + swallowed errors, all fused
**Location:** `src/phases/phase_manager.py:898-929`

**Smell:** Queries the DB for workflow/definition, decides "sequential → return None," and
mutates `self._orchestrators` cache, all inside one `try/except Exception: return None`.

**Why it hurts:** A malformed `orchestrator_config` JSON is silently converted into "treat this
workflow as sequential" — masking real config errors as normal behavior, directly undermining
`config_validator.py`'s purpose (same theme as 2.10).

**Proposed fix:** Split into `_load_orchestrator_config()` (raises on real errors) and
`_get_or_create_orchestrator()` (pure caching). Log real exceptions at ERROR with the actual
traceback instead of a generic message.

### 2.14 `WorkflowTerminationHandler` — well-factored, but non-atomic across its own sub-steps
**Location:** `src/workflow/termination_handler.py:26-349`

**Smell:** `_terminate_workflow_agents` opens its **own fresh session** (line 121) while
`_cancel_workflow_tasks`/`_cleanup_workflow_resources` reuse the parent session from
`terminate_workflow`. A crash between steps can leave agents terminated but tasks not yet marked
failed (or vice versa) — the one place in this scope where a real correctness risk (not just
style) flows directly from an SRP/session-management inconsistency, despite the class being one
of the better-organized files reviewed.

**Proposed fix:** Pass the single parent session into `_terminate_workflow_agents` too, commit
once at the end of `terminate_workflow` — or explicitly document the non-atomicity if agent
termination genuinely can't be transactional with the DB (tmux/process-level side effects).

---

## 3. Agents / monitoring (`src/agents/manager.py`, `src/monitoring/*`, `src/services/*`)

### 3.1 `AgentManager` — god class mixing five unrelated responsibilities
**Location:** `src/agents/manager.py:29-2173` (whole class)

**Smell:** Combines: (1) tmux session lifecycle, (2) prompt/message construction (~250 lines of
templating), (3) per-CLI delivery mechanics with chunking/retry (~170 lines,
`is_claude/is_droid/is_codex/is_pi` branching), (4) DB persistence scattered through nearly every
method, and (5) cross-agent messaging/broadcast.

**Why it hurts:** A prompt-chunking fix for a new CLI tool requires touching the same class as a
tmux-teardown fix or a "paused" status change. Testing prompt formatting in isolation requires
standing up `DatabaseManager`, `libtmux.Server`, `WorktreeManager`, and an LLM provider — none of
which prompt formatting actually needs.

**Proposed fix:** Extract `TmuxSessionManager`, `AgentPromptBuilder`, `PromptDeliveryService`,
`AgentMessenger` as collaborators; `AgentManager` becomes a thin composing orchestrator; DB access
goes through a narrower `AgentRepository`.

### 3.2 `create_agent_for_task` — ~460-line god function with logic duplicated in `restart_agent`
**Location:** `src/agents/manager.py:54-581` (create), `:1445-1526` (restart, near-duplicate of
the GLM-env-var and MCP-timeout-lookup blocks from create)

**Smell:** One method does phase-config lookup, worktree resolution, LLM prompt generation, GLM
env var setup, MCP timeout lookup, tmux session creation, LLM-based complexity classification,
launch-command construction, chunked prompt delivery with retry, DB record creation, and a nested
exception-handling/cleanup tree. The GLM-env and MCP-timeout blocks (~85 lines) are duplicated
nearly verbatim in `restart_agent` and **have already drifted**: restart's version resolves
`workflow_id` from the outer `session` directly rather than opening `get_db()` like create's
version does — a subtle inconsistency invisible without a side-by-side diff.

**Proposed fix:** Decompose into named steps (`_resolve_phase_config`, `_resolve_worktree`,
`_build_env_vars` — shared, `_resolve_mcp_timeout` — shared, `_classify_complexity`,
`_launch_and_verify`), called by both `create_agent_for_task` and `restart_agent`.

### 3.3 Per-CLI-type `isinstance` branching instead of the polymorphic interface that already exists (OCP)
**Location:** `src/agents/manager.py:1038-1154` (`_send_initial_prompt_with_retry`)

**Smell:** Imports concrete CLI-agent classes (`ClaudeCodeAgent`, `CodexAgent`, `DroidAgent`,
`PiAgent`) just to `isinstance`-check them and pick a chunk size — duplicated across two branches
in the same method — despite `CLIAgentInterface` already existing elsewhere in the codebase with
proper polymorphic methods (`format_message`, `recovery_keystrokes`, `get_tui_status_patterns`).

**Why it hurts:** Adding a new CLI backend means editing this method (and its duplicate branch)
instead of the new class simply implementing an interface method the team already knows to use.

**Proposed fix:** Add `chunk_size`/`needs_chunked_send` to `CLIAgentInterface`; call it
polymorphically instead of isinstance-branching.

### 3.4 `MonitoringLoop` — ~2050-line class fusing scheduling, two independent heuristic detectors, Guardian dispatch, orphan cleanup, and a full diagnostic-agent state machine
**Location:** `src/monitoring/monitor.py:407-2455`

**Smell:** Owns the run loop, `_mechanical_recovery_for_agent`/`_detect_repetition_loop` (two
independent no-LLM stuck-detection heuristics), Guardian analysis orchestration (~190 lines),
workflow-switch auto-discovery, orphaned-tmux cleanup, and a ~700-line "stuck workflow" diagnostic
state machine (`_check_workflow_stuck_state` + `_create_diagnostic_agent` +
`_gather_diagnostic_context` + `_generate_diagnostic_prompt`) — all sharing `self` state
(`self._stuck_state`, `self._rep_loop_state`, `self._last_orphan_check_time`,
`self.guardian_summaries_cache`).

**Why it hurts:** This is the largest, most tangled class in the codebase. Tweaking the
diagnostic-agent trigger threshold requires understanding mechanical-recovery keystroke logic,
Guardian wiring, and orphan cleanup that happen to live in the same class. Testing "does the
diagnostic agent trigger after N seconds stuck" requires instantiating the entire monitoring loop
with Guardian, Conductor, TrajectoryContext, RAGSystem, and AgentManager — none of which that
test cares about.

**Proposed fix:** Split into `MechanicalRecoveryDetector` (pure functions over tmux text + small
state), `OrphanSessionReaper`, `WorkflowStuckDiagnostics` (the full diagnostic-trigger pipeline as
its own class with a `should_trigger(workflow_id) -> (bool, reason)` + `DiagnosticAgentFactory`),
leaving `MonitoringLoop` as a thin per-cycle scheduler calling each collaborator once.

### 3.5 Four independent "is this agent broken" heuristics live in two classes with no shared interface (duplication)
**Location:** `src/monitoring/monitor.py:482-689` (`_mechanical_recovery_for_agent`,
`_detect_repetition_loop`) vs. `src/monitoring/guardian.py:468-529` (`detect_agent_exited`,
`detect_garbled_output`)

**Smell:** Frozen-output detection, repetition-loop detection, exited-to-shell detection, and
garbled-TUI detection are all instances of the same conceptual operation ("cheap, no-LLM
stuck/broken detector over tmux text") but live in two different classes with no common
abstraction — and the split isn't principled: `monitor.py` calls `guardian.detect_agent_exited`
from inside `_guardian_analysis_for_agent` while running its own separate detectors in
`_monitoring_cycle`.

**Proposed fix:** A `StuckDetector` strategy interface with one implementation per heuristic;
`MonitoringLoop` iterates a list instead of hardcoding four separate method calls across two
classes.

### 3.6 Guardian's LLM analysis is entangled with its own DB reads and side effects (SRP)
**Location:** `src/monitoring/guardian.py:80-233` (`analyze_agent_with_trajectory`),
`:307-387` (`steer_agent`)

**Smell:** `analyze_agent_with_trajectory` both calls the LLM AND opens DB sessions to build
context (`_build_accumulated_context`, `_get_agent_task`, `_get_phase_context`) inside the same
call chain, then caches into `self.trajectory_cache`. `steer_agent` mixes the pure decision logic
(`_evaluate_steering_eligibility` — correctly pure) with side effects: recovery keystrokes, tmux
message send, and an `AgentLog` DB write, all inlined together.

**Why it hurts:** "Does Guardian steer after 2 consecutive off-track flags" can't be tested
without mocking `agent_manager.send_recovery_keystrokes`, `agent_manager.send_message_to_agent`,
and a DB session — none of which matter to that decision. Conversely "does Guardian build context
correctly from logs" needs no LLM/tmux mocking but is only reachable through the LLM-calling
method.

**Proposed fix:** Split `steer_agent` into `decide_steering()` (pure, returns an action
descriptor) and `apply_steering(action)` (owns the side effects). Have
`analyze_agent_with_trajectory` accept pre-fetched context instead of fetching it itself, so the
LLM-calling code has zero DB dependency.

### 3.7 Guardian's return-dict key remapping is unowned and duplicated at the call site
**Location:** `src/monitoring/guardian.py:194-212` (renames ~10 LLM response keys inline, e.g.
`"steering_message": analysis.get("steering_recommendation")`) and
`src/monitoring/monitor.py:1063-1069` (reads `analysis.get("steering_message")` back out, with a
comment repeating the same rename caveat)

**Why it hurts:** The LLM response schema and Guardian's internal schema are two vocabularies
glued together by scattered `.get()` calls with inconsistent names, with comments apologizing for
the inconsistency in both the producer and consumer rather than fixing it once.

**Proposed fix:** A `TrajectoryAnalysisResult` dataclass with canonical field names; do the
mapping once in a `_parse_llm_response` method.

### 3.8 Dead code: `Guardian._should_steer_agent` superseded by `_evaluate_steering_eligibility` but never removed
**Location:** `src/monitoring/guardian.py:389-406` vs. `:408-466`

**Smell:** `_should_steer_agent` is never called anywhere (`steer_agent` calls only
`_evaluate_steering_eligibility`); its docstring still references pre-rework "last-resort model"
language.

**Proposed fix:** Delete `_should_steer_agent`.

### 3.9 `Conductor` bundles duplicate-detection with an unrelated QA-report-review operation (ISP)
**Location:** `src/monitoring/conductor.py:25-473` (whole class), `review_qa_report` at 335-374

**Smell:** `Conductor` is nominally "orchestrates agents toward collective goals" (duplicate
detection, coherence scoring, resource coordination), but also owns `review_qa_report`, an
unrelated LLM-based QA-report-vs-PRD review that doesn't touch `db_manager` or `agent_manager` at
all. Callers that only need duplicate-agent detection are forced to depend on a class that also
knows QA-review semantics.

**Additionally:** `Conductor.__init__` doesn't accept an `llm_provider` (only `db_manager`,
`agent_manager`) — `review_qa_report` and `analyze_system_state` instead reach for
`from src.interfaces import get_llm_provider` as a **module-level import inside the method body**,
bypassing the constructor-injected pattern used everywhere else in the class (same DIP smell as
3.10 below, localized here).

**Proposed fix:** Move `review_qa_report` to its own `QAReportReviewer` service. Add
`llm_provider` to `Conductor.__init__` and remove the inline `get_llm_provider()` call.

### 3.10 Direct instantiation of infrastructure inside business classes instead of injection (DIP)
**Location:** `src/agents/manager.py:49` (`self.tmux_server = libtmux.Server()` in `__init__`);
pervasive raw `self.db_manager.get_session()` calls (dozens per file) across `manager.py`,
`monitor.py`, `guardian.py`, `queue_service.py`, `ticket_service.py`, instead of the
already-available `session_scope()` context manager.

**Why it hurts:** `AgentManager` cannot be tested without a real tmux server — there's no seam to
substitute a fake. Inconsistent session lifecycle management across the service layer (some flows
open 3 separate sessions across one logical operation, risking read skew) is a direct
consequence of no injected unit-of-work abstraction.

**Proposed fix:** Inject a `TmuxSessionProvider` interface into `AgentManager.__init__`.
Standardize on `session_scope()` everywhere instead of the older raw pattern (a few newer methods
already do this correctly — `monitor.py:591`, `guardian.py:252` — extend it codebase-wide).

### 3.11 `TicketService` — persistence + business rules + approval-wait blocking + broadcast, all in one 470-line method
**Location:** `src/services/ticket_service.py:131-1726` (whole class); `create_ticket` at
163-635

**Smell:** `create_ticket` alone does board-config/workflow validation, persistence, human-approval
blocking wait (`approval_manager.wait_for_approval`), three separate
`from src.mcp.server import server_state; await server_state.broadcast_update(...)` calls inlined
at different branches, ticket-deletion-with-cascade logic (**duplicated twice** — see 3.12), and
finally calls `TicketSearchService` for embedding + duplicate detection.

**Why it hurts:** A developer changing how approval timeouts broadcast to the UI must edit the
same method that also contains board-config validation and blocking checks. Every broadcast is
wrapped in try/except that swallows failures — an implicit admission that importing
`server_state` directly into a service is fragile (same root cause as 1.6).

**Proposed fix:** Extract `TicketApprovalWorkflow` (wait/approve/reject/timeout/broadcast, owning
the cascade-delete in one place used by both branches — see 3.12). Inject a
`TicketEventBroadcaster` interface instead of importing `server_state`.

### 3.12 Duplicate cascade-delete logic in `TicketService.create_ticket`
**Location:** `src/services/ticket_service.py:416-453` (timeout) and `:478-517` (rejection)

**Smell:** Two near-identical ~35-line blocks manually delete `TicketHistory`, `TicketComment`,
`TicketCommit` then the `Ticket`, differing only in log message and broadcast reason.

**Proposed fix:** Extract `_delete_ticket_cascade(ticket_id, db)` called from both branches.

### 3.13 Duplicate similarity thresholds between `TicketSearchService` and `TicketService`
**Location:** `src/services/ticket_search_service.py:401-410` (hardcoded `>= 0.9` → "duplicate",
`>= 0.7` → "related", `>= 0.5` → "similar") and `src/services/ticket_service.py:611`
(independently hardcoded `>= 0.9` duplicate-warning threshold)

**Why it hurts:** Tuning the duplicate-detection threshold (a plausible product change) means
finding and changing it in two files with nothing enforcing they stay in sync.

**Proposed fix:** Named class constants on `TicketSearchService` (`DUPLICATE_THRESHOLD = 0.9`,
etc.); `TicketService` checks the returned `relation_type` instead of re-deriving it from a raw
score.

### 3.14 `QueueService` — clean SRP example, but its priority-ordering expression is duplicated 4×
**Location:** `src/services/queue_service.py:145-151, 214-221, 303-310, 424-431` — identical
SQLAlchemy `case((Task.priority == "high", 3), ...)` expression in 4 methods

**Why it hurts:** Adding a new priority tier requires 4 identical edits; missing one produces a
queue that dequeues in a different order than `queue_position` claims.

**Proposed fix:** Extract a module-level `_PRIORITY_ORDER_CASE` reused in all four places.

---

## 4. Core infrastructure (`src/core/`, `src/interfaces/`, `src/auth/`)

### 4.1 `DatabaseManager` — god class mixing connection lifecycle, DDL, FTS setup, and ad hoc migrations
**Location:** `src/core/database.py:1207-1636`

**Smell:** One class does engine/session construction, pragma tuning, table creation, FTS5
virtual-table + trigger DDL (~60 lines raw SQL), index creation (~90 lines raw SQL), and three
hand-rolled migration methods that fire `ALTER TABLE` wrapped in bare `try/except: pass` as a
substitute for real idempotency tracking — no migration registry, no schema-version table.

**Live bug found as a direct consequence (4.2):** `_migrate_autopilot_designs_columns`
(`database.py:1518-1527`) references `self.db_path`, which is never set anywhere — the real
attribute is `self.database_path`. This raises `AttributeError` on every run, silently caught by
a broad `except Exception` logged only at `debug` — so the `agents.cli_model` migration **never
executes**, masquerading as a harmless "column already exists" no-op. This is exactly the failure
mode unreviewed migration sprawl produces: two different DB-access APIs (engine vs. raw
`sqlite3.connect`) coexisting unnoticed inside one method.

**Proposed fix:** Extract `SchemaMigrator` (ordered `Migration` objects tracked via a
`schema_version` table), `FTS5Setup`, `IndexInstaller` as separate collaborators orchestrated by
`create_tables()`. Fix the `self.db_path`→`self.database_path` typo immediately regardless.

### 4.3 `Config` — one 70-field object with three untyped, independently-maintained enumerations of the same fields
**Location:** `src/core/simple_config.py:17-437`

**Smell:** `_apply_yaml_config` (~160 lines) sets ~70 attributes across server/paths/git/LLM/
agents/vector-store/monitoring/MCP/task-dedup/worktree/diagnostic/ticket-tracking settings.
`_load_env_overrides` (~170 lines) re-enumerates roughly the same 70 names a second time with
manual `int()`/`float()`/`.lower()=="true"` parsing per field. `validate()` checks only 2 of the
70+. `get_api_key()` is an LLM-specific helper bolted onto an otherwise generic object.

**Why it hurts:** Adding one setting means touching three untyped blocks with nothing enforcing
they stay in sync — a typo in an env-var name silently no-ops. Because every subsystem shares one
object, `WorktreeManager` and CLI launch code can (and do) read/mutate fields entirely outside
their own domain.

**Proposed fix:** Split into per-domain value objects (`ServerConfig`, `GitWorktreeConfig`,
`MonitoringConfig`, `TaskDedupConfig`, `TicketTrackingConfig`, ...) composed by a top-level
`AppConfig`; inject only the relevant slice into each subsystem.

### 4.4 `WorktreeManager.reload()` mutates the shared global `Config` singleton in place (action-at-a-distance)
**Location:** `src/core/worktree_manager.py:120-131`, `src/core/simple_config.py:432-437`

**Smell:** `reload(new_path)` writes directly into the process-wide `get_config()` singleton
(`self.config.main_repo_path = new_path`), visible to every other consumer in the process
instantly with no notification.

**Why it hurts:** Any other code reading `config.main_repo_path` can have the ground shift under
it the instant `reload()` runs for an unrelated purpose — hard to reproduce because the mutation
site is far from the read site.

**Proposed fix:** Pass the repo path explicitly into `WorktreeManager` methods instead of reaching
into the global; if shared config is required, make `Config` immutable and have `reload()`
produce a new scoped instance.

### 4.5 `WorktreeManager` — SRP violation fusing git plumbing, DB persistence, OS locking, and a hardcoded (effectively dead) conflict-resolution policy
**Location:** `src/core/worktree_manager.py:95-996` (~900 lines, 21 methods)

**Smell:** One class owns git worktree lifecycle, `fcntl`-based locking, DB persistence of
`AgentBranch`/`WorktreeCommit`/`MergeConflictResolution` interleaved directly into git calls, and
a hardcoded "newest-file-wins" conflict policy in `_resolve_conflicts` (630-683).
`config.conflict_resolution_strategy` and `config.require_manual_review` exist in the config
schema implying pluggable strategies, but **no code path branches on them** — dead configuration.

**Why it hurts:** Testing "did we detect a dirty worktree" requires a live DB (every git op
immediately writes a DB row in the same method, e.g. `_commit_in_worktree`). Switching to
`manual_review` conflict resolution is impossible today despite the config implying support.

**Proposed fix:** Split into `GitWorktreeRepository` (pure git/filesystem), `WorktreeCommitLog`
(DB persistence), `MergeLockManager` (fcntl), and a `ConflictResolutionStrategy` interface with
`NewestFileWinsStrategy` as the current default — making the configurable-strategy promise real.

### 4.6 `session_scope()` exists to centralize commit/rollback/close but is bypassed almost everywhere it matters
**Location:** `src/core/database.py:1616-1631` (definition) vs. nearly every method in
`src/core/worktree_manager.py` (e.g. 177-184, 253-346, 394-410, 445-578) and
`src/auth/auth_api.py:206, 267, 379`

**Smell:** `WorktreeManager` hand-rolls its own `try/finally: session.close()` per method,
several **without a rollback on the exception path** (e.g. `_commit_in_worktree`), while
`merge_to_main` separately reimplements the rollback logic `session_scope()` already provides.
`auth_api.py`'s `with db_manager.get_session() as db:` relies on SQLAlchemy's default
`Session.__exit__`, which does **not** auto-commit — every route must remember to call
`db.commit()` manually, correct today only by discipline.

**Why it hurts:** The exact bug class `session_scope()` was introduced to prevent (leaked/
uncommitted sessions) remains possible in most of the codebase, because the safe pattern is
available but not enforced.

**Proposed fix:** Make `session_scope()` the sole public entry point (privatize `get_session()`);
refactor `WorktreeManager`/`auth_api.py` to use it uniformly, deleting the duplicated
try/finally/rollback boilerplate.

### 4.7 Two parallel, inconsistent LLM abstractions with a silent LSP violation
**Location:** `src/interfaces/llm_interface.py:14-876` (`LLMProviderInterface` ABC, 7 abstract
methods, implemented by `OpenAIProvider`/`AnthropicProvider`) vs.
`src/interfaces/langchain_llm_client.py:65-1152` (`LangChainLLMClient`, used by most of the app
via a `ComponentType` router, shares no base class with the ABC above)

**Smell:** `AnthropicProvider.analyze_agent_state`/`analyze_agent_trajectory`/
`analyze_system_coherence` (lines 670-776) are stubs returning hardcoded defaults with a comment
reading "Implementation details omitted for brevity" — callers coded against the ABC's documented
contract silently get wrong behavior. Two independent provider-selection code paths coexist in
`get_llm_provider()` (816-876): "multi-provider" and "legacy."

**Why it hurts:** New engineers can't tell which abstraction is authoritative; the ABC can drift
out of sync with actual usage (e.g. `AnthropicProvider.analyze_agent_trajectory` doesn't even
accept a parameter newer callers pass) while still needing to be maintained as dead weight.

**Proposed fix:** Retire the legacy `LLMProviderInterface` path, or make `LangChainLLMClient`
implement it. If provider capabilities genuinely differ (e.g. no embeddings support), split into
narrower `TaskEnrichmentProvider`/`EmbeddingProvider`/`TrajectoryAnalysisProvider` roles (ISP)
instead of forcing every provider to stub unsupported methods.

### 4.8 `LangChainLLMClient._create_model` — 145-line if/elif provider dispatch instead of polymorphism (OCP)
**Location:** `src/interfaces/langchain_llm_client.py:186-331` (also the embedding-provider
branch in `_initialize_models`, lines 94-159, repeating the same 5-way shape)

**Smell:** Branches on provider string across `openai`/`groq`/`openrouter`/`azure_openai`/
`google_ai`, with provider-specific kwargs construction (GPT-5 temperature override, OpenRouter
`extra_body`, Azure deployment-name handling) inlined in each branch. Every provider's LangChain
package is unconditionally imported even when only one is configured.

**Proposed fix:** `ChatModelFactory` interface, one concrete factory per provider, registered in a
`PROVIDER_FACTORIES` dict; `_create_model` becomes a single lookup + `.build(...)`.

### 4.9 Duplicated CLI-output-parsing scan logic across `CLIAgentInterface` subclasses
**Location:** `src/interfaces/cli_interface.py:389-501` (`OpenCodeAgent`, `DroidAgent`),
`:654-674` (`PiAgent`) — algorithmically identical prompt-marker scan loops, differing only in
marker strings; `ClaudeCodeAgent`/`CodexAgent` implement a separately-duplicated variant with
different key names (`last_response`/`is_ready` vs. `last_message`/`is_waiting`).

**Why it hurts:** Adding CLI tool #7 means copy-pasting the ~15-line scan loop again; a bug fix
(e.g. handling ANSI-embedded prompts) must be applied in 3-5 places.

**Proposed fix:** A shared concrete helper on `CLIAgentInterface`
(`_parse_prompt_marker_output(output, markers)`); marker-based subclasses supply only
`get_prompt_markers()`.

### 4.10 `auth_api.py` — full auth business logic embedded directly in route handlers
**Location:** `src/auth/auth_api.py:198-467`

**Smell:** `register`/`login`/`refresh_token` each inline the complete workflow (uniqueness
checks, password hashing, DB writes, audit logging, session/token creation) interleaved with
`HTTPException` concerns. `login` (262-364) does ten sequential steps in one route function.
`TODO: Load user roles` / `TODO: Get from request` comments show unfinished domain logic sitting
in the transport layer.

**Proposed fix:** Extract `AuthService` with framework-agnostic methods (`register_user`,
`authenticate`, `refresh_tokens`) raising domain exceptions (`InvalidCredentialsError`,
`AccountLockedError`); routes become thin adapters.

---

## 5. Frontend (`frontend/src/`)

### 5.1 `TaskDetailModal` — 1289-line component fusing 5 data-fetch queries, imperative actions, and rendering
**Location:** `frontend/src/components/TaskDetailModal.tsx:61-1289`

**Smell:** 5 separate `useQuery` calls (task, guardian analyses, steering interventions, related
tickets, original/duplicate task), a raw WebSocket subscription effect, 4 imperative business
actions calling the API directly with `window.confirm`/`alert` for UX, and ~700 lines of JSX
around 9 independently-toggled `expandedSections` booleans — the same disclosure-toggle
boilerplate is duplicated with variation in `TicketDetailModal.tsx:232-287`.

**Proposed fix:** Extract `useTaskDetails(taskId)` (bundles the 5 queries + WebSocket subscription)
and `useTaskActions(taskId)` (mutation-style handlers, real confirm dialog instead of
`window.confirm`); split rendering into presentational subcomponents. Factor the disclosure
pattern into a shared `useDisclosure(keys)` hook used by both modals.

### 5.2 Per-row manual polling bypassing React Query; 3 duplicated status-config maps
**Location:** `frontend/src/components/autopilot/DesignQueuePanel.tsx:381-403`
(`SortableDesignItem` runs its own `setInterval`-based 10s poll per expanded row, outside the
React Query cache/dedup already used elsewhere in the same file); `:326-332`, `:347-358`,
`:560-566` (`STATUS_CONFIG`, `TASK_STATUS_CONFIG`, `FEATURE_STATUS_CONFIG` — three structurally
identical `{color, icon, label}` maps with inconsistent status vocabularies, e.g. `paused` present
in one, absent from another)

**Why it hurts:** Expanding N rows spawns N independent uncoordinated polling intervals against
the same endpoint shape. A new status requires updating 3+ maps with different key sets; missing
one silently renders no badge.

**Proposed fix:** `useQuery({queryKey: [...], refetchInterval: 10000, enabled: expanded})` instead
of the manual interval. One shared `statusConfig.ts` exporting a single typed `STATUS_CONFIG`
keyed by a shared `Status` union.

### 5.3 `MessageCenter.getMessageActions` — 113-line per-render branching function with an embedded async side effect and a stub action
**Location:** `frontend/src/components/autopilot/MessageCenter.tsx:170-282`

**Smell:** Called once per rendered row; re-derives an actions array via 7 sequential `if` blocks
on `msg.type`/`data.*`, including an ad hoc feature-ID extraction heuristic and an inline async
validity check inside an `onClick` handler (`await apiService.getAutopilotInput()`) — a business
decision embedded in JSX action-list construction. One branch is a `// TODO: Implement retry`
stub wired in as if functional.

**Proposed fix:** Pure `deriveMessageActions(msg, handlers): MessageAction[]` driven by a
`Record<eventType, (msg) => MessageAction[]>` dispatch table, testable with plain objects. Move
the async validity check into a dedicated `useRespondToInput()` hook.

**Verified 2026-08-21:** The async-side-effect half was already fixed independently before this
pass — `human_input_required` no longer builds an inline `onClick` around
`apiService.getAutopilotInput()`; that call now lives in a `useQuery` (line ~122) and the response
flow is a dedicated inline panel (`expandedMessageId` state), not a stub in the actions array.

Fixed this pass: `getMessageActions` is now a one-line delegator to a module-level, pure
`deriveMessageActions(msg, handlers)` (testable with plain objects, no `apiService`, no `useState`
closures — `handlers.onViewFeature` is the only side-channel, passed in by the caller). Deviated
from the literal `Record<eventType, handler>` shape: several rules key off `data.*` fields (e.g.
Retry fires on `data.status === 'failed'`, independent of `msg.type`; View Workflow fires on
`data.workflow_id`/`data.workflow` regardless of type), not cleanly per-type, so forcing a
type-keyed Record would silently narrow when those rules apply. Used an ordered array of small
named `MessageActionRule` functions instead — same testability and decomposition goal, without
changing which messages get which actions. Also collapsed the iteration/phase "View Feature"
duplication (previously two near-identical `if` blocks) into one rule.

The `// TODO: Implement retry` stub is now wired to `window.open('/autopilot/queue', '_blank')`,
matching the existing `View Workflow`/`View Agents` pattern in the same function. Retrying a
design is destructive (restarts its pipeline from scratch and deletes its worktree — see
`DesignQueuePanel.tsx`'s `rerunDesignMutation` and its confirm dialog) and the message payload
available here (`data.feature_folder`/`data.feature_id`) does not carry the queue `filename` the
`/autopilot/queue/rerun` endpoint requires — confirmed by tracing `design_complete`'s event
payload (`src/autopilot/orchestrator/pipeline.py`) against `AutopilotDesign.filename`'s actual
source (`path.name`, in `src/autopilot/orchestrator/queue.py`), which are different values with no
safe derivation between them. Rather than guess a filename against a destructive endpoint, Retry
now opens the Design Queue tab, where the real rerun action already resolves and confirms it
safely. Fixed the pre-existing, identically-shaped dead stub on `design_queued`'s "View Queue"
action (`onClick: () => { /* Already on autopilot page, could scroll to queue tab */ }`) the same
way, since it's the same bug class surfaced by this same read-through.

`npx tsc --noEmit` clean. No frontend test runner is configured in this repo (no vitest/jest, no
existing `*.test.*`/`*.spec.*` files) — introducing one was out of scope for this fix. Manually
verified via the running dev server (Vite, hot-reloads on save) that the module transforms and
loads without error; did not interactively click-test in a real browser (no browser-automation
tool available in this session) — flagging this rather than claiming full UI verification per this
repo's own standard.

### 5.4 Duplicated 45-line markdown-rendering config instead of reusing the existing shared component
**Location:** `frontend/src/pages/Results.tsx:253-304` and `:457-509` (identical `ReactMarkdown` +
`rehypeHighlight`/`remarkGfm` block copy-pasted between `ResultContentDialog` and
`ResultValidationDialog` in the same file) — while `TaskDetailModal.tsx`/`FeatureDetailModal.tsx`
already use a shared `MarkdownRenderer` for the same purpose.

**Proposed fix:** Replace both inline blocks with the existing `MarkdownRenderer`, deleting ~90
duplicated lines.

### 5.5 `Graph.tsx` — dagre layout re-runs on every hover because highlight state is entangled with data transformation
**Location:** `frontend/src/pages/Graph.tsx:181-567`, the transform `useEffect` at 229-303

**Smell:** One effect filters raw nodes/edges, joins phase metadata, reshapes into React Flow
types, and runs the dagre layout — but its dependency array includes `highlightedNodes`/
`highlightedEdges`/`hoveredNode`, so the entire pipeline re-executes on every
`onNodeMouseEnter`, not just highlight styling.

**Why it hurts:** On a large graph, hovering triggers a full O(n log n) dagre re-layout per
mouse-enter — a real performance problem stemming directly from interaction state and
data/layout state not being separated, which are logically independent concerns.

**Proposed fix:** Split into `useGraphLayout(data, layoutDirection)` (dagre runs only when
`data`/`layoutDirection` change) and `useHighlightedChain(hoveredNode, edges)` (overlays
highlight/dim styling onto already-laid-out nodes without touching dagre).

---

## Cross-cutting themes

These four patterns account for the large majority of individual findings above — fixing the
pattern once fixes many findings at once:

1. **No service/domain layer beneath FastAPI routes.** Route handlers in `server.py`,
   `autopilot_api.py`, `tickets_api.py`, and `api.py` directly perform DB queries, git operations,
   LLM calls, and business validation. This single gap produces most of section 1's findings
   (1.1-1.3, 1.8, 1.10-1.20) and forces every route to be tested only through the full HTTP stack.
   **Highest-leverage fix:** extract `TaskCreationService`, `TaskCompletionService`,
   `DesignStatusService`, `ProjectService`, `QueueService.release_agent_and_advance` — five
   services would collapse ~3000 lines of duplicated, route-embedded logic (`create_task` +
   `process_queue` + their 4 near-duplicates alone are ~1700 lines).

2. **`status_derivation.py` was built to be the single source of truth but only one caller uses
   it.** Section 2.1 is the most consequential single finding in this review: 4-5 independent,
   disagreeing definitions of "is this done" exist in `orchestrator.py` and `phase_manager.py`,
   which is precisely the recurring bug class visible in recent commit history. This is a
   one-directional wiring fix (route existing writers through the existing module), not new
   design work.

3. **String-keyed if/elif dispatch instead of registries/polymorphism (OCP).** Tool dispatch
   (1.5), condition parsing (2.9), CLI-type branching (3.3), config-provider selection (4.8), and
   phase-action handling (2.12) are all the same shape: a chain of branches that must be edited
   in lockstep with a schema/registry living elsewhere. Each is a small, independent fix once
   named.

4. **Global singletons reached into from deep call stacks instead of injected (DIP).**
   `server_state` (1.6, 2.4, 3.11), the `Config` singleton (4.3, 4.4), and ad hoc
   `DatabaseManager()`/`libtmux.Server()` construction (1.12, 3.10) all block unit testing the
   same way: nothing can be tested without booting the real global. `TicketService` importing
   `from src.mcp.server import server_state` (a domain service reaching into the web layer) is
   the clearest single symptom of this pattern.

### Suggested order of attack

If tackled in priority order, the following give the best bug-reduction-to-effort ratio:

1. **2.1 / 2.2** — wire `orchestrator.py` and `phase_manager.py`'s status writes through
   `status_derivation.py`. Directly fixes a proven, recurring bug class.
2. **4.2** — one-line fix (`self.db_path` → `self.database_path`); currently silently disables a
   migration in production.
3. **1.5 / 2.9 / 2.12** — replace if/elif dispatch with registries in the three highest-traffic
   spots (MCP tools, condition evaluation, phase actions).
4. **1.2 / 1.3** — extract `TaskCompletionService`/`TaskCreationService`, the single largest
   concentration of duplicated, untestable logic in the codebase.
5. **1.6 / 3.11** — break the `server_state` global-import cycle so services can be tested
   without the web server.
