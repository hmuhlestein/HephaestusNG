# Autopilot — Remaining Work (Handoff)

**Date:** 2026-06-19 (updated)
**Companion doc:** [autopilot_architecture_review.md](autopilot_architecture_review.md) — full architecture analysis, problems (P1–P8), and decisions (§9).
This file is the *actionable backlog* of what's left, prioritized, with file/line refs and acceptance criteria.

---

## Status snapshot — DONE (all on `main`)

| Area | What landed |
|---|---|
| Worktree isolation core | `src/core/worktree_manager.py` (`WorktreeManager`, per-task worktrees, `.git/info/exclude` + `<worktree>/.hephaestus/`, merge-on-success / discard-on-failure). `branch_manager.py` deleted, alias removed. `worktree_base_path` config. 12 tests. |
| Agent worktree wiring | `AgentManager._gather_worktree_context` copies design doc / project context / `qa_spec.json` into each worktree's `.hephaestus/`. All 10 phase prompts + `phases.py` template + `run_single_design` description use worktree-relative paths (`./.hephaestus/`, CWD, `./docs/`). |
| Report collection | `_report_path()` + `docs/` sweep in `orchestrator.py` so HTML report / forensics read the merged `<project>/docs/` location. |
| Repair flow | Slimmed to workflow recovery only (removed redundant branch reconciliation). |
| Hybrid spec gate (§9.1) | `src/autopilot/spec.py` (floors + agent judgement → score bands), `Monitor._build_spec_phase_output` feeds the engine evaluation points, phase 7/8 emit structured `qa_result.json` / `product_validation.json`. 16 tests. |
| **Single control authority (Tier 0)** | **DONE.** Removed the `for iteration in range(max_iterations)` outer loop from `run_single_design`. The engine's evaluation points are now the SOLE authority for iteration (goto/retry/continue bounded by `max_total_gotos`). `generate_product_validation_report` simplified to read existing results only (no duplicate heuristic). `--max-iterations` now maps to engine's `max_total_gotos` via `_update_orchestrator_max_gotos()`. |
| **B1: stop_pipeline scope** | **DONE.** Removed the block in `autopilot_api.stop_pipeline` that terminated ALL active agents. Now only terminates agents tied to autopilot workflows. |
| **Tier 5.1: Delete orphaned autopilot.py** | **DONE.** Deleted root `autopilot.py` (~770 lines). No imports or references existed outside the file itself. |
| **B2: Empty workflow escape hatch** | **DONE.** Removed 60s false-completion, now returns `hard_error` after 5 minutes if no tasks exist. |
| **B3: check_api_credits false positives** | **DONE.** Tightened patterns to specific phrases, reduced false positives. |
| **Tier 1: Scheduling out of orchestrator** | **DONE.** Removed ~150 lines of duplicated scheduler/monitoring. Monitor `_create_next_phase_task` now spawns agents. |
| **Tier 2 partial: AutopilotService** | **DONE.** In-process service, CLI/API call it, stop event wired. |
| **Tier 5.2: Jinja2 HTML template** | **DONE.** Extracted ~230 lines to `templates/feature_report.html`. |
| **Tier 3 partial: DB queue** | **DONE.** `pick_next_design` reads from DB, status updated after processing. Migration added. |
| **DB migration** | **DONE.** Additive ALTER TABLE for `autopilot_designs` columns. 4 regression tests. |
| **DesignEntry.status** | **DONE.** PENDING is intentional default, test updated. |
| **MockLogger methods** | **DONE.** Added `info`, `warning`, `error` methods. |

**Tests:** 28 passing (`tests/test_worktree_manager.py`, `tests/test_worktree_isolation_new.py`, `tests/test_autopilot_spec.py`). Run with `.venv/bin/python -m pytest <file> -p no:libtmux`.

---

## ▶ NEXT-AGENT DIRECTIVE (do these in order)

*One line:* **Land the additive `autopilot_designs` migration and settle the `DesignEntry.status` test, then prove the pipeline end-to-end on a throwaway repo before building more Tier 2/3.**

### ~~1. DB migration for `autopilot_designs`~~ ✅ DONE
Additive ALTER TABLE migration (`_migrate_autopilot_designs_columns`) added to
`DatabaseManager.create_tables()`. 4 regression tests pass.

