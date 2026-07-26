# Forensics Report: OpenCode Cost Collector (des-91c8-opencode-collector)

**Date:** 2026-07-26
**Workflow ID:** b0087459-85b3-4937-a50e-faee96194a7e
**Feature:** OpenCode Cost Collector (feature/des-91c8/opencode-collector)
**Parent Design:** Cost Tracking Design (DES-91c8)
**Pipeline Status:** Completed cleanly through doc_review, 0 open blockers, ready for git_commit_push.

## 0. Data sources and a known gap

No `pipeline_metrics.json` or `phase_prompts/` directory exists scoped to this workflow_id — as prior sibling-feature forensics reports for this same parent design have also noted, both artifacts are keyed to the parent multi-feature design session, not to this sub-feature's own workflow_id, and this sub-feature's run never got its own copy. This is now a confirmed *recurring* pattern across at least 3 sibling runs (cost-ui, budget-enforcement, opencode-collector), not a one-off.

Substitute sources used instead, all giving minute-level resolution:
- `git log --date=iso-strict main..HEAD` — 16 commits, full phase-prefixed commit messages.
- `.hephaestus/tmux/*.transcript.log` and the matching `<phase>_<agentid>.log` snapshot files — mtimes and content confirm agent activity windows and rule out crash/retry loops for this run (checked for crash/traceback/rate-limit signatures; none found — the one apparent "unknown command" hit was UI chrome text in a `tmux` pane, not a real error).
- `docs/*` phase artifacts on this branch (all correctly scoped to this feature per their own commit messages, having overwritten stale sibling-feature content).

## 1. Commit-history → phase/iteration mapping

