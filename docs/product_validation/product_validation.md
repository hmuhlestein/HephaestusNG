---
type: product_validation_result
feature_id: des-91c8-opencode-collector
verdict: PASS
unmet_requirements: []
agent_score: 0.97
passed_tests: 83
failed_tests: 0
total_tests: 83
pass_rate: 100.0
requirements_met: 5
requirements_total: 5
blocker_count: 0
regressions_found: false
status: PASS
recommendation: done
---

# Product Validation Report: OpenCode Cost Collector

**Feature ID:** des-91c8-opencode-collector
**Feature Name:** OpenCode Cost Collector
**Validation Date:** 2026-07-26
**Design Document:** `.hephaestus/design.md` — OpenCode section (lines 167-221, 540-577), Implementation Phase 6 (lines 693-706)
**Requirements Document:** `docs/requirements_analysis.md`
**Architecture Document:** `docs/architecture.md`
**Scope Review:** commit `3ee6077` — ruled PROCEED on the design's build/defer gate (see §0 below)
**QA Report:** `docs/qa_validation/qa_report.md` (PASS, 83/83 feature-scoped tests)
**Security Report:** `docs/security_review/security_report.md` (PASS, 0 findings)
**Verdict:** PASS

---

## 0. Note on Superseded Prior Report

The report previously at this path validated a different, already-merged sibling feature, "Cost Tracking UI" (`des-91c8-cost-ui`). This branch, `feature/des-91c8/opencode-collector`, is a separate feature with its own requirements, architecture, and implementation. This report replaces the stale one.

## 0.1 Design Gate — Resolved, Not Re-litigated Here

The design document (`design.md:695-699`) states OpenCode collection work should stay deferred unless `cli_type: opencode` is live in `config/workflows/autopilot/`. `product_requirements` verified that condition is met (zero live usage) and escalated it as a blocking scope question rather than deciding unilaterally. `scope_review` (commit `3ee6077`) explicitly ruled **PROCEED**, on the grounds that this feature was commissioned as its own standalone workflow, which is out-of-band authorization overriding the design's generic anti-speculative-work gate. That is a scope decision already made by the correct phase; this report validates the resulting implementation against the requirements/architecture that followed from it, not the gate decision itself.

---

## 1. Executive Summary

The feature has one job: make `collect_task_cost()`'s `opencode` branch (previously a dead `pass`) actually collect cost data, replacing a stale design assumption (OpenCode as one-shot, stdout-JSON-capturable) with the current reality (OpenCode launches with `-i`, a persistent session, whose cost/token totals live pre-aggregated in `~/.local/share/opencode/opencode.db`'s `session` table).

The entire diff against the merge-base (`a71d84d`) is confined to one file, `src/services/cost_collection_service.py` (+147/-51 lines) — matching both the architecture doc's stated blast radius and NFR-1 ("no new tables/columns"). All 5 functional requirements (FR1-FR5) are implemented and independently re-verified against the code in this report:

- **FR1** (design gate check) — a requirements/scope decision, not code; resolved per §0.1.
- **FR2** (correlate agent → OpenCode session row) — `_discover_opencode_session()` matches `session.directory` to the agent's cwd and `session.time_created` to a `[Agent.created_at, now]` window, tie-breaking on most-recent when multiple rows match.
- **FR3** (replace stdout-JSON collector with a `session`-table query) — `OpenCodeCollector.collect()` rewritten to `SELECT cost, tokens_*, model FROM session WHERE id = ?` via read-only `sqlite3`, mapping columns straight onto `CostEntry` fields.
- **FR4** (wire the dead branch) — the `cli_type == "opencode"` branch in `collect_task_cost()` now calls `_get_agent_cwd()` → `_discover_opencode_session()` → the rewritten collector, instead of `pass`.
- **FR5** (checkpoint safety) — `SessionCostCheckpoint` is now keyed by the per-launch `session_row_id` for OpenCode (not the shared Hephaestus `session_id`, which can repeat across retries/shared roles for `pi`/Claude Code but never corresponds to the same `opencode.db` row twice).

One BLOCKER was found and fixed during `adversarial_review` (naive-datetime `.timestamp()` silently misread as local time, which would have dropped 100% of collected costs on any host not set to UTC) — independently re-confirmed here as fixed (§4). No other blockers; `security_review` and `qa_validation` both passed clean.

---

## 2. Functional Requirements Verification

