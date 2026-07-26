---
type: code_summary
feature_id: des-91c8-opencode-collector
---

# Code Summary: OpenCode Cost Collector

**Feature ID:** des-91c8-opencode-collector
**Branch:** `feature/des-91c8/opencode-collector`

## What this feature does

Makes `collect_task_cost()` produce correct `CostEntry` rows for
`cli_type == "opencode"` tasks by reading OpenCode's own SQLite DB
(`~/.local/share/opencode/opencode.db`) instead of the dead
stdout-JSON-capture code path that never actually ran. No schema changes,
no new call sites, no UI changes — `source="opencode"` was already a
first-class value everywhere downstream (rollups, budget enforcement, UI).

## Changed files

### Backend

- **`src/services/cost_collection_service.py`** — the only file changed:
  - `_discover_opencode_session(cwd, agent_created_at)` (new) — correlates a completed task's agent to an OpenCode `session` row by matching `session.directory == cwd` within a `[agent.created_at, now]` time window (OpenCode has no deterministic session ID Hephaestus controls), returning the most recent in-window match. Opens the DB read-only, verifies the resolved path stays under `~/.local/share/opencode/`, and both time bounds are converted to epoch-ms with explicit `tzinfo=timezone.utc` before calling `.timestamp()`.
  - `OpenCodeCollector.collect()` (rewritten) — no longer parses `session_file` as a JSON blob; queries the `session` table by row ID and maps its pre-aggregated `cost`/`tokens_*`/`model` columns directly onto a `CostEntry` dict. `checkpoint` is a 0/1 "already collected" flag, not a line count.
  - `collect_task_cost()`'s `opencode` branch — replaced the `pass` stub with a call to `_discover_opencode_session()`, then `OpenCodeCollector(session_row_id=...)`. The `SessionCostCheckpoint` key for OpenCode is `opencode_session_row_id`, not the shared Hephaestus `session_id` — because OpenCode never resumes a session, a launch sharing `session_id` with a prior task would otherwise find the prior checkpoint already at 1 and silently drop its own cost.

### Tests

- **`tests/test_cost_collection_service.py`** — three new test classes:
  - `TestOpenCodeCollector` — column mapping, zero-cost handling, missing row, malformed `model` JSON, `session_row_id=None`.
  - `TestDiscoverOpencodeSession` — no DB file, empty result, single/multiple matches (tie-break), directory mismatch, time-window boundaries, path-safety guard.
  - `TestCollectTaskCostOpenCode` — end-to-end: `CostEntry` written with correct `source`/`cost_usd`/tokens, checkpoint prevents double-recording, no `opencode.db` present is a silent no-op, and the shared-`session_id`-doesn't-drop-second-launch regression test for the checkpoint-key fix.

## Explicitly out of scope (by design)

- Codex collection (`CodexStubCollector` untouched).
- Any UI surfacing of OpenCode-specific data — `source="opencode"` was already handled everywhere.
- `AutopilotProject.cli_tool` UI exposure — configuring OpenCode as a project's CLI is a separate feature.
- OpenCode DB schema versioning/migration — graceful-failure-on-unexpected-shape only.

## Verification

- Targeted: `pytest tests/test_cost_collection_service.py tests/test_cost_tracking.py` — 83/83 passing (confirmed by direct collection during this review).
- Two BLOCKERs found and fixed mid-pipeline, both re-verified against the live code by this review:
  - `af59ac8` (adversarial_review, B-1) — naive-datetime `.timestamp()` misread as local time, silently dropping all OpenCode costs on non-UTC hosts. Fixed with explicit `tzinfo=timezone.utc`.
  - `adae90b` (architectural_review, B-1) — `SessionCostCheckpoint` originally spec'd to key on the shared Hephaestus `session_id`; fixed to key on `opencode_session_row_id` instead, since OpenCode never resumes a session.
- Security: `docs/security_review/security_report.md` — PASS, 0 issues found.