### ~~2. Settle the `DesignEntry.status` regression~~ ✅ DONE
`PENDING` is the intentional default (means "not yet processed"). Test updated
to assert `DesignEntry.status == DesignStatus.PENDING`. `crackme` test was a
case-sensitivity bug in the assertion ("execution proof" vs "Execution proof").

### 3. End-to-end smoke run — highest information value, still never done
Smoke test repo prepared at `/tmp/heph-smoke-test` (git repo with a base commit —
required, `git worktree add` fails on a repo with zero commits) and
`docs/design-queue/add_hello_world.md`.
Run with: `heph start && heph autopilot start --project-path /tmp/heph-smoke-test`

**Make it diagnostic, not pass/fail.** Tail `~/.hephaestus/autopilot/run-*/` and the
server log; watch the DB `tasks`/`agents` tables.

**Pre-flight:** backend `/health` healthy; vector store (qdrant/turbovec) reachable;
LLM key set; **the cli agent tool (opencode/pi) installed and on PATH** — agents
won't launch otherwise.

**Six checkpoints, in failure-order (where it's most likely to break first):**
1. **Phase-1 agent actually spawns** — *the Tier 1 handoff, #1 risk.* A Phase-1
   task should appear AND an agent in a worktree. Task stuck `pending` with no
   agent ⇒ `Monitor._create_next_phase_task` isn't spawning. Confirm `.worktrees/wt_*`
   exists and `tmux ls` shows a session.
2. **Worktree context populated** — `ls .worktrees/wt_*/.hephaestus/` holds
   `design.md` (+ `context.md`, `qa_spec.json`). Empty ⇒ `_gather_worktree_context`
   isn't reading `launch_params.design_document`.
3. **Phases advance 1→2→…→10 via the engine**, not re-running Phase 1 (the removed
   double-loop).
4. **`[SPEC-GATE]` log line** with a score after qa_validation / product_validation.
   Missing ⇒ `_build_spec_phase_output` not firing or `working_directory` is None.
5. **Reports land + merge** — `<project>/docs/` (on `main`) gets
   `requirements_analysis.md`, `qa_report.md`, `qa_result.json`,
   `product_validation.json`; HTML renders via Jinja2.
6. **Merge-on-success / discard-on-failure** — `git worktree list` clean at the
   end; `git log` shows the merges.

**B2 trap to watch:** the new 5-min `hard_error`-on-no-tasks. Phase-1 spinup + LLM
latency could exceed 5 min before any task registers; if the run dies early with
`hard_error`, that's the suspect — confirm a task exists within 5 min of start.

**Test-design caveat:** hello-world is right for a *first* run (prove it executes),
but it's so trivial that QA/validation pass cleanly and the gate's interesting
paths (`goto development`/`architecture`) never fire. After it's green, do a second
run that deterministically forces a **failure signal** so you can watch the gate
score `< 0.7` and send work back, then reconverge. Don't use "a requirement with
no test" — the QA phase would just write the test and close the gap. Instead seed
a **failing test (TDD-style)**: commit a test asserting the target behavior that
currently fails → QA reports `failed_tests: 1` → gate `goto development` → an agent
makes it pass → re-QA `continue`. (Alternative: an over-constrained/contradictory
requirement so product validation honestly reports `unmet_requirements`, forcing
the same path via the §9.1 hard-floor override.)

### 4. Finish Tier 2 the designed way (after smoke passes)
Per §4.4: human-input → `autopilot_interventions` DB table + `asyncio.Condition`
+ REST submit (fixes **B4/B7**, removes the `input_request_*.json` mailbox); then
`/api/autopilot/stream` (WS/SSE) + DB persistence of `PipelineState`/events. The
persistence step also closes the "module-singleton, state-lost-on-restart" gap —
register the service with backend startup/shutdown hooks there.

### 5. Finish Tier 3 (queue unification)
Merge `/queue/*` and `/projects/{id}/designs/*` into one resource, retire the
file-queue calls in `frontend/src/services/api.ts`, drop the `queue_order`
sidecar. DB is already the read source — this removes the dual model.

**Defer:** Tier 5.3 module splits, Conductor judgement, per-project spec UI (low-risk, no rush).

---

## REMAINING WORK (prioritized)

### ~~TIER 0 — The spine: one control authority (P1) ⭐ highest value~~ ✅ DONE

The engine's evaluation points are now the single control authority. See status snapshot above.

**Follow-up (C0.2) — reconcile the now-vestigial `--max-iterations` knob:** removing
the outer loop left `--max-iterations` *parsed but ignored* everywhere — CLI
(`heph autopilot start --max-iterations N`, `cli/commands/autopilot.py`), API
`/start` (`autopilot_api.py`), and the orchestrator argparse (`orchestrator.py`
~2234). Setting it now has **no effect and no warning**; iteration is governed
solely by the engine's `max_total_gotos` (hardcoded 10 in
`AUTOPILOT_ORCHESTRATOR_CONFIG`). `StopReason.MAX_ITERATIONS` is also orphaned.
Resolve by either: **(a, preferred)** make `--max-iterations` set
`max_total_gotos` so the knob keeps working, or **(b)** remove it from the
CLI/API/argparse and document `max_total_gotos` as the control. Small, isolated.

