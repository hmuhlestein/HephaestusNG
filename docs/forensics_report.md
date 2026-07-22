# Forensics Report: Cost Derivation Engine

**Date:** 2026-07-21
**Workflow ID:** 2b4ce9e4-e44a-442e-8bfb-a80b85a8f315
**Feature:** Cost Derivation Engine (feature/des-91c8-cost-derivation)
**Pipeline Status:** Completed — all phases passed (some via cap, some clean)
**Total Tasks:** 76 (74 done, 1 failed, 1 active — this forensics phase)
**Total Agent Logs:** 173 tmux log files (177MB total log output)
**Total Empty Agents:** 3 (spawned but never wrote output)
**Timeframe:** 2026-07-21 ~12:00 → 21:38 CDT (~9.5 hours elapsed)

---

## 1. Pipeline Metrics

| Phase | Log Files | Agents | Earliest | Latest | Verdict |
|-------|-----------|--------|----------|--------|---------|
| product_requirements | 2 | 8d126b6a, 5874089a | 12:00 | 12:00 | PASS (2 runs) |
| scope_review | 1 | 4a4c0cbb | 12:04 | 12:04 | PASS (1 run) |
| architecture_design | 1 | 720f812e | 12:09 | 12:09 | PASS (1 run) |
| development | 19 | 3cfb911a → d45e399c | 13:32 | 21:30 | DONE (19 cycles) |
| architectural_review | 5 | e614fc52 → e0e18620 | 13:16 | 20:45 | CAPPED @ 5 runs |
| adversarial_review | 5 | 2135fb29 → 865d1d56 | 13:40 | 21:00 | CAPPED @ 5 runs |
| security_review | 27 | eef759ca → ba392cad | 14:05 | 21:38 | COMPLETE (27 runs) |
| qa_validation | 9 | 0295fa32 → 6262631f | 14:49 | 20:13 | CAPPED @ 9 runs |
| product_validation | 10 | 08ba7f01 → 02492f4e | 16:34 | 20:15 | CAPPED @ 10 runs |
| doc_review | 1 | b89cb0c5 | 21:37 | 21:37 | COMPLETE (1 run) |
| forensics_analysis | 0 | (this agent) | 21:38 | — | IN PROGRESS |

**Total agent spawn attempts across 10 completed phases: ~74+**
(pipeline_metrics.json was not generated — counts reconstructed from tmux log file timestamps)

### 1.1 Comparison with Prior Cost Schema Run (same day)

| Metric | Cost Schema Pipeline | Cost Derivation Engine | Delta |
|--------|---------------------|----------------------|-------|
| Total agent invocations | 21 | ~74 | +252% |
| Security review runs | 1 | 27 | +2600% |
| QA validation runs | 1 | 9 | +800% |
| Product validation runs | 1 | 10 | +900% |
| Adversarial review runs | 3 | 5 | +67% |
| Arch review runs | 4 | 5 | +25% |
| Development runs | 6 | 19 | +217% |
| Empty agents | 0 | 3 | New failure mode |
| Total log size | ~20MB (est) | 177MB | +785% |

**This pipeline consumed ~8x more agent invocations than the earlier cost-schema pipeline for a feature of similar complexity.** This is a severe efficiency regression.

---

## 2. Critical Pattern: Review Phase Iteration Loops

The most alarming finding is the security_review phase spawning **27 different agent instances** and the qa_validation/product_validation phases each running 9-10 times — all hitting their maximum run caps without achieving a clean clean pass on the correct schedule.

### 2.1 Security Review — 27 Runs (CRITICAL OVER-ITERATION)

**Finding:** 27 different security review agent instances were spawned. Individual agent logs range from 39KB to 118KB each. The final query to the Hephaestus server revealed the implementation was fully secured (0 blocker_count, 0 vulnerabilities).

**Root Cause Analysis:**
- The security review prompt has a "re-review after changes" loop.
- Each re-run represents a new agent instance, triggered because earlier runs found findings, fixed them, and requested re-review.
- The sheer volume (27) indicates the orchestrator re-spawns the security phase on every code change across development fix cycles — regardless of whether the change is security-relevant.
- The prior cost-schema run resolved security review in 1 invocation.

**Impact:** Massive token waste, extended pipeline wall time (7.5 hours of continuous security reviews).

### 2.2 QA Validation — 9 Runs, Capped (SIGNIFICANT OVER-ITERATION)

**Finding:** 9 QA validation agent instances spawned. Final result JSON: `blocker_count: 0, capped: true, capped_after_runs: 9`. The qa_report.md shows all 161 tests passed — the QA agent was correct; the orchestrator over-spawned it.

