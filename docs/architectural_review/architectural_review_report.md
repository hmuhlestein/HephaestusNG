---
type: architectural_review_result
feature_id: des-91c8-opencode-collector
verdict: PASS
blocker_count: 0
fix_count: 0
defer_count: 1
---

# Architectural Review Report: OpenCode Cost Collector

**Reviewer:** Architect (Phase 5 - post-development re-review)
**Target:** `src/services/cost_collection_service.py` (+ tests)
**Commit Reviewed:** HEAD of `wt_feature-des-91c8-opencode-collector`
**Prior Review Run Result:** 1 BLOCKER (B-1), 1 FIX (F-1), 1 DEFER (D-1)
**This Run Result:** 0 BLOCKER, 0 FIX, 1 DEFER → **PASS**

---

## 1. Prior Findings — Status

| ID | Severity | Title | Status | Evidence |
|----|----------|-------|--------|----------|
| B-1 | BLOCKER | SessionCostCheckpoint key shared across independent OpenCode launches | **FIXED** | `collect_task_cost()` line ~590 now keys OpenCode checkpoint by `opencode_session_row_id` instead of shared `session_id`. Comment block explains the design rationale. `test_shared_hephaestus_session_id_does_not_drop_second_launch` explicitly verifies two launches under a shared session_id each get their own checkpoint. |
| F-1 | FIX | sqlite3 connections leak on the query-exception path in both `OpenCodeCollector.collect()` and `_discover_opencode_session()` | **FIXED** | Both functions now wrap the query in `try/finally: conn.close()`. Tested implicitly by the full 34-test suite passing. |
| D-1 | DEFER | `opencode.db` URI path not percent-encoded for `?`/`#` characters | **STILL DEFERRED** | Theoretical issue with paths containing `?` or `#` in the home directory. Real-world home directories essentially never contain these characters. Deferred as originally classified. |

---

## 2. Architecture Compliance Review

### 2.1 Component Boundaries — ✅ Compliant

The architecture (§1) specifies exactly one module to change (`cost_collection_service.py`) and no schema changes. The diff is confined to:

- `src/services/cost_collection_service.py` — the single permitted file
- `tests/test_cost_collection_service.py` — test file
- No new files, no imports from outside stdlib/sqlalchemy, no schema changes

No boundary violations.

### 2.2 Interface Contracts — ✅ Compliant

**`_discover_opencode_session(cwd, agent_created_at) -> Optional[Tuple[Path, str]]`** (Arch §2.1):
- Signature matches architecture exactly.
- Resolves `~/.local/share/opencode/opencode.db`.
- Path-safety check (verify resolved path under base dir) — implemented and tested.
- Opens read-only with `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`.
- Returns `None` on DB-absent, zero-rows, or `sqlite3.Error`.
- Tie-break: most recent `time_created DESC` — matches architecture §2.1 step 6.
- Multi-match debug logging with discarded IDs — matches architecture.

**`OpenCodeCollector.__init__(session_row_id)`** (Arch §2.2):
- Constructor accepts `session_row_id: Optional[str] = None`.
- `collect()` repurposes `checkpoint` as 0/1 guard, `session_file` as the `opencode.db` path.
- Column mapping: `cost→cost_usd`, `tokens_input→input_tokens`, etc. — matches architecture exactly.
- `model` JSON parsing with fallback to raw string and `None` — matches architecture.
- Returns `([], checkpoint)` when `checkpoint >=1`, `row_id=None`, or `cost <= 0`.
- Never raises out of `collect()` — catches `sqlite3.Error`.

**`collect_task_cost()` OpenCode branch** (Arch §2.3):
- Calls `_get_agent_cwd` + `_discover_opencode_session` — matches architecture.
- Sets both `session_file` and `opencode_session_row_id` on success, `None` on failure.
- Falls through to existing `if not session_file: ... return` guard — no new early-return path.
- `codex` branch and all other branches are untouched.

