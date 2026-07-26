---
type: architectural_review_result
blocker_count: 1
fix_count: 1
defer_count: 1
overall: NEEDS_WORK
---

# Architectural Review Report

**Reviewer:** Architect (design author)
**Target:** OpenCode Cost Collector implementation, commit `edd0031` (development phase), diff against `4131509` (architecture_design)
**Date:** 2026-07-25
**Design artifacts:** `docs/architecture.md`, `docs/requirements_analysis.md`

## Summary
- **BLOCKERS:** 1 — checkpoint key collides across distinct OpenCode launches that share Hephaestus's role-based session ID, silently dropping cost after the first collection
- **FIX:** 1 — `sqlite3` connections leak on the query-exception path in both new DB-reading functions
- **DEFER:** 1 — no defensive handling for `?`/`#` characters in the DB URI path (theoretical, not exploitable here)
- **Overall:** NEEDS_WORK

The diff is confined to `src/services/cost_collection_service.py` and `tests/test_cost_collection_service.py`, matches architecture.md's Tasks 1–5 line-for-line (including the resolved Task 1 spike: `-s <id>` confirmed resume-only, time-window matching correctly retained), and the developer's own targeted-test/lint claims check out (81 tests pass, `ruff check` clean, the 2 remaining `mypy` errors are in untouched `_get_agent_cwd`/`_extract_session_id` code that predates this feature). The one BLOCKER below is a design-level flaw that both `docs/architecture.md` (which I authored) and the implementation (which faithfully followed it) share — flagging it here regardless of authorship, since that's what this phase is for.

## Findings

### [BLOCKER] SessionCostCheckpoint key is shared across independent OpenCode launches, so only the first one in a role ever gets its cost recorded

- **File:** `src/services/cost_collection_service.py:515-517` (checkpoint read), `:609-619` (checkpoint write), `:554-561` (opencode branch), `OpenCodeCollector.collect` `:293-294`
- **Design intent:** `docs/architecture.md` §2.2/§3 specifies `checkpoint` as a 0/1 "already collected this session" flag, keyed — like every other `cli_type` — by `collect_task_cost()`'s pre-existing `session_id` variable (from `_extract_session_id`, itself derived from `get_session_id()`'s deterministic `project+design+role+model` hash). The architecture's own FR5 rationale: "each session row corresponds to exactly one agent launch... `SessionCostCheckpoint`'s existing guard is sufficient to prevent double-counting."
- **Evidence:** That rationale only holds if one Hephaestus `session_id` maps to exactly one OpenCode launch. It doesn't. `get_session_id()`'s own docstring (`src/autopilot/phases.py:52-56`) states the deterministic ID is intentionally **shared** across (a) any phase retry, and (b) every phase mapped to the same `session_role` in `config/workflows/autopilot/workflow.yaml:9-21` — e.g. `architecture_design` and `architectural_review` both map to role `architect` (line 12, 14 of that file). This is not hypothetical: this very review is running as a resumed "architect" session for exactly that pair of phases, on exactly this design. For `pi`/Claude Code that's correct — sharing the ID makes the CLI resume the *same* transcript file on disk, and the line-count checkpoint (`lines_processed`) correctly advances to cover only the new turns from the second task. OpenCode has no such resumption: `OpenCodeAgent.get_launch_command` (`src/interfaces/cli_interface.py:465-485`) never reads a `session_id` kwarg at all, and the developer's own Task 1 finding confirms `-s <id>` errors "Session not found" for a fresh ID — so every OpenCode launch, whether or not it shares a Hephaestus `session_id` with a prior task, mints a brand-new, unrelated `opencode.db` session row. Concretely: Task A (architecture_design) completes → `_discover_opencode_session` finds session row X (correct) → `record_cost` writes X's total → checkpoint for `session_id="hephaestus-...-architect-<hash>"` is set to 1 (`:609-619`). Task B (architectural_review, same project+design, same role, hence same `session_id`) completes → checkpoint lookup at `:515-517` finds the row already at 1 → `OpenCodeCollector.collect()`'s `if checkpoint >= 1: return entries, checkpoint` (`:293-294`) returns immediately, **without ever querying `opencode.db` for Task B's actual (different) session row Y**. Task B's real dollar cost is never recorded — not deduplicated, permanently lost.
- **Impact:** Any project configured with `cli_type: opencode` undercounts cost for every task beyond the first one sharing a role (`architect`: architecture_design + architectural_review; `product-requirements`: product_requirements + product_validation; `developer`: development + any goto-development cycle; and per `get_session_id`'s own docs, any same-phase retry too). Budget enforcement (`cost_limit_usd` / `_pause_project_workflows`) reads from these same `CostEntry` rollups, so a project silently under-billed this way could blow past its real spend without the pause ever triggering — the exact failure mode this whole feature exists to prevent.
- **Recommended fix:** Key the OpenCode checkpoint by something that's actually unique per OpenCode launch, not by the shared Hephaestus `session_id`. The cleanest fix: use the discovered `opencode_session_row_id` (already computed at `:557-561`, guaranteed fresh per launch since OpenCode never resumes) as the `SessionCostCheckpoint.session_id` for the `cli_type == "opencode"` branch specifically — e.g. compute `checkpoint_key = opencode_session_row_id if cli_type == "opencode" and opencode_session_row_id else session_id` right after the discovery block, and use `checkpoint_key` (not the bare `session_id` variable) for both the checkpoint read at `:515-517` and the write at `:609-619`. This requires moving the checkpoint *read* after cli-type dispatch (currently it happens before, at `:515-517`, since `session_id` is known up front but `opencode_session_row_id` isn't discovered until later) — restructure so the opencode branch resolves its checkpoint key before the generic read, or read/create the checkpoint row after dispatch using whichever key applies. `pi`/`claude_code`/`codex` keep using the existing shared `session_id` unchanged; only the `opencode` path needs the per-launch key. This is a re-scope of Task 4 in architecture.md, not a one-line patch — send back to development.

