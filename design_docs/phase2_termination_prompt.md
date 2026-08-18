# Prompt: Phase 2, §4.2 — agent-termination primitive

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.2 of `docs/AUTOPILOT_REFACTOR_PLAN.md`: consolidate the agent-termination invariant into one shared primitive. This is the plan's second-highest-priority Phase 2 item (after §4.1, done — commit pending) and the one with the highest real-world stakes of anything in this plan: the bug class it closes has independently recurred **eight** times in this codebase's git history, and two of those recurrences caused confirmed live data loss (a completed `adversarial_review` with 5 real BLOCKERs discarded because a race let a dying agent's own completion call land in a window where the invariant was half-applied).

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.2 (full text) for the historical bug trace, the target primitive's shape, and the ordering requirement below. Also skim `design_docs/phase2_dedup_prompt.md` and its findings doc (`design_docs/phase2_dedup_findings.md`, once that lands) for this session's most recent example of the expected rigor.

## Do your own freshness check before anything else — this is more critical here than it's been for any prior target

§4.2's text names specific files and line numbers for the ~21 raw termination sites, `AgentManager.terminate_agent`, and `terminate_agent_direct`. **Every one of those paths is now stale.** Since that text was written, this codebase went through five major decompositions: `src/mcp/api.py` → `src/mcp/frontend/` (the `:2174`/`:3025` `terminated_at`-missing sites §4.2 names are now in `src/mcp/frontend/_shared.py`/`phase_routes.py` — confirm exact lines), `src/agents/manager.py` → `manager.py`/`launch_pipeline.py`/`terminator.py`/`output_capture.py` (`AgentManager.terminate_agent`'s real implementation is now `Terminator.terminate_agent` in `terminator.py`, reached via a delegator stub on `AgentManager`), `src/monitoring/monitor.py` → 5 collaborators (`_auto_restart_agent`'s missing-`terminated_at` violation, and `_detect_orphaned_idle_agent`'s missing-`current_task_id` violation, both named in §4.2, are now in `src/monitoring/auto_restart.py` and `src/monitoring/mechanical_recovery.py` respectively — confirm which), `src/autopilot/orchestrator.py` → `orchestrator/` package (`terminate_agent_direct` is in `src/autopilot/orchestrator/engine_client.py`), and `src/services/task_completion_service.py` → `task_completion/` package. **Re-derive the full list of raw `agent.status = "terminated"` / `Agent.status == "terminated"` write sites from a fresh `grep -rn` across the current tree, not from §4.2's list** — the list is very likely still approximately right in content (same ~21 call sites conceptually: `reset_phase`, `stop_workflow`, `pause_feature`, `pause_project_workflows`/`stop_pipeline`, `requeue_design`, `rerun_design`, the orphan-reaper, the monitor's auto-restart path, `terminate_agent_direct`'s 4 callers) but wrong in every file path and line number.

## Target

One primitive: `terminate_agent(agent_id, *, kill_tmux: bool, reason: str)` (exact name/signature per §4.2 — adjust only if you find a concrete conflict, and say so). It must:

1. **Always set all three invariant fields together**: `status = "terminated"`, `current_task_id = None`, `terminated_at = datetime.utcnow()`. This is `CLAUDE.md`'s own documented critical invariant for this codebase (`agent-termination`) — treat it as load-bearing, not a style preference.
2. **Always reset stray tasks pointing at the agent** (`Task.assigned_agent_id == agent_id`, non-terminal status → `pending`).
3. **Reset the task *before* killing tmux and flipping the agent row, not after** — this exact ordering, not the reverse. Two independent live incidents (`91699b1`, `92caa82`, two weeks apart) trace the same race: if the DB write commits before the task reset, a dying agent's own in-flight completion call can land in the gap, get correctly rejected as coming from a terminated agent, and permanently lose real completed work. The primitive must do the task-reset write itself, first, inside its own transaction — not document it as a convention callers have to remember, which is exactly how this recurred twice after the first attempted fix.
4. `kill_tmux=False` covers the orchestrator's in-process raw-DB-write use case (no live tmux session to tear down); `kill_tmux=True` covers the full teardown `Terminator.terminate_agent` already does correctly (WIP commit, transcript capture, SIGINT-then-SIGKILL) — that existing, correct implementation is your reference for what `kill_tmux=True` must still do, not something to simplify away.
5. Every raw call site found in your freshness-check sweep migrates to call this primitive instead of hand-rolling the three-field write.

## Verification

- Write (or locate — Phase 0 of the parent plan calls for this and it may partially exist already) a parametrized test hitting every confirmed raw call site, asserting the invariant holds after each. This should go from N red sites to 0 as a *result* of the consolidation landing, not as separate patches at each site first.
- A second, specific characterization test for the ordering requirement: simulate a task-completion call arriving *during* termination (i.e., after the agent row flips but before/without the task reset, under the old-shaped code) and assert it's rejected cleanly with the task already `pending`, never left dangling with a terminated agent still assigned to it.
- Existing termination-focused test coverage to locate and keep green (re-derive exact file/class names — they've likely moved with `manager.py`'s split): the `TestTerminateAgent`-shaped tests that used to live in `test_agent_manager.py` (probably now covering `Terminator` directly, or still reached through `AgentManager`'s delegator — check which), and `TestTerminateAgentDirectResetsTask` (originally in `test_orchestrator_helpers.py`, covering `terminate_agent_direct`'s stray-task-reset behavior specifically).

## Consider, don't require

A DB-level `CHECK` constraint or SQLAlchemy `before_update` hook enforcing the invariant (`status == "terminated"` implies `current_task_id IS NULL` and `terminated_at IS NOT NULL`) so a *future* raw write anywhere fails loudly at commit time instead of silently. This is the only item in the parent plan that goes beyond "clean up what exists" into "make the bug class structurally unrepresentable" — genuinely worth doing given this has recurred 8 times independently, but it changes the DB layer's failure mode (an exception on a previously-silent bad write), so treat it as optional and flag the tradeoff explicitly rather than silently including or excluding it.

## Explicitly out of scope

- Anything already shipped (all five prior decompositions, §4.1's dedup work). This prompt is scoped to the termination primitive only.
- Any other Phase 2 item (§4.3 onward). If your sweep surfaces something clearly belonging to one of those, log it in your findings doc, don't fix it.
- The known, already-tracked `terminated_at`/`current_task_id` gaps this section itself documents (`src/mcp/frontend/_shared.py`, `auto_restart.py`, `mechanical_recovery.py`) are not separate bugs to patch individually — they're exactly the call sites this primitive is supposed to absorb. Don't fix them piecemeal and then also build the primitive; migrating them to the primitive *is* the fix.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions or stale doc line numbers. `ruff check` clean on every touched file. Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Anything found outside this scope goes in a findings doc (`design_docs/phase2_termination_findings.md` or similar), not fixed inline. No commits — leave everything in the working tree for review.