| Time (MDT) | Commit | Phase | Outcome |
|---|---|---|---|
| 07:36:34 | `027bc70` | product_requirements (run 1) | Wrote requirements; resolved a stale design premise (OpenCode is no longer one-shot) via live code verification |
| 07:39:29 | `1cff341` | scope_review (run 1) | **FAIL** — FR1 silently overrode the design doc's own explicit build/defer gate instead of surfacing the conflict |
| 07:42:14 | `4b4cd75` | product_requirements (run 2, fix) | Reframed FR1 as a factual gate-check + escalated the proceed/defer call as a blocking decision for scope_review |
| 07:43:42 | `3ee6077` | scope_review (run 2) | PASS — ruled PROCEED (feature was commissioned as its own workflow, overriding the design's generic gate) |
| 22:48:14 | `4131509` | architecture_design | Clean, 1 run. 5 ordered tasks incl. a blocking live spike (task 1) |
| 22:56:40 | `edd0031` | development (run 1) | Implemented per architecture; live-verified the spike finding (`opencode -s <id>` is resume-only) |
| 23:07:26 | `cb9fbc5` | architectural_review (run 1) | 1 BLOCKER: checkpoint keyed by shared Hephaestus `session_id`, which OpenCode's non-resumable sessions invalidate. 1 FIX: sqlite3 connection leak. Traced the BLOCKER back to architecture_design's own §2.3 |
| 23:16:25 | `adae90b` | development (run 2, fix) | Re-keyed checkpoint by `opencode_session_row_id`; fixed the leak; added a regression test proven to fail pre-fix (stash-verified) |
| 23:22:18 | `b585446` | architectural_review (run 2, verify) | PASS — both findings confirmed fixed, 1 DEFER (theoretical URI encoding) left open as recommended |
| 23:28:14 | `45fc706` | adversarial_review (run 1) | 1 BLOCKER: naive-datetime `.timestamp()` calls in `_discover_opencode_session()` are interpreted as local time, silently dropping all OpenCode costs on non-UTC hosts. Root cause traced to architecture.md's own formula; masked by an identically-buggy test fixture |
| 23:33:41 | `af59ac8` | development (run 3, fix) | Attached `tzinfo=timezone.utc` before `.timestamp()`; fixed the same bug in the test fixture; added a TZ-independent regression test (stash-verified fails without fix) |
| 23:35:38 | `9eda280` | adversarial_review (run 2, verify) | PASS — fix verified by actual test execution, not commit-message trust; ran the specific regression test standalone and confirmed it wasn't silently skipped |
| 23:43:07 | `fa67b6f` | security_review | Clean, 1 run. 0 findings of any severity |
| 00:26:38 | `6d9fbc1` | qa_validation | PASS, 1 run. 83/83 feature tests; cross-checked 42 pre-existing unrelated failures in the full suite are not caused by this diff |
| 00:44:49 | `25b358f` | product_validation | PASS, 1 run. 5/5 FR met, agent_score 0.97; independently re-ran tests rather than trusting QA's number |
| 00:49:34 | `c812b72` | doc_review | Clean, 1 run. Found and fixed 2 stale spots in architecture.md left over from before the two blocker fixes landed |

**Elapsed wall-clock:** ~7 min for product_requirements+scope_review (07:36–07:43), then a gap until 22:48 (agent scheduling/queue gap, not phase work), then ~2h01m for architecture_design through doc_review (22:48–00:49).

## 2. Agent performance per phase

- **product_requirements**: strong on verification (queried the live `opencode.db` schema, grepped the whole config tree for `cli_type: opencode` rather than trusting the design doc) but made a judgment call — quietly overriding the design's explicit defer condition — that should have been escalated instead of resolved unilaterally. Corrected on the very next run without re-prompting from a human.
- **scope_review**: caught the FR1 issue precisely, and on re-review made the actual call (PROCEED) rather than bouncing it back again — correctly recognized that a workflow explicitly commissioned by name is out-of-band authorization overriding the design's default-defer stance.
- **architecture_design**: designed a correct query-based approach but reproduced a real bug in its own spec (naive-datetime UTC handling), which propagated through development and wasn't caught until adversarial_review 3 phases later.
- **development**: consistently strong — every fix commit includes a stash-verified regression test (revert fix → test fails; restore fix → test passes) and explicitly checks whether a fix altered previously-passing test expectations that had encoded the same bug (the `_ms()` fixture in run 3).
- **architectural_review**: caught a real, non-obvious BLOCKER (checkpoint-key collision across non-resumable OpenCode sessions) that traces to its own earlier architecture_design output — good self-correction across phase identity.
- **adversarial_review**: caught the TZ bug that both architecture_design and architectural_review missed, specifically because it thought to reproduce it against real non-UTC host behavior rather than just checking code-vs-spec compliance. On verification, re-ran the specific test standalone to rule out silent-skip false positives — good rigor.
- **security_review, qa_validation, product_validation, doc_review**: all single-pass, all independently re-verified claims rather than trusting prior phases' self-reports (re-ran tests, re-diffed against merge-base, cross-checked doc claims against actual code).

## 3. Stuck/crashed agents

None found for this workflow. All 19 tmux transcripts in the relevant time windows (07:36–07:43, 22:48–00:49) show normal Claude Code session activity with no crash, traceback, or rate-limit termination signatures. This run did not exhibit the Guardian-respawn-thrashing or crash-loop issues flagged in sibling forensics reports for other features under this same parent design.

## 4. Common issue pattern: design-doc bugs propagate past their own review gate

Both real defects this run (checkpoint-keying, naive-datetime TZ) originated in `architecture_design`'s own spec, not in development's implementation of it. `architectural_review` and `adversarial_review` check the code against the spec's *intent*, not against the spec's *arithmetic/data-lifecycle correctness* — so a bug baked into the design doc's own formula survives architecture_design, development, and (in the TZ case) architectural_review, since none of those phases independently re-derive the formula against real data. Only adversarial_review's "assume it's broken, prove it" framing caught the TZ bug, and only because it happened to run on a non-UTC host.

**Proposed change — `config/workflows/autopilot/architecture_design.yaml`:** add an explicit self-check step for any design that computes time windows, correlations, or ID matching against externally-owned data (i.e., a system this project doesn't control, like OpenCode's session table): *"If your design includes a time-window, timestamp-arithmetic, or ID-correlation formula, trace it through with one concrete worked example using real values (not variable names) before finalizing — specifically check timezone/epoch assumptions for any `datetime` object that isn't explicitly UTC-aware."* This is cheap (one extra worked example) and would have caught the TZ bug 3 phases earlier than adversarial_review did, at zero cost to development or review cycles.

## 5. Prompt rewrite proposed

**File:** `config/workflows/autopilot/product_requirements.yaml`, near STEP 3 (~line 159, "If the design doc specifies different technologies, note the conflict and flag it for the architect to resolve").

**Before:** the yaml has no instruction covering what to do when the design doc contains its own explicit conditional build/defer gate (as DES-91c8 did at design.md:695–699) and the live-verified facts resolve that gate toward "defer," yet the phase was commissioned to build the feature anyway. The agent's first attempt at this (`1cff341`) resolved the conflict silently and wrongly; only scope_review's FAIL caught it, costing one full product_requirements + scope_review round-trip (~3 minutes, but would be more expensive on a longer phase).

**After — add a new subsection:**
```
  ═══════════════════════════════════════════════════════════════════════
  STEP 3B: DESIGN-DOC CONDITIONAL GATES
  ═══════════════════════════════════════════════════════════════════════

  If the design doc states an explicit build/defer condition (e.g. "only
  build this if X is true in the live system"), verify the condition
  against the current codebase — do NOT just trust the design doc's
  assumption. If your verification result conflicts with what this phase
  was asked to do (e.g. the gate says defer, but you were commissioned
  to build it), do NOT silently resolve the conflict either way. Report
  the verified fact plainly in the requirements doc and flag the
  conflict as an explicit BLOCKING decision for scope_review to rule on.
```

This would let the same one-line escalation happen on the *first* pass instead of requiring a scope_review FAIL to force it — saving one full product_requirements+scope_review cycle on any future feature whose design doc carries a similar conditional gate.

No other phase's prompt showed evidence of causing wasted work this run — security_review, qa_validation, product_validation, and doc_review all passed cleanly on their first attempt with well-scoped, accurate output.

## 6. Tickets filed

None. The one architecture-level pattern worth tracking (§4) is a design-review methodology gap, not a bug in shipped code, and doesn't warrant a code ticket — it's captured here and in memory for the next architecture_design run to internalize directly from the prompt change proposed in §4/§5.

## 7. Summary

- 16 commits across 12 pipeline phases; 3 phases required a fix-and-reverify loop (product_requirements/scope_review gate conflict, architectural_review checkpoint-keying blocker, adversarial_review TZ blocker) — all legitimate review catches, pipeline functioning as designed.
- 0 crashed/stuck agents, 0 infra issues (unlike sibling features under the same parent design, which reported Guardian respawn-thrashing and QA crash loops).
- 1 concrete prompt rewrite proposed (product_requirements.yaml, §5) that would prevent a recurrence of the gate-conflict round-trip.
- 1 methodology gap identified (architecture_design doesn't verify its own time/correlation arithmetic against real data), with a proposed self-check addition (§4).
- Recurring cross-run finding (now 3rd+ occurrence): `pipeline_metrics.json`/`phase_prompts/` are scoped to the parent design session, not per-feature workflow_id, leaving every sub-feature's forensics phase to reconstruct timing from git log + tmux mtimes. Worth an orchestrator-side fix if sub-feature workflows continue to be a supported pattern.
