# Agent Prompt Progression — Verified Findings

Status: findings 1–4 and the housekeeping items are **fixed** (see "What changed"). Findings 5–8 are open.

This document replaces an earlier revision that was written by reading
`config/workflows/autopilot/*.yaml` without reading `src/`. Roughly half of that
revision's claims did not survive contact with the code, and its three
"Critical" items were among the wrong ones — it reported four phases as missing
OKF frontmatter that all declare it, reported metrics as uncollected when they
are written, and proposed adding an `inputs:` schema to phases that already
enumerate their inputs. Meanwhile three real defects sat in the same files it
reviewed. The corrections are recorded at the bottom under "Claims that did not
survive verification", because a stale gap list is worse than no gap list: the
next reader treats every entry as open work.

Every claim below cites the code that establishes it.

---

## Finding 1 — `security_review`'s gate was dead (FIXED)

**Severity: high.** A security review reporting unfixed critical vulnerabilities
advanced to QA, every time, regardless of what it found.

`workflow.yaml` configured a full set of conditions for the phase:

```yaml
- after_phase: security_review
  conditions:
    - if: "score < 0.3"   # -> goto architecture_design
    - if: "score < 0.7"   # -> goto development
    - if: "score >= 0.7"  # -> continue
```

None of the first two bands was reachable. `security_review.yaml` never declared
`spec_gate: true`, so [`build_phase_output`](../src/autopilot/spec.py#L1670)
returned `{}` for it, and
[`_heuristic_evaluate`](../src/workflow_engine/orchestrator.py#L438) scores an
empty `phase_output` at a fixed **0.75** baseline — `json.dumps({})` contains
none of the success or failure keywords it scans for, so the baseline is never
displaced. 0.75 clears the 0.7 bar. Always continue.

This is not a novel bug. It is the *same* bug, in the same mechanism, that
[`spec.py`'s `GATED_PHASES` comment](../src/autopilot/spec.py#L44) records being
found live for `adversarial_review` and `architectural_review` ("an adversarial
review reporting 6 BLOCKERs still completed with `action="continue"`").
`security_review` and `doc_review` were left behind by that fix.

Two other places in the codebase had already noticed the consequences and
worked around them without naming the cause:

- [`verification.py`'s `verify_no_open_tickets`](../src/services/task_completion/verification.py#L295)
  extended itself to `git_expert` specifically because security_review "has no
  content-scored workflow.yaml gate of its own... its only enforcement path is
  this same check firing when the pipeline happens to route back through
  development."
- [`_cap_out_review_phase`](../src/autopilot/orchestrator/phase_transitions.py#L2094)
  grew a whole second branch for phases with no gate artifact, citing a live run
  that "hit 25 re-entries of security_review with `max_review_runs: 4`
  configured and doing nothing."

Both workarounds are correct and stay. Their stated rationale was not.

### The fix

`security_review.yaml` now declares `spec_gate: true` and documents three
frontmatter counters, and
[`score_security_review`](../src/autopilot/spec.py#L1205) reads them.

The scoring polarity is deliberately inverted relative to every other review
phase. `security_review` **fixes** what it finds rather than reporting it for
someone else — its own done definition is "Critical and high vulnerabilities
FIXED in the code" — so finding a lot is not a failure. The gate input is
`unresolved_count`: critical/high findings still live in the code when the agent
marked itself done.

```
critical_count: 6, high_count: 2, unresolved_count: 0   -> 0.9  continue
critical_count: 3, high_count: 1, unresolved_count: 2   -> 0.4  goto development
no security.md at all                                   -> 0.4  goto development
```

Medium and low findings stay out of the gate on purpose — those become tickets,
and `verify_no_open_tickets` remains their backstop at `git_expert`, now as
genuine defense-in-depth rather than as the only line of defense.

### What gating this phase exposed

`security_review` was the only gated phase declaring its output with a
subdirectory prefix (`"security_review/security.md"`); every peer declares the
bare filename. That form resolved *only* via `resolve_declared_output_path`'s
flat-`.hephaestus/` candidate — which is [deliberately skipped for gated
phases](../src/autopilot/spec.py#L221), because accepting a flat report as
"found" produces a file that passes the existence check and then mis-scores as
"no report" at gate time. So making this phase gated made
`verify_output_artifact` unable to find a report sitting in exactly the right
place, rejecting every completion. Fixed by declaring `"security.md"`, matching
the convention and `GATE_RESULT_ARTIFACTS`.

That same mismatch had already been silently disabling something else.
[`verify_output_artifact`](../src/services/task_completion/verification.py#L197)
gates the MANDATORY ash-scan content check on `declared_output == "security.md"`
— a comparison that never matched `"security_review/security.md"`. **The ash-scan
check has never run on any security review.** The prompt's "⚠️ YOUR REPORT WILL
BE REJECTED WITHOUT IT ⚠️" was not enforced by anything. The existing test
covering that check patches `get_phase_required_files` to return `["security.md"]`
directly, which is why it passed while production never exercised the path.
Declaring the bare filename repairs it.

Reviving the ash check also exposed two ways the phase could no longer complete,
both previously masked by the check being dead:

- **STEP 1's skip lists were a stale renumbering.** `STATELESS_LIBRARY` was
  told "SKIP Steps 2, 3, 6" and `DATA_SERVICE` "SKIP Step 2" — Step 2 being the
  scan whose section is now hard-floor required, so an agent obeying the prompt
  produced a report the floor rejects. The rest of the list was equally adrift:
  it skipped STEP 3 (READ SECURITY REQUIREMENTS, which nothing should skip) and
  ran "Step 4 only if the library handles PII or writes files" — but STEP 4 is
  AUTHENTICATION & AUTHORIZATION, and PII/file writes are STEP 6 (DATA
  HANDLING). Read against a pre-ash numbering (2=auth, 3=input validation,
  4=data handling) the original text is exactly coherent, which is what
  identifies it as a renumbering artifact rather than intent: the ash step was
  inserted at position 2 and the classification block was never updated.

  Rewritten against the step titles: STEP 4 is the only step any classification
  skips outright (no auth exists to review), STEP 6 the only conditional one
  (run it if the library handles PII or writes files), and Steps 2, 3, 5, 7, 8
  apply to every feature type. One deliberate change beyond restoring intent:
  the old text skipped input validation for stateless libraries, and a parser
  or formatter is precisely where malformed-input bugs live, so STEP 5 now
  applies to everyone.

  `TestSecurityReviewClassificationSteps` pins the step *numbers* the
  classification names to the step *titles* it means, so the next insertion or
  reorder breaks a test instead of silently mis-routing a security review. It
  was checked against the original text and does trip on it.
- **The missing-`scripts/ash` path wrote no results file at all.** Every other
  failure path in [`_run_ash_scan`](../src/autopilot/orchestrator/worktree_integration.py#L812)
  writes a `SCAN FAILED TO RUN` / `SCAN TIMED OUT` marker that the prompt tells
  the agent to quote verbatim and continue past. That one path returned silently,
  leaving the agent to `cat` a nonexistent file with no sanctioned way to report
  why — and now, rejected for a section it had no way to fill. It writes the
  marker too. The test asserting the old behaviour ("don't write a misleading
  results file") was correct when nothing checked; it is inverted now.

Third: `record_review_finding` read `result["blocker_count"]` unconditionally,
but "blocker" is only three of the seven gated phases' vocabulary.
`security_review` counts `unresolved_count`, `qa_validation` counts
`failed_tests`/`critical_issues`, `product_validation` counts
`unmet_requirements` — all three recorded **0 findings regardless of what they
found**, so the prior-findings block injected into the next run's task
description announced "0 blocker(s)" above a summary describing real ones. Same
class of mistake `synthetic_clean_result`'s docstring already documents:
assuming one phase's schema is every phase's. Fixed with
[`gate_finding_count`](../src/autopilot/spec.py#L438), which reads each phase's
own key; the injected wording is now "N unresolved finding(s)" rather than
borrowing one phase's noun for all of them.

---

## Finding 2 — `doc_review`'s gate thresholds were also unreachable (FIXED)

Same mechanism as Finding 1, much lower stakes. `doc_review` carried
`score < 0.3 -> goto architecture_design` and `score < 0.6 -> goto development`
against the same permanent 0.75.

Unlike `security_review`, the right fix here is *not* to build a scorer.
`doc_review` is the one review phase that fixes what it finds in place — its
done definitions are "stray files organized", "critical inaccuracies fixed" — so
it emits no finding count for a scorer to read, and bouncing the pipeline back
to development over documentation is not a bar this phase was ever designed to
enforce.

The config now states the real behaviour (`score >= 0.0 -> continue`) with a
comment explaining why, rather than advertising an enforcement that has never
once fired. If doc_review should gate for real, that needs a finding count in
`docs.md` and a scorer — not a threshold with nothing behind it.

---

## Finding 3 — `Docs Path` and `Artifacts Path` were never injected (FIXED)

**Severity: high.** 13 of the 14 autopilot phase prompts told the agent to read
or write an "Artifacts Path", with 70 references between them. Three phases went
further and instructed the agent to look the value up in a place it did not
exist:

- `qa_validation.yaml`: *"Read: Your task description for `Docs Path:` and `Project Path:` locations"*
- `security_review.yaml`: same line
- `forensics_analysis.yaml`: `DOCS_ABS="<value of 'Docs Path (absolute)' from your task description>"`, inside a block labelled **MANDATORY FIRST ACTION**

No such field was ever put in a task description.
[`_create_phase_task`](../src/autopilot/orchestrator/phase_transitions.py#L2442)
builds the description as `f"Execute {phase.name}: {phase.description}"` plus
optional goto feedback and prior-findings history — nothing else — and
`prompt_builder` injected only `Working Directory`, `Project Root (absolute)`,
and `REVIEW_MODE`.

This is the same never-injected-field bug class as the `Project Root (absolute)`
one that [`prompt_builder.py`'s own comment](../src/agents/prompt_builder.py#L85)
records fixing ("Nothing ever injected that field... so its STEP 1 'your task
description contains...' lookup had nothing to find").

`Docs Path` was the worse half. `Artifacts Path` is at least self-defined inline
in most phases' CRITICAL PATH RULE (`= ./.hephaestus/`), so an agent could
recover. **`Docs Path` was defined nowhere** — not in any prompt, not in any
code path, not in any constant.

### Why there was never a second path to inject

The phrasing implies a feature-record docs folder distinct from the worktree's
`.hephaestus/`. That folder is real, but it is created by
[`_populate_feature_folder`](../src/phases/phase_manager.py#L1827) at workflow
**finalization**, and its name is timestamped at creation. During a run it does
not exist. There is exactly one artifact directory while agents are working —
`<worktree>/.hephaestus/` — and the feature record is a post-run archive of it.

### The fix

`prompt_builder` now injects
[`Artifacts Path (absolute): <worktree>/.hephaestus`](../src/agents/prompt_builder.py#L102),
placed outside the workflow-lookup `try/except` so a DB hiccup cannot silently
cost every phase its artifact directory. All 27 `Docs Path` references across
seven phase YAMLs were renamed to `Artifacts Path`, eliminating the phantom
second location rather than inventing a value for it.

---

## Finding 4 — `forensics_analysis` read inputs nobody wrote (FIXED)

`forensics_analysis`'s entire job is comparing what each agent was *told* to do
against what it actually did, then proposing prompt rewrites. It read three
inputs to do that:

| Input | Written by |
|---|---|
| `phase_prompts/` | nothing, anywhere in the repo |
| `run_health.json` | nothing, anywhere in the repo |
| `pipeline_metrics.json` | [`phase_manager.py`](../src/phases/phase_manager.py#L1945) — but at finalization, after this phase runs |

Combined with Finding 3, its input contract was unsatisfiable end to end: it
resolved paths through a field that did not exist, to files that did not exist.
Its "MANDATORY FIRST ACTION" was a guaranteed failure.

### The fix

[`_stage_forensics_inputs`](../src/autopilot/orchestrator/phase_transitions.py#L2004)
writes both missing inputs into the worktree's `.hephaestus/` at dispatch, in
the branch that already computes run health and then threw it away:

- `run_health.json` — the health dict `_assess_run_health` already produces,
  carrying `goto_count`, `decision_points`, and per-log-file `tmux_errors` that
  name exactly which phases produced errors.
- `phase_prompts/` — the phase YAMLs copied verbatim (forensics is told to quote
  them, so a summary would be worse than nothing), plus `workflow.yaml`, since
  "why did this phase loop four times" is exactly what forensics is asked and
  the answer lives in its evaluation points.

Staging is best-effort and swallows its own failures: `forensics_analysis` is an
optional phase, and staging trouble must not take down task creation for it.

`pipeline_metrics.json` is not faked. The prompt's STEP 3 now states plainly
that no metrics file exists at this point in the pipeline, that timing and
iteration data comes from `run_health.json` and the tmux logs instead, and that
any metrics file it does find belongs to a *different* run and using it would
put another feature's numbers in this report.

Three further path errors in the same prompt were corrected while it was open,
all of the same "points at something that isn't there" kind: it listed phase
artifacts at flat `.hephaestus/<file>` paths when gated phases write to
`.hephaestus/<phase_name>/<file>`; it told the agent to write `forensics.md` to
two locations, the second being the feature folder that does not exist yet; and
its LIGHT/FULL mode branch was dead, since the orchestrator skips this phase
entirely on a clean run and only ever dispatches it in the not-clean case.

---

## Housekeeping (FIXED)

**`development.yaml` — redundant vague done definition.** `"Code follows project
style guide"` sat four lines above the concrete bar it duplicates (`ruff check .`
and `ruff format --check .` with zero errors, plus `mypy`). The vague line was a
leftover, not the operative standard. Deleted.

**`qa_validation.yaml` — fallback contradicted itself.** A done definition read
`"If TESTING.md missing: used fallback test approach (unit tests only)"` while
the fallback steps in the same file include an Integration Tests section. The
done definition now matches what the steps actually prescribe.

**`doc_review.yaml` — no identity anchor.** The only phase of 14 without a
`YOU ARE` section. Added, stating the thing that actually distinguishes it: it
is the one review phase that fixes rather than reports, so `docs.md` is a record
of what it already fixed.

**`doc_review.yaml` — "Critical inaccuracies fixed" was unverifiable.** Now
defines critical: a documented command, path, flag, endpoint, or config key that
does not exist in the shipped code, or a documented behaviour the code
contradicts.

---

## What changed

| File | Change |
|---|---|
| `src/autopilot/spec.py` | `score_security_review`; `security_review` added to `GATE_RESULT_ARTIFACTS`, `GATE_RESULT_REQUIRED_KEYS`, `GATE_RESULT_TYPE_OVERRIDE`, `synthetic_clean_result`, `build_phase_output`; `gate_finding_count` |
| `src/phases/phase_manager.py` | findings history records the phase's own count via `gate_finding_count` |
| `src/agents/prompt_builder.py` | injects `Artifacts Path (absolute)` |
| `src/autopilot/orchestrator/phase_transitions.py` | `_stage_forensics_inputs`; called at forensics dispatch; `_cap_out_review_phase` docstring corrected |
| `src/services/task_completion/verification.py` | `verify_no_open_tickets` docstring corrected — it backstops ticketed findings now, it is no longer the whole gate |
| `src/autopilot/orchestrator/worktree_integration.py` | missing-`scripts/ash` path writes the failure marker like every other path |
| `config/workflows/autopilot/security_review.yaml` | `spec_gate: true`; gate frontmatter documented; count written after fixing, not before; declared output is the bare `security.md`; STEP 1 skip lists renumbered against the step titles; STEP 11 cross-reference corrected |
| `config/workflows/autopilot/workflow.yaml` | `doc_review` conditions replaced with the reachable one |
| `config/workflows/autopilot/forensics_analysis.yaml` | paths, inputs, output location, dead mode branch |
| 6 other phase YAMLs | `Docs Path` → `Artifacts Path` |
| `tests/test_ash_scan.py` | missing-script assertion inverted to the intended behaviour |
| `tests/test_spec.py` | `TestScoreSecurityReview`, `TestSecurityReviewGateWiring`, `TestGateFindingCount`, `TestSecurityReviewClassificationSteps` (+22) |
| `tests/test_prompt_builder.py` | `TestArtifactsPathInjection` (+3) |
| `tests/test_orchestrator_helpers.py` | `TestStageForensicsInputs` (+4); cap-out test retargeted to `doc_review`; `test_caps_out_security_review_via_its_gate_artifact` added |

The retargeted cap-out test is worth calling out: it used `security_review` as
its example of a phase with *no* gate artifact, which Finding 1 makes untrue.
`doc_review` is now the sole remaining user of that branch, and `security_review`
has its own test asserting the synthetic clean result is written in the schema
its own scorer reads — the same class of mismatch that once made
`qa_validation`'s cap-out score as a 0% pass rate.

---

## Open findings

**5. Test-first is mandated only for bug fixes.** `development.yaml` requires
stash-verification that a test failed before the fix — for bug fixes only. New
features can ship with tests written after the fact. A symmetric acceptance-test
definition would close it. This one is real and unaddressed.

**6. No structured input validation.** Phases do enumerate their inputs in prose
(`development.yaml` lists architecture.md, requirements.md, adversarial.md;
`security_review.yaml` and `qa_validation.yaml` do the same), and Finding 3
makes those paths resolvable. What is still missing is machine-checkable
declaration: nothing validates that a phase's required inputs exist before
dispatch, the way `verify_output_artifact` validates outputs after. That is a
real asymmetry, but it is a feature, not a defect — and it is worth much less
now that the paths resolve at all.

**7. Threshold rationale is undocumented.** `security_review` and `qa_validation`
continue at 0.7 while `design_review` and `adversarial_review` continue at 0.6.
The values are probably fine; nothing records why they differ, so nobody can
tell a deliberate choice from an accident. Note that as of Finding 2 every
remaining threshold in `workflow.yaml` is at least *reachable*, which was not
true before.

**8. Forensics findings still have no tracked path back.** The phase can now
read its inputs and propose rewrites, and it files tickets. Nothing tracks which
proposals were applied or whether the next run improved. A prompt-review UI —
before/after text, approve/reject, applied-vs-outcome tracking — is the obvious
shape, and it is a real feature proposal rather than a bug.

Genuine absences worth noting but not filed as gaps: no per-phase token budget,
no dedicated dependency/license audit phase, no performance-test phase, no
inter-phase clarification protocol. Each is a feature that does not exist, not
something broken.

---

## Claims that did not survive verification

Recorded so the earlier revision's errors don't get re-derived.

| Earlier claim | Reality |
|---|---|
| Four phases lack OKF frontmatter; this is "the root cause of scoring unreliability" | All four declare `type:` (`product_requirements.yaml:35`, `architecture_design.yaml:39`, `doc_review.yaml:124`, `security_review.yaml:35`). Structured parsing exists — `read_okf` plus seven `score_*` functions — and is a hard floor: [`verification.py`](../src/services/task_completion/verification.py#L189) rejects `done` on malformed frontmatter. |
| `product_requirements`, `adversarial_review`, `doc_review` lack a `YOU ARE` section | 13 of 14 phases have one. Only `doc_review` did not. |
| No metrics collection anywhere | `pipeline_metrics.json` is written by [`phase_manager.py`](../src/phases/phase_manager.py#L1945). The real problem was timing, not absence — see Finding 4. |
| No abort mechanism; the pipeline retries indefinitely | `on_budget_exhausted: arbitrate`, `_trigger_arbitration`, `OrchestratorState.IMPASSE`, `optional_phases`. Escalation goes to human review rather than abort, by design. |
| Phases don't declare their inputs | They do, in prose. The defect was that the *path* was unresolvable — Finding 3. |
| `"Code follows project style guide"` is development's style bar | The concrete `ruff`/`mypy` bar was already four lines below it. |
| `git_expert`'s `score >= 0.0` means it always continues | `score < 0.3 -> goto development` is evaluated first. |
| `deploy` has no rollback handling | It reads rollback instructions from DEPLOY.md and reports under a Rollback Notes section. Rollback is not *tested*, which is arguably correct for a real deploy. |
| "What is `ash`?" | A security scanner the orchestrator runs itself before dispatching the agent (`_run_ash_scan`), whose results `verify_output_artifact` then requires `security.md` to contain. |

The generalizable lesson: this pipeline's behaviour is not legible from the
phase YAMLs. A configured threshold may be unreachable, a documented input may
have no writer, and a prompt may reference a task-description field nothing
injects — none of which is visible without reading `src/`. Three of the four
findings above are of exactly that shape, and all three were invisible to a
config-only review.

---

## Verification

```
tests/test_spec.py                   104 passed  (82 before)
tests/test_ash_scan.py                 6 passed
tests/test_prompt_builder.py          28 passed  (25 before)
tests/test_orchestrator_helpers.py   264 passed  (cap-out suite retargeted)
tests/test_phase_transitions_spec_gate.py, test_task_completion_service.py,
test_orchestrator.py, test_workflow_orchestrator_goto_retry_budget.py,
test_prompt_delivery.py               all passed
tests/test_phase_manager.py           76 passed
tests/test_spec_gate_firing.py, test_task_completion_service.py,
test_update_task_status_ordering.py   all passed
all affected suites together         333 passed
ruff                                  no new findings (14 pre-existing, unchanged)
```

End-to-end check of Finding 1 against the real `workflow.yaml` conditions, with
a real `security.md` on disk:

```
2 unresolved criticals   score=0.4  ->  goto development
6 found, all fixed       score=0.9  ->  continue
no report at all         score=0.4  ->  goto development
```

Before the fix all three produced `score=0.75 -> continue`.
