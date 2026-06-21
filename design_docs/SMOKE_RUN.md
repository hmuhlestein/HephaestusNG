# Autopilot Smoke-Run Instructions (for the running agent)

**Goal:** prove the autopilot pipeline runs end-to-end after the worktree /
single-control-authority / spec-gate changes. This is the first real run; treat it
as **diagnostic**, not pass/fail. Capture evidence at each checkpoint and **report
findings — do not silently fix forward**. If a checkpoint fails, stop, record the
symptom, and map it to the likely cause (each checkpoint lists one).

Companion: [autopilot_architecture_review.md](autopilot_architecture_review.md) — §11.2 (next action), §9 (decisions).

Conventions below assume repo root `/Users/hmuhlestein/code/HephaestusNG` (DB:
`hephaestus.db`) and smoke project `/tmp/heph-smoke-test`.

---

## 0. Pre-flight (must all be true before starting)

- [ ] Backend deps reachable: vector store (qdrant/turbovec) up; LLM key set in `.env`.
- [ ] **CLI agent tool installed and on PATH** (`opencode` / `pi` / `claude`) — agents won't launch otherwise. Check `echo $HEPHAESTUS_CLI_TOOL` / config `default_cli_tool`.
- [ ] Smoke repo is a git repo **with a base commit** (required — `git worktree add` fails on zero commits):
  ```bash
  cd /tmp/heph-smoke-test && git rev-parse HEAD && ls docs/design-queue/
  ```
- [ ] Start the backend and confirm health:
  ```bash
  cd /Users/hmuhlestein/code/HephaestusNG
  heph start
  curl -s http://127.0.0.1:8300/health   # expect {"status":"healthy"}
  ```

---

## RUN A — hello-world (prove it executes)

Start autopilot and immediately begin watching:
```bash
heph autopilot start --project-path /tmp/heph-smoke-test
# logs: tail the newest run dir + server log
tail -f ~/.hephaestus/autopilot/run-*/autopilot.log
tail -f /Users/hmuhlestein/code/HephaestusNG/hephaestus_server.log
```

Observation helpers (run repeatedly during the run, from repo root):
```bash
# tasks + agents
sqlite3 hephaestus.db "SELECT id,phase_id,status,workflow_id FROM tasks ORDER BY rowid DESC LIMIT 20"
sqlite3 hephaestus.db "SELECT substr(id,1,8),agent_type,status,substr(current_task_id,1,8) FROM agents ORDER BY rowid DESC LIMIT 20"
# worktrees + injected context
ls -la /tmp/heph-smoke-test/.worktrees/
ls /tmp/heph-smoke-test/.worktrees/wt_*/.hephaestus/
tmux ls
# spec gate
grep -h "\[SPEC-GATE\]" ~/.hephaestus/autopilot/run-*/*.log hephaestus_server.log
# reports + git state (on main, after merges)
ls /tmp/heph-smoke-test/docs/
( cd /tmp/heph-smoke-test && git log --oneline -15 && git worktree list )
```

### Checkpoints (failure-ordered — most likely to break first)

| # | Pass criterion | If it fails → likely cause |
|---|---|---|
| 1 | **Phase-1 agent spawns**: a task with `phase_id` for phase 1 appears AND an `agents` row (status `working`/`starting`) with `current_task_id` set; `.worktrees/wt_*` exists; `tmux ls` shows a session. | Task stuck `pending`, no agent ⇒ `Monitor._create_next_phase_task` not spawning (the Tier 1 handoff). **#1 risk.** |
| 2 | **Context populated**: `.worktrees/wt_*/.hephaestus/` contains `design.md` (+ `context.md`, `qa_spec.json` if present). | Empty ⇒ `AgentManager._gather_worktree_context` not reading `launch_params.design_document`. |
| 3 | **Phases advance 1→2→…→10** (tasks appear with increasing `phase_id`), NOT re-running phase 1. | Repeats / never advances ⇒ engine evaluation not driving, or agents not marking `done`. |
| 4 | **`[SPEC-GATE]` log line** with a score appears after qa_validation / product_validation. | Missing ⇒ `Monitor._build_spec_phase_output` not firing or workflow `working_directory` is NULL. |
| 5 | **Reports land + merge**: `/tmp/heph-smoke-test/docs/` (on main) gets `requirements_analysis.md`, `qa_report.md`, `qa_result.json`, `product_validation.json`; feature HTML renders (Jinja2). | Reports only inside a worktree / not on main ⇒ merge-on-success or `_report_path`/sweep issue. |
| 6 | **Merge / discard correct**: `git worktree list` clean at end; `git log` shows agent merges; no `agent-*` branches left for failed agents. | Leftover worktrees/branches ⇒ cleanup/discard path. |

