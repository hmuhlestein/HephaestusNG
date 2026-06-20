# Autopilot — Remaining Work (Handoff)

**Date:** 2026-06-19
**Companion doc:** [autopilot_architecture_review.md](autopilot_architecture_review.md) — full architecture analysis, problems (P1–P8), and decisions (§9).
This file is the *actionable backlog* of what's left, prioritized, with file/line refs and acceptance criteria.

---

## Status snapshot — DONE this session (all on `main`)

| Area | What landed |
|---|---|
| Worktree isolation core | `src/core/worktree_manager.py` (`WorktreeManager`, per-task worktrees, `.git/info/exclude` + `<worktree>/.hephaestus/`, merge-on-success / discard-on-failure). `branch_manager.py` deleted, alias removed. `worktree_base_path` config. 12 tests. |
| Agent worktree wiring | `AgentManager._gather_worktree_context` copies design doc / project context / `qa_spec.json` into each worktree's `.hephaestus/`. All 10 phase prompts + `phases.py` template + `run_single_design` description use worktree-relative paths (`./.hephaestus/`, CWD, `./docs/`). |
| Report collection | `_report_path()` + `docs/` sweep in `orchestrator.py` so HTML report / forensics read the merged `<project>/docs/` location. |
| Repair flow | Slimmed to workflow recovery only (removed redundant branch reconciliation). |
| Hybrid spec gate (§9.1) | `src/autopilot/spec.py` (floors + agent judgement → score bands), `Monitor._build_spec_phase_output` feeds the engine evaluation points, phase 7/8 emit structured `qa_result.json` / `product_validation.json`. 16 tests. |

**Tests:** 28 passing (`tests/test_worktree_manager.py`, `tests/test_worktree_isolation_new.py`, `tests/test_autopilot_spec.py`). Run with `.venv/bin/python -m pytest <file> -p no:libtmux`.

---

## REMAINING WORK (prioritized)

### TIER 0 — The spine: one control authority (P1) ⭐ highest value

Today **two loops** steer the pipeline and now partly overlap:
- **Engine evaluation** (`WorkflowOrchestrator.evaluate`, driven by `AUTOPILOT_ORCHESTRATOR_CONFIG` evaluation points) — now fed real scores by the hybrid gate via the Monitor.
- **Subprocess iteration loop** (`run_single_design`, `for iteration in range(max_iterations)`, orchestrator.py ~1750) — re-runs the whole 10-phase workflow and gates on its own product-validation heuristic.

**Work:**
1. Remove the outer `for iteration` re-run loop in `run_single_design`; let the engine's `product_validation` evaluation point (now spec-gated) be the sole "iterate-to-spec" mechanism (`goto development/architecture`, bounded by `max_total_gotos`).
2. Collapse the two retry budgets: map/replace `max_iterations` with `max_total_gotos`.
3. Delete the subprocess's duplicate product-validation pass (`generate_product_validation_report`) — the engine + spec gate now own that verdict.

**Acceptance:** a QA/validation failure causes exactly one engine `goto` (not also an outer iteration); no double-counting; `run_single_design` no longer re-invokes the full workflow.

### TIER 1 — Move scheduling/monitoring out of the orchestrator (P2)

`run_single_workflow` (orchestrator.py ~1409–1525) re-implements the task scheduler (depends_on / parallel_group / max_concurrent → `/api/create_agent_for_task`) and a nudge/auto-kill loop (~1544–1590).

**Work:** delete the auto-launch block (engine already owns task→agent creation) and route stuck-agent handling through Guardian/Conductor. Acceptance: the orchestrator no longer calls `create_agent_for_task`; concurrency is enforced in one place.

### TIER 2 — In-process AutopilotService + events (Slice E, P3/P5/P6)

