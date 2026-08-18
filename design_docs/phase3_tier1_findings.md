# Phase 3, Tier 1 — live correctness bugs findings

## Which items needed work vs. which didn't

Per this item's own prompt doc, the freshness check found four of the plan's eight Tier 1 items already resolved before this pass started:

| Item | Status | Note |
|---|---|---|
| 1 (validator-outcome race) | **Already fixed** | `submit_result_validation` (`src/mcp/memory_api.py:834`) already scopes by `workflow_id`, inline comment intact. No work needed. |
| 2 (`orphan_reaper.py` `datetime.now()`) | **2/3 fixed → now 3/3** | One instance remained (`:139`), fixed this pass. |
| 3 (`task_similarity_service.py` `is None`) | **Fixed this pass** | |
| 4 (`queue_service.py` `priority_boosted`) | **Fixed this pass** | Scope corrected: 2 real sites, not the plan's cited 3 — see below. |
| 5 (missing `terminated_at`) | **Already fixed** | Via §4.2's termination primitive, this session. |
| 6 (`monitor.py` missing `current_task_id` clear) | **Already fixed** | Via §4.2, this session. |
| 7 (`get_agent_branch_path` silent fallback) | **Fixed this pass** | |
| 8 (`review_feature` hand-rolled completion check) | **Already fixed** | Routes through `derive_workflow_status`, inline comment intact. No work needed. |

Four items required real work this pass: 2 (remainder), 3, 4, 7. Each below, in that order.

## Item 2 — `orphan_reaper.py:139`, `datetime.now()` → `datetime.utcnow()`

One-line fix. `current_time` feeds `self.last_check_time`'s grace-period comparison for orphaned tmux sessions — the other clock in the same method (`last_activity`'s 30s grace window, a few lines earlier) already used `utcnow()`, so this was the one remaining inconsistency, not a new bug class.

**A real gap surfaced by fixing this, not by the fix itself**: `tests/test_orphan_reaper.py` set `reaper.last_check_time` (and, pre-existing and unrelated, `mock_agent.last_activity`) via local `datetime.now() - timedelta(...)`. On this machine (MDT, UTC-6), those tests were passing *only* because the source used to use local time too — bug matching bug. Fixing the source to `utcnow()` immediately broke `test_grace_period_protects_new_sessions` (confirmed by running it red before touching the test file). Fixed all six `last_check_time = datetime.now()` call sites in the test file to `datetime.utcnow()`, matching the now-corrected source. (`last_activity`'s equivalent test-side mismatch is a separate, pre-existing issue — see "Found, out of scope" below; it wasn't touched, since it was already utcnow()-compared in production before this pass and none of the affected tests' assertions depend on getting the magnitude right, only the sign of "is this a long time ago.")

Added `test_grace_period_uses_utc_not_local_time`: monkeypatches `orphan_reaper`'s `datetime` with a fake whose `.now()` and `.utcnow()` return wildly different values (26 years apart, not just an hours-scale offset), and confirms the grace-period decision follows `.utcnow()`. This is a direct proof the source uses the right clock, not just a same-machine coincidence check.

## Item 3 — `task_similarity_service.py:71`, `Task.phase_id is None` → `.is_(None)`

One-line fix, exactly as scoped. Added `TestPhaselessDuplicateCheckUsesRealQuery`, a new test class in `tests/unit/test_task_similarity_service.py` using a *real* SQLite session (the file's existing `TestTaskSimilarityService` class mocks `.filter()` to return itself regardless of argument — structurally incapable of distinguishing a real predicate from the pre-fix always-`False` one). `test_finds_duplicate_among_phaseless_tasks` confirmed red pre-fix (inserted an exact-duplicate phase-less task, pre-fix code found zero duplicates; post-fix, finds it), `test_ignores_phased_tasks_when_checking_phaseless` confirms a phased task never leaks into a phase-less check.

## Item 4 — `queue_service.py:380,390`, `not Task.priority_boosted` → `Task.priority_boosted.is_(False)`

**Scope correction from the plan, confirmed by direct read**: the plan cites three sites (374, 380, 390); only 380 and 390 are the actual bug. Line 374's `not new_task.priority_boosted` negates a real Python instance attribute (an actual bool), which is correct — left untouched.

**A second scope correction, more consequential**: `_calculate_queue_position` (the method these lines live in) has **zero callers anywhere in `src/`** — grepped the whole tree. The live queue-ordering code path is a *different* method, `_recalculate_queue_positions` (`queue_service.py:537`, called from `enqueue_task`/`dequeue_task`/`boost_task_priority`/`task_blocking_service.py`), which uses `.order_by(Task.priority_boosted.desc(), priority_order.desc(), Task.queued_at.asc())` — correctly, no such bug. So this item's actual current-runtime impact is zero: the plan's own caution ("practical impact of the dead branches isn't yet confirmed either way") understates it — the true answer is "none, because nothing calls this method today," not "unconfirmed." Flagged below as a Phase 4 (dead-code) candidate rather than deleted here, per this item's own instruction not to touch Phase 4 territory.

**The bug itself, confirmed by inspecting compiled SQL directly** (not just reasoning about it): `not Task.priority_boosted` is a Python `not` on a SQLAlchemy `InstrumentedAttribute` — always truthy as a Python object, so `not` always evaluates to the literal `False` before SQLAlchemy ever sees it. `and_(literal False, ...)` compiles to a clause that's always false in SQL. For a non-boosted new task (the docstring's own stated common case — "priority_boosted should not exist for new tasks"), this collapsed the *entire* priority-level and queued_at tie-break `or_()` down to nothing but `Task.priority_boosted` — i.e., `ahead_count` counted only existing boosted tasks and ignored priority level and queue order completely, for every ordinary (non-boosted) new task. Verified via `expr.compile(compile_kwargs={"literal_binds": True})`: `tasks.priority_boosted OR (false OR false) AND tasks.priority = 'high'`.

Per the plan's explicit instruction, wrote the characterization test **before** fixing: `TestCalculateQueuePosition::test_higher_priority_task_counts_as_ahead_for_non_boosted_new_task` (confirmed failing red pre-fix — asserted a high-priority existing task counts as ahead of a new medium-priority one; pre-fix, position came back 1, not 2, since the priority comparison was unreachable) and `test_boosted_existing_task_still_counts_as_ahead` (confirmed passing both before and after — proof the one sub-clause that *wasn't* dead, `and_(Task.priority_boosted, new_task.priority_boosted)`'s sibling `and_(Task.priority_boosted, not new_task.priority_boosted)` at the very first `or_()` branch, kept working and the fix didn't regress it).

