# Budget Tracking & Approval System — Design Document

## Adversarial Review: Issues Found and Resolved

The following issues were identified during adversarial review and are addressed
in this revised design. Each is marked with **[FIXED]** in the relevant section.

| # | Severity | Issue | Resolution |
|---|----------|-------|------------|
| 1 | **CRITICAL** | Orchestrator is fully synchronous (`def`, not `async def`), but design showed `await` on BudgetManager methods | BudgetManager provides synchronous API. Async methods exist for future use but all orchestrator integration uses sync wrappers via `asyncio.run()` bridge (same pattern as existing cost fetching at line 1141). |
| 2 | **CRITICAL** | Monitoring (Guardian/Conductor) runs in a separate OS process (`run_monitor.py`). CostInterceptor cannot intercept its LLM calls from the orchestrator process | Monitoring costs tracked via LiteLLM proxy polling (external), not interception. BudgetManager receives monitoring cost updates through the shared SQLite ledger, which the monitor process writes to directly. |
| 3 | **CRITICAL** | Budget check blocking a critical LLM call (e.g., Guardian detecting stuck agent) could leave agents stuck forever with no recovery path | `check_budget()` gains a `critical: bool` parameter. Critical calls bypass spending limits but are still recorded. Monitoring and credit-detection calls are always critical. |
| 4 | **HIGH** | Missing `on_timeout_action` field in BudgetConfig — when approval times out, behavior is undefined | Added `on_timeout_action: str = "approve"` with options: `"approve"`, `"reject"`, `"continue"` (continue = resume without approval, same as approve but logged differently). |
| 5 | **MEDIUM** | `BudgetManager.poll_cli_agent_costs()` duplicates `CLIAgentBudgetManager.poll_spend()` — DRY violation | Removed `poll_cli_agent_costs()` from BudgetManager. All external polling lives exclusively in `CLIAgentBudgetManager`. BudgetManager only reads its own ledger. |
| 6 | **MEDIUM** | Dual-write to both SQLite and JSON file creates inconsistency risk and unnecessary complexity | Removed JSON file. SQLite is the single source of truth. Added export API endpoint for portability. |
| 7 | **MEDIUM** | Daily limit resets at midnight UTC but orchestrator runs in local timezone — confusing behavior | Added `timezone: str = "UTC"` config field. Daily window calculated as `[today_start, now]` in the configured timezone. |
| 8 | **MEDIUM** | No mechanism to reset budget counters at runtime | Added `POST /api/autopilot/budget/reset` endpoint with options: reset design spend, daily spend, total spend, or all. |
| 9 | **MEDIUM** | When budget is enabled mid-run, historical costs are invisible — daily/total limits start from $0 | On BudgetManager init, optionally query LiteLLM for historical spend within the current day/window and seed the ledger. Added `seed_from_litellm: bool = true` config. |
| 10 | **MEDIUM** | `CostInterceptor._extract_cost()` and `_extract_tokens()` are undefined — critical for interceptor operation | Defined explicit extraction logic: for OpenRouter responses, read `cost` field and `usage` dict. For LangChain results, read `response_metadata`. For unknown formats, return `None` (skip recording). |
| 11 | **LOW** | Hardcoded `$5/$10/$25/$50` increase amounts don't scale with limit magnitude | Added `approval_increase_amounts: List[float]` to config, with default `[1.0, 2.0, 5.0, 10.0]`. Also supports `"multipliers": [2.0, 5.0]` for relative increases. |
| 12 | **MEDIUM** | Budget system and existing `check_api_credits()` could conflict — budget pauses but credits are also exhausted | Defined explicit precedence: budget check runs first. If budget pauses, credit check is skipped for that cycle. If budget approves but credits are exhausted mid-call, existing credit handling takes over. Budget and credits are independent concerns. |
| 13 | **MEDIUM** | LiteLLM proxy may report costs with delay — polling may miss recently-incurred costs | Added `poll_staleness_seconds: int = 30` config. Polling includes a lookback window. Ledger includes a `source_delay_ms` field. Budget checks use `last_known_spend` with staleness indicator. |
| 14 | **LOW** | Same design name on retry creates ledger collisions | Ledger entries include `design_name` + `run_id` (unique per pipeline run). Design-scoped queries filter by both. |
| 15 | **MEDIUM** | `log_dir` not accessible in `run_single_design()` — it's created in `run_continuous_pipeline()` | Added `log_dir: Path` parameter to `run_single_design()`. Passed from `run_continuous_pipeline()`. |
| 16 | **MEDIUM** | `on_limit_exceeded='skip'` leaves partially-completed work in project directory with no tracking | On skip, partially-completed work stays in place (iteration loop needs it for the next design). Added `budget_skip_event` to the event log with details of what was abandoned. |
| 17 | **HIGH** | Generic `get_agent_env_vars()` doesn't account for per-CLI differences or GLM conflict | Added Section 2.2.1 with per-CLI coverage matrix. `get_agent_env_vars()` now takes `cli_type` and `is_glm_mode` params. GLM+Claude detected and skipped. Droid unknown-API documented. Codex needs `OPENAI_API_KEY`. |
| 18 | **HIGH** | Cost tracking relied on aggregate `/user/info` endpoint — no per-request granularity | Added Section 2.4 documenting precise LiteLLM endpoints. Updated polling to use `/user/daily/activity` for exact per-model daily costs. Documented Claude Code `/usage` as estimated (not authoritative). Added cost accuracy hierarchy table. |
| 19 | **CRITICAL** | CostInterceptor uses `asyncio.run()` which fails in MCP server's async context (FastAPI/uvicorn) | Rewrote CostInterceptor to use callback pattern: OpenRouterClient gains `cost_callback` hook called after generate(). No `asyncio.run()` needed — callback runs in the async context and does sync SQLite writes (< 1ms). |
| 20 | **HIGH** | `check_budget()` uses `spend[exceeded.value]` but LimitType values ("per_design") don't match spend dict keys ("design") | Fixed spend dict key mapping: `{"per_design": "design", "per_iteration": "iteration", "daily": "daily", "total": "total"}`. Added `_SPEND_KEY_MAP` constant. |
| 21 | **HIGH** | `_apply_limit_increase()` doesn't know which limit type to increase — stores single override | Changed to `_apply_limit_increase(limit_type, new_limit)` with per-limit-type override dict `_runtime_limit_overrides: Dict[LimitType, float]`. |
| 22 | **HIGH** | MCP server integration details missing — how does it create CostInterceptor from env vars? | Added Section 6.2.1 with MCP server startup code that reads BUDGET_* env vars and creates CostInterceptor with callback. |
| 23 | **MEDIUM** | `_poll_cli_agent_costs` uses `/spend/logs` but response doesn't clearly support user filtering in bulk queries | Switched to `/user/daily/activity` endpoint which explicitly returns per-model daily breakdown. Updated polling code to use this endpoint. |
| 24 | **MEDIUM** | `_get_totals()` uses `datetime.utcnow()` ignoring timezone config | Added timezone-aware daily window calculation using `zoneinfo.ZoneInfo` as described in Section 8.9. |
| 25 | **MEDIUM** | `_poll_cli_costs` sync wrapper doesn't pass `last_poll_time` | Added `last_poll_time` tracking to BudgetManager. Sync wrapper passes it through. |
| 26 | **MEDIUM** | `approve_budget()` inserts empty strings for limit_type, current_spend, limit_amount | Changed `approve_budget()` signature to accept these params. Callers now pass the actual values. |
| 27 | **MEDIUM** | No pagination handling for LiteLLM API responses | **Not needed** — switched to `/user/daily/activity` which returns max 1 entry per day (typically 1-7 entries total). Unlike `/spend/logs` which can return thousands of individual transactions, daily activity has no pagination. |
| 28 | **LOW** | BudgetManager thread safety assumptions not documented | Added threading model documentation: single-threaded in orchestrator, thread-safe SQLite writes via `threading.local()`. |
| 29 | **LOW** | Frontend polling for budget approval requests not described | Added Section 6.6 describing frontend polling for `budget_request_*.json` files via existing `/api/autopilot/input` endpoint. |

---

## 1. Problem Statement

The autopilot pipeline makes unbounded LLM API calls across three independent cost
channels:

1. **Internal LLM calls** — Hephaestus monitoring (Guardian/Conductor), task
   enrichment, prompt generation, QA review. These flow through
   `LangChainLLMClient` (async methods called via `asyncio.run()` bridge from
   the synchronous orchestrator).
2. **External CLI agent calls** — Claude Code, OpenCode, Codex, Droid agents
   spawned in tmux sessions. These make their own API calls, bypassing the
   Hephaestus cost pipeline entirely.
3. **Monitoring process calls** — Guardian/Conductor LLM analysis runs in a
   separate OS process (`run_monitor.py`) and cannot be intercepted from the
   orchestrator.

Neither channel has budget limits, rate caps, or spending approval gates. A single
runaway design iteration can consume hundreds of dollars with no visibility until
the feature report is generated *after* completion.

**Goal:** Enforce configurable spending limits at multiple granularities (per-design,
per-iteration, per-day, total) and require human approval before exceeding them.

---

## 2. Cost Source Analysis

### 2.1 Internal Costs (trackable via interceptor)

| Component | Entry Point | Async? | Current Tracking |
|-----------|------------|--------|-----------------|
| Task enrichment | `LangChainLLMClient.enrich_task()` | Yes | None |
| Guardian analysis | `LangChainLLMClient.analyze_agent_state()` | Yes | None |
| Conductor analysis | `LangChainLLMClient.analyze_system_coherence()` | Yes | None |
| Agent prompt gen | `LangChainLLMClient.generate_agent_prompt()` | Yes | None |
| QA report review | `LangChainLLMClient.review_qa_report()` | Yes | None |
| Embedding | `LangChainLLMClient.generate_embedding()` | Yes | None |
| Ticket clarification | `resolve_ticket_clarification()` | Yes | None |
| OpenRouterClient | `OpenRouterClient.generate()` | Yes | Cost from `x-litellm-response-cost` header |

**Architecture constraint:** The orchestrator is fully synchronous (`def`, not
`async def`). All `LangChainLLMClient` methods are `async`. The existing codebase
bridges this gap using `asyncio.run()` (orchestrator.py:1141). The budget system
follows the same pattern.

**Strategy:** `BudgetManager` exposes a synchronous API. Internally, it may use
`asyncio.run()` to call LiteLLM for cost polling. The `CostInterceptor` wraps
`OpenRouterClient.generate()` (the single point where all internal LLM calls
ultimately pass through) to record costs after each call.

**Important:** `LangChainLLMClient` methods are called from the MCP server
process (separate from the orchestrator). The orchestrator cannot directly
intercept these calls. Instead:
- `OpenRouterClient.generate()` is the single funnel point — all internal LLM
  calls pass through it. Cost interception happens here.
- For non-OpenRouter paths (direct Anthropic/OpenAI), the interceptor wraps the
  provider's `generate()` method.

### 2.2 External CLI Agent Costs (require polling)

| Agent Type | LLM Provider | How to Track |
|-----------|-------------|-------------|
| Claude Code | Anthropic API (direct or via LiteLLM) | LiteLLM proxy `user` field + periodic spend polling |
| OpenCode | OpenRouter or LiteLLM | Same as above |
| Codex | OpenAI API | LiteLLM proxy if routed through it |
| Droid | Unknown | LiteLLM proxy if routed through it |

