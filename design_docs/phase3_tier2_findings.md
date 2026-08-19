# Phase 3, Tier 2 — real but lower-risk bugs findings

## Which items needed work vs. which didn't

| Item | Status | Note |
|---|---|---|
| 9 (`cleanup_all_stale_branches` stale-prefix filter) | **Fixed** | |
| 10 (`get_commit_diff_endpoint` timeouts + FTS5 audit) | **Fixed** | Timeouts added; audit found nothing else needing fixing. |
| 11 (`stop_workflow` un-offloaded subprocess) | **Fixed** | |
| 12 (embedding/vector-store dimension mismatch) | **Fixed** | New startup-time assertion. |
| 13 (blocking calls in async handlers, fresh audit) | **Fixed** | 4 real sites found and fixed (1 already covered by item 10's fix). |
| 14 (`post_phase_prompt_preview` hardcoded DB) | **Already fixed** | Confirmed equivalent via `DatabaseManager`'s engine caching. No work needed. |
| 15 (`validate_file_path` superficial check) | **Fixed, redesigned from the plan's literal ask** | See below — a caller-supplied root turned out necessary, not a default one. |
| 16 (`_check_circular_blocking` one-hop only) | **Fixed** | |
| 17 (rejected tickets hard-delete) | **No change — user decision** | Asked; user chose to keep hard-delete. |
| 18 (`spawn_validator_agent` discarded values) | **Fixed** | |
| 19 (terminal-vs-non-terminal status conflation) | **Deferred** | Explicitly lower priority per this item's own prompt doc; not reached given the size of the rest of this batch. |

Eight items required real code changes. Each below, in plan order.

## Item 9 — `cleanup_all_stale_branches`'s stale-prefix filter

`src/core/worktree_manager.py:1138`: replaced the hardcoded `("agent-", "autopilot-", "feature_architect/")` tuple with `(self.config.branch_prefix, "feature_architect/", "feature/")`. Two corrections: `self.config.branch_prefix` (not the literal `"agent-"`) since `create_agent_worktree` (same file, line 281) already builds real branch names from this same configurable attribute — a hardcoded literal would silently stop matching if anyone overrides the prefix. `"feature/"` added — covers both `f"feature/{design}"` and `f"feature/{design}/{feature}"` (`orchestrator/__init__.py`, `worktree_integration.py`), neither of which the original filter matched, meaning branches from every feature-pipeline run were permanently exempt from cleanup.

New `TestCleanupAllStaleBranchesPrefixFilter` (`tests/test_worktree_manager.py`): confirms a `feature/des12345/my-feature` branch is now swept, and an unrelated branch is left alone. Confirmed red pre-fix.

**Correction, found by a concurrent session working the same file, 2026-08-19**: this pass also dropped `"autopilot-"`, reasoning (via full-repo grep) that no code currently constructs a branch with that prefix — true, but the wrong question. A cleanup sweep's job is to catch old debris, including branches created under a naming convention since renamed away from; "nothing generates X anymore" doesn't imply "nothing that still needs sweeping has X." A pre-existing test, `test_legacy_autopilot_prefixed_branch_still_cleaned_up`, covers exactly this and caught the regression. The other session restored `"autopilot-"` alongside `"agent-"` (kept as a literal fallback too) and `self.config.branch_prefix`/`"feature_architect/"`/`"feature/"` — the filter now reads `(self.config.branch_prefix, "agent-", "autopilot-", "feature_architect/", "feature/")`. Re-ran the full `tests/test_worktree_manager.py` suite after their fix: 11 passed, including both of this item's own tests alongside theirs.

## Item 10 — `get_commit_diff_endpoint` timeouts + FTS5 injection audit

Added `timeout=10` to all three `subprocess.run` calls (`src/mcp/tickets_api.py:1312,1335,1359`). New `TestGetCommitDiffTimeouts` (`tests/test_mcp_server_tickets.py`) mocks `subprocess.run` and asserts every call carries a `timeout` kwarg. Confirmed red pre-fix.

**FTS5/query-grammar audit**: grepped the full codebase for `MATCH`/FTS5 usage — the only real query site is `ticket_search_service.py:225`'s `ticket_fts.MATCH :query`, already routed through `_fts5_query()` (the historical `75a35b3` fix, confirmed intact). Checked the plan's other named risk, `WorkflowOrchestrator._check_condition`'s grammar (`src/workflow_engine/orchestrator.py:542`) — read its full implementation: it extracts `var_name`/`op`/`threshold` via a regex-anchored pattern and dispatches through a fixed operator dict (`CONDITION_OPERATORS[op]`), no `eval()`/`exec()`, no string interpolation into a live query. Safe by construction, not a live gap. Audit found nothing else to fix.

## Item 11 — `stop_workflow`'s un-offloaded subprocess.run

`src/mcp/frontend/_shared.py`: wrapped the per-agent `subprocess.run(["tmux", "kill-session", ...])` in `await loop.run_in_executor(None, functools.partial(...))`, matching `reset_phase`'s identical operation a few lines below. **The plan's own claim that this "becomes moot once §4.2 unifies both routes' termination call" was wrong** — §4.2 (this session) unified the *agent-termination* call (`terminate_agent()`), not the separate tmux kill-session subprocess call each route makes independently; still genuinely live.

New `tests/test_stop_workflow_offloading.py`: mocks the event loop and confirms `run_in_executor` is actually invoked with the right `functools.partial`. Confirmed red pre-fix.

## Item 12 — embedding/vector-store dimension mismatch

Traced both real callers of `store_memory` before assuming the fix was still needed: `rag.py`'s `ingest_document` and `memory_api.py`'s `save_memory` background task both wrap the call in a bare `except Exception: log and continue` — confirming the plan's "silently swallowing a dimension-mismatch exception per store_memory call" is accurate, the per-call `ValueError` guard exists but every caller discards it.

Added `validate_embedding_dimension_compatibility(vector_store, embedding_dim)` to `src/memory/store_factory.py` — reads the vector store's `COLLECTIONS` dict (handles both `TurboVecStore`'s `"dim"` key and `VectorStoreManager`'s `"size"` key; both are uniform across every collection within a given backend, confirmed by reading both classes' full `COLLECTIONS`), raises `ValueError` on mismatch. Wired into `src/mcp/server.py`'s server-state initialization, right after both `self.vector_store` and `self.embedding_service` exist — inside the same `try/except` that already degrades task-dedup gracefully on any init failure, so a mismatch is caught at startup with a clear diagnostic instead of failing every `store_memory` call silently for weeks.