### 2.3 Data Flow — ✅ Compliant

The data flow (Arch §3) is correctly implemented:
1. Task completes → `collect_task_cost(task_id)` — unchanged call site.
2. `_get_agent_cwd` → cwd string.
3. `_discover_opencode_session` → `(db_path, session_row_id)`.
4. `OpenCodeCollector(session_row_id=id).collect(db_path, checkpoint=0|1)`.
5. `record_cost()` writes CostEntry → triggers rollup chain — unchanged.
6. `SessionCostCheckpoint` keyed by `opencode_session_row_id` (not the shared `session_id`), preventing double-collection.

### 2.4 Naming Conventions — ✅ Compliant

- Private helpers prefixed with `_` (`_discover_opencode_session`).
- Class names follow PascalCase (`OpenCodeCollector`).
- `source="opencode"` as specified.
- Checkpoint field `lines_processed` used (repurposed as 0/1, matching architecture §2.2).

### 2.5 Read-Only DB Access — ✅ Compliant

Both `_discover_opencode_session()` and `OpenCodeCollector.collect()` open with `sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)`. No read-write access anywhere.

### 2.6 Graceful Absence — ✅ Compliant

- No DB file → returns None (debug log).
- Zero matching rows → returns None (debug log).
- sqlite3.Error → caught, logged, returns None.
- All tested.

---

## 3. Design Deviations from Architecture

### 3.1 Checkpoint Key Divergence (Intentional Fix)

The architecture (§2.3) originally specified the checkpoint would be keyed by the shared `session_id`. The developer changed this to `opencode_session_row_id` — this is the **correct** fix for BLOCKER B-1. The architecture itself had a design error here; the implementation is more correct than the spec. This is an improvement, not a violation.

### 3.2 No New Issues Found

A careful re-read of the full implementation against the architecture found no additional deviations beyond what was already identified and fixed.

---

## 4. Over-Engineering Check — ✅ Clean

- No unnecessary abstractions beyond the existing `CostCollector` ABC.
- No premature optimization.
- The `_discover_opencode_session` helper is justified — it mirrors `_discover_session_file`'s structure and clearly separates the DB discovery logic from the orchestration.
- The `OpenCodeCollector` is the minimum viable rewrite of the dead-code predecessor.

---

## 5. Test Coverage Assessment

All 34 tests pass. Test coverage for OpenCode-specific paths:

| Scenario | Test |
|----------|------|
| Basic collection from opencode.db | `test_collect_basic` |
| Checkpoint guards against re-collection | `test_already_collected_checkpoint_short_circuits` |
| No session_row_id set | `test_no_session_row_id` |
| Missing row in DB | `test_missing_row` |
| Zero-cost row skipped | `test_zero_cost_skipped` |
| Malformed model JSON fallback | `test_malformed_model_json_falls_back_to_raw` |
| No opencode.db on disk | `test_no_db_file` / `test_no_opencode_db_present` |
| Single match | `test_single_match` |
| Multiple matches (tie-break) | `test_multiple_matches_picks_most_recent` |
| Directory mismatch | `test_directory_mismatch` |
| Time window boundary (before) | `test_session_before_window_excluded` |
| Time window boundary (after) | `test_session_after_now_excluded` |
| End-to-end: writes CostEntry | `test_writes_cost_entry_and_checkpoint` |
| End-to-end: no double-record | `test_second_call_does_not_double_record` |
| End-to-end: no DB present | `test_no_opencode_db_present` |
| **Critical: shared session_id doesn't drop cost** | `test_shared_hephaestus_session_id_does_not_drop_second_launch` |

Coverage is comprehensive for all edge cases specified in the architecture.

---

## 6. Verdict

**PASS.** All prior BLOCKER findings are resolved. No new BLOCKERs or FIXes found. The implementation matches the architecture's component boundaries, interfaces, data flow, and naming conventions. One DEFER (URI percent-encoding) remains as originally classified. `complete_my_task` called.