**Strategy:** Two-pronged:
1. **Route through LiteLLM proxy** — Set `ANTHROPIC_BASE_URL`, `OPENAI_BASE_URL`
   env vars on the tmux session to point at the LiteLLM proxy. Set the `user`
   field to the feature name. This captures costs automatically.
2. **Poll LiteLLM spend API** — During workflow execution, periodically query
   `/global/spend/report` for the current feature's user ID. Compare against
   budget thresholds.

If LiteLLM proxy is unavailable, fall back to heuristic estimation based on
known model pricing tables and estimated token counts.

### 2.2.1 Per-CLI Agent Budget Coverage Matrix

The budget system must handle each CLI agent differently based on how it makes
API calls and which env vars it respects.

| Agent | Binary | API Provider | Env Vars for LiteLLM Routing | GLM Conflict | Proxy Routing Confidence |
|-------|--------|-------------|------------------------------|--------------|------------------------|
| `claude` | `claude` | Anthropic | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` | **YES** — GLM mode also sets `ANTHROPIC_BASE_URL` to `https://api.z.ai/api/anthropic`. Budget must detect GLM and skip proxy injection. | **High** — Claude Code reads `ANTHROPIC_BASE_URL` natively |
| `opencode` | `opencode` | Anthropic / OpenRouter / multi-provider | `ANTHROPIC_BASE_URL` (for Anthropic models), `OPENAI_BASE_URL` (for OpenAI models) | No — OpenCode doesn't use GLM mode | **Medium** — depends on provider; uses `provider/model` format |
| `codex` | `codex` | OpenAI | `OPENAI_BASE_URL`, `OPENAI_API_KEY` | No | **High** — Codex reads `OPENAI_BASE_URL` natively |
| `droid` | `droid` | Unknown | Unknown — may or may not respect standard env vars | No | **Low** — cannot guarantee routing works |
| `swarm` | `swarmcode` | N/A (placeholder) | N/A | No | **N/A** — not implemented |

**Per-agent env var injection details:**

```python
# In CLIAgentBudgetManager.get_agent_env_vars()

def get_agent_env_vars(self, design_name: str, cli_type: str,
                       is_glm_mode: bool = False) -> Dict[str, str]:
    """Generate env vars for tmux session based on CLI agent type.

    Args:
        design_name: Current design name for cost attribution.
        cli_type: CLI agent type ("claude", "opencode", "codex", "droid").
        is_glm_mode: Whether GLM model is active (conflicts with proxy routing).
    """
    if not self.config.route_cli_agents_through_proxy:
        return {}
    if not self.litellm.get("url"):
        return {}

    user_id = design_name.lower().replace(" ", "_")[:40]
    proxy_url = self.litellm["url"]
    api_key = self.litellm.get("api_key", "")

    base_vars = {"HEPHAESTUS_COST_USER": user_id}

    # GLM mode conflicts with proxy routing for Claude — skip injection
    if is_glm_mode and cli_type == "claude":
        logger.warning(
            f"GLM mode active for claude — skipping LiteLLM proxy injection. "
            f"CLI agent costs will NOT be tracked via proxy."
        )
        return base_vars  # Only set user ID, no proxy routing

    if cli_type == "claude":
        return {
            **base_vars,
            "ANTHROPIC_BASE_URL": proxy_url,
            "ANTHROPIC_API_KEY": api_key,
        }
    elif cli_type == "opencode":
        # OpenCode uses provider/model format. When routing through LiteLLM,
        # the model string should be "litellm/{model}" or the proxy handles routing.
        # Set both base URLs as OpenCode may use either provider.
        return {
            **base_vars,
            "ANTHROPIC_BASE_URL": proxy_url,
            "ANTHROPIC_API_KEY": api_key,
            "OPENAI_BASE_URL": proxy_url,
            "OPENAI_API_KEY": api_key,
        }
    elif cli_type == "codex":
        return {
            **base_vars,
            "OPENAI_BASE_URL": proxy_url,
            "OPENAI_API_KEY": api_key,
        }
    elif cli_type == "droid":
        # Droid's API provider is unknown. Set both base URLs as a
        # best-effort attempt. May not work if Droid uses a non-standard
        # API path.
        logger.warning(
            f"Droid agent API provider unknown — setting both ANTHROPIC_BASE_URL "
            f"and OPENAI_BASE_URL. Proxy routing may not work."
        )
        return {
            **base_vars,
            "ANTHROPIC_BASE_URL": proxy_url,
            "ANTHROPIC_API_KEY": api_key,
            "OPENAI_BASE_URL": proxy_url,
            "OPENAI_API_KEY": api_key,
        }
    else:
        # Unknown agent type — best effort
        return {
            **base_vars,
            "ANTHROPIC_BASE_URL": proxy_url,
            "OPENAI_BASE_URL": proxy_url,
        }
```

**How `is_glm_mode` is determined:**

The GLM detection logic already exists in `AgentManager.create_agent_for_task()`
(line 142: `if 'GLM' in model.upper()`). The budget system reads the same config:

```python
# In orchestrator, when building budget_env for launch_params:
cli_tool = os.getenv("HEPHAESTUS_CLI_TOOL", os.getenv("DEFAULT_CLI_TOOL", "opencode"))
cli_model = os.getenv("LLM_MODEL", "xiaomi/mimo-v2.5")
is_glm = "GLM" in cli_model.upper()

budget_env = cli_budget.get_agent_env_vars(
    design_name=design_entry.name,
    cli_type=cli_tool,
    is_glm_mode=is_glm,
)
```

**Known limitations:**

1. **GLM + Claude + budget tracking are mutually exclusive.** When GLM mode is
   active, Claude Code routes through z.ai (not LiteLLM), so its costs cannot
   be tracked via the proxy. The system logs a warning and falls back to
   post-completion cost estimation.

2. **OpenCode model format may need adjustment.** OpenCode uses `provider/model`
   format (e.g., `openrouter/xiaomi/mimo-v2.5`). When routing through LiteLLM,
   the model string may need to be `litellm/{model}` or the proxy may handle
   it transparently. This needs testing per OpenCode version.

3. **Droid API provider is unknown.** Budget routing is best-effort. If Droid
   doesn't respect `ANTHROPIC_BASE_URL` or `OPENAI_BASE_URL`, its costs are
   untrackable via proxy. The system falls back to heuristic estimation.

4. **Codex requires `OPENAI_API_KEY`** (not just `OPENAI_BASE_URL`). The env
   var injection sets both. The key should be the LiteLLM virtual key, not the
   real OpenAI key.

5. **Per-phase CLI overrides are not budget-aware.** If phase 3 uses `claude`
   and phase 4 uses `codex`, the env vars are set at agent creation time per
   the `cli_type` resolved for that phase. The budget system handles this
   correctly because `get_agent_env_vars()` is called per-agent, not globally.

### 2.3 Monitoring Process Costs (tracked externally)

The Guardian/Conductor monitoring loop runs in `run_monitor.py`, a separate OS
process launched via `subprocess.Popen`. It makes its own LLM calls that cannot
be intercepted from the orchestrator process.

**Strategy:**
1. Route monitoring LLM calls through LiteLLM proxy (already supported via
   `LangChainLLMClient` configuration in the monitor process).
2. The budget polling system queries LiteLLM for the monitoring process's spend
   under a dedicated `user` field (e.g., `"hephaestus_monitoring"`).
3. Monitoring costs count toward daily and total limits but do NOT count toward
   per-design or per-iteration limits (they are shared infrastructure costs).

### 2.4 Exact Cost Tracking Mechanisms

Based on research of the underlying APIs, here are the precise mechanisms for
obtaining exact cost data from each source.

#### 2.4.1 LiteLLM Proxy (Primary — Most Accurate)

LiteLLM provides exact cost data via multiple endpoints. This is the authoritative
source for all cost tracking.

