# Prompt: Phase 2, §4.3 — dispatch pipeline reconciliation

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.3 of `docs/AUTOPILOT_REFACTOR_PLAN.md`: reconcile the three independent agent-dispatch implementations, and audit a second, structurally separate family of duplicate-dispatch guards against whatever consolidated shape results. This is the third item in this session's Phase 2 sequence — §4.1 (task-creation-claim/retry/arbitration consolidation) and §4.2 (agent-termination primitive) are both done, committed, and independently verified; read their findings docs (`design_docs/phase2_dedup_findings.md`, `design_docs/phase2_termination_findings.md`) for the established rigor and format before starting.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.3 (full text) for the target shape and the "widen scope by one layer" guard-audit requirement. Also read §5's Tier 1 item 1 — **it is an explicit, hard prerequisite for this item, not optional background**: the plan states "Do this before §4.3's dispatch-pipeline consolidation — it's a correctness bug independent of the pipeline-shape cleanup," and separately (`AUTOPILOT_REFACTOR_PLAN.md` principle #2) that this ordering exists specifically to keep the bug fix and the refactor bisectable — don't land them in the same commit even if you do them in the same session.

## Step 0 — mandatory prerequisite, its own commit, before anything else

**`src/mcp/memory_api.py`'s `submit_result_validation`** (around lines 854-859 per the plan's last check — re-verify) resolves "the validator agent" via `ORDER BY created_at DESC LIMIT 1` on `agent_type == "result_validator"`, with no scoping to the specific `result_id`/workflow. Two concurrent result-validation runs across different workflows can cross-wire outcomes — a live correctness bug, not a duplication-shape issue. Fix: scope the lookup by `result_id`/`workflow_id`. Land this as its own commit, verified independently, before starting the consolidation below.

## Do your own freshness check before the consolidation work

The plan's text for this item predates all five of this session's decompositions. Confirmed current locations for the three dispatch implementations as of this handoff (verify anyway, don't trust this list blindly):
- `AgentDispatchService.dispatch` — `src/services/agent_dispatch_service.py`, not touched by any decomposition, should be as described.
- `create_agent_for_task_direct` — `src/autopilot/orchestrator/engine_client.py:316` (moved here when `orchestrator.py` split into a package; already correctly imported elsewhere in this codebase from this path).
- `spawn_validator_agent` — `src/validation/validator_agent.py`, not touched by any decomposition.

**Not yet re-verified — locate these fresh via `git log -p <hash> -- <original-file>` plus a live grep for the guard logic itself, don't guess from the historical commit's file path**, since two of the three plausible homes for this logic (`AgentManager`, `MonitoringLoop`) were both split into packages since these commits landed:
- The in-memory per-phase cooldown dict (`9a2fd48`, originally touching `_create_phase_task_and_agent` — note this function name may itself be stale; the actual guard may now live in `orchestrator/phase_transitions.py` or one of `MonitoringLoop`'s five collaborators, most likely `mechanical_recovery.py` given its role).
- The same-task active-agent guard inside what was `AgentManager.create_agent_for_task` (`9801da7`) — `create_agent_for_task`'s body is now split across `src/agents/manager.py` (thin coordinator) and `src/agents/launch_pipeline.py` (`LaunchPipeline`, 30 methods including the create/restart orchestrators and their shared steps) — find which one actually holds this specific guard now (`_check_duplicate_active_agent` is the step-method name from that split, per `design_docs/phase_1b_decomposition.md` §4.2 — confirm it's the same guard, not a coincidentally-similar-sounding one).
- The status-transition idempotency check (`7dc2e0d`) — locate fresh, no strong prior on where this lives today.

## Target

Per the plan's own phrasing, this is a choice between two shapes, not a single mandated design — make the choice explicitly and justify it:

1. **Route all three dispatch implementations through one duplicate-guard-aware entry point**, or
2. **Give `AgentDispatchService.dispatch` and `spawn_validator_agent` the same sibling-task-guard treatment `create_agent_for_task_direct` already has** (its guard: query for another `Task` sharing `phase_id`, status in `pending/assigned/in_progress/queued`, `created_by_agent_id` in `(None, orchestrator's own id)` — the last clause deliberately excludes tasks a phase agent creates itself for legitimate subtasks).

Consider which is less disruptive given what you find in the freshness check — three call sites with genuinely different callers (HTTP route, in-process orchestrator, validator spawn path) may not want to collapse into one function if their calling conventions differ enough that the unification itself becomes a bigger behavior-risk than three guarded implementations. Say which you chose and why.

## The guard audit — do this after the consolidation shape exists, not before

The three dispatch implementations are not the only defense against "two agents get spawned for one phase." A second, structurally separate family exists, none of it touching `task_creation_claimed_at`:

- An in-memory per-phase cooldown dict (no DB backing — the one guard in this family with zero protection across a process restart).
- The same-task active-agent guard.
- A status-transition idempotency check.

These are real, still-necessary, independently-evolved guards catching races the §4.1 claim primitive structurally can't reach. **Consolidating the three dispatch implementations does not make these three guards redundant — don't delete or merge them into the dispatch consolidation.** Once the consolidated entry point (or the three-guards-added shape) exists, audit each of these three separately: confirm it still fires correctly through the new call path, and specifically evaluate whether the in-memory cooldown dict should move into the consolidated primitive (giving it DB backing / restart-survival) rather than staying wherever its current process-local home is. This is a real design decision — make it explicitly, don't default to leaving it in-memory just because that's less work.

## Explicitly out of scope

- Anything already shipped (all five decompositions, §4.1, §4.2).
- Any other Phase 2 item (§4.4 onward). Log anything you find belonging to one of those, don't fix it here.
- Don't fold the Step 0 validator-race fix into the same commit as the consolidation — keep them bisectable, per the plan's own explicit instruction.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions or stale doc references. `ruff check` clean on every touched file (verify against `git show HEAD~1 -- <file>` before flagging anything as "introduced by this work," the same way pre-existing findings were told apart from real regressions on the last two targets). Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Write characterization tests for the current (pre-consolidation) behavior of whichever guard-audit findings are non-trivial, before changing them. Findings doc (`design_docs/phase2_dispatch_findings.md` or similar) for anything out of scope, and call out Step 0's fix as a logically separate change within it (so it's easy to split into its own commit later) even though it lands in the same working tree as everything else. No commits — leave everything for review, same as every prior target.