## Item 7 — `worktree_manager.py:774-782`, silent main-repo fallback → `None`

Changed the no-`AgentBranch`-record fallback from `str(self._project_root)` to `None`. Traced every caller (`grep -rn "get_agent_branch_path"`, 3 production call sites): `launch_pipeline.py:520` (`if candidate and Path(candidate).exists(): branch_path = candidate`), `agents_api.py:354` (bare try/except to `None`, then `if not worktree_path: raise HTTPException(404, ...)`), `validator_agent.py:83-85` (`... or "/tmp"`). **All three already treat a falsy return as "no path found" correctly** — meaning all three were silently accepting the main repo as a valid worktree path before this fix (the exact bug the plan describes: `restart_agent` "can silently relaunch into the main project repository"), and are now correctly protected without any caller-side change. No caller needed a matching fix.

Added `test_get_agent_branch_path_returns_none_when_no_record` (confirmed red pre-fix via `git stash`) and a companion `test_get_agent_branch_path_returns_real_path_when_record_exists` proving the working case wasn't broken by the fix.

**Gap-check addendum, per-caller test coverage**: this item's own prompt doc required, for every caller, "either an existing test already covers the caller's None-handling correctly, or a new test is added at the caller level." The first pass verified all three callers' None-handling by reading the code, but only checked test coverage loosely (ran the broader suites and saw green). Rereading the prompt doc caught this as under-verified and checked directly:
- `launch_pipeline.py`'s caller: **already covered** — `tests/test_restart_agent_characterization.py` mocks `get_agent_branch_path` returning `None` at 3 separate scenarios, one of them explicitly documented as "the silent-None behavior the doc says to preserve." No new test needed.
- `agents_api.py`'s caller (`get_task_instructions`, the `GET /api/tasks/{task_id}/instructions` route): **zero existing test coverage of any kind** — no test file for `agents_api.py` exists at all. Added `tests/test_agents_api_task_instructions.py` (2 tests: the 404-when-unresolvable case, and a companion confirming the normal resolved-path case still works).
- `validator_agent.py`'s caller (`spawn_validator_agent`'s `or "/tmp"`): the one existing test (`test_spawn_validator_agent`) mocks `get_agent_branch_path` to a **truthy** path — it never exercised the fallback at all. Added `test_spawn_validator_agent_falls_back_to_tmp_when_no_branch_path`. Writing it surfaced a second, deeper pre-existing defect in the same test class: `mock_db_manager`'s fixture uses a bare `session = Mock()` with `.query()` never actually stubbed, so `spawn_validator_agent`'s own `task = session.query(Task).filter_by(id=target_id).first()` silently receives a fresh, unconfigured `Mock()` instead of the test's carefully-constructed `task` object — whose auto-generated `.phase_id` is itself a truthy `Mock`, which is *why* `test_spawn_validator_agent` hits the pre-existing `check_phase_sibling_active`/`TypeError: 'Mock' object is not subscriptable` failure already confirmed unrelated to this item (see Verification, above) — not a fluke, a structural gap in the fixture. Fixed this within the new test only (a local `session.query.side_effect` wiring `Task` queries to the constructed `task` object) — did not touch the shared fixture or the neighboring pre-existing broken test, since fixing those is unrelated to Phase 3 Tier 1 item 7 and outside this pass's scope.