**A separate, bigger finding, explicitly out of scope here**: `memory_api.py`'s `save_memory` actually generates its embedding via `server_state.llm_provider.generate_embedding(...)` — a completely different pathway from `create_embedding_provider()`/`EMBEDDING_BACKEND` (governed instead by whichever LLM provider is configured). This means the new startup check protects `task_similarity_service`/`ticket_search_service`/`rag.py`'s shared pathway, but **not** the agent-facing `heph_save_memory` tool's actual embedding call — that pathway isn't unified with `EMBEDDING_BACKEND` at all, despite §4.7's stated unification goal. Fixing that is a pathway-unification task, not a narrow startup-assertion fix; flagged for follow-up, not touched here.

New `tests/test_store_factory_dimension_validation.py` (4 tests, using lightweight fake stores plus one sanity check against the real `TurboVecStore` class) plus a full `tests/integration/test_task_deduplication_flow.py` regression run (4 pre-existing failures confirmed unrelated via isolation).

## Item 13 — blocking calls in async handlers, fresh audit

The plan's own file list was fully stale (`autopilot_api.py`/`src/mcp/api.py` don't exist). Grepped fresh for `subprocess.run`/`.call`/`.check_output`/`.Popen` across `src/mcp/`, found 7 files with matches, then checked each call site's enclosing function for `async def` + absence of `run_in_executor`. Four confirmed live gaps, all fixed with the same `run_in_executor(None, functools.partial(...))` pattern used in item 11:

- `tickets_api.py`'s `get_commit_diff_endpoint` (3 calls) — offloaded alongside item 10's timeout fix, since both live at the same 3 call sites.
- `project_routes.py`'s `remove_project_design` — per-agent tmux kill-session inside a loop.
- `control_routes.py`'s `get_system_health` — offloaded at the async caller, not inside `run_health_audit` itself, since that function is shared with the Monitor's own background-thread call path (`health_audit.py`) and must stay sync for that caller.
- `feature_routes.py`'s `review_feature` — the `gh pr merge` call (up to a 30s timeout), run on every feature approval.

One file checked and confirmed already correct, not a false negative: `queue_routes.py`'s repair endpoint already uses `loop.run_in_executor(None, _run_repair, ...)`, matching its own inline comment.

New `tests/test_blocking_calls_offloaded.py` (3 tests, covering the 3 sites not already covered by item 10's test) — each mocks the event loop and confirms `run_in_executor` is invoked with the expected function/args. Confirmed red pre-fix (all 3 fail with the fixes reverted).

## Item 14 — `post_phase_prompt_preview`'s hardcoded DatabaseManager

Confirmed already fixed by the time this item started: `src/mcp/frontend/phase_routes.py:92` now uses `DatabaseManager(None)`, not the literal string `"hephaestus.db"` the plan describes. `DatabaseManager.__init__` resolves `None` via `os.environ.get("HEPHAESTUS_TEST_DB", "hephaestus.db")` — the same env-var fallback every correctly-wired endpoint gets. Verified empirically that this is functionally equivalent to reusing the injected `frontend_api.db_manager`: `DatabaseManager` caches engines by resolved path (`_engines: Dict[str, Any]`), and a script confirmed two separately-constructed instances with the same resolved path share the identical cached engine object. No code change needed.

## Item 15 — `validate_file_path`'s superficial traversal check

**This item required redesigning the plan's literal ask, not just implementing it, after running the existing test suite surfaced a real architectural constraint.** The first implementation (resolve the path, reject anything outside `Path.cwd()`) broke 10 existing tests — not because they were wrong, but because several use `tempfile.NamedTemporaryFile` (a real, legitimate result-file location outside the server's own working directory) and one (`test_validate_file_path_valid`) explicitly asserts an arbitrary absolute path should pass. Traced both real callers (`workflow_result_service.py`, `result_service.py`): neither has a workflow/worktree root available at the point it calls `validate_file_path` — there is no single global root that's correct for every legitimate result-file location (worktrees can live anywhere `worktree_base_path` points, temp files can live anywhere the OS puts them).

Landed on: `allowed_root` as an **optional** parameter, defaulting to `None` (no containment check — today's behavior for the two existing callers, preserved exactly). When a future caller does have a real root to check against, passing it gets real resolve() + `is_relative_to()` containment. Separately, fixed the traversal check itself to operate on `Path.parts` (path segments) instead of a raw substring — the old substring check would reject a filename merely *containing* `".."` as text (e.g. `notes..final.md`, confirmed via a new test that fails red under the old code) while still missing an absolute path needing no `".."` at all to point somewhere unsafe (`/etc/passwd` — the actual gap the plan's `allowed_root` half now closes, when a caller supplies one).

New tests in `tests/test_validation_helpers_coverage.py` (4 new: outside/inside `allowed_root`, traversal-without-root still rejected, false-positive filename no longer rejected) — the three behavior-changing ones confirmed red pre-fix; verified all pre-existing callers/tests (`test_result_service.py`, `test_mcp_results_endpoint_async.py`) still pass unchanged.