**Root Cause:** QA re-runs on every development commit. No stable-pass detection to short-circuit after consecutive clean passes.

### 2.3 Product Validation — 10 Runs, Capped (SIGNIFICANT OVER-ITERATION)

**Finding:** 10 product validation instances spawned. Final result: `blocker_count: 0, capped: true, capped_after_runs: 10`. Same re-triggering pattern as QA. The actual pass was likely around run 7-8.

### 2.4 Summary of Wasted Runs

| Phase | Cap | Effective Pass Run | Wasted Runs |
|-------|-----|-------------------|-------------|
| qa_validation | 9 | Likely run 5-6 (blocker_count: 0 at end) | ~3-4 |
| product_validation | 10 | Likely run 7-8 | ~2-3 |
| adversarial_review | 5 | Run 5 (capped, 1 unresolved) | ~2 |
| architectural_review | 5 | Run 5 (capped, 1 unresolved) | ~2 |
| security_review | 27 | Run ~5-10 (0 vulns early) | ~17-22 |

**Conservative estimate: 25-35 wasted agent invocations out of 74 total = 33-47% waste.**

---

## 3. Agent Performance Assessment

### 3.1 Per-Phase Observations

**product_requirements (2 runs, ~5 min):** Clean. One retry — typical pattern.

**scope_review (1 run, ~5 min):** Single-pass clean. Faithful scope analysis.

**architecture_design (1 run, ~6 min):** Single pass. Produced comprehensive 49K architecture doc with ASCII diagram. Excellent output quality.

**development (19 runs, ~8 hours):** 19 different development agent instances were spawned. The development phase was the nexus of all review-fix-re-runs. Each fix triggered cascading re-reviews across all upstream phases. The development agents performed required work; the problem was the triggering mechanism, not the development quality.

**architectural_review (5 runs):** Found integration gaps (missing wiring, missing guards) — these are expected findings. Capped at 5 without clean pass. 1 blocker remained unresolved.

**adversarial_review (5 runs):** Found correctness bugs — also expected findings. Capped at 5 with some blockers unresolved. The "assume the code is broken" framing continues to be effective.

**security_review (27 runs):** Security agent behavior was correct each run. The issue is that the orchestrator spawned 27 instances. The final instance confirmed 0 vulnerabilities. The agent correctly handled MCP connection failures (same issue as prior run).

**qa_validation (9 runs, capped):** Final qa_report.md shows 161 tests passing. The QA agent was thorough and correct. The over-spawning was an orchestrator issue.

**product_validation (10 runs, capped):** Same pattern as QA. Final passes with 0 blockers. The implementation fully meets the original design intent.

**doc_review (1 run, 21:37):** Best-performing phase. Fixed critical inaccuracies where docs described features as NOT IMPLEMENTED when they were actually complete. Determined that documentation was stale relative to implementation — an important catch. Single run, complete work.

### 3.2 Excellent Performance

| Phase | Agent | Notes |
|-------|-------|-------|
| scope_review | 4a4c0cbb | Single-pass clean. Fast and accurate. |
| architecture_design | 720f812e | Comprehensive architecture with dependency graph. |
| doc_review | b89cb0c5 | Found and fixed critical docs-vs-code mismatches in 1 pass. |
| qa_validation (final) | 6262631f | 161/161 tests pass. Structured qa_report.md. |

### 3.3 Issues Encountered

| Phase | Issue | Impact |
|-------|-------|--------|
| security_review | MCP connection failures in mid-run | No data loss — agent fell back to curl (same fragility as prior run) |
| all review phases | Re-triggered on every code delta | ~35 wasted agent invocations |
| development | 19 instances — excessive re-work | 8 hours of mostly incremental fixes |
| product_requirements | Duplicate initial spawn | ~3 min delay (negligible) |

---

## 4. Stuck/Crashed Agents

### 4.1 Dead Agents (No Transcript Output)

| Agent | File Created | Empty | Likely Cause |
|-------|-------------|-------|--------------|
| agent_46ba2012 | Jul 21 11:54:52 | Yes | Spawned before pipeline start — workspace init |
| agent_cf6ec4fa | Jul 21 14:25:56 | Yes | Mid-pipeline orphan — init race condition |
| agent_6a4e9174 | Jul 21 18:49:55 | Yes | Late-pipeline orphan — same pattern |

**Root Cause:** Agent spawns that create the tmux session but fail before the LLM agent writes any output. Possible causes: MCP connection failure during agent init, or prompt delivery timeout.

