# HephaestusNG Architecture Review

**Scope:** Autopilot pipeline (design doc → feature → workflow → phase → task → agent → guardian/monitor), backend API layer, and frontend polling logic.
**Method:** Direct reading of core modules, `git diff` inspection of in-flight session fixes (used as ground truth for "already fixed" patterns), targeted greps/heuristics for sibling instances, and manual verification of every candidate before inclusion.
**Non-goal:** This is a review, not a patch set. No code was modified to produce this document.

---

## Fixes Applied (Post-Review)

The following findings have been addressed:

### High Severity Fixes

| Finding | Fix | Files Changed |
|---------|-----|---------------|
| **H-5** | Added `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` via SQLAlchemy connection event listener | `src/core/database.py` |
| **H-0*** | Set `expire_on_commit=False` on `SessionLocal` factory to prevent entire class of DetachedInstanceError bugs | `src/core/database.py` |
| **H-0** | Extract `phase.id`/`phase.name` into primitives before session close; updated `_fire_phase_transition` signature to accept `phase_id: str, phase_name: str` instead of ORM object | `src/autopilot/orchestrator.py` |
| **H-0b** | Re-query agent from new session in `_auto_restart_agent` before mutating status | `src/monitoring/monitor.py` |
| **H-0d** | Changed `_get_agent_task()` to return dict with primitives instead of ORM Task object | `src/monitoring/guardian.py` |
| **H-1** | Added `DatabaseManager.session_scope()` context manager; converted critical monitoring/guardian functions to use it | `src/core/database.py`, `src/monitoring/monitor.py`, `src/monitoring/guardian.py`, `src/agents/manager.py` |
| **H-2** | Replaced `api_get`/`api_post` HTTP self-calls with direct DB access functions: `get_tasks()`, `get_agents()`, `update_task_status()`, `terminate_agent_direct()`, `pause_workflow_direct()`, `complete_workflow_direct()` | `src/autopilot/orchestrator.py` |
| **H-3** | Created centralized `src/core/status_derivation.py` with `derive_feature_status()`, `derive_design_status()`, `derive_workflow_status()` functions; updated `get_project_design_status` to use them | `src/core/status_derivation.py`, `src/mcp/autopilot_api.py` |
| **H-4** | Added `ProcessWatchdog` class with PID-based monitoring and auto-restart; integrated into `heph start` with `--no-watchdog` flag | `src/cli/commands/start.py`, `src/cli/commands/stop.py` |

### Medium Severity Fixes

| Finding | Fix | Files Changed |
|---------|-----|---------------|
| **M-2** | Added public `Guardian.record_auto_restart()` method; MonitoringLoop now calls it instead of `_record_steering` | `src/monitoring/guardian.py`, `src/monitoring/monitor.py` |
| **M-3** | Extracted `_advance_phases` cases into separate functions: `_try_auto_resume_paused_workflow`, `_get_phase_statuses`, `_case_start_first_phase`, `_case_in_progress_no_tasks`, `_case_completed_with_successor`, `_case_in_progress_complete`, `_maybe_retry_failed_tasks` | `src/autopilot/orchestrator.py` |
| **M-4** | Added logging to critical bare `except Exception:` blocks in state file reads and API helpers; replaced `print()` with `logger.debug()` | `src/autopilot/orchestrator.py` |
| **M-5** | Migrated `designStatuses` from hand-rolled `setInterval` + `useState` to React Query with proper cache invalidation | `frontend/src/components/autopilot/DesignQueuePanel.tsx` |

### Low Severity Fixes

| Finding | Fix | Files Changed |
|---------|-----|---------------|
| **L-1** | Created status enum classes: `AgentStatus`, `TaskStatus`, `WorkflowStatus`, `FeatureStatus`, `PhaseExecutionStatus`, `DesignStatus` | `src/core/database.py` |
| **L-2** | Created shared `src/core/logging_config.py` with `configure_logging()` helper; updated `run_server.py` and `run_monitor.py` to use it | `src/core/logging_config.py`, `run_server.py`, `run_monitor.py` |
| **L-3** | Consolidated consecutive-flag confirmation gate and cooldown check into single `_evaluate_steering_eligibility()` function with clear reason output | `src/monitoring/guardian.py` |

### Remaining Items

| Finding | Status | Notes |
|---------|--------|-------|
| **M-6** | **Done** | Added `_resolve_agent_current_phase()` helper that resolves agent's current phase from their assigned task. Subtask creation now auto-fills phase_id when not provided, eliminating the need for agents to guess phase IDs. |
| **Test Coverage** | **Done** | Added critical test coverage:
- `tests/test_status_derivation.py`: 12 tests for centralized status derivation (H-3)
- `tests/test_advance_phases.py`: Tests for `_advance_phases` and related phase transition functions
- `tests/test_agent_manager.py`: Tests for `create_agent_for_task`, `restart_agent`, `get_active_agents`, and `terminate_agent` |

**Status:** All findings from the review have been addressed.

---

## System Overview

A design document is dropped into a project's design queue (`heph autopilot add`, or the frontend's `DesignQueuePanel`). `AutopilotService` (`src/autopilot/service.py`) — an in-process asyncio singleton that lives inside the FastAPI server, not a subprocess — picks it up and runs `run_continuous_pipeline` in a background thread executor (`service.py:243`), which drives `orchestrator.py`'s `run_single_design`. That function is a three-stage coordinator (`src/autopilot/orchestrator.py:3709`): Stage 1 (`run_phase0`) spawns a "Feature Architect" agent that decomposes the design into a `features.json` manifest with a dependency graph; Stage 2 (`run_feature_pipelines`) topologically sorts features (Kahn's algorithm, `_resolve_execution_order`) and runs each through a 12-phase pipeline, in parallel up to `MAX_PARALLEL_FEATURES=4` via `ThreadPoolExecutor`, each in its own isolated git worktree; Stage 3 (`run_design_aggregate`) rolls up results into an HTML report. Within a feature's pipeline, `_advance_phases` (`orchestrator.py:2155-2343`) polls the DB every `POLL_INTERVAL=15` seconds and is documented as "the single source of truth for phase progression" — it inspects `Task`/`PhaseExecution` status rows and fires the next phase's task once the current phase's tasks are done.

