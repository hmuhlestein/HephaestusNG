# Autopilot Architecture Review & Redesign

**Status:** Proposal
**Author:** Architecture review
**Date:** 2026-06-19
**Scope:** The Autopilot feature — orchestrator, control loops, API, CLI, state, and UI.

---

## 1. Executive Summary

Autopilot works, but it grew by accretion. The same responsibilities are
implemented two or three times in incompatible ways, two independent control
loops fight for authority over the same pipeline, and the seams between
processes are held together by polling and files in `~/.hephaestus/autopilot/`.
The result is a system that is hard to reason about, prone to race conditions,
and expensive to extend.

This document maps what exists, names the structural problems, and proposes a
target architecture with concrete, incremental changes. The thesis:

> **There should be exactly one orchestrator, one control loop, one source of
> truth for queue/state, and one IPC mechanism. Today there are two or three of
> each.**

The good news: the *domain model* (designs → 10 phases → iterate to spec →
feature report → forensics self-improvement) is sound and worth keeping. The
problems are almost entirely in the plumbing and the control flow, not the
concept.

---

## 2. Current Architecture (as built)

### 2.1 Components

| Layer | File(s) | Role |
|---|---|---|
| **Legacy runner** | `autopilot.py` (root, ~770 lines) | Standalone SDK runner; cycles `example_workflows` (`index-repo → feature-dev → bug-fix → qa → doc-gen`); CLI `input()` prompts. **Orphaned** — referenced by nothing. |
| **Active orchestrator** | `src/autopilot/orchestrator.py` (~2300 lines) | The real engine. Watches a design queue, runs the 10-phase `autopilot` workflow per design, iterates to spec, generates HTML reports, self-recovers. Run as a **subprocess**. |
| **Phase definitions** | `src/autopilot/phase_1..10_*.py`, `phases.py` | Prompts + `AUTOPILOT_PHASES`, `AUTOPILOT_WORKFLOW_CONFIG`, `AUTOPILOT_ORCHESTRATOR_CONFIG` (evaluation points), `AUTOPILOT_LAUNCH_TEMPLATE`. |
| **Engine orchestrator** | `src/workflow_engine/orchestrator.py` | `WorkflowOrchestrator.evaluate()` — score-based `goto/retry/continue` flow control driven by `AUTOPILOT_ORCHESTRATOR_CONFIG`. Invoked from `phase_manager` / MCP server. |
| **HTTP API** | `src/mcp/autopilot_api.py` (~2560 lines) | FastAPI router: queue, projects/designs, features, messages, logs, human-input, start/stop, repair, health. Spawns + signals the orchestrator subprocess. |
| **CLI** | `src/cli/commands/autopilot.py` | `heph autopilot start/stop/status/queue/add`. Also spawns the orchestrator subprocess (a **third** spawn path). |
| **Registry** | `src/workflow_registry.py` | Registers the `autopilot` workflow definition (incl. `orchestrator_config`) into the DB on startup. |
| **Frontend** | `frontend/src/pages/Autopilot.tsx` + `components/autopilot/*` (~3200 lines) | Queue panel, pipeline status card, message center, human-input banner, feature gallery, project selector, design/feature modals. |

### 2.2 Runtime topology

```
                ┌─────────────────────────────┐
   Browser ───► │ FastAPI (MCP server :8300)  │
                │  - autopilot_api router     │
                │  - workflow engine + DB     │
                │  - WorkflowOrchestrator     │ ◄── evaluation_points (goto/retry)
                └──────────┬──────────────────┘
                           │ subprocess.Popen + SIGTERM
                           │ PID file, input_request_*.json
                           ▼
            ┌──────────────────────────────────────┐
            │ src/autopilot/orchestrator.py (proc)  │
            │  - watch design queue (files)         │
            │  - run_single_design (iterations)     │
            │  - run_single_workflow:               │
            │      • polls /api/tasks, /api/agents  │
            │      • AUTO-LAUNCHES agents itself     │ ◄── second control loop
            │      • impasse/credit/stuck detection │
            │  - generates HTML report              │
            │  - starts its OWN HephaestusSDK        │
            └──────────────┬───────────────────────┘
                           │ HTTP back to :8300 (api_post/api_get)
                           ▼
                    Agents (opencode/claude) in git worktrees
```

The orchestrator subprocess **also constructs its own `HephaestusSDK`** and
calls `sdk.start()`, while simultaneously talking back to the already-running
server over HTTP. So "the backend" and "the orchestrator" each believe they own
the SDK/services.

---

## 3. Overarching Problems

These are the structural issues. Section 5 gives the concrete fixes.

### P1 — Two control loops governing the same pipeline

The pipeline is steered by **two independent authorities that don't know about
each other**:

1. **Engine-side** `WorkflowOrchestrator.evaluate()` consumes
   `AUTOPILOT_ORCHESTRATOR_CONFIG.evaluation_points` and decides
   `goto/retry/continue` per phase based on a `score` (e.g. "QA < 0.7 → goto
   development"). This is the declarative, intended flow-control model.

2. **Subprocess-side** `run_single_design()` runs the *entire* 10-phase workflow
   start-to-finish, then **re-runs the whole thing** up to `max_iterations`,
   gated by its own `generate_product_validation_report()` heuristic — *not* by
   the engine's evaluation result.

Consequences:
- A QA failure can trigger an engine `goto development` *and* count toward a full
  outer-loop iteration. The same failure is "handled" twice with different logic.
- `max_phase_retries`/`max_total_gotos` (engine) and `max_iterations`
  (subprocess) are unrelated budgets. There is no single notion of "how hard
  have we tried."
- `product_validated` from Phase 8 (an agent-written `product_validation.md`)
  and the engine's score-based product-validation evaluation point can disagree.

**This is the single most important thing to fix.** Pick one authority.

### P2 — The orchestrator re-implements the scheduler

`run_single_workflow()` contains ~200 lines (orchestrator.py:1409-1525) that
poll `/api/tasks`, compute `depends_on` / `parallel_group` readiness, enforce
`max_concurrent_agents`, and call `/api/create_agent_for_task`. This is a task
scheduler. The workflow engine already owns task lifecycle and agent creation.

So agent scheduling logic lives in *two* places (engine + orchestrator
subprocess) that can both decide to launch agents, with no shared concurrency
accounting. The "nudge / auto-kill stuck agent" logic (orchestrator.py:1544-1590)
is monitoring that duplicates the Guardian/Conductor monitoring subsystem.

### P3 — Three spawn/lifecycle paths, two PID conventions

The orchestrator process can be started/stopped by:
- `autopilot_api.start_pipeline` (Popen, writes `orchestrator.pid` + creates a DB
  `orchestrator` agent row),
- `cli/commands/autopilot.start_pipeline` (Popen, `save_pid("orchestrator")` —
  a *different* PID file), and
- `python -m src.autopilot.orchestrator` directly.

Stop logic is split across `autopilot_api.stop_pipeline`,
`cli.stop_pipeline`, and the orchestrator's own signal handling, each
re-implementing "terminate agents, pause workflows, kill pid, clear state" with
subtly different queries. `pipeline_status` in the CLI greps for
`orchestrator.py` while the API checks `_is_orchestrator_running()` via a
different mechanism — they can disagree about whether it's running.

### P4 — Dual, drifting data models for the queue

Two parallel representations of "designs to process":
- **File queue:** `docs/design-queue/*.md|txt`, ordered by mtime, dedup by
  SHA-256 content hash, plus a `queue_order` sidecar file (`_load_queue_order`).
- **DB model:** `autopilot_projects` / `autopilot_designs` tables, with
  `_sync_project_designs()` reconciling files ↔ rows by filename ordinal.

The API exposes *both* (`/queue/*` and `/projects/{id}/designs/*`). They have
separate add/remove/reorder/content/status endpoints that must be kept in sync
by hand. The frontend talks to both. "What is queued and in what order" has no
single answer.

### P5 — File-based IPC and polling everywhere

Cross-process communication is done with files in `~/.hephaestus/autopilot/`:
- Human input: orchestrator writes `input_request_<id>.json`, polls for
  `input_response_<id>.json`; API serves/writes these; UI polls `/input`.
- Repair: `repair_<id>.json` result files polled by `/queue/repair/{id}`.
- State: `pipeline_state.json` + per-run `state.json` + `processed.json` +
  `events.jsonl`.
- Liveness: `orchestrator.pid`, `orchestrator_agent_id`.

Everything is **polled** (queue every 60s, workflow status every `POLL_INTERVAL`,
UI polls status/messages/input on timers). There is no event stream. This is the
source of most of the perceived "lag" and race conditions (stale reads,
TOCTOU on `_find_pending_input`, response files orphaned on staleness).

### P6 — Massive code duplication

- `get_tasks`, `get_agents`, `check_api_credits`, `detect_impasse`,
  `prompt_human` exist in **both** `autopilot.py` and
  `src/autopilot/orchestrator.py` with diverging behavior.
- The 250-line HTML report generator (`generate_html_feature_report`,
  orchestrator.py:979-1230) is inline string concatenation with a local `esc()`
  — should be a template, and overlaps `report_generator.py`.
- Credit-exhaustion detection is keyword-matching agent output for `"credit"`,
  `"402"`, `"exceeded"`, etc. — fragile, and present in multiple files.

### P7 — God-objects / oversized modules

`orchestrator.py` (~2300 lines) and `autopilot_api.py` (~2560 lines) each hold a
dozen unrelated responsibilities (state, IPC, HTML, recovery, scheduling, queue,
projects, features, messages, health). `Autopilot.tsx` + components are ~3200
lines. These are change-amplifiers: every feature touches a 2000-line file.

### P8 — Heuristic correctness gaps

- **Workflow completion is inferred**, not authoritative: "no active agents AND
  no pending/in-progress tasks → completed" (orchestrator.py:1599). A transient
  empty poll between agent handoffs can be read as "done." There's a 60s "empty
  workflow" escape hatch that can mark a workflow `completed` with zero tasks.
- **Product validation** parses agent-written markdown for PASS/NEEDS_WORK; the
  agent is the judge of its own work with no independent gate.
- **Impasse → human prompt** in the subprocess calls `input()` in some paths
  (legacy) and writes request files in others; under the API spawn (stdout to
  `DEVNULL`) a stray `input()` would hang forever.

---

## 4. Target Architecture

### 4.1 Principles

1. **One orchestrator, in-process.** Autopilot is a long-lived service *inside*
   the backend, not a subprocess that re-creates the SDK and phones home over
   HTTP. It calls the engine through a Python service interface, not `requests`.
2. **One control loop.** The engine's `WorkflowOrchestrator` evaluation model is
   the single authority for phase flow (`goto/retry/continue`). The outer
   "iterate to spec" loop is expressed as evaluation points, not a second loop.
3. **One queue, DB-backed.** `autopilot_designs` is the source of truth; the
   file directory is an *import source*, not a parallel store.
4. **Events, not files+polling.** State changes flow over an event bus →
   WebSocket/SSE to the UI; persistence is the DB. No `.json` mailbox files.
5. **Small modules with single responsibilities.**

### 4.2 Proposed module layout

```
src/autopilot/
  service.py          # AutopilotService: lifecycle (start/stop/pause/resume),
                      #   owns the run loop as an asyncio task in the backend.
  queue.py            # DesignQueue: DB-backed CRUD + ordering; file import.
  pipeline.py         # PipelineRunner: drives ONE design through the engine,
                      #   delegating ALL flow control to WorkflowOrchestrator.
  policy.py           # Stop/impasse/credit/spec policy (pure functions, tested).
  state.py            # PipelineState persistence (DB rows, not JSON files).
  reporting/
    feature_report.py # Report model + Jinja template (no inline HTML).
  phases/             # phase_1..10 prompts (moved from src/autopilot/*.py)
  evaluators.py       # score functions feeding evaluation_points.

src/mcp/autopilot_api.py   # THIN: validates, calls AutopilotService, returns.
                           #   Split into queue_routes / feature_routes / control_routes.
```

`autopilot.py` (root): **delete** after confirming nothing depends on it (grep
shows nothing does). Its `--cycle` example-workflow behavior, if still wanted,
becomes a workflow definition, not a bespoke runner.

### 4.3 Control flow (target)

```
AutopilotService.run_loop (async, in backend):
  while running:
     design = queue.next_ready()          # DB query, ordered
     if not design: await event/timeout; continue
     runner = PipelineRunner(design)
     result = await runner.run()          # ← single authority below
     queue.mark(design, result.status)
     emit(event)

PipelineRunner.run():
  exec = engine.start_workflow("autopilot", launch_params)
  # NO manual agent launching, NO manual goto:
  # the engine + WorkflowOrchestrator.evaluate() already:
  #   - schedule tasks/agents by depends_on/parallel_group
  #   - on each phase completion, score it and goto/retry/continue
  #   - enforce max_phase_retries / max_total_gotos
  await engine.wait_until_terminal(exec, on_event=...)   # event-driven, not poll
  return PipelineResult(status, reports)
```

"Iterate until up to spec" becomes the existing evaluation point after
`product_validation` (already in `phases.py`): score < threshold → `goto
product_requirements/development`, bounded by `max_total_gotos`. The outer
`for iteration in range(max_iterations)` loop is **removed** — it is subsumed.

### 4.4 IPC & UI (target)

- Replace `input_request_*.json` mailbox with a DB table `autopilot_interventions`
  (`id, design_id, reason, options, status, choice, created_at, resolved_at`)
  and an event on create/resolve. The HumanInputBanner subscribes to the event
  stream; submit is a normal `POST` that updates the row and unblocks the runner
  via an `asyncio.Event`/condition — no file polling.
- Replace status/queue/message polling with a single WebSocket channel
  (`/api/autopilot/stream`) emitting typed events
  (`design.status`, `phase.transition`, `intervention.requested`,
  `agent.output`, `message`). Keep REST for reads/actions.

---

## 5. Detailed Changes (prioritized)

### Tier 0 — Decide the control authority (blocking everything else)

- **C0.1** Make `WorkflowOrchestrator.evaluate()` the only place that decides
  `goto/retry/continue`. Remove the `for iteration in range(max_iterations)`
  re-run loop in `run_single_design`. Re-express "up to spec" as the
  `product_validation` evaluation point with a real evaluator (see C3).
- **C0.2** Define one retry budget. Map `max_iterations` → `max_total_gotos`
  semantics, or delete `max_iterations` from the public surface and document
  `max_total_gotos` as the knob.

### Tier 1 — Collapse duplication & lifecycle

- **C1.1** Delete root `autopilot.py`. Move any still-wanted "cycle example
  workflows" behavior into a registered workflow definition.
- **C1.2** Extract the shared API client helpers (`get_tasks`, `get_agents`,
  `check_api_credits`, `peek_agent_output`, …) into one
  `src/autopilot/engine_client.py` (or, once in-process, replace with direct
  service calls). No copy in two files.
- **C1.3** One lifecycle owner: `AutopilotService` with `start/stop/status`.
  The CLI and API both call it; neither re-implements terminate/pause/kill.
  One PID/liveness convention (or none, once in-process — liveness is "is the
  asyncio task alive").
- **C1.4** Move agent scheduling entirely into the engine. Delete the
  auto-launch block (orchestrator.py:1409-1525) and the nudge/auto-kill block
  (1544-1590); route stuck-agent handling through Guardian/Conductor.

### Tier 2 — Unify the queue

- **C2.1** Make `autopilot_designs` the single source of truth. The file
  directory becomes an *importer*: a watcher (or `POST /designs/import`) reads
  files and upserts rows; ordering/dedup live in the DB.
- **C2.2** Collapse `/queue/*` and `/projects/{id}/designs/*` into one resource
  (`/projects/{id}/designs`). Remove the `queue_order` sidecar file; ordering is
  a column. Delete `_sync_project_designs` reconciliation once files are
  import-only.
- **C2.3** Frontend talks to one design API. Retire the `/queue/*` calls in
  `api.ts`.

### Tier 3 — Events over files/polling

- **C3.1** Replace human-input file mailbox with `autopilot_interventions` table
  + event; runner blocks on an in-process `asyncio.Condition`, not file polling.
- **C3.2** Add `/api/autopilot/stream` (WS/SSE). Move UI off interval polling for
  status/messages/input. Keep polling only as a degraded fallback.
- **C3.3** Persist `PipelineState`, messages, and events to DB tables, not
  `pipeline_state.json` / `events.jsonl` / per-run `state.json`. (Keep a
  human-readable log file for debugging only.)

### Tier 4 — Correctness

- **C4.1** Make workflow completion authoritative: the engine emits a terminal
  `completed/failed` state when the phase graph is exhausted. Stop inferring
  completion from "no agents + no pending tasks," and remove the 60s
  "empty workflow → completed" escape hatch (it can ship empty work).
- **C4.2** Replace keyword-matching credit detection with a typed error from the
  LLM client layer (HTTP 402/429 → `CreditExhaustedError`) surfaced on the task,
  rather than grepping agent stdout for `"402"`/`"exceeded"`.
- **C4.3** Independent spec gate: `product_validation` score should come from a
  structured evaluator (e.g. Conductor reviewing the report against the PRD and
  `qa_spec.json`), not from grepping the agent's own PASS/NEEDS_WORK string. Make
  the agent produce structured JSON (`{verdict, unmet_requirements[], score}`)
  rather than prose to be regexed.
- **C4.4** Guarantee no blocking `input()` runs under the API spawn. All
  intervention goes through the intervention table; `prompt_human`'s stdin path
  is removed (or guarded behind an explicit interactive-TTY flag).
- **C4.5** Fix `stop_pipeline` over-reach: it currently terminates **all** active
  agents (orchestrator.py / autopilot_api.py:2307-2318), not just autopilot's.
  Scope termination to the autopilot workflow(s).

### Tier 5 — Module decomposition

- **C5.1** Split `autopilot_api.py` into `queue_routes.py`, `project_routes.py`,
  `feature_routes.py`, `message_routes.py`, `control_routes.py`,
  `intervention_routes.py` under one router package. Thin handlers; logic in
  services.
- **C5.2** Replace inline HTML generation with a Jinja template +
  `FeatureReport` model; converge with `report_generator.py`.
- **C5.3** Split `Autopilot.tsx` by feature area; share a single typed event
  hook (`useAutopilotStream`).

---

## 6. Concrete bugs to fix along the way

| # | Location | Issue |
|---|---|---|
| B1 | `autopilot_api.stop_pipeline` (2307-2318) | Terminates **all** active agents, not just autopilot's — collateral damage to other workflows. |
| B2 | `orchestrator.run_single_workflow` (1599-1611) | Infers "completed" from an empty poll; 60s path marks empty workflows complete. |
| B3 | `check_api_credits` (both files) | `"credit"`, `"exceeded"`, `"402"` substring match false-positives on legitimate agent output (e.g. "credited", "exceeded expectations"). |
| B4 | `_find_pending_input` (2108) | TOCTOU: file can be unlinked between glob and read; stale-cleanup deletes response files mid-flight. |
| B5 | CLI vs API liveness | `pipeline_status` greps `orchestrator.py`; API uses `_is_orchestrator_running()` — can disagree. Two different PID files (`save_pid` vs `orchestrator.pid`). |
| B6 | `run_continuous_pipeline` (2028) | Subprocess constructs a second `HephaestusSDK` and `sdk.start()` while the backend already runs services — duplicate ownership of MCP port/DB. |
| B7 | `submit_human_input` choices `c/s/q/m` vs legacy `c/p/s/q` | Option vocabularies diverge between API and the legacy `prompt_human`. |
| B8 | Iteration vs goto double-counting (P1) | A single QA failure consumes both a `goto` budget and an outer iteration. |

---

## 7. Migration Plan (incremental, low-risk)

**Agreed sequencing (2026-06-19):** do **Slice 0 (isolation / worktrees) first**,
then resume the **hybrid spec gate** (§9.1), then the remaining slices. Rationale:
the spec gate's "discard failed work" semantics are clean only once failures are
isolated worktrees rather than half-merged branches.

**Slice 0 — Restore per-task worktree isolation (§9.2).** Steps:
0.1 Restore upstream `WorktreeManager` into `src/core/worktree_manager.py`
    (currently a shim); base path → `<project>/.worktrees/`; per-task worktrees.
    DB models already present — no migration.
0.2 Add `worktree_base_path` / `worktree_branch_prefix` to `simple_config.py`.
0.3 Add `.git/info/exclude` management (`.worktrees/`, `.hephaestus/`) and
    populate `<worktree>/.hephaestus/` inbound context on worktree creation.
0.4 Audit every out-of-tree path agents are told to read; move that content into
    `<worktree>/.hephaestus/`; update phase prompts accordingly.
0.5 Swap the 5 `BranchManager` call sites → `WorktreeManager`, reconciling
    signatures; point agent launch CWD at the returned worktree path.
0.6 Merge-on-success / discard-on-failure; restore worktree tests; **delete the
    Repair flow.**

The redesign is large; ship it as reversible slices, each independently valuable:

1. **Slice A — Stop the bleeding (no behavior change risk).**
   Delete root `autopilot.py` (C1.1); dedupe helpers into one module (C1.2);
   scope `stop_pipeline` to autopilot agents (B1/C4.5). *Pure cleanup.*

2. **Slice B — One lifecycle.** Introduce `AutopilotService`; route CLI + API
   through it; unify PID/liveness (C1.3, B5). Subprocess can remain for now.

3. **Slice C — One control loop.** Move agent scheduling + stuck handling into
   the engine; delete the auto-launch/nudge blocks (C1.4, P2). Remove the outer
   iteration loop; drive "to spec" via the evaluation point (C0.1/C0.2).
   *This is the highest-value, highest-care slice — gate behind tests.*

4. **Slice D — Authoritative completion + structured gates** (C4.1, C4.3, B2).

5. **Slice E — In-process service + events.** Replace subprocess with an asyncio
   task; replace file IPC with DB + event stream (B6, C3.x). Retire `requests`
   round-trips to self.

6. **Slice F — Unify queue** (C2.x) and **split modules** (C5.x).

Each slice should land with: a regression test for the control path it touches,
and a short note in `docs/autopilot.md` reflecting the new reality (the doc
currently describes file-queue behavior that C2 will change).

---

## 8. What to keep

Not everything needs changing. Preserve:

- The **10-phase domain pipeline** and its "agents fix, not just report"
  philosophy — that's the product.
- The **forensics / self-improvement** phase reading `pipeline_metrics.json` and
  `phase_prompts/` — genuinely novel; keep, just feed it from DB-backed metrics.
- **Per-feature cost tracking** via LiteLLM `user` field — good design.
- The **evaluation-point flow-control model** in `AUTOPILOT_ORCHESTRATOR_CONFIG`
  — it's the *right* abstraction; the fix is to make it the *only* one.
- The **feature-folder report bundle** as a human-review artifact — keep, just
  template the HTML.

---

## 8.1 Implementation Notes & Known Limitations

### No backend lifecycle integration (MVP acceptable)

`AutopilotService` is a module-level singleton accessed via `get_autopilot_service()`. It is **not** registered with the backend's startup/shutdown hooks. If the backend process restarts:
- The in-memory pipeline state (current design, counters) is lost.
- Persistent state is still written to `pipeline_state.json` / `events.jsonl`, so the next `start()` can resume from the last checkpoint.
- The service does not automatically restart a pipeline that was running before the restart.

**Future improvement:** Register the service with the backend's lifespan events (`@app.on_event("startup")` / `@app.on_event("shutdown")`) and persist the service's `running` flag + project path to DB so it can auto-resume.

### Pipeline runs in thread executor

`run_continuous_pipeline` is synchronous (blocking). The service runs it via `asyncio.get_event_loop().run_in_executor(None, ...)` which delegates to the default thread pool. This isolates the blocking loop from the async event loop, but:
- Thread pool exhaustion is possible if many blocking operations compete (unlikely for a single pipeline).
- Subprocesses spawned by the pipeline (agent CLI tools) inherit the thread's context.

**Future improvement:** Rewrite the pipeline loop as native async, eliminating the thread executor.

---

## 9. Resolved Decisions

These were open questions; now settled (2026-06-19):

1. **Multi-design concurrency — NO (for now).** One design at a time, sequentially;
   each design **builds on the previous one**. The queue is an ordered chain, not a
   pool. *Implication:* a design that ships with a silent regression poisons
   everything downstream — this raises the bar for the completion gate (see #4)
   and makes deterministic floors more important, not less. *(When concurrency is
   eventually enabled, the git substrate is designed in §9.6 — per-design
   integration worktrees; the sequential default is the N=1 special case.)*
2. **Single project at a time.** One active project. *Implication:* the
   `autopilot_projects` multi-project machinery can be simplified to a single
   active-project scope; the spec and queue are per-project.
3. **Engine is the driver.** Autopilot co-runs with / is driven by the engine —
   not a standalone subprocess phoning home. This confirms **Slice E** (in-process
   `AutopilotService`, direct service calls, no `requests`-to-self) and makes
   **C0.1 (single control loop = engine evaluation)** the spine of the design.

### 9.1 Completion / spec gate (resolved: hybrid)

**Finding:** `qa_spec.json` is **only** consumed by the orphaned root
`autopilot.py`. The active pipeline (`src/autopilot/orchestrator.py`, phases 7/8)
never reads it. So today there is **no machine-checkable gate** — "up to spec" is
`generate_product_validation_report()` grepping the Phase 8 agent's own prose
`product_validation.md`. The agent is worker and judge with no objective floor.

**Decision — hybrid gate:**
- A **first-class, DB-backed spec** (per-project) provides **hard floors**: tests
  must pass, no critical security/correctness issues, required pass rate.
- An **LLM/Conductor judgment** covers the subjective remainder (UX, fidelity to
  design intent).
- The spec becomes the **evaluator behind the `product_validation` evaluation
  point** in `AUTOPILOT_ORCHESTRATOR_CONFIG`: it produces the `score` that drives
  `goto/retry/continue`. This *replaces* the prose-grep and unifies the gate with
  the single control loop (C0.1).
- Phases 7/8 agents emit **structured JSON**
  (`{verdict, failed_tests, critical_issues, unmet_requirements[], score}`)
  instead of markdown-to-be-regexed (supersedes C4.3).

*Rationale for the hard floor specifically:* because designs are a sequential
chain (decision #1), prose-only judgment has no mechanism to stop a regression
from compounding across the chain. The machine floor is what prevents drift.

### 9.2 Isolation model (resolved: per-task worktrees, in-tree context)

**Finding:** The current "global branch" system (`BranchManager`) isolates agents
by `checkout`-ing per-agent branches in a **single shared working directory**
(`config.main_repo_path`). A merge lock serializes merges but **not working-tree
edits**, so concurrent agents race in one tree; crashed agents leave half-baked
files that bleed into other branches — which is why the **Repair flow** exists.
The Repair flow is a symptom of missing isolation, not a fix.

**Root cause of the original worktree removal:** upstream `WorktreeManager`
placed worktrees at `/tmp/hephaestus_worktrees/wt_<agent_id>` — outside both the
project and `~/.hephaestus/`. Under opencode's CWD-subtree sandbox, agents
couldn't reach out-of-tree paths they were told to read. The fix is to remove the
*coupling* (out-of-tree reads), not the *isolation*.

**Decisions:**
1. **Restore worktrees, per-task** (one worktree per agent/task) — true isolation
   for the up-to-3 concurrent agents per phase. The upstream `WorktreeManager`
   (DB models `AgentWorktree`/`WorktreeCommit`/`MergeConflictResolution` already
   present) returns each agent's `working_directory` = its worktree.
2. **Standard placement, in-repo, git-excluded:** worktree base
   `<project>/.worktrees/wt_<agent_id>/`; ignored via `.git/info/exclude` (shared
   across linked worktrees) so the user's tracked `.gitignore` is untouched.
3. **In-tree inbound context — no out-of-tree reads.** Each worktree gets a
   git-excluded `<worktree>/.hephaestus/` dir the orchestrator populates with the
   curated slice the agent needs: design doc, `qa_spec.json`, task/context
   framing, and any prior-phase artifacts not yet committed to `main`. Agents
   read context from `.hephaestus/` + their checkout; they never reach outside
   the worktree.
4. **Namespace cohesion (option B):** the `.hephaestus/` token is used in both
   `$HOME` (global orchestrator state — unchanged) and per-worktree (inbound
   context). They are different *scopes* sharing a *brand*, not the same store.
   `~/.local/` was rejected (collides with the XDG user dir).
5. **Cross-phase handoff** *(integration target refined by §9.6 — read that for
   the concurrent/atomic model)*: merge-on-success → the next phase's worktree
   branches from the **design's integration branch** (not `main`; see §9.6) and
   sees committed prior outputs; `.hephaestus/` carries the pre-commit/never-commit
   context. **Failure discards the worktree** (`git worktree remove --force` + drop
   branch); the integration branch / `main` never sees half-baked files. **This
   removes the need for the Repair flow** (C4.x / delete).

```
~/.hephaestus/                      # global orchestrator state (unchanged)
<project>/
  .worktrees/wt_<agent_id>/         # worktree base (git-excluded)
    .hephaestus/                    # per-worktree inbound context (git-excluded)
    src/ tests/ features/           # real tree — committed & merged on success
```

### 9.3 Design intake (resolved: DB-authoritative, no forced directory)

**Finding:** intake is currently a *forced, hardcoded, in-repo* directory —
`<project>/docs/design-queue/*.md|txt` (auto-created by the CLI/API, watched by
mtime + a `queue_order` sidecar) — running in parallel with the
`autopilot_designs` DB table. This is the dual-store drift of **P4**, and it's
conceptually backwards: designs are *inputs/specs*, not project artifacts, yet
they're forced into the code repo (committed or git-excluded) and tangled with the
code they describe. It also bifurcates the API (`/queue/*` vs
`/projects/{id}/designs/*`).

**Decision — separate the intake *method* from the *store*:**
- **The DB (`autopilot_designs`) is the single source of truth** for the queue,
  including the design content as a row. The worktree's `.hephaestus/design.md` is
  populated from the DB (the backend already does this from launch params), so a
  design never needs to live in the project repo.
- **File-drop is kept as one *optional, configurable* import method** — not the
  canonical store. It is demoted from "required magic directory" to an importer
  that upserts into the DB; its location is config, **not** a hardcoded
  `<project>/docs/design-queue`, and it is not auto-required.
- **Multiple intake methods all converge on the DB:** API/UI (paste or upload),
  CLI (`heph autopilot add <file>` reads from anywhere), and the optional watched
  folder. Ordering is a DB column; the `queue_order` sidecar is removed.

*Migration caution:* do **not** swing to forcing the API/UI instead — that just
trades one forced path for another and breaks headless/scripted users. Keep
file-drop available, just pointed at the DB. (Aligns with §10.2: if designs become
GitHub issues/milestones, the queue is issues and the directory is irrelevant.)
This is the completion of **Tier 3** (P4), with the "forced in-repo directory"
framing explicitly killed.

### 9.4 Human-in-the-loop is impasse-only (autonomy by default)

**Decision:** the pipeline runs **autonomously by default** — *never* force a human
step in the normal flow. The human is an **escalation on genuine impasse**, not a
routine arbiter between agents or a gate every design must pass.

- **All review layers run without a human:** self-review (§11.3), the dedicated
  review phases, the spec-gate floor (§9.1), the one architecture-gate design
  review (§10.1). They emit scores/findings; the **engine** decides
  goto/retry/continue. A gate sending work back, a review finding issues that get
  fixed, normal goto/retry within budget — **none of these involve a human.**
- **Impasse = the bounded autonomy is genuinely exhausted**, specifically:
  `max_total_gotos` hit without passing the gate; a hard error (crashed/
  unrecoverable agents); an external blocker (API credits, auth); or no progress
  after the recovery attempts. *Only then* is the human asked.
- **Corollary:** human review/comments (§10.2, GitHub) are an *optional* surface a
  human may use when they choose — not a step the pipeline blocks on. "Blocking
  review that asks the human a question" is **opt-in, high-assurance mode only**,
  never the default. The §10.3 "human can drop into the thread" is opportunistic,
  not required.

This keeps the autonomy that is the product's whole point: the human is the
exception handler for when the engine can't converge, not part of the happy path.

### 9.5 Mechanical code checks = linter hard-floor, not an agent

**Decision:** mechanically-checkable code rules (print-vs-logging, imports-at-top,
import sorting, naming, formatting) are enforced by a **linter as a hard floor**,
**not** by an LLM review agent. An LLM for these is the wrong tool — slower,
token-costly, non-deterministic (misses some, invents others) — when a linter does
it exhaustively in milliseconds. Same principle as the §9.1 spec floors and the
§10.1 general-reviewer rejection: *machine-checkable → machine floor.*

- **Now (Python):** the dev phase (and the fix phases) run `ruff check --fix .` +
  `ruff format .` and must reach **zero errors** before `done` (a done-definition
  hard floor, same class as "tests pass"). `phase_4` already runs `ruff check`.
  Hephaestus's own `pyproject.toml` enables `E,F,I,N,T20` (with `T20` ignored for
  `src/cli/*` and `tests/*`, which print intentionally). The AGENTS.md "Logging
  Best Practices" / naming conventions are the source rules; the linter enforces
  the mechanical subset.
- **Generalization (language-agnostic, intended direction):** the autopilot builds
  arbitrary software, so the pipeline must **not** maintain a per-language linter
  list. The floor is generic — *"the project's own configured lint / format /
  typecheck command passes with zero errors"* (`make lint`, `npm run lint`, `ruff
  check`, `cargo clippy`, `gofmt`, …) — exactly like CI. The language-specific
  choice lives in **one place: the architecture phase (2)**, which picks the stack
  and configures + records the canonical check commands (a `Makefile`
  lint/format/typecheck target or the manifest scripts). Dev/fix phases run *that*
  command; if none exists yet, the agent (which knows the stack it built) sets up
  the language-standard tooling. Push the language knowledge to the agent + the
  architecture decision; keep the pipeline a generic "run the declared check."
- **Semantic quality** (code smells, meaningful-vs-swallowed error handling,
  misleading names) is **already** the `adversarial_review` phase (4) — that's
  where LLM *judgment* belongs. A dedicated "style agent" is redundant with
  adversarial_review for judgment and strictly worse than the linter for mechanics.

*Recurring rule:* if a check is mechanical, it's a **tool wired as a hard floor**;
if it's judgment, it's an **existing phase**. A new agent for a checkable thing is
the anti-pattern.

---

### 9.6 Integration model — per-design integration worktree (resolved)

**Supersedes §9.2 decision 5** (the "merge-on-success → next phase branches from
updated `main`" handoff). §9.2 isolates *agents within a phase*; this section
fixes the integration target *across phases and across concurrent designs*.

**Finding:** Today `target_branch = config.base_branch`, which defaults to `main`
([`worktree_manager.py:412`](../src/core/worktree_manager.py#L412),
[`simple_config.py:60`](../src/core/simple_config.py#L60)), and `merge_to_main`
**checks out the target branch in the single shared main repo working tree**
([`worktree_manager.py:434`](../src/core/worktree_manager.py#L434)) before merging.
Two consequences:
- **No pipeline-level atomicity.** Each phase lands on `main` as it completes, so
  a failure at phase 6 leaves phases 1–5 (incl. unvalidated phase-3 dev code) on
  `main`. "Discard on failure" only protects *within* a phase, and phase 9
  (`git_commit_push`) is rendered redundant — `main` was already written 8 times.
- **A single shared checkout cannot go concurrent.** A working tree is on one
  branch at a time; with multiple designs running, every integration contends
  over that one checkout. A global per-run *branch* only renames the contention.

**Decision: each design runs against its own long-lived integration worktree.**
1. **Per-design integration worktree (the "primary").** At design start, cut
   `autopilot/run-<designId>` from `main` and create a persistent worktree
   `<project>/.worktrees/run-<designId>/` checked out on it. This is the design's
   accumulation point; it is **not** ephemeral (unlike the per-task worktrees).
2. **Each task commits into that single integration worktree on completion.** When
   a phase/task finishes, its work is committed and merged **into the design's
   integration worktree** (its run branch) — never into `main`. The next phase's
   ephemeral worktree branches from the run branch and sees all accumulated
   prior-phase commits. `base_branch` for a design's phase worktrees = its run
   branch, not `main`.
3. **`main` is touched only at the end, per design.** On full success (at phase 9
   `git_commit_push`), merge or PR `autopilot/run-<designId>` → `main`. On
   failure, discard the run branch + integration worktree; **`main` never saw the
   half-baked work** → true pipeline-level atomicity.
4. **Concurrency falls out for free.** Designs A and B each merge their phases
   into their own integration worktree on their own branch — zero contention.
   The merge lock (`.git/.hephaestus_merge_lock`) now guards **only** the final
   run-branch → `main` merges, which is exactly where serialization belongs.
5. **Cross-design conflicts surface at the final merge (the genuinely hard part).**
   Isolation during the run is trivial; integration is not. When two designs
   edited the same files, the second `run-X → main` conflicts. Policy: **auto-merge
   when clean; on conflict, open a PR** (fits phase 9's `git_commit_push` and gives
   a review surface) **or escalate to impasse** (§9.4) — **never force-merge.**
6. **N = 1 is the same model.** With one design, the integration worktree *is* the
   per-run branch; there is no second code path. The single-design and
   concurrent-design cases are unified.

```
<project>/                          # real repo, on main — UNTOUCHED during all runs
  .worktrees/
    run-A/                          # integration worktree, branch autopilot/run-A (design A)
    run-B/                          # integration worktree, branch autopilot/run-B (design B)
    wt_<A-phase1-agent>/            # ephemeral phase worktree, branched from run-A → merges into run-A
    wt_<A-phase2-agent>/            # ephemeral, branched from run-A (sees phase-1 commits)
    wt_<B-phase1-agent>/            # ephemeral, branched from run-B
  # end of each design: run-X → main (serialized; auto-merge if clean, else PR / impasse)
```

**Implementation is mostly plumbing** (target is already `config.base_branch`, not
hardcoded): create the run branch + integration worktree at design start and point
the design's phase worktrees at it; route phase merges into the integration
worktree instead of the main checkout; add the final run-branch → `main` step at
phase 9 with the conflict policy above; discard run branch + worktree on failure.
This is the substrate the deferred **concurrent-designs DAG** item (§11.3) needs.

---

### 9.7 Data model — Design is the top-level entity, above Workflow (resolved)

**Finding:** This NG fork introduced **Design** (`autopilot_designs`) as a new concept
that sits above the original **Workflow** (a single execution of the 10-phase pipeline).
But the two are **not linked by an explicit FK** — a Workflow has no `design_id`; the
connection is implicit (the design doc that spawned the run). That looseness is why
lower entities can't trace up: tickets scope to `workflow_id` only, so there's no path
back to the design, and stale tickets accumulate with no design scope to clean by
(observed: ~473 tickets leaking across runs).

**Decision: keep Design isolated *above* Workflow, and make the link explicit.**

| | **Design** (`autopilot_designs`) | **Workflow** (execution) |
| --- | --- | --- |
| Is | the durable *intent* ("build a calculator") | one *attempt* to build it |
| Lifecycle | queued → processing → done/failed; persists | per-run; owns phases/tasks/tickets/agents |
| Cardinality | **1 Design : N Workflows** (every Rerun = a new Workflow) | 1 : N phases/tasks/tickets/agents |

Why above-and-isolated (do **not** merge them):
1. **Rerun vs Resume are clean:** *Rerun* = a new Workflow under the same Design; *Resume*
   = continue the existing Workflow (the UI buttons map directly onto this split).
2. **Design-level status & dedup** — the queue reasons about Designs, not executions (§9.3).
3. **Concurrent designs (§9.6):** each Design owns its integration worktree/branch;
   Workflows are the executions inside it. Design-above-Workflow is the natural parent.
4. **Traceability & cleanup:** ticket/task/agent → Workflow → Design.

**Concrete change:** add a `design_id` FK on `Workflow`, making the hierarchy real:

```
Design ──1:N──> Workflow ──1:N──> { phases, tasks, tickets, agents }
```

Then: rerun creates a new Workflow with the same `design_id`; "tickets tied to the
design" = `ticket → workflow.design_id`; clearing/reruns scope by design; and §9.6
per-design worktrees have a clean key. Backfill existing workflows' `design_id` from
the design doc/content hash. (Implementation is a deliberate schema change — tracked
separately; the FK + backfill, not the loose implicit link.)

---

## 10. Future Direction — Collaborative Review + GitHub-as-Projection

**Status:** Exploratory / v2-horizon. Two ideas that compose into a stronger
"autonomous software team" model. Recorded so the framing survives; **not** to be
started until the core pipeline is proven end-to-end (see the caution below).

### 10.1 Reviewer steering — DECISION: do **not** add a general reviewer

The instinct — *a reviewer checks each completed unit of work and decides what's
next, like a human engineer* — is sound, but **generalizing a reviewer/critic to
every phase boundary is rejected.** The system already has heavy, *specific*
review and a generic layer on top is redundant, costly, and thrash-prone:
- **Five dedicated review phases already do the review** (adversarial_review,
  doc_review, security_review, qa_validation, product_validation — they fix, not
  just report). `product_validation` (phase 8) is already the cross-cutting
  "did we build the right thing vs. the original design" check.
- **The spec gate (§9.1)** is the independent machine floor at the two gates that
  matter; **self-review at handoff (§11.3)** covers warm-context gap-catching.
- A *generic* critic with no specific rubric is the lowest-yield kind (vague
  findings, second-guessing), would roughly **double LLM calls** (Run A was
  already 55 min), and adds over-steering risk with no hard floor. The valuable
  reviews are the specific ones (security vs OWASP, QA vs tests, product vs design).

**The one targeted insertion that *does* pay off — the architecture gate.** A bad
design is currently not caught until `product_validation` (phase 8), after dev,
reviews, and QA built on it — the most expensive place to discover it. The
orchestrator config already has an evaluation point after `architecture_design`
(`score<0.4 → goto requirements`), but it's **heuristic-scored**. Upgrade *that
one point* to a real review: the **architect scores its own design against the
requirements** before development starts (this is where the "architect-as-mentor"
idea belongs — one design-fidelity check at the cheapest catch point, **not** a
persistent everywhere-reviewer).

**What to do instead of a general reviewer:** keep the spec gate as the floor; add
self-review at handoff (§11.3); **sharpen the existing review phases** (e.g. feed
`architecture.md` into `product_validation`'s rubric; have review phases emit
structured findings) — better prompts beat more layers; and add the single
architecture-gate review above. The kernel worth preserving from this idea —
*review → structured score → engine decides, budgeted* — is **already realized**
by the spec gate; it does not need generalizing.

> One-line: don't build a general reviewer. Dedicated phases + spec gate +
> self-review suffice; the only added review is one design check at the
> architecture gate. Do **not** rebuild a parent-watches-child loop.

### 10.2 GitHub issues/milestones as a projection + event + human-I/O layer

Using GitHub as the coordination substrate (designs → **milestones**, issues →
agent status / comms / feature reports, comments → webhooks that trigger
review/fix runs) solves several backlog items at once: an **events backbone**
(webhooks → P5), a **human-in-the-loop surface** (B4/B7), a durable **message /
audit store** (replaces the message center + `input_request_*.json` mailbox),
**report hosting**, and a natural extension of the phase-9 PR flow. It also lets
humans and agents collaborate in one familiar UI and builds far less custom IPC.

**The load-bearing constraint: GitHub is a *projection*, never the source of truth
or the control bus.**
- **DB stays authoritative** (pipeline state, task graph, scores — per §9). GitHub
  *mirrors* it and emits events. Otherwise this recreates the dual-store drift of
  P4 with a third store and worse latency.
- **Reframe "comment triggers a fix in the *same* worktree."** Under §9.2,
  worktrees are ephemeral and **discarded** on completion/failure — there is no
  same worktree to resurrect, nor would stale state be desirable. Correct flow:
  *comment → webhook → create a fix-task in the DB → fresh worktree branched from
  the current `main` (which has the merged work) → agent fixes → PR/comment back.*
  Preserves discard-on-failure; strictly better than reusing a worktree.
- **Design around the operational realities:**
  - **Rate limits** (~5000 req/hr + stricter content-creation secondary limits) —
    coalesce/batch status; don't narrate every step.
  - **Webhook ingress** needs a public endpoint, or you poll the events API
    (coarse polling re-enters). Friction for a local tool.
  - **Offline / no-remote** — today the system runs on a bare `git init` with no
    remote. Keep GitHub an **optional adapter** with the DB/UI path as fallback.
  - **Security (first-class concern):** comment-triggered agent execution is an
    RCE-shaped surface — anyone who can comment can trigger code-modifying runs.
    Needs an explicit authorization model (who may trigger) and is dangerous on
    public / many-collaborator repos. Not a follow-up; a gating requirement.

### 10.3 How they compose

Findings from the *existing* review phases + spec gate (and the one
architecture-gate review, §10.1) **post as the issue comment** (10.2): agents emit
human-readable findings on the design's issue/thread, a human can drop into the
same thread to steer, and the structured artifact still drives the engine. (Note:
this is *not* a new general reviewer — §10.1 rejects that — it's surfacing the
review the pipeline already does.) This reframes Tier 2/3: rather than building a
bespoke WS/SSE stream + message center + intervention table + queue UI, **adopt
GitHub for the human-facing parts and keep the DB as the engine's truth.**

### 10.4 Sequencing caution (the important part)

Both are **v2-horizon scope**, and the pipeline has **not yet been proven to run
end-to-end once** (Monitor-driven agent spawn, single-authority iteration, spec
gate — all unit-verified, not run-verified). Do **not** start either until the
smoke run passes. Building a sophisticated collaboration layer on an unproven
engine is how you get a beautiful coordination model over a pipeline that doesn't
actually execute. **Prove the engine, then layer the collaboration model.**

---

## 11. Status & Remaining Work (live backlog)

This section is the actionable backlog (consolidated from the former
`REMAINING_WORK.md`). Run tests with `.venv/bin/python -m pytest <file> -p no:libtmux`.

### 11.1 Done (all on `main`)

| Area | What landed |
|---|---|
| Worktree isolation core | `src/core/worktree_manager.py` (`WorktreeManager`, per-task worktrees, `.git/info/exclude` + `<worktree>/.hephaestus/`, merge-on-success / discard-on-failure). `branch_manager.py` deleted, alias removed. `worktree_base_path` config. |
| Agent worktree wiring | `AgentManager._gather_worktree_context` copies design doc / project context / `qa_spec.json` into each worktree's `.hephaestus/`. All 10 phase prompts + `phases.py` template + `run_single_design` description use worktree-relative paths. `.gitignore` no longer modified (uses `info/exclude`). |
| Report collection | `_report_path()` + `docs/` sweep so HTML report / forensics read merged `<project>/docs/`. |
| Repair flow | Slimmed to workflow recovery only (branch reconciliation removed). |
| Hybrid spec gate (§9.1) | `src/autopilot/spec.py` (floors + judgement → score bands); `Monitor._build_spec_phase_output` feeds the engine; phases 7/8 emit `qa_result.json` / `product_validation.json`. |
| Tier 0 — single control authority (P1) | Outer `for iteration` loop removed; engine evaluation is sole iteration authority (`max_total_gotos`). `--max-iterations` maps to `max_total_gotos` (**C0.2 done**). |
| Tier 1 — scheduling out of orchestrator (P2) | ~150 lines of duplicated scheduler/nudge removed; Monitor owns agent spawn. |
| 3b — phase-transition authority | `mark_phase_complete` returns the `EvaluationResult`; `Monitor._create_phase_task_and_agent` creates task+agent for the resolved target (continue/goto/retry). |
| 3c — goto reconvergence | `_start_next_phase` returns True for any next phase by order; `in_progress` for `pending`/`completed`. Locked by `tests/test_goto_reconvergence.py` (first engine-level integration tests). |
| Tier 2 (partial) | `AutopilotService` in-process; CLI/API call it (B5/B6 fixed). |
| Tier 3 (partial) | DB queue: `pick_next_design` reads DB first; status updated; additive `autopilot_designs` migration (`_migrate_autopilot_designs_columns`). |
| Tier 5.1 / 5.2 | Root `autopilot.py` deleted; HTML report → Jinja2 `templates/feature_report.html`. |
| Bugs | **B1** (stop_pipeline scoped), **B2** (no 60s false-complete; `hard_error` after 5 min no-tasks), **B3** (credit detection tightened), DB migration on direct invocation, `DesignEntry.status` default, MockLogger. |

**Tests:** 74 passing.

### 11.2 Smoke run — Run A ✅ (goto proven); Run B ❌ (gate not firing + abandoned-phase skip)

**Run A (2026-06-20) succeeded:** 9/10 phases, **goto reconvergence proven end-to-end**
(`adversarial_review → goto development`), real code produced (`calculator.py` + 26
tests). The 3b/3c architecture is validated in practice.

**Run B (with a fast model) — NOT green.** Pipeline *infrastructure* is solid (21 tasks,
0 failures, MCP tool naming through pi's adapter, 9-phase progression, bounded recovery
stopped at 6 attempts). But the two things Run B exists to validate both failed:

1. **⛔ Spec gate never fired — confirmed a real bypass (not the earlier "symptom of
   timeout" theory).** The seeded failing test was in place, but the **QA agent
   hallucinated**: marked `qa_validation` done with **no `qa_result.json`, no
   `failed_tests`, no `[SPEC-GATE]` log, no GOTO** — and the pipeline continued to
   product_validation. This is decisive: if the gate were firing, missing
   `qa_result.json` → `read_result` `None` → `score_qa(None)` = **0.5 (`result_missing`)**
   → `< 0.7` → **`goto development`**. That safety net is exactly what didn't happen, so
   **`_build_spec_phase_output` is not being called on qa_validation completion.** Two
   fixes, complementary:
   - **(primary) Make the gate fire on phases 7/8.** Instrument every completion path,
     find which one completes qa_validation, ensure it calls `_build_spec_phase_output`.
     With the gate live, a missing/hallucinated result self-corrects (0.5 → goto dev).
   - **Output-existence completion floor (general, not QA-special-cased).** In
     `update_task_status`, reject `done` for any output-producing phase whose **declared
     output artifact is missing** (qa→`qa_result.json`, product→`product_validation.json`,
     architecture→`architecture.md`, …); message the agent to produce it. Mechanical →
     hard floor (same class as ruff/tests). Catches the hallucination at the source.

2. **⛔ Abandoned-phase policy violation (§9.4).** `security_review` (phase 6) was
   abandoned after 6 recovery attempts and the pipeline **continued to completion** (ran
   7→10, reported "9/10 / 0 failures"). A required phase silently skipped — *security
   review*, no less. **Fix:** bounded-recovery exhaustion on a required phase → stop
   advancing, mark the workflow **impasse** (→ human, §9.4); do **not** continue
   downstream or report success. Separately diagnose *why* phase 6 stuck (6 attempts is a
   lot — agent/prompt/phase-6-specific tool call?), but the policy fix is the priority.

**Run B is green only when:** `qa_validation` completes → `[SPEC-GATE]` logs a score →
the seeded test drives `failed_tests ≥ 1` → `goto development` → reconverge → `continue`,
**and** an abandoned required phase escalates to impasse rather than skipping. Do not
start the Tier 2/3 remainder until then.

### 11.3 Remaining (prioritized, after the smoke run)

**Tier 2 — finish in-process service + events (P3/P5/P6):**
1. Human-input → `autopilot_interventions` DB table + `asyncio.Condition`; UI submits via REST (fixes **B4** TOCTOU, **B7** option-vocab; removes `input_request_*.json`).
2. `/api/autopilot/stream` (WS/SSE); move UI off interval polling.
3. Persist `PipelineState`/messages/events to DB (not `pipeline_state.json` / `events.jsonl`); register the service with backend startup/shutdown hooks (closes the module-singleton, state-lost-on-restart gap).
4. **Split `OrchestratorLogger`** (`orchestrator.py:259`) — conflates logging (138 sites), an event sink (`event()`→`events.jsonl`), and state (`save_state()`→`state.json`). Logging → stdlib `logging.getLogger("autopilot.orchestrator")` with a **per-run `FileHandler` only** (the in-process `print(...)` currently double-logs); `event()`/`save_state()` → DB/event stream (item 3). Migrate the only consumers: `autopilot_api.py` `_get_latest_run_dir`/`_read_jsonl_tail` + status/logs/messages endpoints, and CLI status. *Don't remove the files standalone.* Only instance of the pattern in `src/`.

**Tier 3 — unify the queue / design intake (P4):** see **§9.3** — DB-authoritative; kill the forced `<project>/docs/design-queue`; file-drop becomes one optional config-located importer; merge `/queue/*` + `/projects/{id}/designs/*`; retire frontend file-queue calls; drop the `queue_order` sidecar.

**Tier 4 — C4.4:** ensure `prompt_human` has no blocking `input()` under API spawn (moot once Tier 2 #1 lands).

**Tier 5.3 — decomposition:** split `autopilot_api.py` (~2560), `orchestrator.py` (~2300), `Autopilot.tsx` (~3200).

**Spec-gate follow-ups (§9.1):** real-run validation (phases 7/8 actually emit the JSON; `[SPEC-GATE]` scores sane); per-project spec in DB + UI (`spec.py:load_spec` already takes a path); optional Conductor judgement instead of agent self-grading; verify the autopilot definition keeps `orchestrator_config.type == "evaluating"`.

**Worktree follow-ups (small):** verify validators get a correct worktree/commit (`validator_agent` + `create_agent_for_task(use_existing_worktree=…, commit_sha=…)`); first-run smoke on a repo with legacy `agent-*` branches.

**Near-term enhancement — one-shot intra-agent self-review at completion.** Empirically,
a dev agent asked to "find your own gaps and fix them" right after it thinks it's done
catches a lot, because it has *peak, warm context* — cheaper and higher-yield than a cold
review agent rebuilding context. Implement as a deterministic, bounded self-review gate at
the agent's own completion moment:
- **Hook the completion signal**, not `send_message`: in the `/update_task_status` handler
  (`server.py:1794`), when a change-producing phase's agent first sets `status="done"`.
- **One-shot flag (mandatory), set *before* the message** so a crash can't re-trigger:
  reuse the existing `tasks.review_done` (or add `self_review_done`). On first `done`: set
  the flag, *don't* complete, `send_message_to_agent` a focused checklist, return
  "not done yet — do this, then mark done again." Second `done` → complete normally.
- **Checklist, not "review for gaps"**: re-read design/requirements; every requirement
  implemented; edge cases + error handling; tests exist for new code and pass; no
  TODOs/stubs/dead code; record changes in `completion_notes`.
- **Scope via phase config (`self_review: true`)** — development and the "fix" phases
  (adversarial/security/doc review); skip pure-reporting phases (requirements, qa,
  product-validation, forensics). Opt-in, not hardcoded.
- **Distinct from, and complementary to, the two review mechanisms** — it is *not* §10.1's
  reviewer evaluation point (a separate critic at phase boundaries, cold context) and *not*
  the §9.1 spec gate (the independent hard-floor at QA). Self-review is a cheap warm-context
  *pre-filter* that reduces how often the gate sends work back (fewer `goto development`
  round-trips → less of the cost/latency Run A exposed); the gate stays as the independent
  floor. Do **not** let self-review replace it. Also **not** the removed Tier-1 nudge loop
  (that was real-time output parsing; this is one deterministic message at one event).
- **Add telemetry**: log when self-review fires and diff the tree before/after the second
  `done`, to measure whether one pass is the right number.
- **Sequencing:** after the SPEC-GATE fix + Run B (a self-review pass on a pipeline that
  can't reach the gate doesn't help). Small; the phase-config toggle makes per-phase rollout safe.

**Defer (v2-horizon):** §10 (collaborative review + GitHub-as-projection); module splits; Conductor judgement; per-project spec UI.

### 11.4 Test / infra notes
- `.venv` lacked pytest; `pytest` 9.x is incompatible with the `libtmux` plugin → always `-p no:libtmux`. Consider pinning `pytest<9` or disabling the plugin in `pyproject.toml`/`conftest.py`.
- Test fixtures must patch `get_config` **in the consumer's namespace** (e.g. `src.core.worktree_manager.get_config`), not just `src.core.simple_config.get_config`, or tests silently use the real config / real repo.

---

## 12. Future Direction — Concurrent Designs (dependency DAG)

**Status:** v2-horizon. Supersedes §9 #1 (sequential one-at-a-time) *when built* —
recorded now, explicitly **not** near-term scope.

**Today:** §9 #1 chose **sequential, one design at a time** (the queue is an ordered
chain), and the orchestrator enforces it — `run_continuous_pipeline` waits on
`get_active_workflows()` before picking the next design. So concurrent designs are
blocked by decision, not accident.

**The expensive enabler is already done.** The hard part of running workflows
concurrently is *isolation*, which §9.2 built: per-task worktrees, serialized merges
(merge lock), discard-on-failure. The data model is already **per-workflow** (each
design = its own workflow/phases/tasks/feature folder), so multiple could coexist. The
blockers are **not** isolation — they're (1) the orchestrator's single-loop and (2) the
flat "chain" queue with no dependency info.

**Design when built — a design dependency DAG.** Each design declares
`depends_on: [designs]`:
- **Independent designs** (no pending shared ancestor) run **concurrently up to a cap**
  (`max_concurrent_designs`, atop the existing `max_concurrent_agents`).
- **Dependent designs** wait for predecessors to merge (the "builds on previous"
  behavior, when wanted).
- **Subsumes both models:** today's sequential chain = "each design depends on the
  previous"; full parallel = "no deps."
- **Stay current:** when an independent design merges to `main`, in-flight designs run
  `merge_main_into_branch` (already supported) to pick up the new `main` before their
  own merge — so concurrent designs don't build against stale state.

**The real risk — "independent" is a human claim that's hard to verify.** Two "independent"
designs can still edit the same file (silent newest-file-wins clobber on merge) or one
builds against a `main` about to change under it. So: **opt-in, conservative default**
(sequential unless marked parallel), and ideally have the **architecture phase detect
file-overlap** between concurrent designs and downgrade colliding ones to sequential —
turning "independent" from a trusted assertion into a checked one (same spirit as the
spec-gate hard floors: don't trust a claim you can verify).

**Sequencing:** the pipeline isn't proven for *one* design yet (Run B red — gate not
firing, abandoned-phase skip; §11.2). Building concurrent designs on an engine whose
single-design quality mechanism doesn't fire is the §10.4 mistake. **Prove one design
end-to-end, then add the DAG.**