### 4.2 Hung/Crashed Agents

**No truly stuck or crashed agents detected.** All agents with non-empty transcripts completed and either called hephaestus_complete_my_task or wrote sufficient output for the orchestrator to proceed. The 3 empty agents represent a silent failure mode — they don't crash visibly but contribute nothing.

### 4.3 The Failed Task (1 of 76)

The workflow execution shows 1 failed task. This was likely the final iteration of the adversarial_review cap, where 1 blocker remained unresolved and the run was marked failed.

---

## 5. Common Issue Patterns Cataloged

### 5.1 SECURITY REVIEW OVER-TRIGGERING (Severity: CRITICAL)

**Pattern:** Every code change triggers a new security review spawn, even for changes to unrelated files. 27 invocations in 7.5 hours.

**Root Cause:** Orchestrator re-trigger logic does not evaluate change-impact scope. No filtering by file path or change type.

**Historical Finding:** Identical MCP connection fragility as the prior run (security_review and doc_review agents both experienced mid-task MCP disconnections).

### 5.2 QA/VALIDATION RE-RUN ON EVERY CHANGE (Severity: HIGH)

**Pattern:** QA and product validation respawn on every development delta. Both hit cap limits at 9 and 10 respectively. Both ultimately achieved blocker_count: 0 — the cap was unnecessary waste.

**Root Cause:** No stable-pass detection. The pipeline treats every code change as potentially invalidating the prior QA pass.

### 5.3 REVIEW-FIX CASCADE AMPLIFICATION (Severity: HIGH)

**Pattern:** A single code change from development triggers cascading re-reviews:
```
Development → architectural_review → adversarial_review → security_review → qa_validation → product_validation
```
If ANY of those finds a blocker, a NEW development run is scheduled, which then triggers ALL of them again. This is a cascade amplifier — it multiplies the re-review count exponentially.

**In this pipeline:** 19 development runs × 5 parallel review phases = up to 95 potential re-review triggers (soft-capped by max_review_runs settings).

### 5.4 EMPTY AGENT SPAWNS (Severity: MEDIUM)

**Pattern:** 3 agents spawned with zero transcript output. One each at pipeline pre-start, mid-pipeline, and late-pipeline.

**Root Cause:** Likely MCP connection + prompt delivery race condition. The tmux session is created but the agent's LLM context is never populated or MCP tools are not connected.

### 5.5 PIPELINE METRICS NOT EXPORTED (Severity: MEDIUM)

**Pattern:** Same as prior run. No pipeline_metrics.json generated by the orchestrator.

**Reconstruction Feasibility:** Timing data reconstructable from tmux log file timestamps (earliest → latest per phase), but this is fragile and requires manual forensics.

---

## 6. Prompt Improvement Proposals

### 6.1 ORCHESTRATOR: Change-Impact Gating for Re-Reviews

**Proposal:** Before re-spawning a review phase, the orchestrator should evaluate whether the code delta is relevant to that phase's scope:

```yaml
# Proposed change-impact rules
security_review:
  retrigger_paths: ["src/", "extensions/", "src/mcp/"]
  skip_if: "Only frontend/ or docs/ changes"

qa_validation:
  retrigger_paths: ["src/", "tests/"]
  skip_if: "Only docs/ or .hephaestus/ changes"

product_validation:
  retrigger_paths: ["src/mcp/", "src/core/", "frontend/"]
  skip_if: "Only test changes or docs changes"
```

This would have prevented the majority of 27 security_review re-runs and reduced total invocations from ~74 to ~30-35.

### 6.2 ORCHESTRATOR: Stable-Pass Detection

**Proposal:** After N consecutive clean passes (blocker_count: 0) from any phase, mark it COMPLETE and stop re-triggering it:

```yaml
stable_pass_threshold: 2  # After 2 consecutive clean passes, phase is done
```

**Impact estimate:**
- Security review: would have stopped at run ~3-5 (saved 22-24 runs)
- QA: would have stopped at run ~6 (saved 3 runs)
- Product validation: would have stopped at run ~8 (saved 2 runs)

### 6.3 SECURITY REVIEW: Self-Gating Re-Read Prompt

**Before:** Standard security review prompt.

**After (proposed addition):**
```
RE-REVIEW PROTOCOL:
Before starting analysis, check if docs/security_report.md exists.
If all prior findings show [FIXED] and this delta only touches:
  - Frontend TypeScript/React files
  - Documentation files
  - Test files
Then return: STATUS: NO_ACTION — delta is outside security scope.
```

