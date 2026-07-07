# Multi-Project Concurrency Design

## Goal

Today, Hephaestus's autopilot can only run one project's pipeline at a time
per backend process. This document designs what's needed to run **N
projects concurrently** in one backend, each with its own design queue,
its own set of feature pipelines, and its own start/stop/status control —
without one project's activity pausing, stopping, or corrupting another's.

**Initial target: 2 concurrent projects, not unbounded N.** The design
below is written generally (nothing here is specific to the number 2), but
the first implementation caps concurrency at a configurable limit rather
than removing the limit entirely. This bounds the blast radius of the
riskiest part of this change (§6.1's service registry and the two
orchestrator hazard fixes in §6.2) to a scale that's easy to reason about
and test, before deciding whether to raise the cap.

The cap lives in config, not code:

```yaml
# hephaestus_config.yaml
autopilot:
  max_concurrent_projects: 2
```

Read the same way `workflow_timeout_seconds` already is
(`simple_config.py:195`):

```python
self.max_concurrent_projects = autopilot.get("max_concurrent_projects", 2)
```

`AutopilotServiceRegistry.get_or_create` (§6.1) enforces it: starting a
pipeline for a project not already in the registry, when the number of
currently-`running` services already equals the configured cap, is
rejected with a 409 (same status code `/start` already uses for "pipeline
already running", `autopilot_api.py:2953`) and a message naming which
projects are occupying the two slots. Raising the cap later is a config
change, not a code change, once the underlying registry exists — the
number 2 is not hardcoded anywhere in the design below.

Within a single project, concurrency already works: `run_feature_pipelines`
(`src/autopilot/orchestrator.py:4067`) runs up to `MAX_PARALLEL_FEATURES`
(currently 4, line 86) features in parallel via a `ThreadPoolExecutor`, each
running its own blocking `run_single_workflow` polling loop in a worker
thread. That mechanism is proven and doesn't need to change. The problem is
one level up: the single global "which project is running" state that sits
above it.

## Current State: Six Places That Assume One Project

### 1. `AutopilotService` — a true singleton

`src/autopilot/service.py:31` — `AutopilotService` holds exactly one
`_task`, `_running`, `_project_path`, `_design_queue`, `_current_design`,
etc. as plain instance attributes (not keyed by anything). It's instantiated
once as a module-level global:

```python
# src/autopilot/service.py:326-334
_service: Optional[AutopilotService] = None

def get_autopilot_service() -> AutopilotService:
    global _service
    if _service is None:
        _service = AutopilotService()
    return _service
```

Calling `start()` for project B while project A is running overwrites A's
state in place — the service has no concept of "more than one pipeline."

### 2. Orchestrator — two specific global-state hazards, not just "the polling loop"

The polling loop itself (`run_single_workflow`, `orchestrator.py:3052`)
already runs safely per-thread — that's not the blocker. Two concrete pieces
of *shared global state* are:

**a) The stop event is a bare module global**, assigned fresh on every
pipeline start:

```python
# src/autopilot/service.py:305-306, inside _run_pipeline_sync
import src.autopilot.orchestrator as orch_module
orch_module._service_stop_event = self._stop_event
```

```python
# src/autopilot/orchestrator.py:4464-4470
def _should_stop() -> bool:
    event = globals().get("_service_stop_event")
    ...
```

If two projects both call `_run_pipeline_sync`, the second one's assignment
silently replaces the first's. Whichever project's `stop()` fires last wins
control of *both* pipelines' stop signal — project A's "stop" can be
swallowed, or can incorrectly stop project B. **This is a real bug today**,
not a hypothetical one introduced by adding concurrency — it just hasn't
been hit yet because only one project has ever run at a time.

**b) `get_active_workflows()` has no project filter**:

```python
# src/autopilot/orchestrator.py:693-708
def get_active_workflows() -> list:
    with get_db() as session:
        workflows = session.query(Workflow).filter(Workflow.status == "active").all()
        ...
```

`run_single_workflow`'s default `pause_existing=True` path
(`orchestrator.py:3081-3105`) calls this and pauses *every* active workflow
it finds — across all projects. `_run_one_feature` already passes
`pause_existing=False` for the parallel-features-within-one-project case
(with a comment explaining exactly this hazard, `orchestrator.py:3953`), but
the *design-level* launch path (`run_phase0`/first-feature kickoff) still
uses the default. Launching project B's Phase 0 while project A has active
feature workflows would pause A.

