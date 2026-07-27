---
type: code_summary
feature_id: des-91c8-pi-extension
---

# Code Summary: Pi Cost Tracker Extension

**Feature ID:** des-91c8-pi-extension

This feature's diff (`main..HEAD`) touches exactly one non-test file. The
real-time pi extension, its install/build wiring, and the API endpoint it
posts to were all already implemented and merged by earlier sibling
features — nothing here changes collection logic.

## `extensions/hephaestus-cost-tracker/README.md`

Fixed FR-1: the "How It Works" step 4 documented the POST target as
`/cost-entries`, but the extension (`src/index.ts:123`) actually posts to
`${apiUrl}/api/autopilot/cost-entries` — the real route, given the router's
`/api/autopilot` prefix (`autopilot_api.py:37`) plus the `@router.post
("/cost-entries")` decorator (`autopilot_api.py:2144`). A developer testing
the endpoint by hand against the documented path would have hit a 404. Now
reads `POST /api/autopilot/cost-entries`.

## `src/services/cost_collection_service.py`

Fixed a High-severity security finding from this feature's security review:
the JSONL-fallback suppression check only matched on `task_id`, so a
caller-forged `source="pi"` cost entry (task_id/agent_id are both
enumerable via unauthenticated `GET /api/tasks`/`GET /api/agents`) could
permanently suppress a victim task's real cost collection. The check now
also requires the entry's `agent_id` to match the task's assigned agent.

## Tests

`tests/test_cost_collection_service.py` gained
`test_unrelated_agent_entry_does_not_suppress_fallback` (24/24 passing) to
cover the fix above. No JS/TS test framework was introduced — this repo has
none anywhere, including `frontend/`, and the extension's own logic
(`index.ts`) is unchanged by this feature.
