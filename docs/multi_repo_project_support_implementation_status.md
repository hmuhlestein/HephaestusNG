# Multi-Repo Project Support — Implementation Status

Feature: `des-c7b9` (branch `feature/des-c7b9/project-repo-api`)
Architecture: `.hephaestus/architecture_design/architecture.md`
Requirements: `.hephaestus/requirements.md`

## Implementation Status: COMPLETE (development phase)

All 10 components (C1–C10) from the architecture doc are implemented:

- **C1: ProjectRepo Model** — `src/core/database.py`. Unique constraints on
  `(project_id, path)` and `(project_id, label)`.
- **C2: Migration + repo_resolution.py** — `src/core/schema_migrations.py`,
  `src/core/repo_resolution.py` (`ensure_primary_repo`, `resolve_primary_repo`,
  `resolve_repo`, `list_repos`). Migration backfills one primary `ProjectRepo`
  per pre-existing project.
- **C3: Per-Feature Worktree Path Resolution** —
  `src/autopilot/orchestrator/pipeline.py` (`_resolve_feature_project_path`).
- **C4: Agent Prompt Context** — `src/agents/manager.py`
  (`get_project_context`, `_build_repo_context`).
- **C5: Commit-Link Validation** — `src/services/ticket_service.py`
  (`_link_commit_impl`), soft-then-hardened existence check via
  `git cat-file -e`.
- **C6: Doc/Design Storage Resolution** — `src/mcp/autopilot/design_file_routes.py`.
- **C7: Commit Resolution** — `src/mcp/tickets_api.py`
  (`_resolve_repo_info_for_commit`).
- **C8: Recovery/Cleanup Repo Scoping** —
  `src/autopilot/orchestrator/worktree_integration.py`
  (`heal_orphaned_agent_branches` enumerates `ProjectRepo` rows).
- **C9: Feature Architect Repo Assignment** —
  `config/prompts/system_prompts.yaml`, `src/autopilot/orchestrator/features.py`.
- **C10: Frontend** — repo CRUD UI, `GitDiffModal` repo badge.

## QA Bounce — Test Suite Fixes (this pass)

Entered this phase with 55 failing tests reported by QA plus 4 open bug
tickets. All 4 tickets fixed and shipped:

- `ticket-7290f611` — 12 tests calling FastAPI route functions directly
  didn't pass `agent_id`, so the un-supplied `Header` sentinel leaked into
  `verify_agent_authentication`. Fixed by passing `agent_id="ui-user"`
  explicitly at each call site. Also found and fixed a real bug this
  surfaced: `feature_review_routes.py`'s local-merge-fallback wrote its
  `review_approved` marker under `wf.working_directory`, but the merge
  itself runs with `cwd=project.base_dir` — a different, non-ancestor
  directory the safe-git wrapper's upward walk never reaches. Now writes
  the marker under `project.base_dir` too.
- `ticket-36213a2d` — 14 tests 401'd because their `verify_agent_authentication`
  monkeypatch targeted `_shared`'s copy of the function while the route
  module had already bound its own name-imported reference (either from
  a prior test file's import, or — for `test_tickets_api_endpoints.py` —
  because `server_state.db_manager` was never wired to the test's own DB
  at all). Fixed by patching the actual binding site / wiring
  `server_state.db_manager` in each affected fixture.
- `ticket-1dae6a3e` — 5 tests used a hardcoded fake commit SHA
  (`abc123def456`); the adversarial-review hardening of `_link_commit_impl`
  now rejects nonexistent SHAs by design. Fixed by using a real
  `git rev-parse HEAD` SHA, and by fixing two tests' `run_in_executor` mocks
  to distinguish the new cat-file existence-check call from the
  pre-existing stats-fetch call.
- `ticket-a00a413e` — 26 pre-existing failures verified by QA to also fail
  on the unmodified main baseline. Fixed 22 of 26:
  - 11 git-worktree/branch-cleanup tests: `scripts/agent-safe-bin/git`, the
    CLI-agent safety wrapper active during test runs, blocks `git merge` on
    protected branches unless a `.hephaestus/review_approved` marker is an
    ancestor of `cwd`. Added the marker to each affected fixture's own
    throwaway repo.
  - `test_agent_output_capture.py` (3): `Mock(spec=Agent)` didn't set
    `pending_message_sent_at`, and `terminator.py`'s grace-period logic
    does `datetime.utcnow() - agent.pending_message_sent_at` — a real
    `TypeError` against the Mock's auto-generated attribute. Fixed the
    mocks.
  - `test_restart_agent_characterization.py` (1): a raw-SQL table
    recreation helper was missing the `working_directory` column added to
    `Agent` since the helper was written (16 vs 17 columns).
  - `test_resume_interrupted_workflows.py` (2): one was the git-wrapper
    issue above; the other was a real bug — `src/mcp/server/lifecycle.py:204`
    read `config.base_branch`, which doesn't exist (`Config` only exposes
    `config.git.base_branch`). Fixed.
  - `test_heal_orphaned_agent_branches.py` (1): C8 changed
    `heal_orphaned_agent_branches` to enumerate `ProjectRepo` rows instead
    of `AutopilotProject.base_dir`; this test's bare `DatabaseManager`
    fixture never runs the migration that backfills one. Registered a
    `ProjectRepo` row explicitly.
  - `test_autopilot_api.py::test_list_features` (1): same
    `server_state.db_manager` wiring gap as ticket-36213.
  - Left unfixed: 4 tests in
    `tests/integration/test_task_deduplication_flow.py` — traced to the
    test's `client` fixture never entering `TestClient` as a context
    manager, which affects whether the ASGI lifespan/background event loop
    persists long enough for `spawn_background_task`'s fire-and-forget
    `asyncio.create_task` calls to run before the test asserts on them. A
    deep TestClient/anyio lifecycle issue in test-only code, unrelated to
    this feature, and confirmed by QA to fail identically on the
    unmodified main baseline.
