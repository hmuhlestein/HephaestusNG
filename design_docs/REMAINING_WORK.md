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

**Tests:** 28 passing (`tests/test_worktree_manager.py`, `tests/test_worktree_isolation_new.py`, `tests/test_autopilot_spec.py`). Run with `.venv/bin/python -m pytest <file> -p no:libtmux`.

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
| ~~**B1**~~ | ~~`autopilot_api.stop_pipeline` (~2307–2318)~~ | **DONE.** Now only terminates agents tied to autopilot workflows. |
| **B2** | `run_single_workflow` (~1599–1611) | Completion is *inferred* from an empty poll; 60s path marks empty workflows "completed". Make the engine emit authoritative terminal state. |
| **B3 / C4.2** | `check_api_credits` (orchestrator.py) | Substring match on `"credit"`/`"402"`/`"exceeded"` false-positives. Surface a typed `CreditExhaustedError` from the LLM client (HTTP 402/429) instead. |
| C4.4 | `prompt_human` | Ensure no blocking `input()` under API spawn (moot once Tier 2 lands). |

### TIER 5 — Cleanup / decomposition (P6/P7)

1. ~~**Delete root `autopilot.py`**~~ **DONE.** Deleted the orphaned legacy runner (~770 lines).
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
2. ~~**Tier 0** (single control loop)~~ **DONE.**
3. ~~**B1** quick fix (scope `stop_pipeline`)~~ **DONE.**
4. ~~**Tier 5.1** delete root `autopilot.py`~~ **DONE.**
5. ~~**Tier 1** (move scheduling out of orchestrator)~~ **DONE.**
6. **Tier 2** (in-process AutopilotService + events) — the larger investment.
7. **B2** fix (authoritative completion from engine).
8. **Tier 3** (unify the queue).