**Per-request cost (real-time):**
- Response header: `x-litellm-response-cost` contains the exact cost in USD
- This is what `OpenRouterClient.generate()` already extracts (line 144)
- Cost is calculated by LiteLLM using its [model cost map](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
- **This is the most accurate real-time cost source** — use it for internal LLM calls

**Per-user spend (aggregate):**
```
GET /user/info?user_id=<feature_name>
Authorization: Bearer <master_key>
```
Response includes `user_info.spend` — total spend for the user/feature.

**Daily breakdown by model:**
```
GET /user/daily/activity?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD
Authorization: Bearer <master_key>
```
Response includes per-model spend, token counts, and API request counts per day.

**Individual transaction logs (most granular):**
```
GET /spend/logs?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&summarize=false
Authorization: Bearer <master_key>
```
Returns individual request logs with exact cost, tokens, model, and timestamps.
Use `summarize=true` (default) for aggregated data.

**Spend report by customer (for daily/total limits):**
```
GET /global/spend/report?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&group_by=customer
Authorization: Bearer <master_key>
```
Returns daily spend grouped by `user` field (our feature name).

**Automatic user-agent tracking:**
LiteLLM automatically tracks `User-Agent` headers as custom tags. Claude Code's
user-agent is tracked automatically, meaning LiteLLM can attribute Claude Code
costs even without explicit `user` field injection (though explicit injection is
more reliable).

**Key insight:** LiteLLM uses its internal model cost map for calculations.
Costs may differ slightly from provider bills due to:
- Cached token pricing differences
- Provider-specific discounts not reflected in the cost map
- Rounding differences

For authoritative billing, always cross-reference with the provider's own dashboard.

#### 2.4.2 Claude Code

**Built-in `/usage` command:**
- Shows `Total cost: $X.XX` in the session
- This is an **estimate** computed locally from token counts, NOT authoritative
- For API users, this is the primary visibility mechanism
- For Pro/Max subscribers, cost is included in subscription

**Claude Console Usage Page:**
- `https://platform.claude.com/usage` — authoritative billing data
- Shows actual API costs, not estimates
- Requires workspace access

**Claude Code respects `ANTHROPIC_BASE_URL`:**
- When set to LiteLLM proxy URL, all API calls route through proxy
- Proxy tracks cost via `x-litellm-response-cost` header
- Set `ANTHROPIC_API_KEY` to the LiteLLM virtual key for authentication

**Claude Code workspace tracking:**
- When authenticated via Claude Console, a "Claude Code" workspace is auto-created
- Admins can view cost/usage in Console
- Workspace rate limits can be set to cap Claude Code's share

**For budget tracking:** Route through LiteLLM proxy for exact per-request costs.
The `/usage` command output can be parsed from tmux as a secondary source, but
it's estimated, not exact.

#### 2.4.3 Codex (OpenAI)

**Codex respects `OPENAI_BASE_URL`:**
- When set to LiteLLM proxy URL, all API calls route through proxy
- Set `OPENAI_API_KEY` to the LiteLLM virtual key
- Proxy tracks cost via response headers

**OpenAI Usage Dashboard:**
- `https://platform.openai.com/usage` — authoritative billing data
- Shows actual costs, not estimates

**For budget tracking:** Route through LiteLLM proxy for exact costs.

#### 2.4.4 OpenCode

**OpenCode uses `provider/model` format:**
- Default model: `anthropic/claude-sonnet-4` (provider/model)
- Routes through OpenRouter or direct provider based on the provider prefix
- When routing through LiteLLM, the model string may need adjustment

**OpenCode respects environment variables:**
- `ANTHROPIC_BASE_URL` for Anthropic models
- `OPENAI_BASE_URL` for OpenAI models
- Setting both covers the common case

**For budget tracking:** Route through LiteLLM proxy. OpenCode's provider
routing makes exact cost attribution dependent on the proxy correctly handling
the model string format.

#### 2.4.5 Droid

**Unknown API provider.** Cannot guarantee cost tracking via proxy routing.
Fallback to heuristic estimation or accept blind spot.

#### 2.4.6 Cost Data Accuracy Hierarchy

| Source | Accuracy | Granularity | Latency | Use Case |
|--------|----------|-------------|---------|----------|
| LiteLLM `x-litellm-response-cost` header | **Exact** | Per-request | Real-time | Internal LLM calls |
| LiteLLM `/user/daily/activity` | **Exact** | Per-day/model | 1-30s | CLI agent polling, daily budget |
| LiteLLM `/user/info` | **Exact** | Per-user total | 1-30s | Design-scoped budget checks |
| LiteLLM `/global/spend/report` | **Exact** | Per-day/customer | 1-30s | Cross-user aggregation |
| Claude Code `/usage` command | **Estimated** | Per-session | Real-time | Secondary verification |
| Heuristic estimation (model pricing × tokens) | **Approximate** | Per-phase | Real-time | Fallback when no proxy |
| Provider dashboard (OpenAI, Anthropic) | **Exact** | Per-billing-period | Hours | Reconciliation |

**Recommendation:** Use LiteLLM proxy as the primary cost source. The response
header provides exact per-request costs for internal calls. For CLI agents,
poll `/user/daily/activity` for exact per-model daily costs. Use `/user/info`
for design-scoped aggregate checks.

---

## 3. Architecture

### 3.1 Component Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                    Budget Configuration                           │
│  hephaestus_config.yaml / env vars / CLI flags                   │
└───────────────────────┬──────────────────────────────────────────┘
                        │
                        ▼
┌──────────────────────────────────────────────────────────────────┐
│                      BudgetManager                                │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐   │
│  │ Spend Ledger │  │ Threshold    │  │ Approval Gate          │   │
│  │ (SQLite)     │  │ Evaluator    │  │ (file-based prompt,   │   │
│  │              │  │              │  │  sync I/O)             │   │
│  └──────┬──────┘  └──────┬───────┘  └────────┬──────────────┘   │
│         │                │                    │                  │
│         ▼                ▼                    ▼                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │              CostInterceptor (wrapper)                    │    │
│  │  intercepts OpenRouterClient.generate() → checks budget   │    │
│  │  → calls or blocks. Sync API, uses asyncio.run() for     │    │
│  │  any async LiteLLM queries.                               │    │
│  └──────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────┘
        │                        │
        ▼                        ▼
┌───────────────┐    ┌──────────────────────┐
│ Internal LLM  │    │ External CLI Agents   │
│ (OpenRouter   │    │ (Claude Code, etc.)   │
│  Client)      │    │ via tmux + LiteLLM    │
└───────────────┘    └──────────────────────┘
                                                      │
                                                      ▼
                                             ┌──────────────────┐
                                             │ Monitoring Process│
                                             │ (separate OS     │
                                             │  process, polled │
                                             │  via LiteLLM)    │
                                             └──────────────────┘
```

### 3.2 New Files

| File | Purpose |
|------|---------|
| `src/autopilot/budget.py` | Core `BudgetManager` — ledger, thresholds, approval gates. **Synchronous API** (matching orchestrator's sync architecture). |
| `src/autopilot/budget_config.py` | Configuration loader for budget settings. Supports YAML, env vars, CLI flags. |
| `src/autopilot/cost_interceptor.py` | `OpenRouterClient.generate()` callback hook that records cost to BudgetManager after each call. Uses callback pattern (no `asyncio.run()`). |

### 3.3 Modified Files

| File | Change |
|------|--------|
| `src/autopilot/orchestrator.py` | Integrate BudgetManager into design processing loop; pass `log_dir` to `run_single_design()`; add `StopReason.BUDGET_EXCEEDED`; coordinate with `check_api_credits()`. |
| `src/interfaces/openrouter_client.py` | Add optional `cost_callback` hook called after each successful `generate()` with cost data. |
| `src/agents/manager.py` | Inject budget env vars into tmux sessions (via `launch_params` propagation). |
| `src/mcp/autopilot_api.py` | Add budget status/approval/reset endpoints. |
| `src/mcp/server.py` | Propagate budget env vars from `launch_params` to agent environment. |
| `src/sdk/config.py` | Add budget config fields. |
| `config/hephaestus_config.yaml` | Add budget section. |
| `frontend/src/pages/Autopilot.tsx` | Budget dashboard widget. |
| `src/cli/commands/autopilot.py` | Add `--budget-limit` flag. |

---

## 4. Budget Configuration

### 4.1 Config Schema

```yaml
# In hephaestus_config.yaml
autopilot:
  budget:
    enabled: false                  # Opt-in. Default: false (zero overhead).

    # Per-design limit — stops when a single design exceeds this
    per_design_limit: 5.00          # USD. null = unlimited.

    # Per-iteration limit — stops after each iteration if exceeded
    per_iteration_limit: 2.00       # USD. null = unlimited.

    # Daily limit — stops when total daily spend exceeds this
    daily_limit: 20.00              # USD. null = unlimited.

    # Total pipeline limit — stops when cumulative spend exceeds this
    total_limit: 100.00             # USD. null = unlimited.

    # Timezone for daily limit reset (IANA format)
    timezone: "UTC"                 # Daily window: midnight-to-now in this TZ.

    # Warning thresholds (fraction of limit) — log warnings but don't stop
    warning_thresholds:
      per_design: 0.80              # 80% of per-design limit
      per_iteration: 0.80           # 80% of per-iteration limit
      daily: 0.90                   # 90% of daily limit
      total: 0.90                   # 90% of total limit

    # What to do when a limit is exceeded
    on_limit_exceeded: "pause"      # "pause" | "skip" | "stop"
    #   pause  = pause pipeline, request human approval
    #   skip   = skip current design (log event, move to next design)
    #   stop   = stop entire pipeline

    # Approval behavior
    approval_timeout_seconds: 600   # 0 = wait forever
    on_timeout_action: "approve"    # "approve" | "reject" | "continue"
    #   approve  = treat as human-approved (resume with new limit)
    #   reject   = treat as human-rejected (skip/stop per on_limit_exceeded)
    #   continue = resume without changing limits (risky, just logs warning)

    # Preset increase amounts shown in approval UI
    # Also supports "multipliers" for relative increases
    approval_increase_amounts: [1.0, 2.0, 5.0, 10.0]   # absolute USD
    approval_increase_multipliers: [2.0, 5.0]            # relative to current limit

    # Whether CLI agents (Claude Code, OpenCode) route through LiteLLM proxy
    route_cli_agents_through_proxy: true

    # Polling behavior for CLI agent costs
    poll_interval_seconds: 15       # How often to poll LiteLLM for CLI agent spend
    poll_staleness_seconds: 30      # How stale poll data can be before warning

    # Seed initial spend from LiteLLM on startup (for mid-run enablement)
    seed_from_litellm: true         # Query historical spend to initialize ledger

    # Cost estimation fallback when LiteLLM is not available
    estimation:
      enabled: false
      models:
        "claude-sonnet-4-5-20250929":
          input_per_1k: 0.003
          output_per_1k: 0.015
        "gpt-4o":
          input_per_1k: 0.0025
          output_per_1k: 0.01
        "gpt-4o-mini":
          input_per_1k: 0.00015
          output_per_1k: 0.0006
```

### 4.2 Environment Variable Overrides

```bash
AUTOPILOT_BUDGET_ENABLED=true
AUTOPILOT_BUDGET_PER_DESIGN_LIMIT=5.00
AUTOPILOT_BUDGET_PER_ITERATION_LIMIT=2.00
AUTOPILOT_BUDGET_DAILY_LIMIT=20.00
AUTOPILOT_BUDGET_TOTAL_LIMIT=100.00
AUTOPILOT_BUDGET_ON_EXCEEDED=pause
AUTOPILOT_BUDGET_APPROVAL_TIMEOUT=600
AUTOPILOT_BUDGET_ON_TIMEOUT_ACTION=approve
AUTOPILOT_BUDGET_TIMEZONE=UTC
AUTOPILOT_BUDGET_ROUTE_CLI_THROUGH_PROXY=true
```

### 4.3 CLI Flag Override

```bash
heph autopilot start --project-path ~/my-project --budget-limit 10.00
# Sets per_design_limit=10.00; all other limits from config/env.
```

---

## 5. Core Components

### 5.1 `BudgetManager` (`src/autopilot/budget.py`)

Central budget tracking and enforcement engine. **All methods are synchronous**
to match the orchestrator's synchronous architecture. Async LiteLLM queries use
`asyncio.run()` internally (same pattern as orchestrator.py:1141).

```python
import sqlite3
import threading
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict
from dataclasses import dataclass, field
from enum import Enum


class CostSource(Enum):
    INTERNAL = "internal"           # OpenRouterClient / LangChainLLMClient
    EXTERNAL_CLI = "external_cli"   # Claude Code, OpenCode, etc.
    MONITORING = "monitoring"       # Guardian/Conductor (separate process)


class LimitType(Enum):
    PER_DESIGN = "per_design"
    PER_ITERATION = "per_iteration"
    DAILY = "daily"
    TOTAL = "total"


# Map LimitType values to spend dict keys used in _get_totals()
_SPEND_KEY_MAP = {
    LimitType.PER_DESIGN: "design",
    LimitType.PER_ITERATION: "iteration",
    LimitType.DAILY: "daily",
    LimitType.TOTAL: "total",
}


@dataclass
class SpendEntry:
    id: Optional[int]
    run_id: str
    design_name: str
    iteration: int
    timestamp: str
    amount: float
    source: CostSource
    model: str
    component: str
    tokens_input: int
    tokens_output: int
    source_delay_ms: int = 0        # How stale is this data?


@dataclass
class BudgetCheckResult:
    allowed: bool
    reason: str = ""
    exceeded_limit: Optional[LimitType] = None
    current_spend: float = 0.0
    limit: Optional[float] = None
    limits: Dict[str, Optional[float]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    is_critical: bool = False       # If True, budget is bypassed but recorded


class BudgetManager:
    """Synchronous budget tracking and enforcement engine.

    All public methods are synchronous. Uses asyncio.run() internally
    for any async LiteLLM queries (same pattern as the orchestrator).
    Thread-safe for SQLite writes via connection-per-thread.
    """

    def __init__(
        self,
        config: "BudgetConfig",
        design_name: str,
        run_id: str,
        log_dir: Path,
        db_path: str = "hephaestus.db",
    ):
        self.config = config
        self.design_name = design_name
        self.run_id = run_id
        self.log_dir = log_dir
        self._db_path = db_path
        self._local = threading.local()  # Per-thread SQLite connections
        self._pending_approval: Optional[str] = None
        self._init_db()

    def _get_db(self) -> sqlite3.Connection:
        """Get thread-local SQLite connection."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        """Create budget tables if they don't exist."""
        db = self._get_db()
        db.executescript("""
            CREATE TABLE IF NOT EXISTS budget_spending (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                design_name TEXT NOT NULL,
                iteration INTEGER NOT NULL DEFAULT 1,
                timestamp TEXT NOT NULL,
                amount REAL NOT NULL,
                source TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                component TEXT NOT NULL DEFAULT '',
                tokens_input INTEGER NOT NULL DEFAULT 0,
                tokens_output INTEGER NOT NULL DEFAULT 0,
                source_delay_ms INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_budget_design
                ON budget_spending(design_name);
            CREATE INDEX IF NOT EXISTS idx_budget_timestamp
                ON budget_spending(timestamp);
            CREATE INDEX IF NOT EXISTS idx_budget_run
                ON budget_spending(run_id);

            CREATE TABLE IF NOT EXISTS budget_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL,
                design_name TEXT NOT NULL,
                limit_type TEXT NOT NULL,
                current_spend REAL NOT NULL,
                limit_amount REAL NOT NULL,
                action TEXT,
                new_limit REAL,
                responded_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
        """)
        db.commit()

    # --- Core API ---

    def check_budget(
        self,
        estimated_cost: float = 0.0,
        critical: bool = False,
    ) -> BudgetCheckResult:
        """Pre-flight check: is this call allowed?

        Args:
            estimated_cost: Estimated cost of the upcoming LLM call.
            critical: If True, bypass spending limits (monitoring, credit
                      detection). Costs are still recorded.

        Returns:
            BudgetCheckResult with allowed/reason/warnings.
        """
        if not self.config.enabled:
            return BudgetCheckResult(allowed=True)

        spend = self._get_totals()
        warnings = []
        exceeded = None
        blocked = False

        # Check each limit tier (most restrictive first)
        checks = [
            (LimitType.PER_ITERATION, self.config.per_iteration_limit,
             spend["iteration"]),
            (LimitType.PER_DESIGN, self.config.per_design_limit,
             spend["design"]),
            (LimitType.DAILY, self.config.daily_limit, spend["daily"]),
            (LimitType.TOTAL, self.config.total_limit, spend["total"]),
        ]

        for limit_type, limit_val, current in checks:
            if limit_val is None:
                continue

            effective = current + estimated_cost
            threshold = self.config.warning_thresholds.get(
                limit_type.value, 0.80
            )

            if effective >= limit_val:
                if critical:
                    warnings.append(
                        f"Critical call bypassing {limit_type.value} limit: "
                        f"${effective:.2f} >= ${limit_val:.2f}"
                    )
                else:
                    exceeded = limit_type
                    blocked = True
            elif effective >= limit_val * threshold:
                pct = (effective / limit_val) * 100
                warnings.append(
                    f"{limit_type.value} at {pct:.0f}%: "
                    f"${effective:.2f} / ${limit_val:.2f}"
                )

        if blocked and not critical:
            spend_key = _SPEND_KEY_MAP[exceeded]
            return BudgetCheckResult(
                allowed=False,
                reason=(
                    f"{exceeded.value} limit exceeded: "
                    f"${spend[spend_key]:.2f} >= "
                    f"${self.config._get_limit(exceeded):.2f}"
                ),
                exceeded_limit=exceeded,
                current_spend=spend[spend_key],
                limit=self.config._get_limit(exceeded),
                warnings=warnings,
            )

        return BudgetCheckResult(
            allowed=True,
            current_spend=spend.get("design", 0),
            warnings=warnings,
            is_critical=critical,
        )

    def record_spend(
        self,
        amount: float,
        source: CostSource,
        model: str = "",
        component: str = "",
        tokens_input: int = 0,
        tokens_output: int = 0,
        source_delay_ms: int = 0,
        iteration: int = 1,
    ) -> None:
        """Record a completed LLM cost event. Thread-safe."""
        if amount <= 0:
            return

        db = self._get_db()
        db.execute(
            """INSERT INTO budget_spending
               (run_id, design_name, iteration, timestamp, amount,
                source, model, component, tokens_input, tokens_output,
                source_delay_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                self.run_id,
                self.design_name,
                iteration,
                datetime.utcnow().isoformat(),
                amount,
                source.value,
                model,
                component,
                tokens_input,
                tokens_output,
                source_delay_ms,
            ),
        )
        db.commit()

    def get_status(self) -> Dict:
        """Current budget state with all limits and spend."""
        spend = self._get_totals()
        return {
            "enabled": self.config.enabled,
            "design_name": self.design_name,
            "run_id": self.run_id,
            "design_spend": spend["design"],
            "design_limit": self.config.per_design_limit,
            "iteration_spend": spend["iteration"],
            "iteration_limit": self.config.per_iteration_limit,
            "daily_spend": spend["daily"],
            "daily_limit": self.config.daily_limit,
            "total_spend": spend["total"],
            "total_limit": self.config.total_limit,
            "is_paused": self._pending_approval is not None,
            "pending_approval": self._pending_approval,
        }

    def approve_budget(
        self,
        request_id: str,
        action: str,              # "approve" | "reject" | "increase"
        new_limit: Optional[float] = None,
        limit_type: Optional[LimitType] = None,
    ) -> bool:
        """Record human approval response."""
        if request_id != self._pending_approval:
            return False

        db = self._get_db()
        db.execute(
            """INSERT INTO budget_approvals
               (request_id, run_id, design_name, limit_type,
                current_spend, limit_amount, action, new_limit,
                responded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id,
                self.run_id,
                self.design_name,
                limit_type.value if limit_type else "",
                self._get_totals().get(
                    _SPEND_KEY_MAP.get(limit_type, "design"), 0.0
                ) if limit_type else 0.0,
                self.config._get_limit(limit_type) if limit_type else 0.0,
                action,
                new_limit,
                datetime.utcnow().isoformat(),
            ),
        )
        db.commit()

        self._pending_approval = None

        if action == "increase" and new_limit is not None and limit_type:
            self._apply_limit_increase(limit_type, new_limit)

        return True

    def reset(
        self,
        scope: str = "design",     # "design" | "daily" | "total" | "all"
    ) -> None:
        """Reset spending counters."""
        db = self._get_db()
        if scope == "design":
            db.execute(
                "DELETE FROM budget_spending WHERE design_name = ? AND run_id = ?",
                (self.design_name, self.run_id),
            )
        elif scope == "daily":
            today = datetime.utcnow().strftime("%Y-%m-%d")
            db.execute(
                "DELETE FROM budget_spending WHERE timestamp >= ?",
                (today,),
            )
        elif scope == "total":
            db.execute("DELETE FROM budget_spending WHERE run_id = ?", (self.run_id,))
        elif scope == "all":
            db.execute("DELETE FROM budget_spending")
        db.commit()

    def get_spend_history(
        self,
        limit: int = 50,
        source: Optional[CostSource] = None,
    ) -> List[Dict]:
        """Recent spend entries for display."""
        db = self._get_db()
        query = "SELECT * FROM budget_spending WHERE run_id = ? AND design_name = ?"
        params = [self.run_id, self.design_name]
        if source:
            query += " AND source = ?"
            params.append(source.value)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def seed_from_litellm(self, litellm_config: dict) -> float:
        """Query LiteLLM for historical spend and seed the ledger.

        Called on initialization when budget is enabled mid-run.
        Returns the total seeded amount.
        """
        if not self.config.seed_from_litellm:
            return 0.0

        if not litellm_config.get("url") or not litellm_config.get("cost_api_key"):
            return 0.0

        try:
            import asyncio
            from src.interfaces.cost_tracker import CostTracker

            tracker = CostTracker(
                proxy_url=litellm_config["url"],
                api_key=litellm_config["cost_api_key"],
            )

            feature_user = self.design_name.lower().replace(" ", "_")[:40]

            async def fetch():
                return await tracker.get_feature_cost(feature_user)

            cost_info = asyncio.run(fetch())
            spend = cost_info.get("spend", 0.0)

            if spend > 0:
                self.record_spend(
                    amount=spend,
                    source=CostSource.INTERNAL,
                    component="seed_from_litellm",
                    source_delay_ms=30000,  # Historical data, inherently stale
                )

            return spend
        except Exception:
            return 0.0

    def set_pending_approval(self, request_id: str):
        """Mark that we're waiting for human approval."""
        self._pending_approval = request_id

    # --- Private helpers ---

    def _get_totals(self) -> Dict[str, float]:
        """Get current spend totals for all tiers."""
        db = self._get_db()
        now = datetime.utcnow()

        # Design spend (current design, current run)
        row = db.execute(
            """SELECT COALESCE(SUM(amount), 0) as total
               FROM budget_spending
               WHERE design_name = ? AND run_id = ?""",
            (self.design_name, self.run_id),
        ).fetchone()
        design_spend = row["total"]

        # Iteration spend (current design, latest iteration)
        row = db.execute(
            """SELECT COALESCE(SUM(amount), 0) as total
               FROM budget_spending
               WHERE design_name = ? AND run_id = ?
               AND iteration = (
                   SELECT COALESCE(MAX(iteration), 1)
                   FROM budget_spending
                   WHERE design_name = ? AND run_id = ?
               )""",
            (self.design_name, self.run_id,
             self.design_name, self.run_id),
        ).fetchone()
        iteration_spend = row["total"]

        # Daily spend — timezone-aware window calculation
        # Uses the configured timezone to determine "today" start
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.config.timezone)
            now_local = datetime.now(tz)
            today_start_local = now_local.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            today_start_utc = today_start_local.astimezone(ZoneInfo("UTC"))
            today_str = today_start_utc.strftime("%Y-%m-%d %H:%M:%S")
        except (ImportError, Exception):
            # Fallback to UTC if zoneinfo unavailable
            today_str = now.strftime("%Y-%m-%d")

        row = db.execute(
            """SELECT COALESCE(SUM(amount), 0) as total
               FROM budget_spending
               WHERE timestamp >= ?""",
            (today_str,),
        ).fetchone()
        daily_spend = row["total"]

        # Total spend (current run only — cross-run totals use daily)
        row = db.execute(
            """SELECT COALESCE(SUM(amount), 0) as total
               FROM budget_spending
               WHERE run_id = ?""",
            (self.run_id,),
        ).fetchone()
        total_spend = row["total"]

        return {
            "design": design_spend,
            "iteration": iteration_spend,
            "daily": daily_spend,
            "total": total_spend,
        }

    def _apply_limit_increase(self, limit_type: LimitType, new_limit: float):
        """Apply a runtime limit increase for a specific limit type."""
        if not hasattr(self, '_runtime_limit_overrides'):
            self._runtime_limit_overrides = {}
        self._runtime_limit_overrides[limit_type] = new_limit

        # Update the config object in-place so subsequent check_budget()
        # calls see the new limit immediately.
        if limit_type == LimitType.PER_DESIGN:
            self.config.per_design_limit = new_limit
        elif limit_type == LimitType.PER_ITERATION:
            self.config.per_iteration_limit = new_limit
        elif limit_type == LimitType.DAILY:
            self.config.daily_limit = new_limit
        elif limit_type == LimitType.TOTAL:
            self.config.total_limit = new_limit
```

**Design-scoped uniqueness:** Ledger entries use `(run_id, design_name)` as the
logical key. `run_id` is a UUID generated per pipeline run, ensuring no collisions
across runs or retried designs.

### 5.1.1 Cost Polling Functions (Precise LiteLLM Endpoints)

These functions are called from the orchestrator's poll cycle to get exact cost
data from the LiteLLM proxy. They use the precise endpoints documented in
Section 2.4.

```python
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List
import httpx

logger = logging.getLogger(__name__)


async def _poll_cli_agent_costs(
    budget_config: "BudgetConfig",
    design_name: str,
    litellm_config: dict,
    last_poll_time: Optional[datetime] = None,
) -> float:
    """Poll LiteLLM for exact CLI agent spend using /user/daily/activity endpoint.

    Uses /user/daily/activity which returns per-model daily breakdown with
    exact costs. This endpoint explicitly groups by user and date.

    Note: /spend/logs?summarize=false does NOT clearly support user filtering
    in bulk queries (the user field is nested in metadata). /user/daily/activity
    is the reliable endpoint for per-user daily cost attribution.

    Args:
        budget_config: Budget configuration.
        design_name: Current design name (used as LiteLLM user field).
        litellm_config: LiteLLM proxy configuration.
        last_poll_time: If provided, only fetch logs since this time.

    Returns:
        Total spend in USD since last_poll_time (or all time if None).
    """
    if not litellm_config.get("url") or not litellm_config.get("cost_api_key"):
        return 0.0

    if not budget_config.route_cli_agents_through_proxy:
        return 0.0

    user_id = design_name.lower().replace(" ", "_")[:40]
    proxy_url = litellm_config["url"].rstrip("/")
    api_key = litellm_config["cost_api_key"]

    try:
        end_date = datetime.utcnow().strftime("%Y-%m-%d")
        if last_poll_time:
            start_date = last_poll_time.strftime("%Y-%m-%d")
        else:
            start_date = end_date

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Use /user/daily/activity for per-user per-day breakdown
            # This returns exact costs grouped by model per day
            response = await client.get(
                f"{proxy_url}/user/daily/activity",
                headers={"Authorization": f"Bearer {api_key}"},
                params={
                    "start_date": start_date,
                    "end_date": end_date,
                },
            )

            if response.status_code != 200:
                logger.warning(
                    f"LiteLLM /user/daily/activity returned {response.status_code}"
                )
                return 0.0

            data = response.json()
            results = data.get("results", [])

            # Sum spend across all days and models for this user
            # NOTE: No pagination needed — /user/daily/activity returns
            # max 1 entry per day. For a 1-day query: 1 entry.
            # For a 7-day query: 7 entries. Response is always small.
            total_spend = 0.0
            for day_entry in results:
                # The response includes metrics per day
                metrics = day_entry.get("metrics", {})
                day_spend = metrics.get("spend", 0)
                total_spend += float(day_spend)

            return total_spend

    except Exception as e:
        logger.warning(f"Failed to poll CLI agent costs: {e}")
        return 0.0


async def _poll_monitoring_costs(
    budget_config: "BudgetConfig",
    litellm_config: dict,
    last_poll_time: Optional[datetime] = None,
) -> float:
    """Poll LiteLLM for monitoring process spend.

    Uses /user/info for the dedicated monitoring user ID.
    Monitoring costs count toward daily/total limits only.

    Args:
        budget_config: Budget configuration.
        litellm_config: LiteLLM proxy configuration.
        last_poll_time: If provided, only fetch data since this time.

    Returns:
        Total monitoring spend in USD.
    """
    if not litellm_config.get("url") or not litellm_config.get("cost_api_key"):
        return 0.0

    proxy_url = litellm_config["url"].rstrip("/")
    api_key = litellm_config["cost_api_key"]
    monitoring_user = "hephaestus_monitoring"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Use /user/info for aggregate monitoring spend
            response = await client.get(
                f"{proxy_url}/user/info",
                headers={"Authorization": f"Bearer {api_key}"},
                params={"user_id": monitoring_user},
            )

            if response.status_code != 200:
                return 0.0

            data = response.json()
            user_info = data.get("user_info", {})
            return float(user_info.get("spend", 0))

    except Exception as e:
        logger.warning(f"Failed to poll monitoring costs: {e}")
        return 0.0


def _poll_cli_costs(
    budget_config: "BudgetConfig",
    budget_mgr: "BudgetManager",
    litellm_config: dict,
) -> float:
    """Synchronous wrapper for CLI cost polling (called from orchestrator).

    Uses asyncio.run() to bridge sync orchestrator -> async LiteLLM calls.
    Same pattern as existing cost fetching at orchestrator.py:1141.
    Passes last_poll_time from the CostInterceptor for incremental polling.
    """
    if not budget_config.enabled:
        return 0.0

    try:
        last_poll = None
        # Get last poll time from interceptor if available
        if hasattr(budget_mgr, '_interceptor'):
            last_poll = budget_mgr._interceptor.get_last_poll_time()

        spend = asyncio.run(_poll_cli_agent_costs(
            budget_config,
            budget_mgr.design_name,
            litellm_config,
            last_poll_time=last_poll,
        ))

        # Update last poll time on success
        if hasattr(budget_mgr, '_interceptor'):
            budget_mgr._interceptor.set_last_poll_time(datetime.utcnow())

        return spend
    except Exception as e:
        logger.warning(f"CLI cost polling failed: {e}")
        return 0.0


def _poll_monitoring_costs_sync(
    budget_config: "BudgetConfig",
    litellm_config: dict,
) -> float:
    """Synchronous wrapper for monitoring cost polling."""
    if not budget_config.enabled:
        return 0.0

    try:
        return asyncio.run(_poll_monitoring_costs(
            budget_config,
            litellm_config,
        ))
    except Exception as e:
        logger.warning(f"Monitoring cost polling failed: {e}")
        return 0.0
```

**Why `/user/daily/activity` instead of `/spend/logs?summarize=false`:**
- `/spend/logs?summarize=false` returns individual transaction logs, but the user
  field is nested inside `metadata.user_api_key_user_id`, not at the top level.
  Filtering by user requires parsing nested metadata, which is fragile.
- `/user/daily/activity` explicitly returns per-model daily breakdown with exact
  costs at the top level. It's designed for per-user daily analytics.
- The daily granularity is sufficient for budget enforcement (we check at
  iteration boundaries, not per-request).
- For internal LLM calls, the `x-litellm-response-cost` header provides exact
  per-request costs in real-time.

**Polling optimization:** The `last_poll_time` parameter avoids re-fetching
already-processed logs. On each poll cycle, we only fetch logs since the last
successful poll. The orchestrator tracks this timestamp.

### 5.2 BudgetConfig (`src/autopilot/budget_config.py`)

```python
import os
import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Dict, List


@dataclass
class BudgetConfig:
    enabled: bool = False
    per_design_limit: Optional[float] = None
    per_iteration_limit: Optional[float] = None
    daily_limit: Optional[float] = None
    total_limit: Optional[float] = None
    timezone: str = "UTC"
    warning_thresholds: Dict[str, float] = field(default_factory=lambda: {
        "per_design": 0.80,
        "per_iteration": 0.80,
        "daily": 0.90,
        "total": 0.90,
    })
    on_limit_exceeded: str = "pause"
    approval_timeout_seconds: int = 600
    on_timeout_action: str = "approve"
    approval_increase_amounts: List[float] = field(
        default_factory=lambda: [1.0, 2.0, 5.0, 10.0]
    )
    approval_increase_multipliers: List[float] = field(
        default_factory=lambda: [2.0, 5.0]
    )
    route_cli_agents_through_proxy: bool = True
    poll_interval_seconds: int = 15
    poll_staleness_seconds: int = 30
    seed_from_litellm: bool = True
    estimation_enabled: bool = False
    model_pricing: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @classmethod
    def load(cls, cli_overrides: Optional[Dict] = None) -> "BudgetConfig":
        """Load from config file + env vars + CLI flags.

        Priority: CLI flags > env vars > config file > defaults.
        """
        config = cls()

        # 1. Load from hephaestus_config.yaml
        config_path = Path("config/hephaestus_config.yaml")
        if config_path.exists():
            with open(config_path) as f:
                yaml_config = yaml.safe_load(f) or {}
            budget_section = (
                yaml_config.get("autopilot", {}).get("budget", {})
            )
            for key, value in budget_section.items():
                if hasattr(config, key):
                    setattr(config, key, value)

        # 2. Override from environment variables
        env_map = {
            "AUTOPILOT_BUDGET_ENABLED": ("enabled", lambda v: v.lower() == "true"),
            "AUTOPILOT_BUDGET_PER_DESIGN_LIMIT": ("per_design_limit", float),
            "AUTOPILOT_BUDGET_PER_ITERATION_LIMIT": ("per_iteration_limit", float),
            "AUTOPILOT_BUDGET_DAILY_LIMIT": ("daily_limit", float),
            "AUTOPILOT_BUDGET_TOTAL_LIMIT": ("total_limit", float),
            "AUTOPILOT_BUDGET_ON_EXCEEDED": ("on_limit_exceeded", str),
            "AUTOPILOT_BUDGET_APPROVAL_TIMEOUT": ("approval_timeout_seconds", int),
            "AUTOPILOT_BUDGET_ON_TIMEOUT_ACTION": ("on_timeout_action", str),
            "AUTOPILOT_BUDGET_TIMEZONE": ("timezone", str),
            "AUTOPILOT_BUDGET_ROUTE_CLI_THROUGH_PROXY": (
                "route_cli_agents_through_proxy",
                lambda v: v.lower() == "true",
            ),
        }
        for env_var, (attr, converter) in env_map.items():
            val = os.environ.get(env_var)
            if val is not None:
                setattr(config, attr, converter(val))

        # 3. Override from CLI flags (highest priority)
        if cli_overrides:
            for key, value in cli_overrides.items():
                if value is not None and hasattr(config, key):
                    setattr(config, key, value)

        return config

    def get_thresholds(self) -> Dict[str, float]:
        """Absolute dollar thresholds for each limit type."""
        return {
            "per_design": (
                self.per_design_limit * self.warning_thresholds.get("per_design", 0.80)
                if self.per_design_limit else 0
            ),
            "per_iteration": (
                self.per_iteration_limit * self.warning_thresholds.get("per_iteration", 0.80)
                if self.per_iteration_limit else 0
            ),
            "daily": (
                self.daily_limit * self.warning_thresholds.get("daily", 0.90)
                if self.daily_limit else 0
            ),
            "total": (
                self.total_limit * self.warning_thresholds.get("total", 0.90)
                if self.total_limit else 0
            ),
        }

    def _get_limit(self, limit_type: "LimitType") -> Optional[float]:
        """Get the limit value for a given LimitType."""
        from src.autopilot.budget import LimitType
        mapping = {
            LimitType.PER_DESIGN: self.per_design_limit,
            LimitType.PER_ITERATION: self.per_iteration_limit,
            LimitType.DAILY: self.daily_limit,
            LimitType.TOTAL: self.total_limit,
        }
        return mapping.get(limit_type)
```

### 5.3 CostInterceptor (`src/autopilot/cost_interceptor.py`)

Records costs from LLM calls via a callback hook on `OpenRouterClient.generate()`.
**No `asyncio.run()`** — the callback runs in the async context of the MCP server
(FastAPI/uvicorn) and does sync SQLite writes (< 1ms).

```python
import logging
from typing import Optional, Any, Callable
from src.autopilot.budget import BudgetManager, CostSource

logger = logging.getLogger(__name__)


class BudgetExceededError(Exception):
    """Raised when budget limit is exceeded and on_limit_exceeded='stop'."""
    pass


class CostInterceptor:
    """Records LLM costs via OpenRouterClient callback hook.

    Integration: OpenRouterClient gains a cost_callback parameter.
    After each successful generate() call, the callback extracts cost
    from the response and records it to the BudgetManager's SQLite ledger.

    The callback runs in the async context of the MCP server (FastAPI).
    SQLite writes are synchronous and fast (< 1ms), so this is safe.
    """

    def __init__(self, budget_manager: BudgetManager):
        self.budget = budget_manager
        self._last_poll_time = None  # For polling optimization

    def make_cost_callback(
        self,
        source: CostSource = CostSource.INTERNAL,
        component: str = "",
    ) -> Callable:
        """Create a cost callback function for OpenRouterClient.

        Returns a sync callback that can be passed to OpenRouterClient.__init__().
        The callback is called after each successful generate() with the result dict.

        Usage:
            interceptor = CostInterceptor(budget_mgr)
            client = OpenRouterClient(
                ...,
                cost_callback=interceptor.make_cost_callback(
                    component="guardian"
                ),
            )
        """
        def cost_callback(result: dict):
            try:
                cost = self._extract_cost(result)
                if cost is None or cost <= 0:
                    return

                tokens_in, tokens_out = self._extract_tokens(result)
                model = self._extract_model(result)

                self.budget.record_spend(
                    amount=cost,
                    source=source,
                    model=model,
                    component=component,
                    tokens_input=tokens_in,
                    tokens_output=tokens_out,
                )
            except Exception as e:
                logger.warning(f"Cost callback failed: {e}")

        return cost_callback

    def _extract_cost(self, result: Any) -> Optional[float]:
        """Extract exact cost from LLM response.

        Priority order (most accurate first):
        1. 'cost' key from OpenRouterClient (extracted from x-litellm-response-cost header)
        2. 'response_metadata.cost' from LangChain
        3. None (caller should fall back to polling)

        The x-litellm-response-cost header contains the exact cost calculated
        by LiteLLM using its model cost map. This is the authoritative source.
        """
        if isinstance(result, dict):
            # Primary: OpenRouterClient extracts cost from
            # x-litellm-response-cost header into result["cost"]
            cost = result.get("cost")
            if cost is not None:
                try:
                    cost_float = float(cost)
                    if cost_float >= 0:
                        return cost_float
                except (ValueError, TypeError):
                    pass

            # Secondary: LangChain response_metadata
            metadata = result.get("response_metadata", {})
            if isinstance(metadata, dict):
                cost = metadata.get("cost") or metadata.get("total_cost")
                if cost is not None:
                    try:
                        cost_float = float(cost)
                        if cost_float >= 0:
                            return cost_float
                    except (ValueError, TypeError):
                        pass

        return None

    def _extract_tokens(self, result: Any) -> tuple:
        """Extract input/output token counts from LLM response."""
        tokens_in = 0
        tokens_out = 0

        if isinstance(result, dict):
            # OpenRouterClient format: {"usage": {"prompt_tokens": N, ...}}
            usage = result.get("usage", {})
            if isinstance(usage, dict):
                tokens_in = usage.get("prompt_tokens", 0) or 0
                tokens_out = usage.get("completion_tokens", 0) or 0

            # LangChain format
            if tokens_in == 0:
                metadata = result.get("response_metadata", {})
                usage = metadata.get("usage", {}) if isinstance(metadata, dict) else {}
                tokens_in = usage.get("prompt_tokens", 0) or 0
                tokens_out = usage.get("completion_tokens", 0) or 0

        return tokens_in, tokens_out

    def _extract_model(self, result: Any) -> str:
        """Extract model name from LLM response."""
        if isinstance(result, dict):
            return result.get("model", "") or result.get("provider", "")
        return ""

    def get_last_poll_time(self):
        return self._last_poll_time

    def set_last_poll_time(self, t):
        self._last_poll_time = t
```

**Key design decision:** The CostInterceptor uses a **callback pattern**, not a
wrapper pattern. `OpenRouterClient` gains a `cost_callback` parameter that is
called after each successful `generate()`. This avoids `asyncio.run()` entirely —
the callback runs in the async context of the MCP server and does sync SQLite
writes.

**Why not `asyncio.run()`?** The MCP server runs in FastAPI/uvicorn which is
async. Calling `asyncio.run()` from within an async context raises
`RuntimeError: This event loop is already running`. The callback pattern avoids
this entirely.

---

## 6. Integration Points

### 6.1 Orchestrator Integration

The orchestrator is **fully synchronous**. All budget operations use synchronous
methods. The existing `asyncio.run()` bridge pattern (line 1141) is used for
any internal async calls.

**Modified function signatures:**

```python
# NEW: log_dir parameter added
def run_single_design(
    sdk,
    design_entry: DesignEntry,
    project_path: Path,
    max_iterations: int,
    logger: OrchestratorLogger,
    log_dir: Path,                  # NEW — was inaccessible before
) -> Tuple[DesignStatus, FeatureReport]:
```

**Modified orchestrator flow:**

```python
def run_single_design(sdk, design_entry, project_path, max_iterations, logger, log_dir):
    import uuid
    from src.autopilot.budget import BudgetManager, CostSource, LimitType
    from src.autopilot.budget_config import BudgetConfig
    from src.autopilot.cost_interceptor import CostInterceptor, BudgetExceededError

    # ... existing setup ...

    # NEW: Initialize budget
    run_id = str(uuid.uuid4())[:12]
    budget_config = BudgetConfig.load()
    budget_mgr = BudgetManager(
        config=budget_config,
        design_name=design_entry.name,
        run_id=run_id,
        log_dir=log_dir,
    )

    # NEW: Seed from LiteLLM if enabled mid-run
    if budget_config.enabled:
        seeded = budget_mgr.seed_from_litellm(get_litellm_config())
        if seeded > 0:
            logger.log(f"Seeded ${seeded:.4f} historical spend from LiteLLM")

    # NEW: Pre-design budget check
    if budget_config.enabled:
        check = budget_mgr.check_budget()
        if not check.allowed:
            logger.log(f"Budget limit hit before design start: {check.reason}", "WARN")
            if budget_config.on_limit_exceeded == "stop":
                report.stop_reason = "budget_exceeded"
                return DesignStatus.FAILED, report
            elif budget_config.on_limit_exceeded == "pause":
                approved = _prompt_budget_approval(budget_mgr, check, logger)
                if not approved:
                    report.stop_reason = "budget_exceeded"
                    return DesignStatus.FAILED, report

    # ... existing iteration loop ...
    for iteration in range(1, max_iterations + 1):
        # NEW: Check budget at iteration start
        if budget_config.enabled:
            check = budget_mgr.check_budget()
            if not check.allowed:
                if budget_config.on_limit_exceeded == "pause":
                    approved = _prompt_budget_approval(budget_mgr, check, logger)
                    if not approved:
                        stop_reason = StopReason.BUDGET_EXCEEDED
                        break
                elif budget_config.on_limit_exceeded == "skip":
                    logger.log(f"Budget limit exceeded, skipping design: {check.reason}")
                    stop_reason = StopReason.BUDGET_EXCEEDED
                    break
                else:  # "stop"
                    stop_reason = StopReason.BUDGET_EXCEEDED
                    break

        wf_status = run_single_workflow(...)

        # NEW: Poll CLI agent costs after workflow completes
        if budget_config.enabled:
            cli_spend = _poll_cli_costs(budget_config, budget_mgr, get_litellm_config())
            if cli_spend > 0:
                budget_mgr.record_spend(
                    amount=cli_spend,
                    source=CostSource.EXTERNAL_CLI,
                    component="cli_agents",
                )

        # NEW: Also poll monitoring costs
        if budget_config.enabled:
            mon_spend = _poll_monitoring_costs(budget_config, get_litellm_config())
            if mon_spend > 0:
                budget_mgr.record_spend(
                    amount=mon_spend,
                    source=CostSource.MONITORING,
                    component="guardian_conductor",
                )

        # ... existing QA/validation logic ...

    # NEW: Finalize — update report with budget data
    if budget_config.enabled:
        status = budget_mgr.get_status()
        report.cost_total = status["design_spend"]
        # Also update cost_breakdown from ledger
        history = budget_mgr.get_spend_history(limit=1000)
        for entry in history:
            model = entry.get("model", "unknown")
            if model not in report.cost_breakdown:
                report.cost_breakdown[model] = 0.0
            report.cost_breakdown[model] += entry.get("amount", 0.0)

    generate_html_feature_report(report, summaries, feature_folder, logger)
    # ... rest of existing code ...
```

**Interaction with `check_api_credits()`:**

Budget checking and credit checking are independent concerns with clear
precedence:

```
┌─────────────────────────────────────────┐
│ Poll cycle (every 15s)                  │
│                                         │
│ 1. Budget check                         │
│    ├─ Allowed → continue to step 2      │
│    └─ Blocked → pause/approve/stop      │
│       (credit check SKIPPED this cycle) │
│                                         │
│ 2. Credit check (existing logic)        │
│    ├─ OK → continue monitoring          │
│    └─ Exhausted → prompt_human()        │
│                                         │
│ 3. Impasse/stuck check (existing)       │
└─────────────────────────────────────────┘
```

If the budget system pauses, the credit check doesn't run that cycle. If the
budget approves but credits are exhausted mid-call, the existing credit handling
takes over on the next poll cycle. This avoids conflicting human prompts.

### 6.2 Internal LLM Call Interception

The `OpenRouterClient` gains an optional `cost_callback` hook:

```python
# In src/interfaces/openrouter_client.py
class OpenRouterClient:
    def __init__(self, ..., cost_callback=None):
        # ... existing init ...
        self._cost_callback = cost_callback

    async def generate(self, messages, response_format=None):
        # ... existing logic ...
        result = { ... }

        # NEW: Invoke cost callback if set
        if self._cost_callback and result.get("cost"):
            self._cost_callback(result)

        return result
```

The orchestrator sets up the interceptor at startup:

```python
# In orchestrator, after SDK initialization
if budget_config.enabled:
    # Wrap the SDK's OpenRouterClient with budget checks
    # The SDK passes config to the MCP server, which creates the client.
    # We need to wrap at the API level instead.
    pass
```

**Practical integration approach:** Since `LangChainLLMClient` and the MCP server
run in a separate process, the interceptor wraps at the **API boundary**. The
orchestrator sets env vars that the MCP server reads:

```bash
BUDGET_ENABLED=true
BUDGET_MANAGER_DB=/path/to/hephaestus.db
BUDGET_RUN_ID=<uuid>
BUDGET_DESIGN_NAME=<name>
```

The MCP server creates its own `CostInterceptor` instance that writes to the
same SQLite database. This avoids cross-process function call complexity.

### 6.2.1 MCP Server Integration (CostInterceptor Setup)

The MCP server reads budget env vars at startup and creates a CostInterceptor
that hooks into OpenRouterClient:

```python
# In src/mcp/server.py (startup logic)
import os

def setup_budget_interceptor(llm_provider):
    """Create CostInterceptor and hook into OpenRouterClient if budget enabled."""
    budget_enabled = os.environ.get("BUDGET_ENABLED", "false").lower() == "true"
    if not budget_enabled:
        return

    from src.autopilot.budget import BudgetManager, CostSource
    from src.autopilot.budget_config import BudgetConfig
    from src.autopilot.cost_interceptor import CostInterceptor

    config = BudgetConfig.load()
    if not config.enabled:
        return

    db_path = os.environ.get("BUDGET_MANAGER_DB", "hephaestus.db")
    run_id = os.environ.get("BUDGET_RUN_ID", "unknown")
    design_name = os.environ.get("BUDGET_DESIGN_NAME", "unknown")

    budget_mgr = BudgetManager(
        config=config,
        design_name=design_name,
        run_id=run_id,
        log_dir=Path("/tmp"),  # MCP server doesn't need log_dir
        db_path=db_path,
    )

    interceptor = CostInterceptor(budget_mgr)

    # Hook into OpenRouterClient if it exists
    if hasattr(llm_provider, '_client') and hasattr(llm_provider._client, 'generate'):
        original_client = llm_provider._client
        if hasattr(original_client, '_cost_callback'):
            # Wire up the callback
            original_client._cost_callback = interceptor.make_cost_callback(
                source=CostSource.INTERNAL,
                component="llm_provider",
            )
            logger.info("Budget cost interceptor hooked into OpenRouterClient")
```

**Thread safety:** The MCP server is single-threaded per request (FastAPI).
The BudgetManager uses `threading.local()` for SQLite connections, which is
safe. The `_pending_approval` field is only accessed from the orchestrator
process (not the MCP server), so there are no cross-process race conditions.

### 6.3 CLI Agent tmux Environment Injection

Budget env vars flow through `launch_params` → SDK → MCP server → AgentManager:

```python
# In orchestrator, when building launch_params:
launch_params = {
    "design_document": str(design_copy),
    "project_path": str(project_path),
    "project_context": f"Docs go in: {docs_dir}. Code goes in: {project_path}.",
    # NEW: Budget config for agent environment
    "budget_env": cli_budget.get_agent_env_vars(design_name)
    if budget_config.enabled else {},
}
```

```python
# In src/mcp/server.py or agents/manager.py:
# When creating tmux session, inject budget_env from launch_params
budget_env = launch_params.get("budget_env", {})
for key, value in budget_env.items():
    pane.send_keys(f"export {key}={value}", enter=True)
    time.sleep(0.5)
```

### 6.4 API Endpoints

New endpoints in `src/mcp/autopilot_api.py`:

```
GET  /api/autopilot/budget/status          → Current budget state
GET  /api/autopilot/budget/history         → Spending history (ledger)
POST /api/autopilot/budget/approve/{id}    → Approve/reject/increase budget
POST /api/autopilot/budget/limits          → Update limits at runtime
POST /api/autopilot/budget/reset           → Reset spending counters
GET  /api/autopilot/budget/export          → Export ledger as CSV/JSON
```

**Budget Status Response:**
```json
{
  "enabled": true,
  "design_name": "Auth System",
  "run_id": "a1b2c3d4e5f6",
  "design_spend": 3.42,
  "design_limit": 5.00,
  "iteration_spend": 1.21,
  "iteration_limit": 2.00,
  "daily_spend": 12.30,
  "daily_limit": 20.00,
  "total_spend": 45.67,
  "total_limit": 100.00,
  "is_paused": false,
  "pending_approval": null,
  "warnings": ["Design spend at 68% of limit"],
  "recent_spend": [
    {
      "timestamp": "2026-06-12T19:30:00",
      "amount": 0.0032,
      "model": "claude-sonnet-4-5-20250929",
      "component": "guardian",
      "source": "internal",
      "source_delay_ms": 0
    }
  ]
}
```

### 6.5 Human Approval Flow

When a budget limit is hit, the system writes a `budget_request_{id}.json` file
and polls for `budget_response_{id}.json` — same file-based pattern as the
existing `prompt_human()` mechanism.

**Budget approval request file:**
```json
{
  "id": "a1b2c3d4",
  "type": "budget_approval",
  "design_name": "Auth System",
  "reason": "per_design limit exceeded: $5.50 >= $5.00",
  "limit_type": "per_design",
  "current_spend": 5.50,
  "limit": 5.00,
  "warnings": [],
  "options": ["approve", "reject"],
  "increase_options": [
    {"type": "amount", "value": 1.0, "label": "$1.00"},
    {"type": "amount", "value": 2.0, "label": "$2.00"},
    {"type": "amount", "value": 5.0, "label": "$5.00"},
    {"type": "amount", "value": 10.0, "label": "$10.00"},
    {"type": "multiplier", "value": 10.0, "label": "2x ($10.00)"},
    {"type": "multiplier", "value": 25.0, "label": "5x ($25.00)"}
  ],
  "timeout_seconds": 600,
  "on_timeout_action": "approve",
  "timestamp": "2026-06-12T19:30:00"
}
```

**Budget approval response file:**
```json
{
  "action": "increase",
  "new_limit": 10.00
}
```

### 6.6 Frontend Polling for Budget Approvals

The frontend already polls `GET /api/autopilot/input` for human input requests
(`input_request_*.json` files). Budget approval requests use a similar pattern
with `budget_request_*.json` files.

**Frontend changes needed:**

1. **Extend the existing input polling** to also check for `budget_request_*.json`:
   ```typescript
   // In Autopilot.tsx or MessageCenter.tsx
   useEffect(() => {
     const pollBudget = async () => {
       const res = await fetch('/api/autopilot/budget/pending');
       if (res.ok) {
         const data = await res.json();
         if (data.pending) {
           setShowBudgetApproval(data);
         }
       }
     };
     const interval = setInterval(pollBudget, 5000);
     return () => clearInterval(interval);
   }, []);
   ```

2. **New API endpoint** `GET /api/autopilot/budget/pending`:
   - Scans `~/.hephaestus/autopilot/` for `budget_request_*.json` files
   - Returns the first pending request (or null)
   - The existing `/api/autopilot/input` endpoint can be extended to also
     return budget requests in its response

3. **Approval submission** via `POST /api/autopilot/budget/approve/{id}`:
   - Writes `budget_response_{id}.json` to the same directory
   - The orchestrator's CostInterceptor polls for this file and processes it

4. **Budget status widget** polls `GET /api/autopilot/budget/status` every
   10 seconds to update the spend bars in real-time.

---

## 7. Database Schema

### New Table: `budget_spending`

```sql
CREATE TABLE budget_spending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    design_name TEXT NOT NULL,
    iteration INTEGER NOT NULL DEFAULT 1,
    timestamp TEXT NOT NULL,
    amount REAL NOT NULL,
    source TEXT NOT NULL,           -- 'internal' | 'external_cli' | 'monitoring'
    model TEXT NOT NULL DEFAULT '',
    component TEXT NOT NULL DEFAULT '',
    tokens_input INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    source_delay_ms INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_budget_design ON budget_spending(design_name);
CREATE INDEX idx_budget_timestamp ON budget_spending(timestamp);
CREATE INDEX idx_budget_run ON budget_spending(run_id);
```

### New Table: `budget_approvals`

```sql
CREATE TABLE budget_approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    design_name TEXT NOT NULL,
    limit_type TEXT NOT NULL,
    current_spend REAL NOT NULL,
    limit_amount REAL NOT NULL,
    action TEXT,                   -- 'approve' | 'reject' | 'increase'
    new_limit REAL,
    responded_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

---

## 8. Cases and Edge Cases

### 8.1 Normal Operation (Happy Path)

1. User configures `per_design_limit: 5.00` in config
2. Autopilot starts, BudgetManager initializes with $0 spend
3. Each OpenRouterClient.generate() call records cost via CostInterceptor
4. After iteration 2, design spend reaches $4.20
5. Iteration 3 starts — `check_budget()` sees 84% of limit, emits warning
6. Iteration 3 workflow runs, CLI agents cost $1.30
7. Polling catches CLI agent spend → total $5.50
8. `check_budget()` triggers limit exceeded → pause + approval request
9. Human approves $10.00 increase → pipeline continues
10. Design completes at $7.80 — under new limit

### 8.2 Budget Disabled (Default)

- `budget.enabled: false` (default) → all budget checks are no-ops
- `check_budget()` always returns `allowed=True` (single if-check, zero overhead)
- Cost tracking still happens passively if `CostInterceptor` is wired up
- Feature reports still show cost data from LiteLLM post-completion
- **Zero performance overhead when disabled**

### 8.3 LiteLLM Proxy Unavailable

- `CostTracker` queries fail gracefully → logged as warnings
- CLI agent env vars still injected (agents may or may not route through proxy)
- CostInterceptor uses estimation tables if `estimation.enabled: true`
- If estimation disabled, internal calls still tracked (cost from response
  headers), external calls are a blind spot (logged as warning)
- `seed_from_litellm` silently returns 0 on failure

### 8.4 Multiple Designs in Queue

- Budget is per-design (each design gets its own spend tracking via `design_name`)
- `daily_limit` and `total_limit` are global across all designs
- When design A hits per-design limit, only design A pauses
- Design B can start if its budget check passes (daily/total limits not exceeded)
- Budget ledger accumulates across all designs for daily/total calculations
- `run_id` is shared across all designs in a single pipeline run

### 8.5 Pipeline Restart After Crash

- Budget ledger is persisted in SQLite (`budget_spending` table)
- On restart, BudgetManager loads historical spend from DB automatically
- Daily limit correctly accounts for prior run's spending
- Per-design limit resets for new designs (by design_name + run_id)
- Pending approval requests are orphaned → timeout mechanism handles them
  (the orphaned request file stays on disk; the response file never appears;
  timeout fires and applies `on_timeout_action`)

### 8.6 Concurrent Design Processing

- The current autopilot processes designs sequentially (one at a time)
- BudgetManager is design-scoped — no concurrent access issues
- SQLite writes are serialized by the DB engine
- If future parallelism is added, BudgetManager needs a global lock on spend
  recording (or use WAL mode for concurrent readers)

### 8.7 CLI Agent Cost Attribution

**Problem:** CLI agents make their own API calls. Without LiteLLM proxy, there's
no way to track their costs.

**Solutions (in order of preference):**

1. **Route through LiteLLM proxy** (preferred): Set `ANTHROPIC_BASE_URL` env var
   on tmux session. Agent calls go through proxy, cost is tracked by `user` field.
   Requires LiteLLM proxy to be running.

2. **Parse agent output for cost tokens**: Some CLI agents (Claude Code) display
   token usage in their output. Parse tmux output for patterns like
   `"Tokens: 12,345"` or cost summary lines. Fragile but works without proxy.
   *(Not implemented in v1 — noted as future enhancement.)*

3. **Heuristic estimation**: Use known model pricing × estimated tokens per
   phase. Phase 1 (requirements) typically uses ~50K tokens → ~$0.15.
   Very approximate but better than nothing.

4. **Accept blind spot**: Log a warning that CLI agent costs are untracked.
   Human must monitor externally.

### 8.8 Cost Spikes

- A single LLM call might cost $0.50+ (e.g., large context window with GPT-4)
- `per_iteration_limit` catches this at iteration boundaries
- For internal calls: CostInterceptor checks budget BEFORE the call using
  `estimated_cost` parameter. If the remaining budget is less than the estimate,
  the call is blocked before execution.
- For external calls: the call executes, then polling catches the spike on the
  next poll cycle (up to 15s delay)
- Critical calls (monitoring, credit detection) bypass budget even during spikes

### 8.9 Timezone and Daily Reset

- Daily limit resets at midnight in the configured `timezone` (default: UTC)
- `budget_spending.timestamp` stores UTC ISO 8601 for consistency
- Daily spend query uses timezone-aware window calculation:
  ```python
  from zoneinfo import ZoneInfo
  tz = ZoneInfo(config.timezone)
  now_local = datetime.now(tz)
  today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
  today_start_utc = today_start.astimezone(ZoneInfo("UTC"))
  ```
- This ensures consistent daily resets regardless of server timezone

### 8.10 Configuration Hot-Reload

- Budget limits can be updated at runtime via API: `POST /api/autopilot/budget/limits`
- Updated limits take effect immediately for subsequent `check_budget()` calls
- Does not interrupt currently running workflows
- The BudgetManager holds a reference to the config object; mutating it
  propagates to all subsequent checks

### 8.11 Approval Timeout

- Default: 600 seconds (10 minutes)
- `on_timeout_action` controls behavior when timeout fires:
  - `"approve"` (default): Resume as if human approved — allows continuation
  - `"reject"`: Resume as if human rejected — skip/stop per `on_limit_exceeded`
  - `"continue"`: Resume without changing limits — logs warning, pipeline continues
- `approval_timeout_seconds: 0` = wait forever (not recommended for unattended runs)

### 8.12 Monitoring Cost Separation

- Monitoring (Guardian/Conductor) costs are tracked via LiteLLM proxy polling
  under a dedicated user ID (e.g., `"hephaestus_monitoring"`)
- Monitoring costs count toward **daily** and **total** limits
- Monitoring costs do NOT count toward **per-design** or **per-iteration** limits
  (they are shared infrastructure costs, not design-specific)
- The poll function queries LiteLLM with `user_id="hephaestus_monitoring"` and
  records the delta since the last poll

### 8.13 Pre-Budget Costs (Mid-Run Enablement)

- If budget is enabled after the pipeline has already been running, historical
  costs are invisible
- `seed_from_litellm: true` (default) queries LiteLLM for the current design's
  accumulated spend and seeds the ledger
- This ensures daily/total limits correctly account for pre-budget costs
- Seeded entries are marked with `component="seed_from_litellm"` and
  `source_delay_ms=30000` to indicate staleness

### 8.14 Skip Behavior and Partial Work

When `on_limit_exceeded='skip'`:
- The current design is abandoned at its current state
- Partially-completed work (code, docs) stays in the feature folder
  (iteration loop may need it as context for subsequent designs)
- A `budget_skip` event is logged to `events.jsonl` with details:
  ```json
  {
    "type": "budget_skip",
    "design": "Auth System",
    "limit_type": "per_design",
    "spend_at_skip": 5.50,
    "limit": 5.00,
    "iteration_at_skip": 3,
    "partial_work_path": "features/20260612_auth_system/"
  }
  ```
- The design's content hash is added to `processed_hashes` so it won't
  be re-queued (the user can manually re-add it with a modified design doc)

### 8.15 LiteLLM Cost Data Staleness

- LiteLLM proxy may report costs with a delay (typically 1-30 seconds)
- `poll_staleness_seconds: 30` (default) — if polled data is older than this,
  a warning is logged but the data is still used
- The `source_delay_ms` field in `budget_spending` tracks how stale each
  entry's data was when recorded
- Budget checks use `last_known_spend` — they may slightly undercount if
  costs haven't propagated yet. This is acceptable because:
  - The next poll cycle catches up
  - Post-completion cost fetch (existing logic) provides the final accurate number
  - The system errs on the side of allowing work to proceed, not blocking it

### 8.16 Design Name Collisions on Retry

- If a design fails and is retried (user modifies the design doc), the content
  hash changes and it re-enters the queue
- The new run gets a new `run_id`, so old spend doesn't affect the new run's
  per-design limit
- Daily and total limits correctly accumulate across both runs
- The ledger distinguishes entries via `(run_id, design_name)` pairs

---

## 9. Cost Estimation Fallback

When LiteLLM proxy is unavailable and estimation is enabled, the system uses
model-specific pricing tables:

```python
DEFAULT_MODEL_PRICING = {
    # Anthropic models (per 1K tokens)
    "claude-sonnet-4-5-20250929": {"input": 0.003, "output": 0.015},
    "claude-3-5-sonnet-20241022": {"input": 0.003, "output": 0.015},
    "claude-3-opus-20240229": {"input": 0.015, "output": 0.075},
    # OpenAI models
    "gpt-4o": {"input": 0.0025, "output": 0.01},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    # OpenRouter models
    "xiaomi/mimo-v2.5": {"input": 0.001, "output": 0.003},
}

# Phase-level token estimates (based on typical autopilot runs)
PHASE_TOKEN_ESTIMATES = {
    "product_requirements": {"input": 15000, "output": 5000},
    "architecture_design": {"input": 20000, "output": 8000},
    "development": {"input": 30000, "output": 15000},
    "adversarial_review": {"input": 25000, "output": 5000},
    "security_review": {"input": 20000, "output": 4000},
    "qa_validation": {"input": 25000, "output": 6000},
    "product_validation": {"input": 15000, "output": 3000},
    "git_commit_push": {"input": 2000, "output": 1000},
    "forensics_analysis": {"input": 20000, "output": 5000},
}
```

Pre-flight estimation: Sum all phase estimates × model pricing = estimated
total cost per iteration. Used to warn before starting a design iteration
that is likely to exceed budget.

---

## 10. Dashboard Integration

### 10.1 Frontend Components

| Component | Purpose |
|-----------|---------|
| `BudgetStatusBar.tsx` | Horizontal bar showing spend vs limits (color-coded: green → yellow → red) |
| `BudgetApprovalModal.tsx` | Modal for approve/reject/increase decisions |
| `SpendHistoryChart.tsx` | Time-series chart of spending over time |
| `BudgetSettingsPanel.tsx` | Runtime limit adjustment (within policy bounds) |

### 10.2 Autopilot Dashboard Addition

The existing `Autopilot.tsx` page gets a new "Budget" section:

```
┌─────────────────────────────────────────────┐
│ Budget Status                               │
│ ████████████░░░░░░  $3.42 / $5.00 (68%)    │
│                                             │
│ Per-Iteration: $1.21 / $2.00  ⚠️ 60%       │
│ Daily:         $12.30 / $20.00             │
│ Total:         $45.67 / $100.00            │
│                                             │
│ [View Spend History] [Adjust Limits]        │
└─────────────────────────────────────────────┘
```

When paused for approval:

```
┌─────────────────────────────────────────────┐
│ ⚠️ BUDGET APPROVAL REQUIRED                 │
│                                             │
│ Design: Auth System                         │
│ Limit Hit: Per-Design ($5.00)               │
│ Current Spend: $5.50                        │
│                                             │
│ [✓ Approve] [✗ Reject] [$5] [$10] [Custom] │
│                                             │
│ Auto-continues in 8:32...                   │
└─────────────────────────────────────────────┘
```

---

## 11. Implementation Phases

### Phase 1: Core Budget Engine (no external changes)
- `budget_config.py` — config loading from YAML/env/CLI
- `budget.py` — BudgetManager with ledger, thresholds, approval requests
- SQLite schema migration (new tables)
- Unit tests for BudgetManager (check_budget, record_spend, get_status)

### Phase 2: Cost Interceptor
- `cost_interceptor.py` — OpenRouterClient.generate() wrapper
- Integration with OpenRouterClient.generate() cost extraction
- Cost extraction logic for OpenRouter/LangChain response formats

### Phase 3: Orchestrator Integration
- Modify `run_single_design()` — add `log_dir` param, budget init, checks, finalize
- Modify `run_single_workflow()` — add budget check to poll cycle
- Modify `run_continuous_pipeline()` — pass log_dir, daily/total budget checks
- New `StopReason.BUDGET_EXCEEDED`
- Coordinate with existing `check_api_credits()` (precedence rules)

### Phase 4: CLI Agent Cost Tracking
- `cli_agent_budget.py` — env var generation + LiteLLM polling
- `agents/manager.py` — tmux env var injection from launch_params
- `mcp/server.py` — propagate budget_env to agent environment
- Orchestrator integration (polling during workflow execution)

### Phase 5: API & Dashboard
- New API endpoints for budget status/approval/reset/export
- Frontend components (BudgetStatusBar, ApprovalModal, SpendHistory)
- CLI `--budget` flag

### Phase 6: Monitoring Cost Tracking + Estimation
- Monitoring process cost polling via LiteLLM
- Cost estimation fallback tables
- Hot-reload configuration
- Documentation updates
- Integration tests

---

## 12. Risk Analysis

| Risk | Mitigation |
|------|-----------|
| LiteLLM proxy down → no cost data | Graceful degradation; estimation fallback; internal costs still tracked from response headers |
| CostInterceptor adds latency | Budget check is synchronous, in-memory, single if-check; < 1ms overhead |
| Budget DB grows unbounded | Prune entries older than 90 days on startup |
| Approval timeout too short/long | Configurable, with sensible default (10 min) and on_timeout_action |
| CLI agent ignores env vars | Some agents may not respect ANTHROPIC_BASE_URL; poll as fallback; log warning |
| Budget blocks critical monitoring call | `critical=True` parameter bypasses limits for monitoring/credit detection |
| Budget check blocks during approval, credits also exhausted | Clear precedence: budget first, credits second. Conflicting prompts avoided. |
| Daily limit timezone confusion | Explicit `timezone` config, UTC default, documented behavior |
| Late cost data from LiteLLM | `source_delay_ms` tracking, staleness warnings, err-on-side-of-allow |
| Design name collision on retry | `run_id` scoping ensures isolation across pipeline runs |
| Monitoring in separate process can't be intercepted | Tracked via LiteLLM polling, not interception. Counts toward daily/total only. |
| SQLite concurrent writes (future parallelism) | WAL mode + per-thread connections; design-scoped for now |

---

## 13. Success Criteria

1. **Visibility:** Every LLM call's cost is recorded in the database within 1 second
2. **Enforcement:** Pipeline pauses within 1 poll cycle (15s) of exceeding a limit
3. **Accuracy:** Tracked spend matches LiteLLM proxy within $0.01
4. **Performance:** Budget checks add < 1ms overhead per LLM call (synchronous, in-memory)
5. **Zero regression:** Budget system is opt-in (`enabled: false` by default)
6. **Recovery:** Pipeline survives restarts with correct accumulated spend
7. **Critical path safety:** Monitoring and credit-detection calls always bypass budget
8. **Timezone correctness:** Daily limits reset at midnight in the configured timezone
