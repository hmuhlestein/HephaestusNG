# Prompt: Phase 2, §4.4 — worktree removal & merge-conflict-resolution primitives

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.4 of `docs/AUTOPILOT_REFACTOR_PLAN.md`: consolidate the remaining worktree-removal duplication and pick one merge-conflict-resolution strategy. This is the fourth item in this session's Phase 2 sequence — §4.1, §4.2, §4.3 are done. Read their findings docs (`design_docs/phase2_dedup_findings.md`, `design_docs/phase2_termination_findings.md`, `design_docs/phase2_dispatch_findings.md`) for the established rigor and format before starting.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.4 (full text). It documents that this item is **narrower than it originally looked**: Phase 1's `worktree_integration.py` extraction (part of the `orchestrator.py` → `orchestrator/` package split) already fixed the worst bug here — `_cleanup_worktree` used to bypass `WorktreeManager._remove_worktree`'s `require_clean` safety guard entirely; it now routes through it correctly. **Verify that fix is still intact before doing anything else** — `_cleanup_worktree` now lives in `src/autopilot/orchestrator/worktree_integration.py`, and there's a dedicated regression test (`tests/test_cleanup_worktree_safety_guard.py`) that should still be green; if either has drifted, that's a red flag worth stopping on, not something to route around.

## Freshness check

