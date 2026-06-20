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

1. **Multi-design concurrency — NO.** One design at a time, sequentially; each
   design **builds on the previous one**. The queue is an ordered chain, not a
   pool. *Implication:* a design that ships with a silent regression poisons
   everything downstream — this raises the bar for the completion gate (see #4)
   and makes deterministic floors more important, not less.
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
5. **Cross-phase handoff:** merge-on-success → the next phase's worktree branches
   from updated `main` and sees committed prior outputs; `.hephaestus/` carries
   the pre-commit/never-commit context. **Failure discards the worktree**
   (`git worktree remove --force` + drop branch); `main` never sees half-baked
   files. **This removes the need for the Repair flow** (C4.x / delete).

```
~/.hephaestus/                      # global orchestrator state (unchanged)
<project>/
  .worktrees/wt_<agent_id>/         # worktree base (git-excluded)
    .hephaestus/                    # per-worktree inbound context (git-excluded)
    src/ tests/ features/           # real tree — committed & merged on success
```