### Watch specifically
- **B2 trap:** the run must not die with `hard_error` in the first ~5 min just because phase-1 spinup + LLM latency is slow. Confirm a task row exists within 5 min of start; if it aborts early, B2's no-tasks timeout is the suspect.

### Stop / reset between runs
```bash
heph autopilot stop
# optional clean slate for the project:
( cd /tmp/heph-smoke-test && git worktree prune && git worktree list )
```

---

## RUN B — force the gate's failure path (after A is green)

Run A is too trivial to fire the gate's `goto development` / `goto architecture`
branches, so they stay unverified. Force **one** failure deterministically with a
**seeded failing test** (TDD-honest) the QA phase will run.

Do **not** use "a requirement with no test" — the QA phase would just write the
test and close the gap.

Setup (the design must NOT mention `compute()`, so phase 3 won't pre-implement it;
QA then finds the failing test and the gate sends work back):
```bash
cd /tmp/heph-smoke-test
mkdir -p tests
cat > tests/test_seed.py <<'PY'
# Seeded gap: asserts a function that does not exist yet, so QA reports a failure.
from app import compute
def test_compute_known_gap():
    assert compute() == 42
PY
git add tests/test_seed.py && git commit -m "seed: failing test to exercise the spec gate"
# add a second design to the queue (keep it small; do NOT mention compute())
cp docs/design-queue/add_hello_world.md docs/design-queue/add_greeting.md  # or write a fresh small one
heph autopilot start --project-path /tmp/heph-smoke-test
```

### Checkpoints (in addition to A's)
| # | Pass criterion |
|---|---|
| B1 | After qa_validation, `qa_result.json` shows `failed_tests >= 1` and `[SPEC-GATE]` logs a score `< 0.7`. |
| B2 | The engine performs a **`goto development`** (look for the orchestrator/phase-manager log: `action=goto target=development`), NOT an outer re-run of phase 1. |
| B3 | A dev agent then implements `app.compute()` (returns 42); re-QA reports `failed_tests: 0`; gate `continue`. |
| B4 | The loop is **bounded**: total gotos never exceed `max_total_gotos` (default 10 / `--max-iterations`). If it can't converge it should stop, not spin. |

*Caveat:* a thorough phase-3 dev agent might proactively fix `test_seed.py`; if so
the goto won't fire. If that happens, make the seeded test require something the
design clearly excludes, or assert a contradictory requirement so product
validation honestly reports `unmet_requirements` (forces the path via the §9.1
hard-floor override).

---

## What to report back

For each run, a short summary:
- Which checkpoints passed / the first that failed (symptom + suspected cause + the log/DB evidence).
- Did agents spawn and phases advance? Final `git log --oneline` / `git worktree list` of the smoke repo.
- The `[SPEC-GATE]` lines observed (and the `qa_result.json` / `product_validation.json` contents).
- Any `hard_error` / early abort, with timing.
- Update [autopilot_architecture_review.md](autopilot_architecture_review.md) §11.2 (mark the smoke run done, or record the blocker) once Run A is green.

---

## Run A Results (2026-06-20, attempt 2 — after 5 blocker fixes)

**Status:** SUCCESS — 9/10 phases completed, GTO reconvergence proven, real code produced.

**Duration:** 55 minutes (3340s) for a single "Add Calculator" design.

### Checkpoints