**Superseded, 2026-08-19, by a stronger fix from a concurrent session working the same file.** The opt-in-only design above left the real gap open in practice: since neither real caller ever passes `allowed_root`, an absolute path like `/etc/passwd` still sailed straight through — the fix existed but nothing used it. The concurrent session closed that by making the check apply *by default* instead of only when a caller opts in: `_default_allowed_roots()` builds a real root set (the repo/worktree paths from config, falling back to `cwd` if config can't be read, plus the system temp directory) so the containment check is meaningfully live for every current caller, not just theoretically available to a future one. New `tests/test_validate_file_path_containment.py` pins both halves directly: arbitrary system paths (`/etc/passwd`, `/etc/shadow`, `/root/.ssh/id_rsa`, `/usr/bin/python3`) are now genuinely rejected, and the two real locations (repo/worktrees, system temp dir) keep working. An explicit `allowed_root` still overrides and narrows further when a caller has one.

This fix broke 5 pre-existing tests that used a fake, arbitrary absolute path (`/nonexistent/file.md`, `/valid/path/to/file.md`) to exercise a *different* code path (the file-not-found check that runs after `validate_file_path`) — under the new default containment, those fake paths are now correctly rejected by containment first, before ever reaching the not-found check they meant to test. Fixed all 5 (`test_validation_helpers_coverage.py`'s own regex wording, plus `test_result_service.py`, `test_workflow_result_service.py`, `test_mcp_results_endpoint_async.py`) to use a real nonexistent path under the system temp dir instead, so each exercises what it originally intended to. Full combined suite (this file's tests, the new containment tests, and all four caller test files): 44 passed, 1 skipped, 1 pre-existing unrelated failure (a network-timeout integration test, confirmed unrelated earlier this session).

## Item 16 — `_check_circular_blocking` one-hop only

`src/services/ticket_service.py`: replaced the pairwise direct-neighbor check with BFS over the `blocked_by_ticket_ids` graph, starting from each candidate blocker — if the traversal ever reaches the ticket being updated, adding the dependency would close a cycle of any length, not just the direct A↔B case. `visited` guards against an already-existing cycle elsewhere in the data causing an infinite loop.

New `tests/test_ticket_circular_blocking.py` (4 tests: direct two-hop cycle still caught, a three-hop chain the old pairwise check couldn't see, a non-cyclic chain correctly accepted, a pre-existing unrelated cycle doesn't hang the BFS). The three-hop test confirmed red pre-fix.

## Item 17 — rejected/timed-out tickets hard-delete

Per this item's own prompt doc, presented the retain-vs-delete choice to the user rather than picking one. Researched the actual rejection flow first (who rejects, why, what survives) to inform the decision: a ticket only enters `pending_review` when a project enables `board_config.ticket_human_review`; rejection is a human dashboard action (already warns "It will be deleted") or a 30-minute timeout; both paths hard-delete `TicketHistory`/`TicketComment`/`TicketCommit` plus the `Ticket` row itself. **User decision: keep hard-delete as-is.** No code changed.

## Item 18 — `spawn_validator_agent`'s discarded diff/results values

Confirmed via `git log --follow -p` that the omission was accidental, not deliberate: a ruff unsafe-fix commit (`bffa389`, "fix: ruff unsafe-fix 34 issues") stripped the now-unused-variable *assignments* (`workspace_changes = ...` → `...`) because the values were already never read anywhere — but that's the symptom, not the cause; the prompt-building code never referenced them even before that commit.

Wired both through: `spawn_validator_agent` (`src/validation/validator_agent.py`) now captures `get_workspace_changes`'s and `get_agent_results`'s return values (the former left unguarded, matching its pre-existing behavior exactly — a real failure there already failed the whole spawn before this fix, and still does; a first attempt added a try/except to make this resilient, but that's an unrequested behavior change beyond what this item asked for, reverted). Extended `format_task_validation_prompt` (`src/monitoring/prompt_loader.py`) with two new optional parameters, each building a new prompt section via `get_prompt()` (matching the existing `previous_feedback_section` pattern) — added the two new template fragments to `config/prompts/system_prompts.yaml` and the corresponding placeholders to `src/prompts/task_validation_prompt.md`, positioned right before STEP 2 ("EXAMINE THE WORK") so the validator gets a pre-computed starting point before its own independent exploration.

New tests: `tests/test_validation_prompts.py` (2, formatter-level — with/without the new values, confirming no `KeyError`/unformatted placeholders), `tests/test_validation_system.py` (1, `spawn_validator_agent`-level, confirming the real captured values reach the formatter's call — not just that the formatter accepts them). All confirmed red pre-fix.

## Verification

- Every fix has a regression test independently confirmed to fail against pre-fix code via `git stash push --keep-index -- <file>` isolation.
- `ruff check` clean on every touched file. Pre-existing findings confirmed unchanged from HEAD on every file that had any (`control_routes.py`, `project_routes.py`, `server.py`, `validator_agent.py`).
- Broad regression run across all touched files' test suites (12 files, ~95 tests): 7 pre-existing failures, all individually confirmed unrelated via isolation (`test_mcp_server_tickets.py::TestCreateTaskValidation`, `test_mcp_results_endpoint_async.py::test_integration_with_server`, `test_validation_system.py::test_spawn_validator_agent`, and 4 in `test_task_deduplication_flow.py` — the last two categories already confirmed pre-existing during Phase 3 Tier 1's own work this session).

## Explicitly out of scope

- Item 19 (terminal-vs-non-terminal status conflation) — deferred per its own explicit lower-priority framing in this item's prompt doc.
- The `memory_api.py` embedding-pathway divergence found during item 12 — a §4.7-scale unification task, not a narrow fix.
- Tier 3 (items 22-28) and Phase 4 (dead code deletion).

No commits — left in the working tree for review.

## Gap-check addendum

Reread this item's own prompt doc (`design_docs/phase3_tier2_prompt.md`) end to end against what was actually delivered. Three real gaps found, all closed:

- **Item 9's `"autopilot-"` removal violated this item's own prompt doc.** The Target section explicitly said: "the plan says it matches nothing ever produced — verify that's still true before removing it, a prefix matching nothing is harmless but removing a genuinely-dead check isn't this item's job unless it's clearly safe." A full-repo grep confirmed no *current* code produces that prefix and was treated as sufficient — but a cleanup sweep's job is to catch old debris, including branches from a since-renamed convention, so "nothing generates it now" was the wrong question to have answered as "clearly safe." A concurrent session working the same file caught this via a pre-existing test (`test_legacy_autopilot_prefixed_branch_still_cleaned_up`) and restored the prefix; see the correction under item 9 above and the plan doc's own updated status note. Re-ran the full `tests/test_worktree_manager.py` suite after their fix: 11 passed.
- **Item 15's verification never touched `workflow_result_service.py`**, one of the two real production callers of `validate_file_path` (its `submit_result` calls it at two separate sites, lines 48 and 69) — only `result_service.py`'s test coverage was run. Ran `tests/test_workflow_result_service.py` and `tests/test_result_submission_flow.py` (both cover the missed caller): 9 passed, 1 skipped, no regressions.
- **`ruff check` was never run on any of the new/modified test files**, only source files — despite the prompt doc's quality bar saying "clean on every touched file" with no source/test distinction. Running it found one real, newly-introduced issue: `tests/test_blocking_calls_offloaded.py`'s helper class `_session_ctx` violated `N801` (should be CapWords) — genuinely mine, not pre-existing noise. Renamed to `_SessionCtx` throughout; all 3 tests in that file still pass, ruff clean.

Everything else on reread held: the freshness-check claims for items 10-14 and 16-21 were independently re-verified during implementation, not just copied from the prompt; item 17's product-decision boundary was respected (asked, didn't pick); item 19 was deferred with its own explicit permission cited; the "full-suite gate" language was interpreted as the broad multi-file regression run actually performed, consistent with the standing user preference (recorded in memory) against running the entire test suite unless asked, and consistent with the same interpretation Tier 1's own gap-check settled on.