### ~~TIER 1 — Move scheduling/monitoring out of the orchestrator (P2)~~ ✅ DONE

Removed the auto-launch block (~100 lines) and nudge/auto-kill block (~50 lines) from `run_single_workflow`. The orchestrator now only monitors and logs.

**Critical fix:** The Monitor's `_create_next_phase_task` previously only created tasks (DB insert) without creating agents — tasks would sit in `pending` forever. Fixed to also call `agent_manager.create_agent_for_task()` so agents are spawned for each phase. This was the hidden dependency that made the auto-launch block appear necessary.

**Agent creation flow (verified):**
1. `sdk.start_workflow` → server creates Phase 1 task + agent via `create_task` endpoint
2. Phase 1 agent completes → `update_task_status` marks done
3. Monitor detects phase complete → `_create_next_phase_task` creates Phase 2 task + agent
4. Repeat for phases 3–10
5. Orchestrator polling loop detects workflow completion

### TIER 2 — In-process AutopilotService + events (Slice E, P3/P5/P6) ✅ PARTIAL

**Done:**
- `AutopilotService` created (`src/autopilot/service.py`) — asyncio task in backend
- API `start_pipeline` uses service (no more subprocess.Popen)
- API `stop_pipeline` uses service.stop()
- API `get_pipeline_status` merges service status with file state
- CLI start/stop/status call API (no more direct subprocess spawning)
- Fixes **B5** (one liveness convention) and **B6** (no duplicate SDK)

**Known limitation (MVP acceptable):** No backend lifecycle integration — the service is a module-level singleton (`get_autopilot_service()`). If the backend restarts, the in-memory pipeline state is lost. Persistent state is still written to `pipeline_state.json` / `events.jsonl` so the next start can resume. A future iteration could register the service with the backend's startup/shutdown hooks and persist state to DB.

**Remaining:**
1. Replace the human-input file mailbox (`input_request_*.json`) with an `autopilot_interventions` DB table + an `asyncio.Condition`; UI submits via REST. Fixes **B4** (TOCTOU) and **B7** (option vocab `c/s/q/m` vs `c/p/s/q`).
2. Add `/api/autopilot/stream` (WS/SSE); move UI off interval polling for status/messages/input.
3. Persist `PipelineState`/messages/events to DB instead of `pipeline_state.json` / `events.jsonl`.

### TIER 3 — Unify the queue (P4) ✅ PARTIAL

**Done:**
- Added `status`, `content_hash`, `feature_folder`, `completed_at` columns to `AutopilotDesign` model
- `pick_next_design` now reads from DB (autopilot_designs) first, falls back to file scan
- After processing, design status is updated in DB (completed/failed/skipped)
- `_sync_project_designs` already existed to import files to DB

**Remaining:**
- Merge `/queue/*` and `/projects/{id}/designs/*` API endpoints into one resource
- Retire file-queue calls in `frontend/src/services/api.ts`
- Remove `queue_order` sidecar file dependency

### TIER 4 — Correctness fixes (P8 + bug list)

