# Feature: Backend OpenRouter Direct Cost Capture

## Overview
Refactor `src/interfaces/langchain_llm_client.py` to capture cost from the backend's own direct OpenRouter calls (task enrichment, guardian, conductor, and ~6 other call sites). Build a `_invoke_and_record` helper that all ~9 `model.ainvoke()` sites route through, which adds `usage: {include: true}` to `extra_body` so OpenRouter returns cost in the response, extracts usage from `response.response_metadata`, and writes a `CostEntry` with `source='openrouter_direct'`. Thread `task_id` (and `workflow_id` where available) through method signatures that currently lack them (e.g. `enrich_task`, `analyze_agent_trajectory`) so cost entries are correctly attributed.

## Files Owned
- `src/interfaces/langchain_llm_client.py`

## Dependencies
- `cost-schema` — writes to `cost_entries` table
- `cost-derivation` — calls `derive_cost_totals()` after writing entries
- `budget-enforcement` — calls `_enforce_budget_limit(project_id)` after derivation to trigger enforcement if limit crossed (one-directional: openrouter-direct → orchestrator.py, not the reverse)

## Implementation Notes

### The `_invoke_and_record` helper
All ~9 `model.ainvoke()` call sites in `LangChainLLMClient` should be routed through one private helper:

```python
async def _invoke_and_record(
    self,
    model: Any,
    messages: list,
    component: str,       # e.g. "enrich_task", "guardian", "conductor"
    task_id: str | None = None,
    workflow_id: str | None = None,
) -> Any:
```

Steps inside the helper:
1. Add `"usage": {"include": true}` to `extra_body` in `model_kwargs` if not already present
2. Call `response = await model.ainvoke(messages)`
3. Extract usage from `response.response_metadata` (LangChain preserves raw provider-specific fields here for ChatOpenAI)
4. Create a `CostEntry` with `source="openrouter_direct"`, the extracted token counts, and cost_usd; capture `reasoning_tokens` if present in usage data
5. Write to DB
6. Call `derive_cost_totals(db, task_id)` if task_id is not None
7. Call `_enforce_budget_limit(project_id)` if project_id is available — triggers enforcement check
8. Return the response (callers continue using it as before)

### OpenRouter cost extraction
OpenRouter returns `usage.cost` (dollar amounts) as a non-standard field when `usage: {include: true}` is in the request body. LangChain's `ChatOpenAI` normally drops non-standard fields, but `response.response_metadata["token_usage"]` preserves the raw dict. Verify this with a smoke test before relying on it:
- `response.response_metadata.get("token_usage", {}).get("cost", {}).get("total", 0.0)`

If this doesn't work, fall back to computing cost from token counts using OpenRouter pricing (similar to the Claude Code collector's approach).

### Threading `task_id` through signatures
The ~9 call sites that need updating:
1. `enrich_task(...)` — add `task_id: str | None = None` parameter. Caller (`TaskEnrichmentService.enrich` called from `process_queue` in `src/mcp/server.py:1347`) has the task ID in scope.
2. `analyze_agent_trajectory(...)` — receives `task_info: Dict[str, Any]` which likely contains task ID. Extract with `task_info.get("task_id")`.
3. `analyze_agent_state(...)` — check if task_id is available in the context dict
4. `resolve_ticket_clarification(...)` — likely ticket-scoped, may not have task_id
5. `classify_complexity(...)` — may not be task-scoped
6. `analyze_system_coherence(...)` — conductor level, workflow-scoped at best
7. `review_qa_report(...)` — may have task context from the QA report
8-9. Remaining call sites — check each individually

Methods where `task_id` is genuinely unavailable should pass `None` — those cost entries still get recorded (rolled up to workflow or "overhead" level), they just aren't task-attributed. This is correct behavior per the data model design.

### Test plan
The smoke test for `usage: {include: true}` is a prerequisite — if cost doesn't surface in `response_metadata`, this feature needs a different approach (query OpenRouter's usage API after the fact, or maintain a local pricing table).

## Acceptance Criteria
- [ ] `_invoke_and_record` helper exists and is used by all `model.ainvoke()` call sites in LangChainLLMClient
- [ ] `usage: {include: true}` is added to OpenRouter requests so cost data is available in responses
- [ ] Cost entries are written to `cost_entries` with `source="openrouter_direct"` and correct task_id where available
- [ ] `enrich_task` and `analyze_agent_trajectory` signatures include `task_id` parameter
- [ ] Callers in `src/mcp/server.py` pass task_id down to the LLM client methods
- [ ] `derive_cost_totals()` is called after writing new cost entries
- [ ] Methods without task_id context still record cost (with task_id=NULL) — no silent cost loss