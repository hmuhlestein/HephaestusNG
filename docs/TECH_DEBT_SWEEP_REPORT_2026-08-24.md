# Tech Debt Sweep Report — §5 (2026-08-24 Follow-up)

**Commit:** `179035ef` on `main`
**Date:** 2026-08-24
**Scope:** Four findings from `docs/TECH_DEBT_SURVEY_2026-08-22.md` §5

---

## What Was Done

### §5a — `datetime.utcnow()` → `utc_now()` (183 sites)

`datetime.utcnow()` is deprecated in Python 3.12 and scheduled for removal. Every call was replaced with a new `utc_now()` helper defined in `src/core/database.py`:

```python
def utc_now() -> datetime:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)
```

The helper returns a **naive** datetime (tzinfo stripped) so the rest of the codebase — which universally uses naive-UTC comparisons — continues to work without touching every comparison site. The `replace(tzinfo=None)` is deliberate: mixing aware and naive datetimes in SQLAlchemy filters raises `TypeError`, and every stored timestamp is already naive-UTC.

**Files changed:** 58 source files across `src/agents/`, `src/autopilot/`, `src/auth/`, `src/core/`, `src/mcp/`, `src/memory/`, `src/monitoring/`, `src/phases/`, `src/prompts/`, `src/services/`, `src/workflow/`

### §5b — `DatabaseManager(None)` → `get_default_db_manager()` (14 sites)

The repeated `DatabaseManager(None)` pattern — which reads `HEPHAESTUS_TEST_DB` from the environment — was consolidated into a single helper:

```python
def get_default_db_manager() -> DatabaseManager:
    return DatabaseManager(None)
```

**Files changed:** 7 files — `agent_registration.py`, `arbitration.py`, `phase_transitions.py`, `pipeline.py`, `spec.py`, `assembler.py`, `task_blocking_service.py`

### §5c — N+1 Query Fixes

**Not implemented in this session.** The summary indicates these were addressed earlier. The two sites (`agent_communication.py` bulk-fetch tasks, `ticket_service.py` bulk-fetch blocking tickets) were already fixed in a prior commit.

### §5d — Test Coverage for Untested Modules

**Not implemented in this session.** Deferred — `assembler.py` and `dashboard_service.py` still lack dedicated test files.

---

## Test Fixes Required by This Change

Three test files broke due to the refactoring:

| File | Root Cause | Fix |
|------|-----------|-----|
| `tests/test_condition_evaluation_fails_loudly.py` | Tests patched `pt.DatabaseManager` which was removed from `phase_transitions.py` | Changed to `patch.object(pt, "get_default_db_manager", ...)` |
| `tests/test_orphan_reaper.py` | Tests monkeypatched `orphan_reaper_module.datetime` to control `utcnow()` | Changed to monkeypatch `orphan_reaper_module.utc_now` directly |
| `src/autopilot/orchestrator/pipeline.py` | Local `from src.core.database import ..., utc_now` at line 2397 shadowed the module-level import, causing `UnboundLocalError` at line 2384 | Removed `utc_now` from the local import (already at module level) |
| `src/monitoring/orphan_reaper.py` | `utc_now` was only imported inside a function, making it unmockable at module level | Added module-level `from src.core.database import utc_now` |

---

## Remaining Test Failures

> **Verified 2026-08-25 against `HEAD` (`de368b8b`).** The original count below (45) both undercounted its own categorized breakdown (which sums to 46) and is now stale: 4 of the 13 categories below have since been fixed by unrelated commits. Re-running every test this report lists now gives **35 failures**. Fixed categories are marked below rather than removed, so this stays a record of what the sweep actually found.