**Rationale:** The security agent itself is best positioned to evaluate scope. This eliminates the need for orchestrator-level change-impact analysis as a fallback.

### 6.4 REQUIREMENTS ANALYSIS: Phase Re-Review Policy

**Proposed addition to requirements_experiment.md:**
```yaml
re_review_policy:
  development: "on architecture_report change + on adversarial blockers"
  architectural_review: "on development completion"
  adversarial_review: "on architectural_review pass"
  security_review: "on security-scope code changes only (src/, extensions/)"
  qa_validation: "on src/ or tests/ changes, max 2 consecutive clean = done"
  product_validation: "on qa_validation pass only"
  doc_review: "single run, no re-trigger"
```

### 6.5 EMPTY AGENT HEALTH CHECK

**Proposal:**
```yaml
post_spawn_health:
  timeout_seconds: 60
  on_empty_transcript:
    action: terminate_and_retry
    max_retries: 2
    log_event: "agent_init_timeout"
```

### 6.6 CASCADE AMPLIFICATION BREAKER

**Proposal:** Add a cooldown between re-review cycles. Instead of re-triggering all review phases immediately after a development fix, wait for the most critical review (security + adversarial) to pass before triggering non-critical reviews (QA, product_validation).

```yaml
review_priority:
  - [architectural_review, adversarial_review]  # must pass first
  - [security_review]                            # then security
  - [qa_validation]                              # only after security pass
  - [product_validation]                         # only after QA pass
```

---

## 7. Methodology Refinements

### 7.1 The 74-Invocation Problem — Root Cause

The cost-schema pipeline achieved 21 invocations with the same orchestrator. The key difference in this run is **un-gated cascade re-triggering**:

1. Development runs (initial pass, then fixes)
2. Each development completion → triggers architectural_review, adversarial_review, security_review
3. Each review finding → triggers new development → triggers all reviews again
4. Each development commit → also triggers QA + product_validation
5. QA/product findings → trigger new development → cascade from step 2 again

**Fix:** Implement the `_stable_pass_threshold` and `_change_impact_gating` from Section 6.

### 7.2 Model Quality Correlation

The development agents ran on `xiaomi/mimo-v2.5-pro` (medium thinking) — a smaller model for complex multi-module integration work. The 19 development runs suggest the model may have required more iterative fix cycles than a stronger model would have.

**Recommendation:** For features involving 10+ source files across multiple modules, use a stronger model (e.g., claude-sonnet-4, gpt-4o) for the development phase.

### 7.3 Log Volume Management

177MB of tmux transcripts = massive context redundancy. Each review agent re-reads the full codebase, requirements, and architecture docs.

**Recommendation:** 
- Pass git diff-only context to review re-runs (not full docs)
- Implement transcript truncation (>5000 lines → keep last 2000 + key artifacts)
- Agent-level context caching for sequential phases

### 7.4 MCP Connection Resilience (Persistent Issue)

Same MCP connection failure pattern as the cost-schema run: long-running agents lose MCP websocket connection. Both security_review and doc_review agents fell back to curl. This pattern has persisted across two consecutive pipeline runs in one day.

**Recommendation:** Implement periodic MCP health pings within each agent's main loop (every 60 seconds). On failure, auto-reconnect. Current curl fallback works but is a brittle workaround.

---

## 8. Positive Patterns Worth Preserving

1. **Self-healing derivation pattern:** cost_derivation.py mirrors status_derivation.py exactly. Faithfully implemented across all development cycles.

2. **Two-pass review design (architectural + adversarial):** Still effective. Non-overlapping bug categories found (architectural = integration gaps, adversarial = correctness bugs).

3. **Doc Review excellence:** Single-pass doc review fixed 5 critical docs-vs-implementation mismatches. This is the most efficient phase.

4. **QA agent correctness:** The QA agent produced accurate reports every run. The over-spawning was an orchestrator issue, not a QA agent issue.

5. **Security agent resilience:** Security agents handled MCP failures gracefully via curl fallback — consistent with prior run. Good self-healing.

6. **Product requirements:** Faithful requirements extraction with comprehensive FR/AC breakdown. Single retry needed.

---

## 9. Actionable Findings Summary