Decision (§9 #3): engine is the driver; Autopilot co-runs in the backend.

**Work:**
1. `AutopilotService` (asyncio task in the backend) owns start/stop/pause/resume; **CLI and API both call it** — remove the 3 spawn paths and 2 PID conventions (`autopilot_api.start_pipeline`, `cli/commands/autopilot.start_pipeline`, direct `-m`). Fixes **B5** (liveness disagreement) and **B6** (subprocess builds a *second* `HephaestusSDK`).
2. Replace the human-input file mailbox (`input_request_*.json`) with an `autopilot_interventions` DB table + an `asyncio.Condition`; UI submits via REST. Fixes **B4** (TOCTOU) and **B7** (option vocab `c/s/q/m` vs `c/p/s/q`).
3. Add `/api/autopilot/stream` (WS/SSE); move UI off interval polling for status/messages/input.
4. Persist `PipelineState`/messages/events to DB instead of `pipeline_state.json` / `events.jsonl`.

### TIER 3 — Unify the queue (P4)

Two stores: file `docs/design-queue/*` and DB `autopilot_designs` (+`queue_order` sidecar). Collapse to **DB as source of truth**, files become an import source. Merge `/queue/*` and `/projects/{id}/designs/*` into one resource; retire the file-queue calls in `frontend/src/services/api.ts`.

### TIER 4 — Correctness fixes (P8 + bug list)

| ID | Where | Fix |
|---|---|---|
| **B1** | `autopilot_api.stop_pipeline` (~2307–2318) | **Still open.** It terminates **all** active agents, not just autopilot's — scope to autopilot workflows. |
| **B2** | `run_single_workflow` (~1599–1611) | Completion is *inferred* from an empty poll; 60s path marks empty workflows "completed". Make the engine emit authoritative terminal state. |
| **B3 / C4.2** | `check_api_credits` (orchestrator.py) | Substring match on `"credit"`/`"402"`/`"exceeded"` false-positives. Surface a typed `CreditExhaustedError` from the LLM client (HTTP 402/429) instead. |
| C4.4 | `prompt_human` | Ensure no blocking `input()` under API spawn (moot once Tier 2 lands). |

### TIER 5 — Cleanup / decomposition (P6/P7)

1. **Delete root `autopilot.py`** (orphaned legacy runner, ~770 lines; referenced by nothing). Its `get_tasks`/`get_agents`/`check_api_credits`/`detect_impasse`/`prompt_human` duplicate the orchestrator.
2. Template the 250-line inline HTML generator (`generate_html_feature_report`); converge with `report_generator.py`.
3. Split `autopilot_api.py` (~2560 lines) into queue/project/feature/message/control/intervention routers; split `orchestrator.py` (~2300) and `Autopilot.tsx` (~3200).

---

## Spec-gate follow-ups (finish what §9.1 started)

- **Real-run validation:** confirm phase 7/8 agents actually emit `qa_result.json` / `product_validation.json` and the Monitor logs `[SPEC-GATE]` with sane scores. The gate enforces hard floors regardless of verdict, but behavior under real agents is unverified.
- **Per-project spec in DB + UI:** `qa_spec.json` is a single file at `~/.hephaestus/autopilot/qa_spec.json`. Make it first-class per-project (DB), editable from the UI. (`spec.py:load_spec` takes a path, so this is localized.)
- **Conductor judgement:** `agent_score` is currently the agent grading itself. Optionally replace the subjective portion with a Conductor review of the report vs PRD for independence.
- Wire the spec gate's `score` only matters in **evaluating** mode — it is, but verify the autopilot workflow definition keeps `orchestrator_config.type == "evaluating"` after any registry changes.

---

## Worktree follow-ups (small)

- **`.gitignore` redundancy:** `run_single_design` adds `.hephaestus/` to the project's tracked `.gitignore` (orchestrator.py ~815), but `WorktreeManager` already excludes it via `.git/info/exclude`. Pick one (prefer `info/exclude`, keep `.gitignore` pristine).
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
2. **Tier 0** (single control loop) — the biggest correctness/clarity win; the hybrid gate is already feeding the engine, so this is the natural next step.
3. **B1** quick fix (scope `stop_pipeline`) — small, isolated, real bug.
4. **Tier 5.1** delete root `autopilot.py` — trivial, removes confusion.
5. Then Tier 2 (in-process service + events) as the larger investment.