### [FIX] `sqlite3` connections leak on the query-exception path

- **File:** `src/services/cost_collection_service.py:299-309` (`OpenCodeCollector.collect`), `:452-462` (`_discover_opencode_session`)
- **Design intent:** architecture.md §2.1/§2.2 didn't specify connection lifecycle explicitly, but the codebase's existing collectors (`PiJsonlCollector`, `ClaudeCodeCollector`) use `with open(...)` so the file handle is closed even on exception.
- **Evidence:** Both new functions do `conn = sqlite3.connect(...)` then later `conn.close()` on the same line sequence, but `conn.close()` is only reached if `conn.execute(...)`/`.fetchone()`/`.fetchall()` succeeds. If `execute()` itself raises `sqlite3.Error` (e.g. DB locked, schema mismatch after an OpenCode version bump — a scenario the NFRs explicitly call out as unversioned/possible), the `except sqlite3.Error` block catches it and returns without ever closing `conn`. Low severity in practice (CPython refcounting GCs the connection promptly; SQLite connections are cheap), but it's a real leak on every exception path in both new functions.
- **Recommended fix:** Wrap the connect/execute/close sequence in `with sqlite3.connect(...) as conn:` (commits/rolls back but does not auto-close in stdlib `sqlite3`, so also wrap in `contextlib.closing`), or use `try/finally` to guarantee `conn.close()` runs regardless of outcome.

### [DEFER] `opencode.db` URI path isn't percent-encoded for `?`/`#`