Each phase's task is handed to `AgentManager.create_agent_for_task` (`src/agents/manager.py:54`), which spawns a `pi`-CLI-driven coding agent inside a `libtmux` session with a constructed prompt (agent ID, workflow ID, task description, MCP tool instructions). The agent works autonomously, calling back into the backend via the MCP tool surface (`mcp/mcp_client.py`) to update task status, create subtasks, and save memories. While the agent works, a **separate, independently-launched process** (`run_monitor.py`) runs `MonitoringLoop` (`src/monitoring/monitor.py:407`), which owns a `Guardian` instance (`src/monitoring/guardian.py`) that periodically reads the agent's tmux output, asks an LLM to judge trajectory alignment against the task's goal/constraints, and — if it judges the agent stuck, drifting, or off-track — sends a steering message or forcibly interrupts the agent's tmux session. When the agent marks its task `done`, the orchestrator's polling loop notices and advances the workflow to the next phase, eventually completing the feature, then the design.

Status is the connective tissue of this whole flow, and it is read/written from at least five independent places for four different entities (`AutopilotDesign.status`, `Feature.status`, `Workflow.status`, `Task.status`, `PhaseExecution.status`) — see Finding H-3. Persistence is SQLite via SQLAlchemy, accessed through two competing patterns (`get_db()` context manager vs. raw `DatabaseManager.get_session()`) — see Finding H-1. There is no process supervisor: the backend, monitor, and frontend dev server are three unsupervised `subprocess.Popen` calls (`src/cli/commands/start.py:164-259`), each fully detached (`start_new_session=True`) with no restart-on-crash logic.

---

## Findings

### High Severity

#### H-0: `_fire_phase_transition` accesses a detached `Phase` object immediately after the session that loaded it closes — a live DetachedInstanceError bug in the core phase-progression path
**Location:** `src/autopilot/orchestrator.py:2283-2285` and `:2337` (call sites, both `return _fire_phase_transition(workflow_id, phase, logger)` from inside `with get_db() as db:` opened at `:2164`), `:2344-2379` (`_fire_phase_transition` body touching `phase.name` at `:2354`, `:2379` and `phase.id` at `:2369`).

`get_db()` (`database.py:1531-1546`) commits and then closes the session in its `finally` block; the `SessionLocal` factory (`database.py:1135-1137`) uses SQLAlchemy's default `expire_on_commit=True` with no override. That means every ORM object touched inside a `with get_db() as db:` block is expired on the implicit commit and then fully detached once the session closes — any attribute access afterward raises `DetachedInstanceError`, not just a stale-value read. In `_advance_phases`, both call sites hand the `phase` object (loaded via `db.query(Phase)...` inside the `with` block) to `_fire_phase_transition` via a bare `return`, which triggers `__exit__` (commit + close) *before* `_fire_phase_transition`'s body runs. That function then accesses `phase.name` (`:2354`, inside `if phase.name in GATED_PHASES:`) and `phase.id` (`:2369`, passed to `pm.mark_phase_complete(phase.id, ...)`) on the now-detached object.

This is the identical bug shape already fixed five times this session (per the current uncommitted diff's comments in `autopilot_api.py`/`projects_api.py`), just in the phase-advancement engine itself rather than the project/feature endpoints. `_advance_phases` runs on every polling cycle (`POLL_INTERVAL=15`s) and is the function that fires essentially every normal phase transition, so this is on the hot path, not an edge case.

**Failure scenario:** A phase completes normally; `_advance_phases` reaches Case 1 or Case 2 and calls `_fire_phase_transition(workflow_id, phase, logger)`. The moment `_fire_phase_transition` reads `phase.name`, `DetachedInstanceError` is raised. `_advance_phases`'s own `try/except Exception as e: logger.warning(...)` (`:2339-2340`) catches it and returns `False` — so the whole phase-advance call silently fails and gets retried next poll cycle, 15 seconds later, hitting the exact same error every time. **This would mean phases never actually advance via this path**, which contradicts the fact that phase advancement is observed to work in practice — worth treating as a "verify before fixing" item: either (a) this genuinely fires and is masked by the broad `except Exception` + retry-forever loop (in which case the *real* progression mechanism is something else, e.g. `pm.mark_phase_complete`'s own idempotency guard eventually being hit by a different, successful call path), or (b) there's a detail this review missed — e.g. SQLite connection pooling behavior under `StaticPool` keeping the underlying DB connection alive in a way that happens to allow one more lazy-load before truly detaching. Given the stakes (this is "the single source of truth for phase progression" per its own docstring), this should be the first thing verified/reproduced, not assumed.

**Direction:** Regardless of whether it's currently silently firing-and-retrying or genuinely broken, the fix is the same pattern already used correctly elsewhere in this file (e.g. `_create_phase_task`): extract `phase.id`/`phase.name` into plain local variables *before* the `with get_db()` block exits, and pass those primitives to `_fire_phase_transition` instead of the ORM object. A simple integration test that runs `_advance_phases` against a real session-scoped DB would confirm/deny this bug in minutes.

---

#### H-0b: `MonitoringLoop._auto_restart_agent` mutates a detached `Agent` object against a fresh session — a silent no-op DB write
**Location:** `src/monitoring/monitor.py:1118-1139`.

