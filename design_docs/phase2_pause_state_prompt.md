# Prompt: Phase 2, §4.8 — pause-state primitive

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.8 of `docs/AUTOPILOT_REFACTOR_PLAN.md`. Eighth item in this session's Phase 2 sequence — §4.1 through §4.7 are done; read their findings docs for the established rigor and format before starting. This item is the pause-state sibling to §4.2's termination-state consolidation — same shape of problem, different field set.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.8 (full text, short) and `design_docs/phase2_termination_findings.md` (§4.2's findings doc) — §4.2 built the equivalent primitive for `status="terminated"`/`current_task_id`/`terminated_at`; this item is the same pattern for `Workflow.status`/`paused_by`/`paused_at` plus cascaded `Feature.status`. Match its structure (single primitive, all field writes atomic, every call site migrated) rather than re-deriving the approach from scratch.

## Freshness check — do your own, nothing below is pre-verified this round

`docs/AUTOPILOT_REFACTOR_PLAN.md`'s §4.8 text names four historical pause-write bugs by commit hash (`9aa2a19`, `ce0c4a7`, `bacaf6b`, `22178b1`) and one related auto-resume bug (`a333616`), each tied to a specific call site or code path. **None of these locations have been re-verified in this session** — unlike §4.2 through §4.7, this prompt was written without reading current source. Locate fresh, in this order:

1. `grep -rn "paused_by\|paused_at" src/` to find every current write site for those two `Workflow` fields.
2. For each hit, `git log --oneline -1 -S"paused_by" <file>` or `git show <commit>` on the four named hashes to confirm which current location each historical bug maps to (files have moved — the orchestrator split alone relocated most of what used to be `orchestrator.py`; §4.2's own work likely already moved `pause_workflow_direct`-shaped code to `src/autopilot/orchestrator/engine_client.py`, the same place `terminate_agent` landed — verify, don't assume).
3. Locate `_pause_feature_for_review`, the Pause-button route handler, and `_try_auto_resume_paused_workflow` the same way — grep for the function names, don't trust any implied path.

## Target

One primitive, `pause_workflow(workflow_id, *, reason: Literal["user","budget","review","system"], cascade_to_feature: bool = True)`, that always sets `Workflow.status`/`paused_by`/`paused_at` together in one transaction and, when `cascade_to_feature`, syncs the linked `Feature.status` in the same transaction — this closes the gap `22178b1` patched for exactly one call site (the Pause button never touched `Feature.status` before that fix) by construction, for every call site, the same way §4.2's `terminate_agent` closed the termination-triad gap everywhere at once.

Pair it with `resume_workflow(workflow_id, *, force: bool = False)` that narrows on `paused_by` per `a333616`'s fix: `"system"`-paused workflows are eligible for auto-resume once their premise changes; `"user"`/`"budget"`/`"review"`-paused workflows are not, unless `force=True` is passed explicitly. `a333616`'s bug was that `_try_auto_resume_paused_workflow`'s guard (`paused_by is not None`) was accidentally dead code — every real pause site sets a non-`None` value, so the guard always skipped and never actually gated anything. Confirm this dead-guard shape still exists at that call site before assuming the fix needs to happen there; it may have drifted since `a333616` landed.

Migrate every call site found in the freshness check to call `pause_workflow`/`resume_workflow` instead of writing the fields directly, mirroring how §4.2 migrated every termination-triad write site to `terminate_agent`.

## Verification

A characterization test asserting every one of the four historical pause call sites, after migration, leaves `Workflow.status`/`paused_by`/`paused_at` and `Feature.status` mutually consistent. Per the plan's own text, this test should **fail against current code specifically at the Pause-button call site** — `22178b1`'s fix only patched that one site's `Feature.status` write inline, it never built a shared primitive, so before this item lands that site is the one place the four-way consistency check breaks. If your freshness check finds it's already fixed some other way, say so explicitly rather than assuming the plan text is still accurate.

Also test `resume_workflow`'s `force` narrowing directly: a `"system"`-paused workflow auto-resumes without `force`; a `"user"`-paused one does not, and only resumes with `force=True`. This is the `a333616` regression case — write it as an actual failing-then-passing test, not just prose confirmation.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.7).
- Any other Phase 2 item (§4.9 onward). Log anything found belonging to one of those.
- Reworking `derive_feature_status`/`derive_workflow_status` (`src/core/status_derivation.py`) itself — this item wires pause writes through a new primitive, it doesn't touch the derivation functions those primitives may call into for the `Feature.status` cascade. If the cascade logic turns out to need a derivation-function change, flag it rather than making it — that overlaps §4.6 sub-problem 2's territory.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions. `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>`. Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Findings doc (`design_docs/phase2_pause_state_findings.md` or similar) for anything out of scope, and for exactly which historical call sites needed migration vs. were already fixed by something else. No commits — leave everything in the working tree for review.