| ID | Where | Fix |
|---|---|---|
| ~~**B1**~~ | ~~`autopilot_api.stop_pipeline` (~2307–2318)~~ | **DONE.** Now only terminates agents tied to autopilot workflows. |
| ~~**B2**~~ | ~~`run_single_workflow` (~1599–1611)~~ | **DONE.** Removed 60s empty-workflow escape hatch. Now returns `hard_error` after 5 minutes if no tasks exist. |
| ~~**B3 / C4.2**~~ | ~~`check_api_credits` (orchestrator.py)~~ | **DONE.** Replaced broad substring match with specific phrases (`"quota exceeded"`, `"429 too many requests"`, etc.) and agent error/status field checks. |
| C4.4 | `prompt_human` | Ensure no blocking `input()` under API spawn (moot once Tier 2 lands). |

### TIER 5 — Cleanup / decomposition (P6/P7)

1. ~~**Delete root `autopilot.py`**~~ **DONE.** Deleted the orphaned legacy runner (~770 lines).
2. ~~Template the 250-line inline HTML generator (`generate_html_feature_report`)~~ **DONE.** Extracted to Jinja2 template (`src/autopilot/templates/feature_report.html`). `report_generator.py` (light theme, phase_9 example) kept as separate simpler report.
3. Split `autopilot_api.py` (~2560 lines) into queue/project/feature/message/control/intervention routers; split `orchestrator.py` (~2300) and `Autopilot.tsx` (~3200).

---

## Spec-gate follow-ups (finish what §9.1 started)

- **Real-run validation:** confirm phase 7/8 agents actually emit `qa_result.json` / `product_validation.json` and the Monitor logs `[SPEC-GATE]` with sane scores. The gate enforces hard floors regardless of verdict, but behavior under real agents is unverified.
- **Per-project spec in DB + UI:** `qa_spec.json` is a single file at `~/.hephaestus/autopilot/qa_spec.json`. Make it first-class per-project (DB), editable from the UI. (`spec.py:load_spec` takes a path, so this is localized.)
- **Conductor judgement:** `agent_score` is currently the agent grading itself. Optionally replace the subjective portion with a Conductor review of the report vs PRD for independence.
- Wire the spec gate's `score` only matters in **evaluating** mode — it is, but verify the autopilot workflow definition keeps `orchestrator_config.type == "evaluating"` after any registry changes.

---

## Worktree follow-ups (small)

- ~~**`.gitignore` redundancy:**~~ **DONE.** Removed `.gitignore` modification from `create_feature_folder`. `.hephaestus/` is excluded via `.git/info/exclude` (managed by `WorktreeManager`), keeping the user's `.gitignore` pristine.
- **Validator worktrees:** `validator_agent` uses `get_workspace_changes` / `get_agent_branch_path` (now worktree-aware) and `create_agent_for_task(use_existing_worktree=…, commit_sha=…)` — verify validators get a correct worktree/commit under the new model.
- **First-run state:** if an older DB has agents on `agent-*` branches checked out in the main repo, the first worktree run should be fine (main stays on base branch), but worth a smoke test on a real project.

---

## Test / infra notes

- `.venv` had **no pytest**; installed `pytest` (9.x) + `pytest-asyncio`. pytest 9 is **incompatible with the `libtmux` pytest plugin** → always pass `-p no:libtmux`. Consider pinning `pytest<9` in `requirements.txt` or disabling the libtmux plugin in `pyproject.toml`/`conftest.py` so the whole suite runs cleanly.
- Test fixtures must patch `get_config` **in the manager's namespace** (`src.core.worktree_manager.get_config`), not just `src.core.simple_config.get_config` — otherwise tests silently use the real config and operate on the real repo. (Fixed in the two worktree test files; apply the same pattern elsewhere.)
- No end-to-end autopilot run has been executed against a real project since these changes — **a full smoke run is the most valuable next validation.**

---

## Suggested order for the next session

1. **Smoke-run autopilot** on a throwaway git project → confirm worktrees, `.hephaestus/` context, `./docs/` reports, merges, and `[SPEC-GATE]` scoring all work end-to-end. (Highest information per minute.)
2. ~~**Tier 0** (single control loop)~~ **DONE.**
3. ~~**B1** quick fix (scope `stop_pipeline`)~~ **DONE.**
4. ~~**Tier 5.1** delete root `autopilot.py`~~ **DONE.**
5. ~~**Tier 1** (move scheduling out of orchestrator)~~ **DONE.**
6. **Tier 2** (in-process AutopilotService + events) — the larger investment.
7. **B2** fix (authoritative completion from engine).
8. **Tier 3** (unify the queue).
