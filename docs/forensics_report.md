# Forensics Report: Backend OpenRouter Direct Cost Capture

**Date:** 2026-07-24
**Workflow ID:** 8f2e9f79-2395-4d73-89f7-dc86d24f169c
**Feature:** Backend OpenRouter Direct Cost Capture (feature/des-91c8/openrouter-direct)
**Parent Design:** Cost Tracking Design (DES-91c8)
**Pipeline Status:** Completed cleanly through doc_review, no unresolved gaps.

## Data quality caveat

No `pipeline_metrics.json` or `phase_prompts/` directory exists scoped to this workflow_id (`8f2e9f79-...`) — both live at the parent multi-feature design session level and this sub-feature's run never got its own copy written to this worktree or discoverable feature folder. All timing/iteration data below is reconstructed from `git log --date=iso-strict` on this branch (main..HEAD, 12 commits) and the phase commit bodies, which are detailed enough to substitute for per-phase logs. This report itself was overwriting a stale `docs/forensics_report.md` left in this worktree by a prior, unrelated sibling feature ("Budget Enforcement and Pipeline Throttling", workflow `0acbf2fc-...`) — direct evidence for finding §3.1 below.

## 1. Commit-history → phase/iteration mapping

| Time (local) | Commit | Phase | Outcome |
|---|---|---|---|
| 17:04:51 | `5cecc2f` | product_requirements | Clean, 1 run |
| 17:07:11 | `2124093` | scope_review | PASS — flagged one minor unsourced advisory note (non-blocking); **overwrote a stale `scope_review_result.json` left by a prior sibling feature in this design pipeline** |
| 17:09:28 | `ec54d47` | architecture_design | Clean, 1 run |
| 17:16:57 | `a07e6f5` | development (run 1) | Implemented both scoped tasks: `TestInvokeAndRecord` test class, and narrowed a bare `except Exception` log level from debug→warning |
| 17:18:49 | `fd64913` | architectural_review | PASS, 0 BLOCKER; **overwrote stale placeholder report/result files left from a prior capped review loop** |
| 17:23:35 | `d3e4b3c` | adversarial_review (run 1) | 1 BLOCKER found: explicit JSON `null` in `token_usage`/`cost`/`prompt_tokens_details` raised `AttributeError` in `_invoke_and_record`, silently dropping a legitimate `CostEntry` |
| 17:26:07 | `b085435` | development (run 2, fix) | Fixed via `.get(key) or {}` guard on all three chained lookups; added regression test `test_null_prompt_tokens_details_still_writes_cost_entry` |
| 17:27:46 | `f42a8d8` | adversarial_review (run 2, verify) | Confirmed fix, 0 BLOCKER/WARNING remaining, 2 pre-existing nits carried forward out-of-scope |
| 17:35:48 | `910c244` | security_review | No new attack surface; 1 low finding filed as ticket (out of scope, not exploitable); **wrote `security_report.md` to the Docs Path root instead of `docs/security_review/`** |
| 17:39:34 | `10705b7` | qa_validation | 102/102 tests pass; **overwrote stale `qa_validation` docs left over from an earlier, unrelated feature (Budget Enforcement, dated 2026-07-21)** |
| 17:43:31 | `52a1307` | product_validation | PASS, all 5 FR / 3 NFR met; flagged the misplaced `security_report.md` for doc_review to fix |
| 17:45:59 | `9383d09` | doc_review | Moved the misplaced `security_report.md` into `docs/security_review/`; verified doc accuracy against the diff, no other inaccuracies |

**Total wall-clock time:** ~41 minutes (17:04:51 → 17:45:59) across 12 commits, including one full BLOCKER → fix → verify loop. This is a fast, low-friction run: exactly one adversarial-review iteration was needed, and no phase looped more than twice.

## 2. What worked well (preserve, don't change)