### 3. Agent Manager — mostly fine, worktree base is the risk

`AgentManager` (`src/agents/manager.py`) already scopes work by
`agent_id`/`task_id`, and multiple agents already run concurrently within
one project (that's the whole point of `MAX_PARALLEL_FEATURES`). The
specific risk for multi-project is **worktree path collisions**: worktree
base paths are derived from the project's own repo
(`WorktreeManager.worktree_base`, `src/core/worktree_manager.py:136-141`,
`<repo>/.worktrees/wt_<branch>`), which is safe as long as two projects
never share a repo path — true today since each project has its own
`base_dir`. `terminate_agent` and the shared-worktree commit path
(`agents/manager.py`) key everything off `agent_id`/`workflow_id`, both
already globally unique (UUIDs), so no change needed here beyond auditing
for accidental global agent-status queries (see #5).

### 4. SQLite

WAL mode is already on (`src/core/database.py:1262`,
`PRAGMA journal_mode=WAL`), which is exactly the right call for this:
WAL allows concurrent readers alongside a single writer, and
`busy_timeout` (`database.py:1258` area) makes writers block-and-retry
instead of failing outright. For 2-3 concurrent projects each doing modest
write volume (a handful of task/agent status updates per phase transition),
this should hold up fine. It becomes the wrong tool once write contention
gets high enough that `busy_timeout` retries start compounding into visible
latency — a genuinely different profile than "a few devs running the smoke
test," more like "10+ projects with agents completing tasks every few
seconds." Cross that bridge with a real load test, not a guess; Postgres is
a bigger migration (connection config, `StaticPool` assumptions in
`database.py`, every raw `sqlite3.` reference in tooling scripts) than this
document should scope in.

### 5. Queue Processor — global concurrency cap, global priority queue

`QueueService` (`src/services/queue_service.py:17-26`) is constructed once
with a single `max_concurrent_agents` and no project dimension at all:

```python
# queue_service.py:29-42
def get_active_agent_count(self) -> int:
    count = session.query(Agent).filter(Agent.status.in_([...])).count()
```

```python
# queue_service.py:197-198+
def get_next_queued_task(self) -> Optional[Task]:
    tasks = session.query(Task).filter(Task.status == "queued")...
```

Both queries are unscoped — the concurrency cap is shared across every
project, and the priority queue picks whichever queued task ranks highest
*system-wide*, not per-project. With one project this is invisible; with
two, project B's high-priority task can starve behind project A's queue, or
one project can consume the entire global agent-concurrency budget and
starve every other project outright.

### 6. Frontend Status — already mostly done

`GET` endpoints already thread `project_id` through
(`src/mcp/autopilot_api.py`'s `get_project_design_status`,
`FrontendAPI.get_system_overview`, etc. — see this session's earlier
`get_workflow_info`/`selectedExecutionId` fixes). The gap is entirely on
the *service* side: `POST /api/autopilot/stop` and `POST
/api/autopilot/start` call `get_autopilot_service()` — the same singleton
for every project — so there is currently no way to target "stop project
B" specifically; there's only one pipeline to stop, whichever one is
running.

## Design

### 6.1 `AutopilotService` → per-project registry, capped

Replace the single `_service` global with a registry keyed by `project_id`,
enforcing `Config.max_concurrent_projects`:

```python
class AutopilotServiceRegistry:
    def __init__(self, max_concurrent: int):
        self._services: Dict[str, AutopilotService] = {}
        self._max_concurrent = max_concurrent
        self._lock = threading.Lock()

    def get_or_create(self, project_id: str) -> AutopilotService:
        with self._lock:
            if project_id not in self._services:
                self._services[project_id] = AutopilotService(project_id)
            return self._services[project_id]

    def get(self, project_id: str) -> Optional[AutopilotService]:
        return self._services.get(project_id)

    def running(self) -> List[AutopilotService]:
        return [s for s in self._services.values() if s.running]

    def can_start(self, project_id: str) -> Tuple[bool, str]:
        """Check the concurrency cap before starting. Doesn't reserve a
        slot -- caller still needs its own check against a concurrent
        second start() for the same project (existing `service.running`
        check in the /start route already covers that case)."""
        running = self.running()
        if any(s.project_id == project_id for s in running):
            return True, ""  # restarting/already-tracked project, not a new slot
        if len(running) >= self._max_concurrent:
            names = ", ".join(s.project_id for s in running)
            return False, (
                f"Max concurrent projects ({self._max_concurrent}) reached: "
                f"{names}. Stop one before starting another."
            )
        return True, ""
```

`AutopilotService.__init__` takes `project_id` and uses it to namespace its
own persisted-state file (`_RUNNING_STATE_PATH`,
`service.py:27`, currently one fixed path for the whole backend — needs to
become `running_pipeline_{project_id}.json` so two projects' crash-recovery
state doesn't collide) and its stop event (see 6.2).

The actual routes live in `src/mcp/autopilot_api.py`, not `server.py`
(corrected from an earlier draft of this doc):

- `GET /status` (`autopilot_api.py:322`) already takes optional
  `project_id` and already works around the singleton by querying
  `Workflow.project_id` directly when given one (see the comment at
  `autopilot_api.py:335`: *"the service.running flag is global, not
  per-project"*) — this becomes unnecessary once services are truly
  per-project, but isn't harmful to leave as a belt-and-suspenders check.
- `POST /start` (`autopilot_api.py:2940`) currently takes only
  `project_path` — no `project_id` at all. Resolve it server-side via
  `AutopilotProject.filter_by(base_dir=project_path)` (the same lookup
  `AutopilotService.start()` already does at `service.py:104-107` to
  activate the project) rather than requiring a frontend change; call
  `registry.can_start(project_id)` right after that lookup and before
  calling `service.start()`.
- `POST /stop` (`autopilot_api.py:2968`) already takes optional
  `project_id` and already uses it to scope which workflows get paused/
  which agents get terminated (`autopilot_api.py`'s `query.filter(Workflow.project_id
  == project_id)` when given) — but it still calls the one global
  `service.stop()` regardless. Once `get_autopilot_service` takes
  `project_id`, this becomes `get_autopilot_service(project_id).stop()`,
  making the existing optional param actually control which pipeline
  stops instead of only scoping the side effects around a global stop.

No frontend changes are required for `/start`/`/stop` to work correctly —
`project_path` is already sufficient to resolve `project_id` server-side.
Passing `project_id` explicitly from the frontend (it already has it
everywhere else, per §6.6) is a nice-to-have that avoids the extra DB
lookup, not a requirement for correctness.

### 6.2 Orchestrator: fix the two concrete hazards, not the whole module

**Stop event**: replace the bare module global with a dict keyed by
`workflow_id` (not `project_id` — a project can have several concurrent
feature workflows, each needs to be independently stoppable):

```python
# module-level in orchestrator.py
_stop_events: Dict[str, asyncio.Event] = {}

def _should_stop(workflow_id: str) -> bool:
    event = _stop_events.get(workflow_id)
    return event.is_set() if event else False
```

Every call site of `_should_stop()` (`orchestrator.py:3339`, `:4675`)
already has `workflow_id`/`exec_id` in scope — this is a signature change,
not a structural one. `AutopilotService.stop()` sets the event(s) for its
own project's workflow(s) only, looked up via `Workflow.project_id`
(already a real column, `database.py:370`).

**`pause_existing`**: make `get_active_workflows()` take an optional
`project_id` filter (one `.filter()` clause, since `Workflow.project_id`
already exists) and have every design-level launch call site pass its own
`project_id` — same fix `_run_one_feature` already applied for the
feature-level case, just extended to the design/Phase-0-level kickoff path
that currently omits it.

### 6.3 Agent Manager

No structural change. Add one thing: an assertion/log-warning in
`terminate_agent` and the shared-worktree commit path if an operation
somehow spans two different projects' workflows (a canary for "did the
above fixes actually hold," not a functional change) — cheap insurance
during rollout, safe to remove once multi-project has run in production
for a while without tripping it.

### 6.4 SQLite

No change now. Track write-latency percentiles once multi-project ships
(a metric this doc doesn't currently have) and revisit Postgres if `busy_timeout`
retries start showing up in p95/p99 task-status-update latency. Don't
migrate speculatively.

### 6.5 Queue Processor: per-project scoping

Two changes to `QueueService`:

1. `get_active_agent_count(project_id: Optional[str] = None)` — join
   `Agent` through `Task.workflow_id` → `Workflow.project_id` when a
   project_id is given.
2. `get_next_queued_task(project_id: Optional[str] = None)` — same join,
   added to the existing filter.

Then decide the concurrency-cap policy explicitly (this is a product
decision, not just an engineering one — flagging as an open question
below): is `max_concurrent_agents` a **global** ceiling shared fairly
across projects (needs a per-project sub-allocation, e.g. round-robin
across projects with pending queue items), or does each project get its
**own** `max_concurrent_agents`? The per-project queries above support
either; the allocation policy on top of them is the actual design
decision and should be picked before implementation, not during it.

### 6.6 Frontend / API

- `POST /api/autopilot/start`: no required frontend change — `project_id`
  is resolved server-side from `project_path` (§6.1). `/stop` already
  accepts optional `project_id`; no change needed there either, though
  the frontend could start passing it explicitly to skip a DB lookup.
- Status/design-queue endpoints: no change, already correct.
- One new consideration not in the original 6 areas: the **queue
  processor's own trigger** (`trigger_queue_processing()`,
  `app_context.py:68`) is a single fire-and-forget callback with no
  project argument at all currently — callers that want to nudge a
  specific project's queue can't. Low-risk fix: accept an optional
  `project_id` and pass it through to `process_queue()`
  (`server.py:1182`), which already has everything else it needs to scope
  by project once `QueueService` does (6.5).

## Rollout Order

1. `Workflow.project_id`/`AutopilotDesign.project_id` already exist — no
   schema migration needed for the core fix. Good foundation; this is why
   the fix is smaller than "add project scoping to the whole system" might
   sound.
2. Land the two orchestrator hazard fixes (6.2) first, independently of
   the service registry — they're real bugs today (single-project mode
   just never triggers them) and are safe, additive changes with no
   behavior change for the single-project case.
3. `max_concurrent_projects` config value (§Goal) + `AutopilotServiceRegistry`
   (6.1), cap included from day one — this is the actual "turn on
   multi-project" switch, and per the initial 2-project scope, it ships
   *with* the cap enforced, not as a follow-on hardening step.
4. Queue scoping (6.5) — at a cap of 2 projects, one project starving the
   other for agent-concurrency budget is a real but bounded, directly
   observable problem (unlike at N=10+, where it's a silent, hard-to-debug
   fairness issue). Reasonable to ship the 2-project cap first and land
   6.5 as a fast follow once real usage shows whether it's actually
   needed at this scale — but don't defer it indefinitely; revisit before
   raising `max_concurrent_projects` past 2.
5. Frontend param threading (6.6) — not required for the 2-project cut
   (§6.1's server-side `project_id` resolution covers it); do only if/when
   the extra DB lookup on `/start` is worth trimming.

## Non-Goals

- Migrating off SQLite (see 6.4) — revisit only with real contention data.
- Cross-project agent/task dependencies (e.g. one project's ticket blocking
  another's) — not a currently-supported concept even within one project's
  ticket system; out of scope here.
- Per-project resource quotas beyond agent concurrency (e.g. LLM spend
  caps) — a real need eventually, but a separate design.

## Open Questions

- **Queue allocation policy** (6.5): global fair-share vs. per-project cap.
  Needs a product decision before implementation.
- Does `AutopilotDesign`'s existing per-project design queue ordering
  (`ordinal` column, `database.py:1134`) need any change when queue
  *processing* becomes project-scoped, or does it already compose cleanly
  since it's already scoped to `project_id`? (Believed to compose cleanly —
  worth a specific test case rather than an assumption.)

## Effort Estimate

Broadly agrees with the original estimate (medium-large, service refactor
~200 lines, orchestrator isolation ~100 lines, concurrency-edge-case
testing is the real work) with one adjustment: the two orchestrator
hazards in 6.2 (`_service_stop_event`, unscoped `pause_existing`) are
small, targeted fixes (~30-50 lines total, not part of the "~100 lines" of
broader isolation work) and are worth landing *before* the service
registry regardless of the rest of this design's timeline — they're latent
bugs today, not new surface area multi-project introduces.

**Capping at 2 (vs. unbounded N) shrinks the estimate**, mainly by
deferring 6.5 (queue scoping) out of the initial cut (see Rollout Order
step 4) and by removing any need to think about registry scaling — a
plain `dict` + lock comfortably handles 2 entries with no further design.
Rough initial-cut estimate: §6.2's two hazard fixes (~30-50 lines) + config
value (~5 lines) + `AutopilotServiceRegistry` with the cap check (~80-100
lines, smaller than the original ~200 since it's not also solving queue
fairness) + tests for the cap-rejection path and the two hazard fixes.
Small-medium, not medium-large — the medium-large estimate was carrying
the queue-fairness work, which this cut defers.