- Additionally found and fixed, during full-suite verification (not in the
  original QA report — order-dependent, invisible to a single-file run):
  `test_design_status_derivation.py`'s 11 tests all 401 in a full-suite run
  because `design_file_routes.py` name-imports
  `verify_agent_authentication`; patching `_shared`'s copy only works if
  this test file is the very first thing in the session to import
  `design_file_routes`. Patched the actual binding site instead, verified
  order-independent by running after both `test_autopilot_api.py` and
  `test_mcp_server_tickets.py`.

### Final Test Results

Full suite (`pytest tests/ -q`): 3435 passed, 51 skipped, 4 known failed
(the documented `test_task_deduplication_flow.py` pre-existing issue,
tracked separately from this feature).

## Review Bounce — Unmet Requirements Fixed (this pass)

Re-entered development after the architectural reviewer found that several
C1-C10 pieces existed but were never actually wired to production call
sites, so their behavior never reached agents/users despite the code
technically existing:

- `REQ-09`/`REQ-17` — `AgentDispatchService.resolve_task_project_context`
  (repo-aware wrapper around `get_project_context`) had zero callers, and
  the real dispatch site (`agent_dispatch_service.py:70`) called
  `get_project_context()` bare, so the multi-repo section never reached an
  agent prompt in production. Fixed by adding an optional `task` param to
  `build_dispatch_context` that routes through
  `resolve_task_project_context` when given, and threading `task=`
  through all 4 real call sites (`task_admin_routes.py` x2,
  `background_loops.py`, `lifecycle.py`). Also wired `workflow_id` into
  the enrichment-time path (`TaskEnrichmentService.enrich` /
  `gather_dispatch_context`) so the plain repo list (architect mode)
  reaches the prompt even before a task exists to resolve `repo_id`
  against. Stash-verified new tests in `test_agent_dispatch_service.py`.
- `REQ-18` — writable-vs-read-only repo distinction: same root cause/fix
  as REQ-17 (it rides through `resolve_task_project_context`, which
  already implemented the WRITABLE/read-only split correctly — it just
  had no caller).
- `REQ-16` — `policy.py`'s `_resolve_recovery_project_path` fell back to
  a single process-wide `$PROJECT_PATH` env var when a workflow had no
  `working_directory`, ignoring which project/repo the workflow actually
  belonged to — a real risk of `git reset --hard`/`git clean -fd` running
  against an unrelated repo. Fixed to resolve via the workflow's
  `project_id` + primary `ProjectRepo` first, env var only as the true
  last resort (no project association at all). `terminator.py`'s
  `_commit_wip_in_shared_worktree` assumed the workflow's single
  `working_directory` was always the right repo for a task's WIP commit;
  now checks the task's own `repo_id` (when set) against the resolved
  repo path and skips the commit (logging why) on a mismatch, rather than
  silently committing into what could be the wrong git tree. Both
  stash-verified in new `test_policy_recovery_repo_awareness.py` /
  `test_terminator_repo_awareness.py`.
- `REQ-23` — `CommitDiffResponse` had no `repo_label` field, so
  `GitDiffModal`'s repo-badge render branch (already present in the
  frontend) was fed a field the backend never populated. Added
  `repo_label: Optional[str]`, populated via `_resolve_repo_info_for_commit`
  (already resolved path+label, previously only the path half was used).
  Stash-verified in `test_commit_resolution_multi_repo.py`.
- `REQ-24` — `ProjectSettingsModal` had no add/label-repo form, so
  `apiService.addProjectRepo()` (already implemented) had zero call
  sites. Added an inline add-repo form (label + path inputs) per project
  row, wired to a new `addRepoMutation`. No frontend test harness exists
  in this repo (no `test`/`vitest`/`jest` script, no existing test
  files) — verified via `npm run type-check` (clean) instead of a
  stash-verified test.

## Code Quality

- `ruff check` / `ruff format --check`: clean on every file touched this
  pass (pre-existing lint debt in files outside this pass's scope was left
  alone per minimal-touch policy, and verified unchanged by this pass).
- `mypy`: no new errors introduced. QA-bounce pass: `feature_review_routes.py`
  and `lifecycle.py` have the same pre-existing error counts before/after.
  Review-bounce pass (REQ-09/16/17/18/23/24): diffed all 9 touched source
  files' mypy output against this pass's own starting commit — identical
  error messages, only line numbers shifted from the inserted code (one
  new `no-any-return` did appear from a new `return repo.path` line in
  `policy.py`; fixed with an explicit `str()` cast rather than leaving it
  as new debt).