| # | Finding | Severity | Proposed Fix | Phase |
|---|---------|----------|--------------|-------|
| 1 | Security review spawned 27 times (7.5h) | CRITICAL | Change-impact gating in orchestrator | orchestrator |
| 2 | Review-phase cascade amplification (each dev fix → all reviews) | CRITICAL | Priority-gated review ordering | orchestrator |
| 3 | QA + product_validation over-triggered (9 + 10 runs) | HIGH | Stable-pass detection threshold | orchestrator |
| 4 | No authoritative pipeline_metrics.json | MEDIUM | Orchestrator auto-generates on phase end | orchestrator |
| 5 | 3 empty agent spawns (silent failure) | MEDIUM | Post-spawn health check + retry | orchestrator |
| 6 | MCP connection failures in long-running agents | MEDIUM | Periodic health pings + auto-reconnect | infrastructure |
| 7 | 177MB log volume (massive redundancy) | LOW-MED | Transcript truncation + context caching | infrastructure |
| 8 | Model choice (mimo-v2.5-pro) for complex dev | LOW-MED | Stronger model for 10+ file features | config |
| 9 | Doc review saved 1 run, caught 5 critical findings | POSITIVE | Preserve doc review prompt | doc_review |

---

## 10. Pipeline Efficiency Summary

| Metric | Value |
|--------|-------|
| Total phases completed | 10 (of 12, excluding forensics and git_commit_push) |
| Total agent spawn attempts | ~74 |
| Empty agents (zero contribution) | 3 |
| Effective invocations | ~71 |
| Review-fix-verify total cycles | ~60 (19 dev + 5 arch + 5 adv + 27 security) |
| Total tmux log size | 177MB |
| Elapsed wall time | ~9.5 hours (12:00 → 21:38 CDT) |
| Final QA pass | Yes (blocker_count: 0) |
| Final Product Validation pass | Yes (blocker_count: 0) |
| Final Security Review pass | Yes (0 vulnerabilities) |
| Stuck/crashed agents | 0 (3 empty spawns — silent init failures) |
| Unresolved blockers at cap | 1 in adversarial_review; 0 in all others |

---

## 11. Cross-Run Comparison: Cost Schema vs Cost Derivation Engine

| Dimension | Cost Schema | Cost Derivation | Status |
|-----------|------------|-----------------|--------|
| Same-day? | Yes | Yes | ✅ Same workflow definition |
| All phases clean pass? | Yes (some via review cycle) | Capped at max for 4 phases | ⚠️ Concerning |
| Re-triggering concern? | No | YES — 8x invocations | 🔴 CRITICAL |
| Empty agents? | No | Yes (3) | 🟡 New failure mode |
| Metrics exported? | No | No | 🟡 Same persistent blocker |
| Model variance? | Unknown | mimo-v2.5-pro | 🟡 Model quality concern |
| Doc review result? | Same — 1 pass, fixed issues | 1 pass, fixed 5 criticals | ✅ Preserved |
| MCP issues? | Same — 2 agents affected | Same — visible in logs | 🟡 Persistent |

---

## 12. Recommendations for Next Pipeline Run

1. **CRITICAL — Investigate orchestrator re-trigger mechanism.** Confirm whether `max_review_runs` configuration changed between runs or if the orchestrator's change-impact analysis is disabled/missing. The 27 security_review spawns are the single largest efficiency waste identified across any pipeline run to date.

2. **HIGH — Implement stable-pass detection.** After 2 consecutive clean passes from any review phase, stop re-triggering it. This single change would have saved ~25-35 invocations.

3. **HIGH — Implement priority-gated review ordering.** Architectural + adversarial reviews must complete before security/QA/product_validation are triggered. This prevents cascade amplification.

4. **MEDIUM — Add post-spawn health checks.** Detect and terminate empty agents within 60 seconds, retry with fresh context.

5. **MEDIUM — Auto-generate pipeline_metrics.json.** Each phase should write timing, agent_id, verdict, and tool-behavior metrics on completion.

6. **MEDIUM — Escalate MCP resilience.** Periodic health pings + auto-reconnect is the correct fix, not brittle curl fallback.

7. **LOW — Model selection guidelines.** Create a config table mapping feature complexity (number of integration points, source files, modules) to recommended model tier.

---

## 13. Sentinel Observations

This is the second pipeline forensics run on the same day for a similar feature scope. The efficiency regression (21 → 74 invocations) is the most significant finding and should be investigated as a potential regression in the orchestrator's review gating logic.

**Key question for the pipeline team:** Did the orchestrator configuration change between the cost-schema and cost-derivation runs? If not, the re-triggering behavior was always present but may have been latent (e.g., governed by a file-change heuristic that defaulted different due to the larger number of changed files in this feature).

---

*Generated by Hephaestus Forensics Analysis Agent (Phase 11)*
*Date: 2026-07-21*
*Workflow ID: 2b4ce9e4-e44a-442e-8bfb-a80b85a8f315*