- **File:** `src/services/cost_collection_service.py:300`, `:453`
- **Reason:** `sqlite3.connect(f"file:{session_file}?mode=ro", uri=True)` embeds the path directly into the URI without escaping. SQLite's own URI parser is lenient about spaces (no `%20` needed in practice), but a literal `?` or `#` in the path would be misinterpreted as the query/fragment delimiter, truncating or corrupting the path. The only variable component of this path is `Path.home()`, and real-world home directory names essentially never contain `?`/`#` — theoretical, not worth blocking on, but a one-line `urllib.parse.quote()` around the path would close it off entirely if anyone wants to harden it later.

## Architecture Deviations

None beyond the BLOCKER above, which is a flaw the implementation inherited faithfully from architecture.md rather than introduced independently. The Task 1 spike's outcome (`-s <id>` is resume-only) was correctly folded back into the time-window design exactly as architecture.md's conditional instructed. Tasks 2–5 match the architecture doc's specified interfaces, column mappings, and tie-break policy exactly — verified by re-reading the diff against `docs/architecture.md` §2.1–§2.3 line by line.

## Design Invariants

- **No new schema/tables/call-sites** (NFR): held — diff touches exactly the two files architecture.md named, no migrations, no new endpoints.
- **Read-only access to `opencode.db`** (NFR): held — both new functions use `mode=ro` URI connections exclusively; no write statement anywhere in the diff.
- **Path safety** (NFR — resolve-and-verify-under-base): held — `_discover_opencode_session` reproduces the exact resolve/`startswith` pattern from `_discover_session_file`/the Claude Code branch.
- **Graceful absence** (NFR): held — missing DB file, empty query result, and `sqlite3.Error` all return `None`/`([], checkpoint)` without raising into `collect_task_cost()`'s caller; verified by the `test_no_opencode_db_present` integration test actually asserting no exception and zero `CostEntry` rows.
- **"Prevent double-counting on collector re-runs"** (FR5): **violated** — see BLOCKER. The guard prevents double-counting within a single Hephaestus session_id, but conflates "already collected for this checkpoint key" with "already collected for this OpenCode session," which are different things once a checkpoint key can span multiple OpenCode launches.

## Assumptions & Gaps

- Neither `docs/requirements_analysis.md` nor `docs/architecture.md` explicitly considered `SESSION_ROLES`/`get_session_id()`'s cross-phase session-sharing behavior when reasoning about "one agent launch = one session row = one checkpoint." Requirements FR5 came close ("keyed by whatever ID FR2 settles on") but the architecture phase (mine) resolved that ambiguity toward the wrong key. Worth a note for future collector work: any new `cli_type` whose CLI can't resume a session should default to a per-launch checkpoint key, never the shared Hephaestus `session_id`, unless that CLI's own resumption story matches pi/Claude Code's.
- This review is static-only per phase instructions (no `pytest`/program execution) with one exception: I ran the targeted test suite once early in this review to corroborate the developer's "81 tests pass" claim before I'd re-derived the checkpoint-sharing issue from source; all 81 pass, which is expected and unsurprising — none of the existing tests construct the cross-phase-shared-session-id scenario the BLOCKER depends on, so a green test suite is fully consistent with the bug being real.

## Positive Observations

- Column mapping, model-JSON parsing (with raw-string fallback), zero-cost handling, and the multi-match tie-break policy in `_discover_opencode_session` all match architecture.md exactly, including the debug-logging of discarded candidate IDs on ties.
- Test coverage is thorough for everything the architecture actually specified: no-DB, empty/single/multiple match, directory mismatch, both time-window boundaries, malformed model JSON, and three real integration tests through `collect_task_cost()` using the `db_manager` fixture rather than mocking the DB layer.
- The developer correctly ran the live `-s <id>` test called for by Task 1 before implementing, rather than assuming an answer — exactly the kind of verification this pipeline's `product_requirements`/`scope_review` phases modeled earlier in this same feature.
- Zero scope creep: no code outside `cost_collection_service.py`/its test file touched, `pi`/`claude_code`/`codex` branches byte-for-byte preserved except the one necessary `opencode_session_row_id` threading change.