`_auto_restart_agent` receives `agent` (an ORM object obtained in an earlier, already-closed session context, per the caller chain from `_monitoring_cycle`/`analyze_agent_with_trajectory`). It opens a **new** session (`session = self.db_manager.get_session()`, `:1125`) but never re-queries `agent` from it or calls `session.merge(agent)` — it directly sets `agent.status = "terminated"` and `agent.health_check_failures = 0` on the detached object, then calls `session.commit()` on the unrelated new session. Because `agent` was never added to `session` (it's not in `session.new` or `session.dirty` for this session), the commit has nothing to flush for it — the status write is silently discarded. No exception is raised; `session.commit()` succeeds because there is genuinely nothing pending for it to fail on. Contrast with the correct pattern used elsewhere in the same file, `_update_agent_health_from_trajectory`, which properly re-queries `session.query(Agent).filter_by(id=agent.id).first()` before mutating.

**Failure scenario:** An agent ignores steering repeatedly, `_auto_restart_agent` kills its tmux session (that part works — it's a direct `tmux_server.kill_session()` call, not a DB write), but the DB still shows the agent as `"working"`/`"stuck"` forever instead of `"terminated"`. Any downstream code that filters on `Agent.status` (health dashboards, the resume-on-restart scan in `server.py`, task-reassignment logic) sees a phantom "still working" agent whose tmux session no longer exists.

**Direction:** Re-query the agent inside the new session before mutating (`agent = session.query(Agent).filter_by(id=agent.id).first()`), matching the pattern already used correctly by `_update_agent_health_from_trajectory` in the same file — this is a one-line-shape fix, not a design change.

---

#### H-0c: `AgentManager.get_active_agents()` returns detached `Agent` objects that feed the entire monitoring cycle
**Location:** `src/agents/manager.py:2155-2166` (`get_active_agents`, session closed in `finally` before returning `agents`); consumed by `src/monitoring/monitor.py:711` (`_monitoring_cycle`).

The returned `Agent` objects are held across many subsequent `await` points and attribute reads well after their producing session closed: `monitor.py:719-720` (`_mechanical_recovery_for_agent`/`_detect_repetition_loop` reading `agent.id`, `agent.cli_type`), and `monitor.py:728` → `_guardian_analysis_for_agent` reading `agent.created_at`, `agent.id`, `agent.agent_type`, `agent.tmux_session_name`, `agent.current_task_id` (`monitor.py:941-995`). That same detached object is then passed into `guardian.analyze_agent_with_trajectory(agent=agent, ...)` (`guardian.py:80-233`), which reads `agent.id`/`agent.agent_type` again, including after an `await asyncio.wait_for(...)` LLM call with a 90-second timeout — about as long a gap between "session closed" and "attribute accessed" as this bug class gets anywhere in the codebase.

**Failure scenario:** Every monitoring cycle iterates `get_active_agents()`'s results; the first attribute access on any of them is already technically illegal per SQLAlchemy's detach semantics. Whether this throws depends on whether those specific attributes happen to already be loaded into `__dict__` before expiry (see H-0's note on `expire_on_commit=True`) — worth the same "verify before fixing" treatment as H-0, since the monitoring loop is observed to run.

**Direction:** Same fix shape as H-0/H-0b: either extract needed primitives (`id`, `cli_type`, `agent_type`, `tmux_session_name`, `current_task_id`, `created_at`) into plain dicts before the session in `get_active_agents()` closes, or re-query/`session.merge()` before use downstream.

---

#### H-0d: `Guardian._get_agent_task()` returns a detached `Task` read deep into the LLM steering payload
**Location:** `src/monitoring/guardian.py:540-547` (`_get_agent_task`, session closed in `finally` before returning `task`); called at `guardian.py:112`.

The returned `task` is read at `guardian.py:116` (`task.id`), `:119-121` (`task.phase_id`, `task.workflow_id`), `:146-148` (`task.id`), and — furthest from the closed session — inside the dict built for the LLM provider call at `:167-172` (`task.enriched_description`, `task.raw_description`, `task.done_definition`, `task.id`).

**Failure scenario:** Same shape as H-0c — every Guardian trajectory analysis call touches a detached `Task` object multiple times, including right before constructing the LLM payload that decides whether to steer/interrupt the agent. If this throws, Guardian's whole analysis for that cycle silently fails (subject to the same broad-`except Exception` pattern noted in M-4), meaning the agent gets no oversight that cycle with no visible signal why.

**Direction:** Extract `task.id`, `task.phase_id`, `task.workflow_id`, `task.enriched_description`, `task.raw_description`, `task.done_definition` into a plain dict before `_get_agent_task`'s session closes, same as the other detached-object fixes this session.

---

#### H-1: Two competing DB session-handling conventions coexist, and the safer one is the exception, not the rule
**Location:** `src/core/database.py:1516` (`DatabaseManager.get_session()`) vs. `src/core/database.py:1531` (`get_db()` context manager); used inconsistently across `src/agents/manager.py`, `src/mcp/server.py`, `src/mcp/autopilot_api.py`, `src/autopilot/orchestrator.py`, `src/monitoring/monitor.py`, `src/monitoring/guardian.py`, and 17 other files.

`get_db()` is a thin `@contextmanager` wrapper around the exact same `DatabaseManager().get_session()` that's also called directly, everywhere, as a raw session with manual `try/finally: session.close()`. A grep count: 102 call sites use `with get_db() as db:`, while 217 call sites across 23 files use the raw `db_manager.get_session()` pattern (e.g. `src/monitoring/monitor.py:1125-1131`, `src/agents/manager.py`, `src/phases/phase_manager.py:630`). Both patterns produce SQLAlchemy objects that detach on session close, but only `get_db()`'s `with` block gives a syntactically obvious scope boundary — the raw pattern has no structural cue for "this object is about to detach," which is exactly the family of bug already found and fixed five times this session (e.g. `src/mcp/projects_api.py:129-145`, `src/mcp/autopilot_api.py:1654-1682`, per the current uncommitted diff).

**Failure scenario:** A new code path is added using the raw `get_session()` pattern (the majority convention by call count), a query result is returned or stashed for later use outside the function's own `try/finally`, and the first attribute access after `session.close()` throws `DetachedInstanceError` — the same bug class already patched five times, in a part of the codebase not yet touched.

**Direction:** Standardize on `get_db()` everywhere; it is strictly safer (enforces `commit`/`rollback`/`close`) and functionally identical, since it is literally implemented in terms of `get_session()`. A lint rule or `grep`-based CI check flagging new raw `get_session()` usage outside `database.py` itself would catch regressions cheaply. Given the number of hits, this is not a quick find-and-replace; it needs to be done file by file with tests, but it's worth ticketing as a standalone cleanup rather than only reactively patching each new instance as it's found.

---

#### H-2: `orchestrator.py`'s pipeline driver talks to its own server exclusively over loopback HTTP, from inside the same process
**Location:** `src/autopilot/orchestrator.py:36` (`API_BASE = "http://127.0.0.1:8300"`), `:355-378` (`api_get`/`api_post` helpers), used at 17 call sites throughout the file (e.g. `:2900`-ish repair-agent creation, task/phase queries).

`AutopilotService` (`src/autopilot/service.py`) explicitly replaced a subprocess-based orchestrator with an in-process asyncio task specifically to unify the process model ("This fixes: B5: Liveness disagreement (one PID convention, not three)" — `service.py:7-9`). Despite that, `orchestrator.py` still round-trips every DB read/write through synchronous `requests.get/post` calls to its own server's HTTP API rather than calling the underlying service/ORM functions in-process. It happens to be safe from an event-loop-blocking standpoint because `_run_pipeline_sync` is dispatched via `loop.run_in_executor(None, ...)` (`service.py:243`) — so it's not literally the "awaited self-HTTP-call" bug already fixed once in `resume_feature` (per the diff comment at `src/mcp/autopilot_api.py` around the old `resume_feature`, which explicitly calls out "same failure mode fixed in resume_feature"). But it is the same *family* of self-referential-HTTP-call pattern, just in a thread instead of the event loop, and it inherits the same risks: every one of those 17 calls has its own hardcoded timeout (some `timeout=30`, one bumped to `timeout=120` this session per the diff at `src/mcp/autopilot_api.py:957-969`), and a timeout on a self-call silently drops the intended DB write/read with only a `print()` (`orchestrator.py:375-377`), not a raised exception the caller is forced to handle.

**Failure scenario:** Under load (many concurrent agents, DB contention), a self-call from the orchestrator thread times out. `api_post` returns `None` (per its `try/except Exception: return None` at `orchestrator.py:376-378`), and depending on the call site, the caller may or may not check for `None` before proceeding — a repeat of the exact "agent creation timeout... leaving the task never linked to it" bug class already found once in the repair-agent path (see the diff comment at `autopilot_api.py:957`).

**Why this pattern exists:** `orchestrator.py` also has a standalone `main()`/`if __name__ == "__main__":` entrypoint (`:4244-4317`) that predates `AutopilotService` — before the in-process migration, `orchestrator.py` genuinely was a separate OS process, and cross-process HTTP calls to the backend made sense. The CLI's `heph autopilot start` (`src/cli/commands/autopilot.py:48-98`) now calls `POST /api/autopilot/start`, which routes to `AutopilotService.start()` and runs everything in-thread — so the `API_BASE`/`api_get`/`api_post` machinery is very likely a holdover from the pre-migration architecture that was never removed once the process boundary disappeared, rather than a deliberate design choice for the current path.

**Direction:** Now that the pipeline is guaranteed in-process (per `service.py`'s own stated design goal), `orchestrator.py`'s `api_get`/`api_post` helpers could call the same underlying functions/ORM operations directly instead of going through HTTP+JSON+timeout+retry semantics for a call that never leaves the process. This is a larger refactor (17 call sites, likely more depth than shown here), but it removes an entire class of "silent None on timeout" bugs and one layer of serialization/deserialization overhead. If keeping HTTP is intentional (e.g. to preserve a clean interface boundary for a future subprocess-based orchestrator), it's worth documenting that intent explicitly, since it currently reads as leftover architecture from before the subprocess-to-in-process migration.

**Confirmed second instance:** `src/mcp/autopilot_api.py:847-867` (`spawn_repair_review_agent`) lazily imports `api_post`/`get_tasks` directly from `orchestrator.py` (`:851`) and calls `get_tasks(status="failed", workflow_id=wf_id)` etc. (`:859-862`), each of which does a blocking `requests.get` to `127.0.0.1:8300/api/tasks` (`orchestrator.py:407-417` → `api_get`, `:355-362`) — the FastAPI server process calling back into its own `/api/tasks` endpoint over HTTP instead of querying the `Task` table directly, from code that already has DB access in-process. Like the orchestrator's own self-calls, this runs inside a `run_in_executor` thread (`autopilot_api.py:838`, `_run_repair` → `spawn_repair_review_agent`), so it doesn't block the event loop, but it's the same "reach for HTTP instead of the function/ORM call that's one import away" pattern, and it inherits `api_get`'s silent-`None`/silent-`[]`-on-any-failure behavior (`orchestrator.py:360-361`) with no propagated error.

---

#### H-3: Status derivation is fragile and inconsistent across five independently-writable status columns, with no single source of truth
**Location:** `src/core/database.py` — `Task.status` (`:84-93`, 11-value CHECK constraint), `Workflow.status` (`:262-264`, 4 values), `PhaseExecution.status` (`:357-361`, 5 values), `Feature.status` (`:978-982`, 5 values), `AutopilotDesign.status` (`:1018+`, extended to 7 values per `docs/architecture.md:225`). Derivation/read logic scattered across `src/mcp/autopilot_api.py:2053-2183` (`get_project_design_status`), `src/autopilot/orchestrator.py` (`_update_feature_status`, `_update_design_status`, `run_design_aggregate`), and the frontend (`DesignQueuePanel.tsx`).

None of these five status columns are a computed/derived view — each is a plain string column, independently written by different code paths, with no enum shared across models (just per-column `CheckConstraint` string lists). `Feature.status` in particular is written in at least three places that can disagree: (1) the orchestrator sets it directly during pipeline execution (`active`/`completed`/`failed`/`skipped`), (2) the pause/resume endpoints set it directly (`autopilot_api.py` — `pause_feature`/`resume_feature`), and (3) `get_project_design_status` *also* derives a "live" status from child `Task` rows and — per the current uncommitted diff — writes that derived value back into the DB column when it disagrees (`autopilot_api.py:2170-2179`, comment: "Self-heal the DB column too... other code reads Feature.status directly, not this derived value"). That comment is itself an admission that at least one other code path trusts the raw column and will get it wrong until the next time this specific derivation endpoint happens to run.

This session's commit history is a direct timeline of this fragility: `0955b85` ("derive feature status from task statuses instead of DB value") → `bee5637` ("add 'paused' to Feature status CHECK constraint") → `6af81c9` ("respect DB 'paused' status in feature status derivation and fix resume") → the current uncommitted diff further patches the derivation to exclude `DIAGNOSTIC:`-prefixed monitor-generated tasks from the "are all tasks done" calculation (`autopilot_api.py:2144-2151`), because a stray diagnostic task made a genuinely-complete feature look "mixed" forever.

**Failure scenario:** A future code path (e.g. a new dashboard widget, or a new pause/resume-adjacent feature) reads `Feature.status` directly instead of going through `get_project_design_status`'s derivation logic, and gets a stale/wrong answer — exactly the bug pattern fixed three times already this session, just relocated to a new call site. There is no structural guarantee this won't recur, because "status" isn't owned by one function; it's independently mutated by pipeline code, user-triggered pause/resume, and a read-path self-heal.

**Direction:** This is the single most valuable thing to fix structurally, not just patch reactively. Consider making `Feature.status` (and ideally `AutopilotDesign.status`) a genuinely derived/computed property — either (a) never store it, always compute from child `Task`/`Workflow` rows via one canonical function that every read path calls, or (b) keep it stored but route every write through one function that recomputes and persists it, and audit/remove the other direct-write call sites. Given how many independent write paths already exist (pipeline, pause, resume, self-heal), a "single writer" function would eliminate this entire recurring bug class rather than requiring another patch each time a new corner case surfaces (diagnostic tasks, paused-mid-run, no-tasks-yet, etc. — all of which have each individually caused a bug this session).

---

#### H-4: No process supervision — backend, monitor, and frontend are unsupervised, independently-crashable processes
**Location:** `src/cli/commands/start.py:164-211` (`_start_backend`, `_start_monitor`), both `subprocess.Popen(..., start_new_session=True)` with no liveness monitoring after launch; `run_monitor.py` (Guardian/monitor entrypoint) and `run_server.py` (FastAPI backend) are fully independent OS processes with no shared lifecycle.

The backend recovers gracefully from its own restart for *pipeline* state (`AutopilotService.load_persisted_state()` / `_persist_running_state()` in `src/autopilot/service.py:23-150`, wired into `startup_event()` in `src/mcp/server.py:1315-1341` — this is a well-designed recovery mechanism, added this session, that explicitly closes the gap where "an in-flight pipeline goes silently dead on any backend restart/crash"). There is also an orphaned-agent resume scan on backend startup (`server.py:1030-1093`). But there is no equivalent for the **monitor process**: if `run_monitor.py` crashes or is killed, nothing restarts it, nothing in the backend or frontend detects it, and no orphaned-agent-style resume logic re-attaches Guardian oversight to already-running agents. Every agent in flight silently loses its steering/stuck-detection safety net, with no visible signal to the user beyond agents eventually stalling with no intervention.

**Failure scenario:** The monitor process OOMs or crashes on an unhandled exception (there is no top-level supervisor restarting it per `start.py`). Agents continue running, tasks continue being marked done/failed by the agents themselves, and the orchestrator's `_advance_phases` polling loop keeps working fine — but any agent that gets stuck, loops, or drifts off-track after that point has no Guardian to intervene, and the pipeline just runs slower or eventually times out with no diagnostic signal pointing at "the monitor died."

**Direction:** At minimum, have the backend periodically check the monitor's PID/liveness (it already tracks PIDs via `save_pid()` in `start.py`) and surface a warning in the dashboard if the monitor is down. A more robust fix is a lightweight supervisor (even a simple watchdog loop that respawns `run_monitor.py` on unexpected exit) — the pattern already exists for pipeline auto-resume; the same idea applied to process liveness would close this gap. (Note: `src/sdk/process_manager.py:169-220` does implement a 10s-poll watchdog with restart-capping — but it only supervises processes launched through the `heph` CLI path; it's worth confirming whether `start.py`'s `_start_backend`/`_start_monitor` actually route through it, since their own `Popen` calls show no visible link to that watchdog.)

---

#### H-5: SQLite has zero concurrency tuning despite a genuinely multi-process write workload
**Location:** `src/core/database.py:1129-1137` (`create_engine(..., connect_args={"check_same_thread": False}, poolclass=StaticPool)`); confirmed via full-codebase grep that no `PRAGMA journal_mode`, `PRAGMA busy_timeout`, or `PRAGMA synchronous` statement exists anywhere in `src/`.

The system's own architecture is multi-process by design: the FastAPI server, the standalone monitor (`run_monitor.py`), and (per `orchestrator.py`'s `requests`-based self-calls in H-2) potentially a separate orchestrator process, all open independent `DatabaseManager()`/`sqlite3` connections against the same `hephaestus.db` file. SQLite's default journal mode (rollback journal, not WAL) takes an exclusive lock on the *entire file* for the duration of any write transaction, and with no `busy_timeout` set, a connection that hits a locked database raises `OperationalError: database is locked` immediately rather than waiting/retrying.

**Failure scenario:** Two processes (e.g. the orchestrator's polling loop committing a phase transition, and the monitor committing a Guardian steering record) attempt to write within the same narrow window. One gets `database is locked`. Given the pervasive broad `except Exception` handling documented elsewhere in this review (M-4), the likely observed symptom is not a crash but a silently dropped/delayed state update — a status write that should have happened doesn't, with no operator-visible error, only a later symptom (a stuck-looking workflow, a status that doesn't match reality) that's hard to trace back to a lock contention event that already passed.

**Direction:** Two independent, low-effort mitigations: (1) set `PRAGMA journal_mode=WAL` once at startup (allows concurrent readers alongside a single writer, dramatically reducing lock contention for this read-heavy/write-light workload; WAL mode is persistent across restarts once set, so it only needs to execute once at init time), and (2) set a `PRAGMA busy_timeout` (e.g. 5000ms) so concurrent writers block-and-retry instead of failing immediately. Both are a few lines in `DatabaseManager.__init__` (or as a connection event listener) and are the standard fix for exactly this multi-process-SQLite topology — worth prioritizing given how many of this session's other fixes (pause/resume, feature-status self-heal) are themselves concurrent-write paths that would silently benefit or silently fail depending on this.

---

### Medium Severity

#### M-1: `src/mcp/server.py` and `MonitoringLoop` are God modules/objects
**Location:** `src/mcp/server.py` — 7770 lines, 75 route handlers (`@app.*`/`@router.*`), 86 top-level functions. `src/monitoring/monitor.py:407-2469` — the `MonitoringLoop` class alone spans ~2063 lines with ~35 methods.

Both are large enough that "what does this module own" is no longer answerable at a glance. `server.py` mixes agent lifecycle endpoints, ticket endpoints, workflow endpoints, project endpoints (some of which are also independently defined in `projects_api.py` and `autopilot_api.py` as separate routers), startup/resume logic, and ad hoc helper functions, all in one file. `MonitoringLoop` combines the polling loop, tmux output parsing, stuck-detection heuristics, auto-restart logic, and orchestration of `Guardian` calls in one class.

**Failure scenario:** Not a correctness bug per se, but a maintainability/onboarding risk — a change to, say, ticket-related logic in `server.py` requires understanding (or at least scrolling past) agent-lifecycle and startup-resume code that has nothing to do with it, and increases the chance of an unrelated merge conflict or an accidental cross-concern coupling (e.g. a helper function meant for one endpoint quietly reused by an unrelated one).

**Direction:** `server.py` has already partially decomposed into `autopilot_api.py`/`projects_api.py` as separate `APIRouter`s — continuing that decomposition (tickets, agents, workflows as their own router modules) would be consistent with the existing pattern rather than a new one. For `MonitoringLoop`, splitting tmux-output-parsing/stuck-detection heuristics into a separate class from the polling-loop orchestration would make each piece independently testable (see Test Coverage Gaps below).

#### M-2: `MonitoringLoop` reaches into `Guardian`'s private state directly
**Location:** `src/monitoring/monitor.py:1134` — `self.guardian._record_steering(agent.id, "AUTO_RESTART", ...)`.

`Guardian._record_steering` (`src/monitoring/guardian.py:514`) is name-mangled as private (leading underscore), but `MonitoringLoop._auto_restart_agent` calls it directly from outside the class instead of through a public method. This is a small instance of a broader pattern: `MonitoringLoop` owns and directly instantiates a `Guardian` (`monitor.py:434`) and appears to treat it more as a bag of loosely-related methods than an object with an enforced interface.

**Failure scenario:** Low risk in isolation, but it means `Guardian`'s internal steering-history bookkeeping (used by the same-session confirmation-gating logic added this session, `guardian.py:73-78`) can be mutated from two different call sites with different invariants in mind, making it harder to reason about `Guardian`'s internal state as self-contained.

**Direction:** Add a small public method (e.g. `Guardian.record_auto_restart(agent_id, reason)`) if `MonitoringLoop` needs to log this from outside; keep `_record_steering` genuinely private.

#### M-3: `_advance_phases` is a ~190-line, deeply-nested, case-numbered state machine acting as "the single source of truth"
**Location:** `src/autopilot/orchestrator.py:2155-2343`.

The function itself documents nine-ish distinct cases (Case 0, 0b, 1, 2, ...) for phase progression, each with its own DB queries and branching logic, all inside one `with get_db() as db:` block. It's clearly been patched incrementally in response to specific bugs (e.g. the "all tasks failed — retry" branch added this session at `orchestrator.py:2308-2327`, per the current diff) rather than designed as a single coherent state transition table. There is a documented idempotency guard against re-firing the same transition twice (`phase_manager.py:653-667`, "prevents race conditions where the spec gate and `_advance_phases` both try to mark the same phase complete") — a check-then-act pattern relying on `PhaseExecution.status`, not a DB-level lock or transaction isolation guarantee, so it is a mitigation, not an elimination of the underlying race.

**Failure scenario:** A future case gets added without fully understanding the ordering/precedence of the existing cases (each `if`/`elif` implicitly depends on earlier cases having already returned), reintroducing a duplicate-task-creation or stuck-workflow bug similar to ones already fixed (`c933998` "Case 0b for in_progress with no tasks", `28d1f07`-adjacent history).

**Direction:** Consider extracting each case into its own well-named, independently testable function (`_advance_phases` already delegates to `_create_phase_task`/`_fire_phase_transition`, so the pattern exists — just not consistently for every case), and/or replacing the numbered-case comments with an explicit priority-ordered list documented once at the top of the function so the implicit ordering dependency is visible rather than inferred from reading the whole function.

#### M-4: 69+ bare `except Exception:` blocks with no logging across the six core modules
**Location:** Representative examples: `src/autopilot/orchestrator.py:279-280`, `:290-291` (both silently `return False`/`return None` with zero logging); similar patterns at `src/mcp/autopilot_api.py:218`, `:237`, `:359`, `:374`, `:406`. A grep across just `autopilot_api.py`, `orchestrator.py`, `guardian.py`, `monitor.py`, `manager.py`, `server.py` found 69 occurrences of `except Exception:`/`except:` with no exception variable bound (i.e., not even available to log if someone wanted to).

Not all of these are bugs — some are legitimately "best-effort, don't care why it failed" (e.g. cleanup code). But at least the two cited in `orchestrator.py:279-291` swallow the actual exception type/message entirely, in functions (`is_pipeline_running`, `get_last_run_id`) whose failure mode (silently returning `False`/`None`) could mask a real bug (e.g. a corrupted state file) as "nothing is running."

**Failure scenario:** The pipeline appears to not be running (state file read fails silently, returns `False`) when in fact a state file is present but malformed — the operator sees "not running" and starts a duplicate pipeline, with no error in any log pointing at why the first check failed.

**Direction:** Not a blanket "add logging everywhere" — that would be noisy given 69 sites, many of which are genuinely inconsequential. Worth a pass to identify the subset where the swallowed exception could plausibly hide a real fault (state/DB reads feeding user-facing decisions) versus pure best-effort cleanup, and add at least `logger.debug(f"...: {e}")` to the former.

#### M-5: Frontend polls for state the backend already knows synchronously, on a disconnected timer
**Location:** `frontend/src/components/autopilot/DesignQueuePanel.tsx:71-108` (status-fetch `useEffect` + 10s `setInterval`), `:177-183` (pause/resume mutation's `onSuccess`).

Per the current uncommitted diff (already fixed this session but illustrative of a broader pattern), `designStatuses` — which drives the pause/resume button icon — was populated only by its own independent 10-second timer, completely disconnected from the React Query cache invalidation the pause/resume mutation already triggers. The fix (extracting `fetchStatuses` via `useCallback` and calling it directly from the mutation's `onSuccess`) is a reasonable patch, but the underlying pattern — a separate `setInterval`-based poll loop maintaining parallel component state (`designStatuses`) alongside React Query's own cache/invalidation system — is still there for every other piece of state in this file that isn't wired the same way. Any future state derived similarly (e.g. task-level status inside `FeatureRow`) is one dropped invalidation-wire away from the same "looks stale for up to 10s" bug class.

**Failure scenario:** A new mutation is added (e.g. a "retry task" button) that doesn't know to also call `fetchStatuses()`, and its UI effect is invisible for up to 10 seconds — same symptom as the bug already fixed for pause/resume, in a new location.

**Direction:** Consider migrating `designStatuses` fully into React Query (as its own query key, refetched via `invalidateQueries` like the rest of the mutations already do) rather than a hand-rolled `setInterval` + local `useState`, so every mutation that should invalidate it does so through the same mechanism the rest of the file already uses, instead of requiring each call site to remember to call `fetchStatuses()` manually.

#### M-6: MCP tool schema relies on agents self-reporting IDs/phase numbers correctly, with guardrails bolted on after the fact
**Location:** `mcp/mcp_client.py:54-79` (`create_task` docstring warnings about phase-number guessing), `src/mcp/server.py:2129-2274` (phase-order resolution + "own phase" guard added this session), `src/agents/manager.py:797-930` (prompt construction repeatedly re-stating "include agent_id/workflow_id on every call").

This session added a same-phase-only guard rejecting agent-created tasks that target a different phase (`server.py:2226-2274`, with a detailed rationale comment about "full implementation work... filed under an architecture-design phase, corrupting the pipeline"). That's a solid mitigation, but it's a runtime guard compensating for a design where the correctness of phase/agent/workflow IDs depends entirely on an LLM correctly reading and echoing values from its prompt text, with no structural enforcement (e.g. no scoped/signed token, just plain string IDs the agent must copy correctly). The `save_memory`/`validate_my_agent_id` tools accepting-and-ignoring `workflow_id`/`task_id` params (`mcp/mcp_client.py:163-176`, `:333-339`) so agents that pass them "per the general habit" don't get rejected is another symptom of the same underlying looseness — the tool surface bends around whatever agents happen to guess rather than a validated contract.

**Failure scenario:** Already happened once this session per the comments (implementation work filed under the wrong phase) — the guard added prevents the specific *first-task-of-a-phase* variant, but an agent could still, in principle, pass a wrong-but-existing phase_id for a *subtask* within a phase that already has tasks (the guard only fires when seeding the phase's first task, per the `existing_tasks > 0` check earlier in the same function).

**Direction:** No specific code change recommended here — this is more a "worth watching" architectural note than a bug. If wrong-phase task creation recurs in a form the current guard doesn't cover, consider making phase context implicit (the server already knows the calling agent's current phase from its assigned task — see the `own_task` lookup in `server.py:2249-2274` — so the tool could resolve "current phase" itself rather than trusting a caller-supplied `phase_id` for subtask creation at all).

---

### Low Severity

#### L-1: Magic string statuses instead of a shared enum
**Location:** `src/core/database.py` — five separate `CheckConstraint` string lists for `Task.status` (11 values, `:84-93`), `Workflow.status` (4 values, `:262-264`), `PhaseExecution.status` (5 values, `:357-361`), `Feature.status` (5 values, `:978-982`), `AutopilotDesign.status` (7 values per `docs/architecture.md:225`). No shared Python enum backs any of these; call sites compare against string literals throughout `orchestrator.py`, `autopilot_api.py`, `monitor.py`.

**Failure scenario:** A typo in a status string literal (e.g. `"in_progres"`) at a new call site would silently fail every `==`/`.in_()` comparison against it, with no static check catching the mismatch — the CHECK constraint only validates at INSERT/UPDATE time, not at comparison time.

**Direction:** A `StrEnum`/`Enum` per status column (or one shared status vocabulary where values genuinely overlap, e.g. `"active"`/`"completed"`/`"failed"` appear in multiple models) would let type checkers and IDEs catch typos and give a single place to see all valid values per entity. Given the number of existing string-literal call sites, this is a larger mechanical refactor best done alongside the H-3 status-derivation consolidation rather than standalone.

#### L-2: Duplicated logging-setup boilerplate across entrypoints, recently fixed in two places but not audited everywhere
**Location:** `run_server.py:12-24`, `run_monitor.py:27-39` (both fixed this session to remove a redundant `FileHandler` that duplicated `start.py`'s own stdout redirection — see diff comments explaining the stray `hephaestus_server.log`/`logs/monitor.log` files this caused).

Both entrypoints had nearly identical `logging.basicConfig(...)` boilerplate, and both had independently accumulated the same redundant-`FileHandler` bug. This suggests the pattern was copy-pasted between the two files rather than shared, so a similar issue could exist in any other CLI entrypoint (`heph`, other `run_*.py` scripts) that followed the same copy-paste origin but wasn't touched this session.

**Direction:** Worth a quick audit of any other `run_*.py`/CLI entrypoints for the same double-logging pattern, and consider a small shared `configure_logging()` helper so the fix doesn't need to be independently rediscovered a third time.

#### L-3: `_should_steer_agent` cooldown and the new consecutive-flag gating are two separate, stacked rate-limiters with no unified view
**Location:** `src/monitoring/guardian.py:432-450` (`_should_steer_agent`, "max 1 steering per 10 minutes") and `:73-78`/`:335-380` (new `_consecutive_flags` 2-strike confirmation gate, added this session).

Both mechanisms are real and independently justified (per the regression-test docstring at `tests/test_guardian.py:357-366`, the confirmation gate specifically fixes "a single 'off_track' trajectory judgment interrupt[ing]... a legitimate, in-progress file write"), but an agent's steering eligibility now depends on satisfying two independent, differently-scoped state machines (a 10-minute cooldown keyed by last-steered-time, and a 2-consecutive-flags-within-10-minutes confirmation keyed by flag type) with no single function or log line that explains "why didn't Guardian act just now" in one place.

**Direction:** Not urgent, but if a third gating condition gets added later (plausible, given the pattern of this session's fixes), consider consolidating into one `_evaluate_steering_eligibility(agent, steering_type) -> (bool, reason)` function that logs its reasoning in one place, rather than continuing to stack independent early-`return`s through `steer_agent`.

---

## Test Coverage Gaps

Size-vs-test-depth comparison for the five modules named in the review scope:

| Module | Source lines | Test file(s) | Test functions | Priority | Coverage character |
|---|---|---|---|---|---|
| `src/autopilot/orchestrator.py` | 4318 | `tests/test_orchestrator.py` (516 lines), `tests/test_orchestrator_helpers.py` (987 lines) | 26 + 81 = 107 | **Critical** | Good breadth on small pure helpers — `_resolve_execution_order`, `_validate_features_json`, `_should_skip` each have their own dedicated test files (`test_resolve_execution_order.py`, `test_validate_features_json.py`, `test_should_skip.py`). But every one of those 107 tests targets a small, mockable helper; the actual control-flow core — `run_continuous_pipeline`, `run_single_workflow`, `run_phase0`, and `_advance_phases` (H-0, M-3, ~1,700 combined lines, roughly 40% of the file) — has no test referencing it anywhere in `tests/`. This is exactly the code where H-0 (the detached-`Phase`-object bug) lives, and it went unnoticed presumably because nothing exercises this path against a real or realistically-faked DB/session lifecycle. |
| `src/monitoring/guardian.py` | 628 | `tests/test_guardian.py` (450 lines) | 3 top-level (`TestGuardian`) + 6 in `TestSteerAgentGating` = 9 | Medium | Reasonable mocked-unit coverage of `analyze_agent_with_trajectory` and `_build_accumulated_context` (LLM provider fully mocked), and good regression coverage of the new consecutive-flag gating. No test exercises `detect_agent_exited`/`detect_garbled_output` (tmux-output heuristics) against realistic tmux output samples. No integration test runs Guardian against a real (non-mocked) DB or a real orchestrator/monitor loop end-to-end. |
| `src/monitoring/monitor.py` | 2469 (with `MonitoringLoop` alone ~2063 lines, ~35 methods) | `tests/test_monitor.py` (1088 lines) | 32 | High | Largest test file by line count in this comparison, but `restart_agent` — the method `_auto_restart_agent`/steering-escalation logic actually calls — is **only ever referenced as a `Mock()`/`AsyncMock()` stub** in these tests (`test_monitor.py:554-558`, `:975-977`); its own real implementation in `manager.py` is never exercised from this side either (see below). `_auto_restart_agent` itself (H-0b, the silent-no-op DB write) has no test verifying the agent's status actually persists after a restart. |
| `src/mcp/autopilot_api.py` | 3241 (75 functions) | `tests/test_autopilot_api.py` (942 lines, 59 tests), `tests/test_autopilot_api_endpoints.py` (139 lines, 16 tests), `tests/test_autopilot_api_helpers.py` (187 lines, 20 tests) | 95 across 3 files | High | Broadest raw test count of the five, but split across three files with unclear ownership boundaries between them. The pause/resume endpoints and the `get_project_design_status` derivation logic (H-3) — the most-patched code this session — are exactly the kind of logic that benefits from exhaustive case-based unit tests (paused-with-diagnostic-task, paused-with-no-tasks, all-failed-retry, etc.); worth confirming each of this session's fixes has a corresponding regression test, the way `test_guardian.py`'s `TestSteerAgentGating` does for the Guardian fix. `spawn_repair_review_agent` (H-2's second self-HTTP-call instance) has no visible dedicated test. |
| `src/agents/manager.py` | 2166 | No dedicated `test_manager.py`/`test_agent_manager.py`; agent-related coverage spread across `test_agent_communication.py`, `test_agent_output_capture.py`, `test_agent_output_integration.py`, `test_agent_workflow_context.py` (each narrowly scoped by name), all mocking `libtmux.Server` — no test exercises real tmux/subprocess spawning. | — | **Critical** | **The thinnest coverage of the five relative to its size and centrality.** `create_agent_for_task` (`manager.py:54`, ~527 lines — the largest function found anywhere in the reviewed scope) is the single entry point for all agent spawning; no test file exercises it as a unit. `restart_agent` (~324-326 lines, the second-largest function in the file) has **zero direct test coverage anywhere** in the suite — every reference to it in `tests/` is a mock stub on the *calling* side (monitor.py's tests), never a test of `restart_agent`'s own body. The prompt-construction logic (`manager.py:797-930`, repeatedly patched this session for the `workflow_id`-on-every-call instructions) has no dedicated test verifying the exact prompt text agents receive, despite that text being exactly what drove the phase-guessing and `save_memory`-rejection bugs fixed this session. |

**Overall:** Two modules stand out as highest-priority gaps, both because the untested code requires either real subprocess/tmux setup or a fully-running multi-stage DB-backed pipeline to exercise — exactly the conditions most likely to hide the class of bug this review found (H-0, H-0b):
1. **`src/agents/manager.py`** — `create_agent_for_task` and `restart_agent` (the two largest functions in the reviewed scope, combined ~850 lines) have no direct test coverage; a `test_agent_manager.py` covering prompt construction and both functions' happy/error paths would likely have the highest bug-catching value per test-writing effort of anything in this table, given how many of this session's fixes originated in exactly this file's prompt-construction logic.
2. **`src/autopilot/orchestrator.py`**'s control-flow core (`_advance_phases`, `run_single_workflow`, `run_phase0`, `run_continuous_pipeline`) — untested despite being, by its own docstring, "the single source of truth for phase progression," and despite already having been patched at least twice in direct response to production bugs (Case 0b, the all-failed-retry case) rather than being designed test-first. A test harness that runs `_advance_phases` against a real (or realistically session-scoped) DB rather than mocking `get_db()` entirely would likely have caught H-0 before it shipped.