| Req | Design/Requirements Intent | Implementation | Status |
|-----|---------------------------|-----------------|--------|
| FR1 | Design's build/defer gate on live `cli_type: opencode` usage | Not code — a scope decision. Requirements correctly identified the gate condition as met and escalated; `scope_review` explicitly ruled PROCEED (commit `3ee6077`) | ✅ PASS (resolved upstream) |
| FR2 | Correlate a completed task's agent to its OpenCode session row, with explicit zero/multiple-match handling | `_discover_opencode_session()` (`cost_collection_service.py:423-482`): matches `directory = cwd` and `time_created` in `[start_ms, end_ms]`, `ORDER BY time_created DESC`; zero matches → `None` (caller logs+skips); multiple matches → most recent used, rest logged as discarded | ✅ PASS |
| FR3 | Replace stdout-JSON `OpenCodeCollector` with a `session`-table query | `OpenCodeCollector.collect()` (lines 264-343) now takes `session_row_id`, queries `cost, tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, model` via read-only `sqlite3.connect(f"file:{...}?mode=ro", uri=True)`, maps directly onto `CostEntry` fields | ✅ PASS |
| FR4 | Wire `collect_task_cost()`'s dead `opencode` branch | Branch at `cost_collection_service.py:559-566` now calls `_get_agent_cwd()` then `_discover_opencode_session()`, setting `session_file`/`opencode_session_row_id` for the collector dispatch below | ✅ PASS |
| FR5 | Checkpointing keyed correctly so re-runs don't double-count and same-`session_id` reuse doesn't silently skip collection | `checkpoint_key = opencode_session_row_id if cli_type == "opencode" ... else session_id` (line 579); `OpenCodeCollector.collect()` itself also short-circuits on `checkpoint >= 1` | ✅ PASS |

All 5/5 functional requirements met.

---

## 3. Non-Functional Requirements

| NFR | Requirement | Status | Evidence |
|-----|-------------|--------|----------|
| NFR-1 | No new tables/columns | ✅ PASS | `git diff --stat a71d84d..HEAD` touches only `cost_collection_service.py`; no `database.py` diff |
| NFR-2 | Read-only access to `opencode.db` | ✅ PASS | Both DB reads use `sqlite3.connect(f"file:{path}?mode=ro", uri=True)` — no `INSERT`/`UPDATE`/`PRAGMA` writes |
| NFR-3 | Path safety (resolved path must stay under `~/.local/share/opencode/`) | ✅ PASS | `_discover_opencode_session()` lines ~432-440 resolves the path and checks it starts with the resolved base dir before opening, matching the existing `pi`/Claude Code branches' pattern |
| NFR-4 | Graceful absence (missing DB / no match) never raises into `collect_task_cost()`'s caller | ✅ PASS | `db_path.exists()` check returns `None` early; `sqlite3.Error` caught and logged, returns `None`/`(entries, checkpoint)` rather than propagating |
| NFR-5 | No timer-based collection — triggered once at task completion | ✅ PASS | Only call site is `collect_task_cost()`, unchanged trigger point |

---

## 4. Test & Quality Evidence (independently re-run, not re-stated blindly)

Ran the feature-scoped suites myself: `pytest tests/test_cost_collection_service.py tests/test_cost_tracking.py -q` → **83 passed**, 0 failures — matches QA's reported total exactly.

Independently re-verified the one BLOCKER fix carried forward from `adversarial_review` (B-1): `_discover_opencode_session()` (lines 456-457 per the QA report's line numbers) attaches `tzinfo=timezone.utc` to both `agent_created_at` and `datetime.utcnow()` before calling `.timestamp()` — confirmed present in the current diff (see the inline comment at `cost_collection_service.py` explaining exactly this naive-datetime hazard). Without this fix, any host not set to UTC would silently compute a shifted time window and drop 100% of OpenCode cost collection with no error. Fixed correctly.

`security_review` (commit `fa67b6f`) reported 0 findings across path traversal, SQL injection, and untrusted-deserialization surfaces — independently spot-checked here: the two SQL queries in this diff (`SELECT ... FROM session WHERE id = ?` and `WHERE directory = ? AND time_created >= ? AND time_created <= ?`) both use parameterized placeholders, not string interpolation.

`architectural_review` left one item (D-1) explicitly deferred, not a blocker: the `opencode.db` file URI isn't percent-encoded for literal `?`/`#` characters in a home directory path. This is a real, narrow edge case (a username containing `?` or `#`) correctly scoped out as theoretical rather than something this validation pass needs to re-litigate.

---

## 5. Design-Intent Cross-Check

Re-reading `design.md` lines 167-221 and 540-577 directly against the final diff (not just the requirements doc's paraphrase):

- The design's original mechanism (stdout `--format json` capture, "no checkpoint needed") is **not** what got built — and correctly so, since `product_requirements` §0 established this premise is stale (OpenCode now launches with `-i`, a persistent session, invalidating one-shot stdout capture). The design's own §"SQLite DB read as fallback" is what was actually implemented, which the design itself anticipated as a valid alternative if the stdout path didn't pan out.
- The design's Phase 6 gate text is honored procedurally: the gate was checked, found triggered, and explicitly escalated to `scope_review` rather than silently bypassed by any phase — including this one.
- No scope creep: the diff is confined to the one file the architecture doc scoped it to. No `CostEntry`/`SessionCostCheckpoint` schema changes, no UI changes, no changes to `pi`/Claude Code collection paths.

---

## 6. Verdict

**PASS.** All 5 functional requirements and all 5 non-functional requirements are met and independently verified against both the design document and the code. Test suite is green (83/83, independently re-run), the one prior BLOCKER (B-1, naive-datetime timezone bug) is confirmed fixed, no regressions, no new blockers. The design's build/defer gate — correctly identified as triggered by `product_requirements` — was explicitly and properly resolved by `scope_review`, not silently overridden by any implementation phase.

---

## 7. Deliverables

- `docs/product_validation/product_validation.md` — this report
- `docs/product_validation/product_validation.json` — structured pass/fail summary for the pipeline gate

---

*Report generated: 2026-07-26*
