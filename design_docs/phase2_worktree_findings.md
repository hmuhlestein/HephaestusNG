# Phase 2, §4.4 — Worktree removal & merge-conflict-resolution findings

## What was done

### Merge-conflict-resolution strategy decision
**Decision: Keep abort-and-preserve as the one true strategy.** Rationale:
- Silent newest-file-wins auto-resolution on a design-level merge is materially riskier for a self-hosting system than requiring a human to resolve a real conflict
- `cleanup_all_stale_branches`'s `_merge_and_delete` had a fourth, unnamed behavior: force-deleting branches on conflict when no `agent_id` was available (untracked branches) — strictly worse than either named strategy
- `run_single_workflow`'s abort-and-preserve block is the only strategy that never force-deletes or auto-resolves; it always leaves a human a real branch to work with

### Merge primitive created
- **`merge_shared_branch(branch_name, *, message)`** on `WorktreeManager` — the single merge primitive for all worktree cleanup paths
- On conflict: abort and preserve the branch for manual resolution — never auto-resolve, never force-delete
- Returns `{"action": "merged"|"preserved"|"skipped", "branch": branch_name}`

### Refactored `cleanup_all_stale_branches`
- `_merge_and_delete` closure now delegates to `merge_shared_branch` instead of inline merge logic
- Removed the force-delete-on-conflict path (the unnamed fourth behavior)
- Removed the `_resolve_conflicts` (newest-file-wins) call
- Branch deletion only happens after a successful merge

### Verification
- `_cleanup_worktree` safety guard fix confirmed intact (routes through `_remove_worktree` with `require_clean=True`)
- `tests/test_cleanup_worktree_safety_guard.py` — 2 tests green

### Dead code confirmed (not deleted — Phase 4's job)
- `merge_to_main` / `merge_to_parent` — only caller is `merge_to_parent` (bare alias). Dead from outside the file.
- `_archive_and_cleanup` — zero call sites. Dead.

### Fourth unnamed behavior found
`cleanup_all_stale_branches`'s `_merge_and_delete` had a force-delete path (`git branch -D`) for branches where `_resolve_conflicts` wasn't applicable (no `agent_id`). This was reachable for untracked branches matching `agent-`/`autopilot-`/`feature_architect/` prefixes. This path has been removed — such branches now get the same abort-and-preserve treatment as tracked branches.

## Test results
44 targeted tests pass (zero regressions). 3 characterization tests added for `merge_shared_branch` (clean merge, conflict preservation, nonexistent branch skip).

## Ruff
No new issues introduced.

## Out-of-scope findings
- `run_single_workflow`'s abort-and-preserve block (lines 1160-1195 in `__init__.py`) is inline, not using `merge_shared_branch`. Could be refactored to use it, but the function creates its own `WorktreeManager` instance locally — the consolidation would require passing the instance. Not migrated — low risk since it already uses the correct strategy.
- `merge_to_main`/`merge_to_parent` are now fully superseded by `merge_shared_branch`. Deletion is Phase 4's job.