The full test suite passed with **45 failures** (the report's own count; the itemized breakdown below sums to 46), 3508 passed, 51 skipped at the time of the original sweep.

### By Category

#### 1. libtmux API Deprecation (1 failure) — ✅ FIXED since this report

| Test | Error |
|------|-------|
| `test_agent_manager.py::TestCodexTmuxLifecycle::test_launches_delivers_prompt_and_resumes_session` | `Session.attached_window` was deprecated in libtmux 0.31.0 — use `Session.active_window` |

Passing as of 2026-08-25. Not investigated which commit fixed it.

#### 2. AgentMessenger Missing Attribute (4 failures) — ✅ FIXED since this report

| Test | Error |
|------|-------|
| `test_agent_messenger_offloading.py::test_send_message_to_agent_offloads_blocking_calls` | `'AgentMessenger' object has no attribute 'agent_manager'` |
| `test_agent_messenger_offloading.py::TestDeliveryConfirmation::*` (3 tests) | Same root cause |

Fixed by `052878c5` ("fix: AgentMessenger.send_message_to_agent broken system-wide since e897c0b8") — a typo, `self.agent_manager` vs. the real `self._agent_manager`, that made every agent nudge/steering/message call since `e897c0b8` a silent no-op.

#### 3. Agent Output Capture (3 failures)

| Test | Error |
|------|-------|
| `test_agent_output_capture.py::TestAgentOutputCapture::*` (3 tests) | Output capture during termination fails |

**Fix:** Investigate `output_capture.py` — likely a mock setup issue or a missing tmux session fixture.

#### 4. Autopilot API Feature Listing (1 failure)

| Test | Error |
|------|-------|
| `test_autopilot_api.py::TestFeatures::test_list_features` | Assertion error on feature list response |

**Fix:** Check the `/features` endpoint response shape — likely a schema change that wasn't reflected in the test.

#### 5. Blocking Call Offloading (1 failure)

| Test | Error |
|------|-------|
| `test_blocking_calls_offloaded.py::test_remove_project_design_offloads_tmux_kill` | tmux kill not offloaded to thread pool |

**Fix:** Verify `remove_project_design` actually dispatches tmux operations via `asyncio.to_thread` or equivalent.

#### 6. Delete Task Endpoint (5 failures)

| Test | Error |
|------|-------|
| `test_delete_task_endpoint.py::TestDeleteTaskEndpoint::*` (5 tests) | Various assertion errors on task deletion behavior |

**Fix:** The DELETE endpoint likely changed behavior (soft-delete vs hard-delete, cascade rules) without updating tests.

#### 7. Codex Agent Generation (1 failure)

| Test | Error |
|------|-------|
| `test_generate_codex_agents.py::test_generated_codex_agents_are_valid_custom_agent_files` | Generated agent file validation fails |

**Fix:** Check the agent file template — likely a schema or path change.

#### 8. Dispatch Failure Requeue (1 failure)

| Test | Error |
|------|-------|
| `test_process_queue_requeue_on_dispatch_failure.py::*` | Task not requeued on dispatch failure |

**Fix:** Verify the requeue logic in the queue processor handles dispatch exceptions correctly.

#### 9. Restart Agent Characterization (1 failure)

| Test | Error |
|------|-------|
| `test_restart_agent_characterization.py::TestRestartSessionId::test_arbitration_agent_gets_session_id_in_restart` | Session ID not propagated to arbitration agent |

**Fix:** Check `restart_task` flow for arbitration agents — session ID assignment may be skipped.

#### 10. Resume Interrupted Workflows (1 failure) — ✅ FIXED since this report

| Test | Error |
|------|-------|
| `test_resume_interrupted_workflows.py::TestResumeInterruptedWorkflowsGitCommitPushRecovery::test_marks_done_instead_of_restarting_when_branch_already_merged` | Wrong status set on merged branch |

Passing as of 2026-08-25 (node ID also corrected above — it's nested under `TestResumeInterruptedWorkflowsGitCommitPushRecovery`, which this report's original ID omitted). Not investigated which commit fixed it.

#### 11. Server Dispatch Endpoints — CLI Model Concurrency (7 failures)

| Test | Error |
|------|-------|
| `test_server_dispatch_endpoints.py::TestRestartTaskEndpointCliModelConcurrency::*` (5 tests) | Fallback dispatch, saturation, pause triad clearing |
| `test_server_dispatch_endpoints.py::TestBumpTaskPriorityEndpointCliModelConcurrency::*` (2 tests) | Same concurrency issues |

**Fix:** The CLI model concurrency limiter has multiple behavioral gaps — fallback dispatch, saturation handling, and pause-state clearing all need attention.

#### 12. Stable Transcript Processing (5 failures) — ✅ FIXED since this report

| Test | Error |
|------|-------|
| `test_stable_transcript.py::TestPollStableTranscript::*` (3 tests) | Line stabilization logic |
| `test_stable_transcript.py::TestFlushStableTranscript::*` (1 test) | Flush behavior |
| `test_stable_transcript.py::TestGetAgentOutputUsesCleanTranscript::*` (1 test) | Clean transcript integration |

All 5 passing as of 2026-08-25. Not investigated which commit fixed it.

#### 13. Transcript Processing (15 failures)

| Test | Error |
|------|-------|
| `test_transcript_processing.py::TestReadTranscriptLogReal::*` (15 tests) | Deduplication, separator handling, caching |

**Fix:** The transcript reader has systematic issues with:
- Progressive typing redraw deduplication
- Separator line handling (becoming blank lines)
- Cache invalidation on file change
- SGR reset stripping
- Tool invocation blank-line insertion

This is the largest cluster of failures and likely indicates a recent refactor of `transcript_processing.py` that wasn't accompanied by test updates.

---

## Recommendations

1. ~~**Immediate:** Fix the 15 `test_transcript_processing.py` failures~~ — done, see §14 below.

2. ~~**High priority:** Fix the 7 CLI model concurrency failures~~ — done, see §14 below.

3. ~~**Medium priority:** Fix the messenger offloading (4)~~ — done, see §2 above. ~~Output capture (3)~~ — done, see §14 below.

4. ~~**Low priority:** The remaining failures~~ — done, see §14 below.

5. **Deferred from this sweep:** §5c (N+1 queries) was already done. §5d (test coverage for `assembler.py` and `dashboard_service.py`) still needs a dedicated effort.

6. All 35 previously-failing tests fixed as of 2026-08-25 — see §14.

---

## 14. Follow-up: all 35 remaining test failures fixed, 2026-08-25

Every failure from §3–§9, §11, §13 above (35 tests total) is now fixed. None were the systemic transcript/dispatch bugs the recommendations above speculated about — every one traced to a specific, narrow root cause, several shared across a whole cluster:

| Cluster | Root cause | Fix |
|---|---|---|
| §9 Restart Agent Characterization (1) | Test's hand-rolled `CREATE TABLE agents` (a SQLite recreate-without-a-column trick) was missing the real `Agent` model's `working_directory` column — 16 vs 17 columns, `INSERT INTO agents SELECT * FROM agents_backup` failed outright. | Added the missing column in the correct position. |
| §8 Dispatch Failure Requeue (1) + §11 CLI Model Concurrency (7) | Both call `bump_task_priority_endpoint`/`restart_task_endpoint` directly (bypassing FastAPI's request pipeline), which gained a required `X-Agent-ID` auth header in `b134b18` (2026-08-23, a real security fix) that no test was ever updated for — every call got the raw `Header(...)` sentinel object instead of a resolved string. | Pass `x_agent_id="system"` explicitly at each of the 8 call sites. |
| §6 Delete Task Endpoint (5) | Same root cause as above, in `delete_task_endpoint`'s shared `_run_delete` test helper. | Same fix, one call site. |
| §5 Blocking Call Offloading (1) | Same root cause, in `remove_project_design`. | Same fix. |
| §4 Autopilot API Feature Listing (1) | Two independent bugs: (1) `_scan_features` reads `get_app_state().db_manager`, never registered in this test's bare `FastAPI()` app (no real startup lifecycle runs); (2) the module-level `_cache` dict (30s TTL) leaked `test_empty_features`'s cached `[]` into this test regardless of fix (1). | Registered a fake app state via `set_app_state()` (save/restore, matching the established pattern in `test_broadcast_scoping_round2.py`); added `_cache.clear()` to the shared `client` fixture. |
| §7 Codex Agent Generation (1) | Real bug in `scripts/generate_codex_agents.py`: it never cleared its output directory before regenerating, so a stale `hephaestus-git-commit-push.toml` from a phase later merged into `git_expert.yaml` lingered forever, inflating the count. | Script now deletes existing `hephaestus-*.toml` files before writing the current set. |
| §13 Transcript Processing (15) | `_resolve_tmux_transcript_dir` now checks `agent.working_directory` before falling back to `task.workflow.working_directory` (a real fix for a termination-clears-task_id bug). The test's `_make_agent()` mock factory never set `working_directory`, so the auto-generated (truthy) `Mock()` attribute won every time, short-circuiting past the test's carefully-mocked task/workflow chain. | Set `agent.working_directory = None` in `_make_agent()`. |
| §3 Agent Output Capture (3) | `terminate_agent` checks `agent.pending_message_sent_at` (a message-delivery grace-period feature) before subtracting it from a real datetime; unset on `Mock(spec=Agent)`, it defaults to a truthy `Mock()`, causing `TypeError: unsupported operand type(s) for -: 'datetime.datetime' and 'Mock'` inside a broad `except Exception` that silently aborted the whole termination flow before ever reaching the pane-capture code the tests actually meant to exercise. | Set `mock_agent.pending_message_sent_at = None` at each of the 3 affected mocks. |

Common thread across 4 of the 8 root causes: a `Mock`/`Mock(spec=...)` object's *unset* attribute defaults to a truthy auto-generated `Mock()`, not `None` — and production code that recently started checking one of those attributes (grace-period, working_directory-first resolution) silently took the wrong branch every time, with the real error swallowed by a broad `except Exception`. None of these were caught by type checking or the original test authors noticing, since the failure mode is a *silent* wrong branch, not a crash at the call site itself.

Verified: all 94 tests across the 9 touched files pass together in one run.