Confirmed current locations and exact shapes as of this handoff (re-verify line numbers before relying on them — they'll drift the moment anything above them in these files changes):

- **`WorktreeManager`** — `src/core/worktree_manager.py`, not touched by any decomposition.
  - `_remove_worktree(self, worktree_path, require_clean=True)` — line 791. The guarded removal primitive. Unchanged, still correct.
  - `cleanup_worktree(...)` — line 877.
  - `cleanup_all_stale_branches(self) -> Dict[str, Any]` — line 925. Its own docstring literally says what it does: *"1. Prune and remove stale worktrees. 2. Merge active branches into main (newest-file-wins on conflict). 3. Delete branches (force-delete unmergeable ones)."* The merge step is an inline closure, `_merge_and_delete(branch_name, agent_id)`, defined at line 1090, called once per tracked record and once per untracked branch matching `agent-`/`autopilot-`/`feature_architect/` prefixes. On `GitCommandError` containing `"CONFLICT"`, it calls `self._resolve_conflicts(agent_id, session, self.main_repo)` (defined at line 597) — **this is the newest-file-wins auto-resolve strategy**, the one the plan's own text argues is the riskier default to keep. If no `agent_id` (an untracked branch) or `_resolve_conflicts` isn't applicable, it aborts the merge and force-deletes the branch instead (`git branch -D`) — note this is a *third*, even more silent behavior on some conflict paths (just deleting the unmergeable branch, no preservation at all) that the plan's three-strategies framing doesn't explicitly name — flag this as a finding if you confirm it's a real, reachable path, since it's arguably worse than either named strategy.
  - `merge_to_main(self, agent_id) -> Dict[str, Any]` — line 448, docstring: *"Conflicts resolve with newest-file-wins."* Confirmed dead from outside the file (only internal caller is `merge_to_parent`, line 785, a bare alias — `return self.merge_to_main(agent_id)`). Re-verify this is still true with a fresh `grep -rn "\.merge_to_main(\|\.merge_to_parent(" src/` before treating it as safe to eventually delete.

- **`_cleanup_worktree`** — `src/autopilot/orchestrator/worktree_integration.py` (moved here by the orchestrator split). Already fixed by Phase 1 to route through `_remove_worktree`'s `require_clean` guard — verify `tests/test_cleanup_worktree_safety_guard.py` is still green before doing anything else, per the note above.

- **`_archive_and_cleanup`** — moved to `src/autopilot/orchestrator/__init__.py`, confirmed dead code (zero call sites) as of the split's own research pass. **Do not delete it here** — Phase 4 (§6)'s job, not this item's. Leave it alone even though it's dead.

- **`run_single_workflow`'s abort-and-preserve merge block** — `src/autopilot/orchestrator/__init__.py`, roughly lines 1160-1195 (re-verify). Sequence: checkout base branch, abort any in-progress merge, hard-reset + clean the main repo, then `git merge(design_branch, no_ff=True, m=...)`. On success, logs the merge SHA. On `GitCommandError` containing `"CONFLICT"`: **abort the merge and explicitly preserve the branch** ("`Conflict detected — branch {design_branch} preserved for manual merge/PR`"), no auto-resolution attempted, no destructive fallback. This is the strategy the plan recommends keeping — it's the only one of the three that never force-deletes or auto-resolves; it always leaves a human a real branch to work with on conflict.

## Scope — two sub-problems

### 1. Worktree-removal consolidation

`cleanup_all_stale_branches`'s inline removal sweep is a second, independent removal path — distinct from `_cleanup_worktree`, which Phase 1 already fixed to route through `WorktreeManager._remove_worktree`. Give the target API described below to `WorktreeManager` and have `cleanup_all_stale_branches` call it instead of its own inline implementation, the same way `worktree_integration.py`'s `_cleanup_worktree` already does for the removal-only case.

### 2. Merge-conflict-resolution consolidation — a product decision, not just engineering

Three independent strategies exist for the same conceptual operation:
- `WorktreeManager.merge_to_main` — dead in production (re-verify).
- `cleanup_all_stale_branches`'s inline merge — auto-resolve, newest-file-wins.
- `run_single_workflow`'s abort-and-preserve block — live, handles the common (shared-worktree) case today.

**The plan recommends keeping abort-and-preserve as the one true strategy** and deleting the other two (after confirming they're unreachable post-consolidation) — its stated reasoning: silent newest-file-wins auto-resolution on a design-level merge is a materially riskier default for a self-hosting system than requiring a human to resolve a real conflict. This is flagged in the plan as needing a product call, not just an engineering one. **Make the decision explicitly and record it in your findings doc** — don't silently implement the plan's recommendation without confirming it still makes sense against the current code, and don't silently pick a different strategy without saying so and why.

One more concrete data point for that decision, found while locating these three strategies: `cleanup_all_stale_branches`'s `_merge_and_delete` closure has what's effectively a *fourth*, unnamed behavior on some conflict paths — when there's no `agent_id` to resolve against (untracked branches) or `_resolve_conflicts` doesn't apply, it aborts the merge and **force-deletes the branch outright** (`git branch -D`), with no preservation at all. That's strictly worse than either named strategy — confirm whether this path is actually reachable in practice (which branches lack an `agent_id`?) and treat it as a finding either way, since the plan's "three strategies" framing doesn't account for it.

## Target API

`create_shared_worktree(branch_name, base_ref)` / `merge_shared_branch(branch_name, *, on_conflict)` on `WorktreeManager`, used by both the isolated-agent and shared-worktree callers — so `cleanup_all_stale_branches` calls the same guarded primitives `_cleanup_worktree` already uses, instead of maintaining its own separate inline implementation. Verify this exact signature still fits the current call sites; adjust and document if not.

## Explicitly out of scope

- Deleting `_archive_and_cleanup` — Phase 4, §6, not here, even though it's confirmed dead code.
- Deleting `WorktreeManager.merge_to_main`/`cleanup_worktree` outright — only after this consolidation confirms they're unreachable, and even then that deletion is Phase 4's job per the plan's text, not this item's. Leave a note in your findings doc if you confirm they're now fully superseded; don't delete them yourself.
- Anything already shipped (all five decompositions, §4.1, §4.2, §4.3).
- Any other Phase 2 item (§4.5 onward). Log anything found belonging to one of those.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions. `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>` before flagging anything as introduced by this work. Full targeted-test verification (`tests/test_cleanup_worktree_safety_guard.py` plus whatever covers `cleanup_all_stale_branches` and the merge paths — locate fresh) plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Write characterization tests for current merge/removal behavior before changing which strategy wins, so the conflict-resolution decision is provably a decision, not an accident. Findings doc (`design_docs/phase2_worktree_findings.md` or similar) for anything out of scope, including the product-decision writeup. No commits — leave everything in the working tree for review.
