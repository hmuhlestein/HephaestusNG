# Product Requirements Analysis: Backend OpenRouter Direct Cost Capture

**Feature ID:** des-91c8-openrouter-direct
**Feature Name:** Backend OpenRouter Direct Cost Capture
**Status:** Requirements Extracted
**Date:** 2026-07-24
**Design Document:** `.hephaestus/design.md` — §"Backend's own direct OpenRouter calls" (lines 234-253), §"Backend's own OpenRouter calls (task enrichment, guardian, conductor)" (lines 579-620), §Data Model (lines 254-311), §Implementation Phases item 5 (lines 692-694)
**Scope Document:** `.hephaestus/features/openrouter-direct/scope.md` — **does not exist in this worktree** (directory `.hephaestus/features/openrouter-direct/` is empty). Scope below is taken from the task assignment's explicit in-scope/out-of-scope statement, cross-checked against design.md Implementation Phase 5. This gap is flagged in §6.

---

## 0. Critical Finding: This Scope Appears Already Implemented

Before extracting requirements, source inspection of this worktree found that the mechanism described in scope — direct interception of the orchestrator's own OpenRouter calls, extraction of token/cost usage, and writing to the `CostEntry` ledger — **already exists on this branch** (which is currently even with `main`, 0 commits ahead). It landed via prior `phase(development)` commits (visible in `git log`, e.g. `b426b21`, `15242e5`) associated with earlier features in this same design (Cost Tracking Schema, Budget Enforcement), not via a dedicated openrouter-direct development phase on this branch.

Verified present:

| Requirement area | File:Line | State |
|---|---|---|
| `usage: {include: true}` opt-in on OpenRouter requests | `src/interfaces/langchain_llm_client.py:243` | Done |
| Single choke-point helper wrapping `model.ainvoke()` | `src/interfaces/langchain_llm_client.py:323-395` (`_invoke_and_record`) | Done |
| All LLM call sites routed through the helper | `langchain_llm_client.py:417, 474, 538, 600, 699, 758, 850` (7 call sites: `classify_complexity`, `enrich_task`, `resolve_ticket_clarification`, `analyze_agent_state`, `analyze_agent_trajectory`, `analyze_system_coherence`, `review_qa_report`) | Done |
| `task_id` threaded to call sites where available | Same call sites; `enrich_task`, `analyze_agent_state`, `analyze_agent_trajectory`, `review_qa_report` pass `task_id`; `classify_complexity`, `resolve_ticket_clarification`, `analyze_system_coherence` have no task context and correctly omit it (rolls up as overhead per design.md:296-298) | Done |
| `CostEntry` ledger write with `source="openrouter_direct"` | `langchain_llm_client.py:368-389` calling `src/core/cost_derivation.py:38` (`record_cost`) | Done |
| `CostEntry` table schema | `src/core/database.py:1230` | Done, matches design.md:266-293 |
| Rollup to `Task`/`Feature`/`AutopilotDesign`/`AutopilotProject.cost_total_usd` | `src/core/cost_derivation.py` | Done (shared with other sources, not openrouter-specific) |

**Not yet done / genuinely open:**

1. **Live smoke-test confirmation.** Design.md:245-252 explicitly flags that whether OpenRouter's `usage.cost` field actually survives LangChain's `ChatOpenAI` response parsing into `response_metadata["token_usage"]["cost"]["total"]` (the exact path `_invoke_and_record` reads at `langchain_llm_client.py:361-366`) has not been confirmed against a real API response — LangChain guarantees promotion of *known* OpenAI usage fields but not provider-specific extensions. No test in the repo exercises this path with a real or realistically-shaped mocked response.
2. **No test coverage of `_invoke_and_record`'s extraction logic.** `tests/test_cost_tracking.py` covers `CostEntry`/`record_cost`/rollup with `source="openrouter_direct"` as a literal string, but never invokes `_invoke_and_record` itself or asserts it correctly parses a `response_metadata` shape. A regression that changes the metadata key path (e.g. a LangChain upgrade) would silently degrade to `cost_usd=0` and pass every existing test (see the `except Exception` swallow at `langchain_llm_client.py:392-393`, which only logs at `debug` level).

This changes the shape of this phase's requirements: the primary deliverable is **verification and test-hardening of an existing mechanism**, not net-new construction. Architecture/development phases should confirm this finding before planning new implementation work.

---

## 1. Scope Boundary (from task assignment)

**In scope:**
- OpenRouter direct API call interception in the orchestrator
- Token count and cost extraction from OpenRouter response headers/body
- Writing cost entries to the `CostEntry` ledger
- Wiring into the existing cost tracking pipeline

**Explicitly out of scope (do not touch):**
- Budget enforcement guards (`cost_limit_usd` checks, pause/resume logic) — separate feature, already delivered per `git log` (`Merge branch 'feature/des-91c8/budget-enforcement'`)
- Claude Code / OpenCode / Codex collector implementations
- Pi extension changes
- UI budget configuration

---

## 2. Functional Requirements

### FR-1: Enable OpenRouter usage/cost data on all orchestrator LLM calls
**Source:** design.md:234-243
**Status:** Already satisfied — `langchain_llm_client.py:243`
**Acceptance criteria:**
- Every `ChatOpenAI` instance built for `provider == "openrouter"` includes `extra_body={"usage": {"include": True}}` (or equivalent merged `extra_body`).
- Verify no regression: confirm this survives alongside the existing `provider` and `reasoning` `extra_body` keys built at `langchain_llm_client.py:224-245` (they are merged into one dict, not overwritten).

