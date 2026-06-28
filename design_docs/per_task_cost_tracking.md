# Per-Task Cost Tracking Design

> **Status:** Design Phase  
> **Parent Document:** `budget_tracking_approval_system.md` (Section 10)  
> **Created:** 2026-06-27  

---

## Problem
Currently, cost is only tracked at the design/feature level via `pipeline_metrics.json`. There's no visibility into which tasks consume the most tokens/cost, making it hard to optimize expensive phases or identify cost outliers.

## Goal
Track LLM token usage and cost per task, display it in the TaskDetailModal, and aggregate it for design-level reporting.

## Design

### 1. Database Schema Changes

Add columns to `tasks` table:

```python
# In src/core/database.py - Task model
input_tokens: int = Field(default=0)      # Prompt tokens consumed
output_tokens: int = Field(default=0)     # Completion tokens generated
total_tokens: int = Field(default=0)      # input + output
cost_usd: float = Field(default=0.0)      # Cost in USD
llm_model: str = Field(default=None)      # Model used (e.g., "anthropic/claude-3-opus")
```

### 2. LLM Client Changes

**Already implemented** in `src/interfaces/openrouter_client.py`:

```python
# generate() already returns:
{
    "content": str,
    "provider": str,
    "usage": {
        "prompt_tokens": int,
        "completion_tokens": int,
        "total_tokens": int
    },
    "cost": float | None,  # From LiteLLM proxy headers
    "user": str  # Feature name for cost tracking
}
```

**No changes needed** for OpenRouter client - just need to propagate these values to the task.

**LangChain client** (`src/interfaces/langchain_llm_client.py`) currently doesn't return usage data. Options:
1. Add callback to capture usage from LangChain response
2. Track usage separately via LiteLLM proxy (already supported)
3. Accept that LangChain providers won't have per-call cost data

### 3. Task Execution Updates

In `AgentManager` or wherever LLM calls happen for tasks:

```python
# After each LLM call in task execution
task.input_tokens += response.input_tokens
task.output_tokens += response.output_tokens
task.total_tokens = task.input_tokens + task.output_tokens
task.cost_usd += response.cost_usd
task.llm_model = response.model  # Track primary model used
```

### 4. API Changes

Add to task endpoints in `server.py`:

```python
# GET /api/tasks/{task_id}/full-details
# Add: input_tokens, output_tokens, total_tokens, cost_usd, llm_model

# GET /api/autopilot/projects/{project_id}/designs/{filename}/status
# Already returns tasks - add cost fields
```

### 5. Frontend Changes

**TaskDetailModal.tsx** - Add cost section:
```
┌─────────────────────────────────────────┐
│ Token Usage                             │
│ ┌─────────────┬─────────────┬─────────┐ │
│ │ Input       │ Output      │ Total   │ │
│ │ 12,450      │ 3,200       │ 15,650  │ │
│ └─────────────┴─────────────┴─────────┘ │
│ Model: claude-3-opus    Cost: $0.18     │
└─────────────────────────────────────────┘
```

**DesignDetailModal.tsx** - Aggregate task costs:
```
Total Design Cost: $2.45
├── Phase 1 (requirements): $0.12
├── Phase 2 (architecture): $0.34
├── Phase 3 (development): $1.89
└── Phase 4 (review): $0.10
```

### 6. Migration

```bash
# Add columns to existing tasks table
alembic revision --autogenerate -m "add task cost tracking"
alembic upgrade head
```

## Files to Modify

1. `src/core/database.py` - Add Task columns
2. `src/interfaces/openrouter_client.py` - Return token counts
3. `src/agents/manager.py` - Track tokens during execution
4. `src/mcp/server.py` - Add cost to task endpoints
5. `frontend/src/components/TaskDetailModal.tsx` - Display tokens
6. `frontend/src/components/autopilot/DesignDetailModal.tsx` - Aggregate costs

## Open Questions

1. Should we track cost for non-LLM operations (tool calls, etc.)?
2. Should cost be rounded or stored with full precision?
3. Do we need a cost budget/alert system per design?
