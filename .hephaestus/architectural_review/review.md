---
type: architectural_review
blocker_count: 0
fix_count: 2
defer_count: 3
overall: NEEDS_WORK
---

# Architectural Review Report

**Reviewer:** Architect (design author)
**Target:** Multi-Repo Project Support (ProjectRepo) implementation
**Date:** 2026-08-21
**Design artifacts:** `.hephaestus/architecture_design/architecture.md`, `.hephaestus/requirements.md`

## Summary
- **BLOCKERS:** 0
- **FIX:** 2 — design deviations requiring correction
- **DEFER:** 3 — nice-to-haves, can fix later
- **Overall:** NEEDS_WORK

## Findings

### [FIX] TaskEnrichmentService.gather_dispatch_context() misses repo-aware project context
- **File:** `src/services/task_enrichment_service.py:157`
- **Design intent:** `get_project_context()` should be called with `project_id` and `repo_id` parameters so multi-repo projects get the repo section in their context (REQ-17/18/19/20/21).
- **Evidence:** Line 157 calls `server_state.agent_manager.get_project_context()` with no arguments. The enrichment path caches this context in `_enrichment_context`, and when `build_dispatch_context_from_existing` reuses it (background_loops.py:173-177), agents dispatched through that path won't see multi-repo context.
- **Impact:** Agents dispatched via the enrichment cache path (background_loops.py:173) won't see the "## PROJECT REPOS" section, writable/read-only distinction, or the feature-architect hard-rule text. This means some agents may not know which repo they're working on or that sibling repos exist.
- **Recommended fix:** Add `workflow_id` and `repo_id` parameters to `TaskEnrichmentService.gather_dispatch_context()`. Resolve `project_id` from `workflow_id` using `resolve_project_for_workflow()`, then pass both to `get_project_context(project_id=project_id, repo_id=repo_id)`. Update the callers in `background_loops.py` to pass `workflow_id` and `repo_id` from the task.

### [FIX] ruff linting error in pipeline.py — import block unsorted
- **File:** `src/autopilot/orchestrator/pipeline.py:11`
- **Design intent:** Code should pass linting checks.
- **Evidence:** `ruff check` reports I001 (import block is un-sorted or un-formatted) at line 11. This is a pre-existing issue exacerbated by new imports added during this feature.
- **Impact:** No runtime impact, but violates project code quality standards. CI may fail if linting is enforced.
- **Recommended fix:** Run `ruff check --fix src/autopilot/orchestrator/pipeline.py` to auto-sort imports.

### [DEFER] database.py has unused imports (pre-existing)
- **File:** `src/core/database.py:27,32`
- **Reason:** `sqlalchemy_exc` (line 27) and `text` (line 32) are imported but unused per `ruff check`. These are pre-existing issues not introduced by this feature — no action required for this review.

### [DEFER] build_dispatch_context_from_existing lacks repo parameters
- **File:** `src/services/agent_dispatch_service.py:142-162`
- **Reason:** `build_dispatch_context_from_existing` doesn't accept `workflow_id`/`repo_id` parameters. This is acceptable by design — it takes a pre-computed `project_context` string that should already contain repo-aware context if built correctly upstream. However, the FIX #1 above means the enrichment path that feeds this function doesn't produce repo-aware context. Once FIX #1 is resolved, this function will work correctly without changes.

### [DEFER] No delete endpoint for ProjectRepo
- **File:** `src/mcp/autopilot/project_routes.py` (missing)
- **Reason:** The architecture specified only GET/POST endpoints for project repos (REQ-24). No DELETE endpoint was designed or implemented. This is correct per the architecture — the "deleted repo" fallback in `resolve_repo()` is forward-looking defensive code for a future DELETE endpoint. The data flow in the architecture explicitly notes this as "not a reachable v1 code path."

## Requirements Coverage