## Verification

- Item-by-item red/green regression tests, each independently confirmed to fail against pre-fix code via `git stash push --keep-index -- <file>` isolation (not just written and assumed correct).
- `ruff check` clean on every touched `src/` and `tests/` file, including the two added during the gap-check pass (`tests/test_agents_api_task_instructions.py`, and the edits to `tests/test_validation_system.py`). Two pre-existing findings noted, both confirmed unchanged from HEAD via `git show HEAD:<file> | ruff check -`: `tests/test_queue_service.py:37` and `tests/test_validation_system.py:203` (now `:291` after this pass's insertions), both the same `N806` on a fixture's `Session` variable name.
- Broader regression run across all four touched files' test suites plus every file with a production caller of the fixed functions (`tests/test_task_completion_service.py`, `tests/test_restart_agent_characterization.py`, `tests/test_validation_system.py`, plus the gap-check pass's new `tests/test_agents_api_task_instructions.py`): 162 passed, 1 failed pre-gap-check; 164 passed (both new tests), 1 failed post-gap-check. The one persistent failure (`test_validation_system.py::TestValidatorAgent::test_spawn_validator_agent`, a `TypeError: 'Mock' object is not subscriptable` inside `check_phase_sibling_active`'s sibling-task-warning branch) is unrelated to any of this pass's four fixes — confirmed via `git stash push --keep-index` on all four touched source files simultaneously, rerunning in isolation: fails identically with the fixes fully reverted. Pre-existing, not a regression from this item. Root cause identified during the gap-check pass (see item 7's addendum below): the neighboring test's own `session` mock never stubs `.query()`, so `spawn_validator_agent`'s `task` variable silently becomes an auto-generated Mock whose `.phase_id` is truthy.

## Found, out of scope — flagged, not fixed

- **`_calculate_queue_position` has zero callers** (item 4, above) — a genuine Phase 4 (delete dead code) candidate, not touched here per this item's own explicit scope boundary.
- **`tests/test_orphan_reaper.py`'s `mock_agent.last_activity = datetime.now() - timedelta(...)` sites** (lines using local time for a field the *production* code already compared against `utcnow()` before this pass) — a pre-existing test/production clock mismatch, currently masked because the affected tests only need "this is a long time ago" to hold, which is true regardless of the sign error at typical local-UTC offsets. Not touched — outside item 2's stated scope (which was specifically about `current_time`/`last_check_time`, not `last_activity`), and touching it isn't needed for this pass's own regression coverage to be sound.
- **`tests/test_validation_system.py::TestValidatorAgent`'s `mock_db_manager` fixture never stubs `session.query(...)`** (found during the gap-check pass, item 7's addendum) — every test in that class that relies on `spawn_validator_agent` reading back its own constructed `task`/`phase` objects via a DB query is silently exercising a fresh auto-generated `Mock()` instead, not the object the test built. This is the direct cause of `test_spawn_validator_agent`'s pre-existing failure (not touched, per this item's scope) and was fixed locally, inside only the new test added this pass — the shared fixture itself would need updating for every other test in the class to actually test what it claims to, but that's a pre-existing test-quality issue unrelated to Phase 3 Tier 1 item 7, not fixed here.

## Explicitly out of scope

- Items 1, 5, 6, 8 — confirmed already fixed, not touched.
- Tier 2 and Tier 3 of Phase 3 (items 9-28).
- Phase 4 (dead code deletion) — see `_calculate_queue_position` above.

No commits — left in the working tree for review.
