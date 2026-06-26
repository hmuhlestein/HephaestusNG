# Test Failure Report — 164 failed, 15 errors (605 passing)

> Run: `pytest tests/ -p no:libtmux --tb=no -q` — 780s, 2026-06-25

---

## Group 1 — `worktree_path` → `branch_path` rename (method signature mismatch)

**Root cause:** `AgentManager._format_initial_message()` uses `branch_path` param; tests still pass `worktree_path=`. Also `terminate_agent(merge_work=)` and `create_agent_for_task(parent_agent_id=)` kwargs removed.

| File | Count |
|---|---|
| `tests/test_agent_workflow_context.py` | 2 remaining (16 already fixed this session) |
| `tests/test_validation_prompts.py` | 4 |
| `tests/test_worktree_integration.py` | 3 |

---

## Group 2 — `bcrypt` + `passlib` version mismatch

**Root cause:** `passlib 1.7.4` with `bcrypt 4.x`: test password is >72 bytes; `bcrypt 4.x` raises `ValueError` instead of silently truncating. Also `module 'bcrypt' has no attribute '__about__'`.

| File | Count |
|---|---|
| `tests/test_authentication.py` — `TestPasswordHashing` | 4 |
| `tests/test_authentication.py` — `TestAuthenticationAPI` | 8 |

---

## Group 3 — Missing `autopilot_projects` table in test DB

**Root cause:** Test fixture mocks `get_config().database_path`; the returned mock object is used as an SQLite path, creating a fresh DB with no migrations. Tables `autopilot_projects` (and related) never exist.

| File | Count |
|---|---|
| `tests/test_autopilot_api.py` — `TestDesignQueue`, `TestCaching`, `TestFeatures`, `TestPipelineStatus`, `TestPathTraversal` | 13 |

---

## Group 4 — Async mocks need `AsyncMock` (not `Mock`)

**Root cause:** Tests mock async methods with plain `Mock`; `await mock()` raises `TypeError: object Mock can't be used in 'await' expression`. Subgroup: `asyncio.coroutine` decorator removed in Python 3.12.

| File | Count | Extra notes |
|---|---|---|
| `tests/test_conductor.py` | 7 | `'coroutine' object has no attribute 'upper'`; report title changed "GPT-5" → "LLM" |
| `tests/test_guardian.py` | 4 | `KeyError: 'summary'`; `_check_trajectory_alignment` method removed |
| `tests/test_monitoring_integration.py` | 7 | `Mock` not iterable for tmux session list |
| `tests/test_trajectory_monitoring.py` | 5 | `KeyError: 'progress_percentage'`; `_calculate_work_similarity` removed |
| `tests/test_diagnostic_integration.py` | 1 | |
| `tests/test_steering_fix.py` | 3 | |
| `tests/test_validation_agent_protection.py` | 1 | |
| `tests/test_validation_inheritance.py` | 1 | `asyncio.coroutine` removed in Python 3.12 |
| `tests/test_result_submission_flow.py` | 1 | |

---

## Group 5 — `TrajectoryContext` API renamed

**Root cause:** Method and key names changed in the implementation; tests use the old names.

| Old name (test expects) | New name (actual) |
|---|---|
| `_extract_lifted_constraints` | `_extract_persistent_constraints` |
| `_identify_current_focus` | `_determine_current_focus` |
| `_extract_discovered_blockers` | `_find_discovered_blockers` |
| `_extract_constraints` | *(removed?)* |
| `clear_agent_cache` | *(removed?)* |
| `task["done_definition"]` key | *(key removed from dict)* |
| `context["session_start"]` key | *(key renamed)* |
| `result["progress_percentage"]` | *(key removed)* |

| File | Count |
|---|---|
| `tests/test_trajectory_context.py` | 12 |
| `tests/test_trajectory_monitoring.py` | 5 (shared with Group 4) |

---

## Group 6 — `Config.worktree_retention_hours` renamed

**Root cause:** `Config` now exposes `branch_retention_hours`; tests reference old attribute `worktree_retention_hours`.

| File | Count |
|---|---|
| `tests/test_worktree_integration.py` | 3 (test_cleanup_policies, test_disk_usage_tracking, test_child_merge_with_active_parent_worktree) |

---

## Group 7 — `ValidationSystem` API changed

**Root cause:** `build_validator_prompt()` requires new `validator_agent_id` positional arg; `spawn_validator_agent()` no longer accepts `task_id=` kwarg.