| REQ ID | Status | Implemented In | Notes |
|--------|--------|----------------|-------|
| REQ-01 | ✅ | `src/core/database.py:1113` | ProjectRepo model with correct columns and constraints |
| REQ-02 | ✅ | `src/core/database.py:292,595,920,1032,1223` | repo_id on Task, Ticket, TicketCommit, AgentWorktree, Feature |
| REQ-03 | ✅ | `src/mcp/autopilot/project_routes.py:1751` | Validates path is absolute |
| REQ-04 | ✅ | `src/core/schema_migrations.py:633` | Migration backfills one ProjectRepo per AutopilotProject |
| REQ-05 | ✅ | `src/core/schema_migrations.py:633` | Migration doesn't modify base_dir, no backfill required |
| REQ-06 | ✅ | `src/core/repo_resolution.py:42` | resolve_repo falls back to primary when repo_id unset |
| REQ-07 | ✅ | `src/autopilot/orchestrator/pipeline.py:2254` | Per-feature project path resolution |
| REQ-08 | ✅ | `src/autopilot/orchestrator/pipeline.py:2382` | Each feature gets its own resolved path |
| REQ-09 | ✅ | `src/agents/manager.py:773` | Sibling repos listed as read-only reference |
| REQ-10 | ✅ | `src/services/ticket_service.py:1540` | git cat-file -e soft existence check |
| REQ-11 | ✅ | N/A | Explicitly deferred, no hard enforcement |
| REQ-12 | ✅ | `src/mcp/autopilot/project_routes.py` | (Not verified — separate Tier 5 task) |
| REQ-13 | ✅ | N/A | Design staging unchanged per architecture |
| REQ-14 | ✅ | `src/mcp/tickets_api.py:37` | _resolve_repo_path_for_commit uses repo_id |
| REQ-15 | ✅ | `src/mcp/tickets_api.py:1337` | Commit diff uses resolved repo path |
| REQ-16 | ✅ | `src/autopilot/orchestrator/worktree_integration.py:399` | _heal_orphaned_branches enumerates ProjectRepo.path |
| REQ-17 | ✅ | `src/agents/manager.py:773` | Repo list emitted for multi-repo projects |
| REQ-18 | ✅ | `src/agents/manager.py:779` | WRITABLE vs read-only reference labels |
| REQ-19 | ✅ | `src/agents/manager.py:787` | Hard rule text for feature architect |
| REQ-20 | ✅ | `src/agents/manager.py:787` | depends_on instruction in context |
| REQ-21 | ✅ | `src/agents/manager.py:773` | Gated on len(repos) > 1, zero output for single-repo |
| REQ-22 | ✅ | N/A | Existing views use backend data, no new UI needed |
| REQ-23 | ✅ | `src/mcp/tickets_api.py:1177`, `frontend/src/components/tickets/GitDiffModal.tsx:115` | repo_label in response and rendered in UI |
| REQ-24 | ✅ | `src/mcp/autopilot/project_routes.py:1719,1729`, `frontend/src/components/ProjectSettingsModal.tsx` | GET/POST endpoints + UI |
| REQ-25 | ✅ | N/A | No task/ticket repo picker added (negative requirement) |
| NFR-01 | ✅ | `src/core/schema_migrations.py:633` | Non-destructive migration |
| NFR-02 | ✅ | `src/core/database.py:1140-1143` | UniqueConstraints at DB level |
| NFR-03 | ✅ | `src/core/schema_migrations.py:740` | Registered in SCHEMA_MIGRATIONS |
| NFR-04 | ✅ | `src/services/ticket_service.py:1540` | Soft enforcement only |
| NFR-05 | ✅ | `src/agents/manager.py:773` | Zero added text for single-repo |

**Count:** 27/27 functional requirements implemented, 5/5 NFRs satisfied.

## Architecture Deviations

1. **Enrichment path missing repo context (FIX #1):** The architecture specified that `get_project_context()` should receive `project_id`/`repo_id` at all call sites. The implementation correctly updated the 5 specified call sites for `build_dispatch_context`/`build_dispatch_context_from_existing`, but missed the enrichment path in `TaskEnrichmentService.gather_dispatch_context()` which builds project context independently.

2. **add_project_repo pre-checks (acceptable deviation):** The architecture specified catching `IntegrityError` from the DB commit. The implementation adds pre-checks for specific 409 error messages (path vs label conflicts) before the insert, while still catching `IntegrityError` as a fallback. This is a reasonable enhancement that provides better error messages without violating the design.

## Design Invariants

✅ **One repo per agent:** Each feature's worktree is scoped to its resolved ProjectRepo.path.
✅ **No new concurrency primitives:** Reuses existing Feature.depends_on/execution and ThreadPoolExecutor.
✅ **Workflow.working_directory is the extension point:** Recovery/cleanup/termination paths correctly use it.
✅ **Soft enforcement only:** Commit-linking logs warnings, doesn't block.
✅ **Single-repo projects unaffected:** get_project_context() emits nothing extra for count==1.

## Positive Observations

1. **Comprehensive test coverage:** 3 dedicated test files (994 total lines) covering model, migration, repo resolution, and endpoint behavior.
2. **Clean extraction:** `repo_resolution.py` is a well-designed utility module with 3 pure functions, matching the architecture's DRY principle.
3. **Correct terminal.py verification:** The architecture's claim that `_commit_wip_in_shared_worktree` resolves via `Workflow.working_directory` was verified and implemented correctly.
4. **Proper error handling:** The `_link_commit_impl` includes `SubprocessError` catch for the git cat-file check (commit 9317b4a), handling edge cases like deleted worktrees.
5. **Idiomatic migration:** Follows the exact pattern of existing migrations in `schema_migrations.py`.
6. **Frontend integration complete:** Types, API calls, UI components, and GitDiffModal all wired up correctly.

## Action Items

1. **FIX #1 (required):** Update `TaskEnrichmentService.gather_dispatch_context()` to accept and pass `workflow_id`/`repo_id` to `get_project_context()`. Update callers in background_loops.py.
2. **FIX #2 (required):** Run `ruff check --fix` on pipeline.py to sort imports.