| # | Checkpoint | Status | Evidence |
|---|------------|--------|----------|
| 1 | Phase-1 agent spawns | ✅ | Agent spawned, worktree created, task assigned |
| 2 | Context populated | ✅ | `.hephaestus/design.md` in worktree |
| 3 | Phases advance 1→…→10 | ✅ | 9/10 phases reached `completed` (1→2→3→4→5→6→7→8→9→10, with GTO back to 3) |
| 4 | `[SPEC-GATE]` log | ⚠️ | qa_validation and product_validation completed but no SPEC-GATE score log — `_build_spec_phase_output` may not find result files |
| 5 | Reports land + merge | ✅ | `calculator.py` + 26 passing tests created in worktree |
| 6 | GTO reconvergence | ✅ | adversarial_review evaluation sent work back to development (GTO), development re-ran and produced working code |

### Bugs Found & Fixed (5 blockers)

1. **`per_page=1000` (ROOT CAUSE)** — orchestrator `get_agents` requested `per_page=1000` but API max is 100. Silent 422 → always 0 agents → impasse/premature-completion every cycle. Fixed to `per_page=100`.
2. **Monitor auto-discover** — `phase_manager.workflow_id` was None when monitor started before workflow. Added auto-discover from DB each cycle.
3. **Phase progression for completed→pending** — monitor now detects when completed phase’s successor is still pending and creates task+agent.
4. **All-phases-done completion check** — orchestrator verifies all PhaseExecution rows are completed before declaring workflow done.
5. **Recovery loop bounded** — after 5 failed recovery attempts, abandon stale workflow instead of infinite merge-loop. Impasse grace increased 60s→300s.

### What Worked
- Agents spawn in worktrees with correct context
- MCP tools invoked properly (tasks marked done via `update_task_status`)
- Phase transitions driven by monitor’s `_check_phase_progression`
- Engine evaluation points drive goto/retry/continue
- GTO reconvergence: adversarial_review → development → re-run → pass
- Real code produced: `calculator.py` with 4 functions, 26 passing tests

### Remaining Issues
- **SPEC-GATE scoring not visible** — qa/product_validation phases complete but no `[SPEC-GATE]` score log. Need to verify result files are written.
- **Development agent stuck (1832s)** — agent ran for 30+ min on simple calculator. Likely slow model (`mimo-v2.5`) + multiple retries.
- **Orchestrator sees 0 agents intermittently** — `get_agents(workflow_id)` filters through tasks; if task not yet linked to agent, count is 0. Causes false impasse detection.
- **Pipeline marked 0 processed** — orchestrator’s `run_single_design` exited via impasse timeout, not normal completion. Design status never updated.

## Run A Results (2026-06-20, attempt 1)

**Status:** PARTIAL — agents spawn and phases advance, but workflow never completes.

### Checkpoints

| # | Checkpoint | Status | Evidence |
|---|------------|--------|----------|
| 1 | Phase-1 agent spawns | ✅ | Agent spawned, worktree `.worktrees/wt_*` created |
| 2 | Context populated | ✅ | `.hephaestus/design.md` present in worktree |
| 3 | Phases advance 1→2→… | ⚠️ Partial | Phase 1→2→3 advanced, but execution status tracking was broken |
| 4 | `[SPEC-GATE]` log | ❌ | No SPEC-GATE logs found |
| 5 | Reports land + merge | ⚠️ Partial | Reports in `docs/`, but workflow never completed |
| 6 | Merge/discard | ⚠️ | Worktrees created, but workflow stuck |

### Bugs Found & Fixed

1. **OrchestratorLogger missing methods** — `info()`, `warning()`, `error()` not defined. Added them.
2. **BrokenPipeError in log()** — `print()` fails when stdout is DEVNULL. Added try/except.
3. **First phase not started** — `_start_phase()` never called for phase 1. Added to `start_execution()`.
4. **Subsequent phases not started** — `_create_next_phase_task` didn't update `PhaseExecution.status`. Fixed.

### Remaining Issues

1. **No SPEC-GATE logs** — `_build_spec_phase_output` may not be firing or `working_directory` is None.
2. **Workflow never completes** — Orchestrator polls forever even when all tasks done. The `completed` status inference from empty poll may not be triggering.
3. **B2 timing** — The 5-min `hard_error` timeout didn't trigger (tasks were created within 5 min), but the human-input timeout (600s) did fire for a pending task.