### FR-2: Single choke point for cost extraction across all orchestrator LLM call sites
**Source:** design.md:589-600
**Status:** Already satisfied — `_invoke_and_record` at `langchain_llm_client.py:323-395`
**Acceptance criteria:**
- A single helper wraps `model.ainvoke()`, extracts usage from the response, and writes a `CostEntry`.
- All orchestrator-side LLM call sites (task enrichment, ticket clarification, complexity classification, Guardian agent-state/trajectory analysis, Conductor system-coherence analysis, QA review) call this helper rather than `model.ainvoke()` directly.
- Verify count: confirm no `model.ainvoke(` call sites remain outside `_invoke_and_record` itself (checked: none do, per `grep -n ainvoke`).

### FR-3: Attribute cost entries to task/agent/workflow where known, else overhead bucket
**Source:** design.md:296-298, 602-614
**Status:** Already satisfied
**Acceptance criteria:**
- Call sites with a known `task_id` (enrich_task, analyze_agent_state, analyze_agent_trajectory, review_qa_report) pass it through.
- Call sites with no task context (classify_complexity, resolve_ticket_clarification, analyze_system_coherence) write `task_id=None`, and the entry still gets recorded (not silently dropped) — `CostEntry.task_id` is nullable per schema.

### FR-4: Extract token counts and cost from OpenRouter response and write to `CostEntry`
**Source:** design.md:616-619, 254-293
**Status:** Mechanism present but **unverified against a live response** (§0)
**Acceptance criteria:**
- `_invoke_and_record` reads `response.response_metadata["token_usage"]` and extracts `cost.total`, `prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens`.
- On a real OpenRouter call with `usage.include=true`, the written `CostEntry` has `source="openrouter_direct"`, non-zero `cost_usd`, and populated `input_tokens`/`output_tokens`.
- **Gap to close in this feature:** run (or write an automated test simulating) one confirmatory call and assert the extraction path produces a non-zero `CostEntry`. Currently, a failure here is silent (caught by a bare `except Exception` at `debug` log level, `langchain_llm_client.py:392-393`) and covered by zero tests.

### FR-5: Raw usage payload retained for debugging
**Source:** design.md:284-288
**Status:** Already satisfied — `raw_usage=usage` passed at `langchain_llm_client.py:388`

---

## 3. Non-Functional Requirements

- **NFR-1 (reliability):** A failure to extract cost data must never break the underlying LLM call. Already satisfied — extraction is wrapped in `try/except` and the response is returned regardless (`langchain_llm_client.py:357-395`). This is correct per design intent but currently over-broad: it also swallows genuine extraction bugs silently (see FR-4 gap) rather than distinguishing "no cost data present" (expected for non-OpenRouter providers) from "cost data present but malformed" (a real bug worth surfacing above `debug`).
- **NFR-2 (no double-instrumentation):** Cost capture must not duplicate `CostEntry` rows for a single LLM turn. Single call to `record_cost` per `_invoke_and_record` invocation — satisfied by construction.
- **NFR-3 (non-OpenRouter providers unaffected):** Call sites route through `_invoke_and_record` regardless of provider; for non-OpenRouter models (Groq, Azure, Google), `response_metadata["token_usage"]["cost"]` will simply be absent, `cost_usd` defaults to `0`, and no `CostEntry` is written (`langchain_llm_client.py:368` guards on `cost_usd > 0`). This is existing, correct behavior — no change needed, but worth an explicit test since it's load-bearing.

---

## 4. Integration Points

- `src/interfaces/langchain_llm_client.py` — `LangChainLLMClient._invoke_and_record`, all 7 call sites, and the `ChatOpenAI` construction branch for `provider == "openrouter"`.
- `src/core/cost_derivation.py` — `record_cost()`, shared with `pi`/`claude_code`/`opencode`/`codex` sources; not modified by this feature, only called.
- `src/core/database.py` — `CostEntry` model (line 1230); not modified by this feature.
- Callers one level up that don't yet thread `task_id` into `enrich_task` were flagged in design.md:602-606 as needing a check — verified already fixed (`enrich_task(..., task_id=task_id)` present at call site and signature, `langchain_llm_client.py:439-445, 474`).

---

## 5. Technology Constraints

- LangChain's `ChatOpenAI` / `langchain_openai` response metadata shape is the single point of fragility — it's an implicit contract with a third-party library that is not guaranteed to preserve non-standard OpenRouter fields across LangChain version bumps. Any test added for FR-4 should pin against the actual installed `langchain_openai` version's behavior, not assume the shape is stable.
- OpenRouter API: `usage.include=true` request param and `usage.cost.total` response field are OpenRouter-specific, undocumented in the OpenAI-compatible spec LangChain models against.

---

## 6. Open Issue: Missing scope.md

`.hephaestus/features/openrouter-direct/scope.md` does not exist — the directory was created but is empty. This requirements analysis was scoped from the task assignment's inline description instead, which is internally consistent with design.md Implementation Phase 5. If a scope.md is expected to exist by the next pipeline phase (scope_review), that phase will need to either source it from this document or have it created upstream — flagging so it isn't mistaken for this phase having skipped a required input.

---

## 7. Recommendation for Next Phase

Given §0's finding, the architecture_design phase should treat this feature as **verify-and-harden**, not build-from-scratch:
1. Add a smoke test (or manual confirmatory call, logged) proving `usage.cost` survives into `response_metadata["token_usage"]`.
2. Add unit test coverage for `_invoke_and_record`'s extraction logic using a realistic mocked `response_metadata`.
3. Consider narrowing the `except Exception: logger.debug(...)` in `_invoke_and_record` (langchain_llm_client.py:392-393) to distinguish "no cost fields present" (expected, silent) from "cost fields present but extraction raised" (a real bug, should log at `warning`).