- **Adversarial review caught a real bug in run 1** — the `AttributeError`-on-null-metadata bug that silently dropped a valid `CostEntry` is a genuine defect that would have shipped undetected without this phase. It was fixed in exactly one development iteration and verified in one adversarial re-run, with a targeted regression test added. This is the pipeline functioning as designed — no prompt change indicated here.
- **Security review correctly scoped itself** — it traced the full cost-ingestion path end-to-end, confirmed the actual diff didn't introduce new attack surface, and filed a genuinely out-of-scope low-severity hardening gap as a ticket instead of scope-creeping into an unrelated fix.
- **product_validation and doc_review acted as an effective safety net** for the one process defect that did occur (see below), catching and correcting it one phase later rather than letting it ship.

## 3. Issues found and root causes

### 3.1 Systemic: stale cross-feature artifacts pollute shared docs directories (recurring, 4+ occurrences)

**Pattern:** `scope_review`, `architectural_review`, `qa_validation`, and this very `forensics_analysis` phase each independently discovered leftover doc/result files from *other, unrelated sibling features* in the same parent "Cost Tracking Design" pipeline (a prior capped review loop, and the separately-shipped "Budget Enforcement" feature dated 2026-07-21/23). Each phase silently handled it inline, costing agent effort re-deriving "is this stale or is this mine?" on every affected phase, and creating risk that a less careful agent run could accept stale data as ground truth instead of overwriting it — or, in this phase's case, could have shipped a forensics report analyzing the wrong feature entirely.

**Root cause:** Docs/result directories are shared and persist across sibling feature workflows spawned from the same parent design session; nothing resets or namespaces them per sub-feature workflow_id before a new sub-feature's pipeline starts.

**Recommendation:** Before `product_requirements` (or as a pipeline pre-step), clear or archive phase-output subdirectories (`docs/scope_review/`, `docs/architectural_review/`, `docs/qa_validation/`, `docs/forensics_report.md`, etc.) that don't belong to the current feature's workflow_id, rather than relying on each downstream phase to detect and paper over staleness independently.

### 3.2 `security_review.yaml` output path is ambiguous relative to established convention (1 occurrence, propagated through 2 downstream phases)

**Finding:** `config/workflows/autopilot/security_review.yaml` writes `security_report.md` directly to the Docs Path root, while the actual established convention — followed by `architectural_review` (`docs/architectural_review/architectural_review_report.md`), `qa_validation` (`docs/qa_validation/...`), `adversarial_review`, and `product_validation` — is a phase-named subdirectory: `docs/<phase_name>/<file>.md`. This caused `security_review` to place its report at the wrong path, `product_validation` had to flag it as a non-blocking issue, and `doc_review` had to spend part of its run moving the file into `docs/security_review/`.

**Current text** (`config/workflows/autopilot/security_review.yaml`, lines 23–24 and 80–81):
```yaml
outputs:
  - "security_report.md"
```
```
  - ALL docs/reports go in "Docs Path:" (security_report.md, etc.).
```

**Proposed rewrite:**
```yaml
outputs:
  - "security_review/security_report.md"
```
```
  - ALL docs/reports go in "Docs Path:", inside a phase-named subdirectory:
    docs/security_review/security_report.md — NOT the Docs Path root.
    (Every other review phase follows this same docs/<phase_name>/ convention;
    match it exactly.)
```
Also update the matching `done_definitions` entry from `"security_report.md created with findings and fixes applied"` to `"docs/security_review/security_report.md created with findings and fixes applied"`.

## 4. Summary of proposed actions

1. Add a docs-directory reset/namespacing step scoped to workflow_id at the start of multi-feature design pipelines, to stop stale sibling-feature artifacts from leaking into new sub-feature runs (§3.1).
2. Fix `config/workflows/autopilot/security_review.yaml`'s output path to `docs/security_review/security_report.md`, matching the convention every other review phase already follows (§3.2).
3. No change recommended to adversarial_review, architecture_design, or development phase prompts — this run's single BLOCKER → fix → verify loop is the pipeline working as intended.

Tickets filed for both items above (see ticket references in memory).