| File | Count |
|---|---|
| `tests/test_validation_system.py` | 2 |
| `tests/test_validation_inheritance.py` | 1 (also `asyncio.coroutine` removed — see Group 4) |

---

## Group 8 — Embedding / vector store (`Can't instantiate abstract class`)

**Root cause:** `TypeError: Can't instantiate abstract class` — an abstract base class changed its interface; a concrete subclass in the test or production code is missing a required method implementation.

| File | Count |
|---|---|
| `tests/test_llm_interface.py` | 5 |
| `tests/test_rag_system.py` | 2 |
| `tests/test_vector_store.py` | 1 |
| `tests/unit/test_embedding_service.py` | 2 |
| `tests/unit/test_task_similarity_service.py` | 9 |
| `tests/integration/test_qdrant_mcp_integration.py` | 2 |
| `tests/integration/test_task_deduplication_flow.py` | 7 |

---

## Group 9 — Agent output capture (tmux API changed)

**Root cause:** Capture-pane call signature changed — tests expect `cmd("capture-pane", "-p", "-S", "-10000")` and `cmd("capture-pane", "-p", "-S -200")` (single arg with space), but code now calls `cmd("capture-pane", "-p", "-S", "-1000")` and `cmd("capture-pane", "-p", "-S", "-")` (separate args). Also `output_lines` key removed from log details; fallback string not returned on empty output.

| File | Count |
|---|---|
| `tests/test_agent_output_capture.py` | 5 |
| `tests/test_agent_output_integration.py` | 2 ERROR (fixture setup failure) |

---

## Group 10 — `workflow_id` now required in task creation

**Root cause:** MCP `create_task` endpoint now requires `workflow_id` in the request body; tests omit it and expect success.

| File | Count |
|---|---|
| `tests/test_ticket_id_validation.py` | 2 |
| `tests/test_ticket_id_validation_simple.py` | 3 |
| `tests/test_mcp_results_endpoint.py` | 9 |
| `tests/test_mcp_server_tickets.py` | 13 ERROR (fixture setup fails before tests run) |

---

## Group 11 — `multi_workflow` DB fixture missing tables

**Root cause:** Same pattern as Group 3 — fresh SQLite with no migrations applied; `phases`, `workflows`, and related tables don't exist.

| File | Count |
|---|---|
| `tests/test_multi_workflow.py` | 10 |
| `tests/test_multi_workflow_e2e.py` | 8 |

---

## Group 12 — `TurboVecStore` persistence API changed

**Root cause:** `test_persistence_survives_reload` and flush/delete tests expect old persistence format or method signatures that no longer match the current `TurboVecStore` implementation.

| File | Count |
|---|---|
| `tests/test_turbovec_fastembed.py` | 4 |

---

## Group 13 — Miscellaneous single-cause failures

| File | Error | Count |
|---|---|---|
| `tests/test_ticket_system.py::test_get_ticket_full_details` | `KeyError: 'ticket_id'` — key renamed in ticket detail dict | 1 |
| `tests/test_prompt_delivery.py` | Prompt delivery API changed (retry/chunking signatures) | 7 |
| `tests/test_prompt_delivery_cleanup.py` | DB error handling path changed | 1 |
| `tests/test_prompt_loader.py` | Guardian prompt truncation format changed | 1 |
| `tests/test_queue_service.py` | Active agent count query changed | 1 |

---

## Summary

| Group | Root cause | Failures |
|---|---|---|
| 1 | `worktree_path`/`branch_path` rename + removed kwargs | 9 |
| 2 | `passlib` + `bcrypt 4.x` incompatibility | 12 |
| 3 | Test DB missing migrations (`autopilot_projects`) | 13 |
| 4 | Plain `Mock` where `AsyncMock` needed; `asyncio.coroutine` removed | 25 |
| 5 | `TrajectoryContext` method/key renames | 12 |
| 6 | `Config.worktree_retention_hours` → `branch_retention_hours` | 3 |
| 7 | `ValidationSystem` signature changes | 3 |
| 8 | Abstract base class interface change (embedding/vector) | 28 |
| 9 | tmux capture-pane arg format changed | 7 |
| 10 | `workflow_id` now required in task creation | 27 |
| 11 | `multi_workflow` DB fixture missing migrations | 18 |
| 12 | `TurboVecStore` persistence API changed | 4 |
| 13 | Miscellaneous single-file regressions | 11 |
| **Total** | | **164 FAILED + 15 ERROR** |
