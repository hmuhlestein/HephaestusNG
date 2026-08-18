# Phase 2, §4.9 — declared-output path & schema resolver findings

Implemented directly (not via the usual prompt-doc handoff), continuing §4.8's pattern at the user's request.

## What was already built vs. what was actually done here

The prompt doc's own freshness check turned out to be right: two of the plan's three target sub-parts were already substantially built in `spec.py`, as fallout from the `99d19b6` fix the plan itself cites — just never named or wired consistently. This item's real scope narrowed to:

1. **Path resolution (part a)** — genuinely not consolidated. Built.
2. **Schema validation (part b)** — already built (`validate_gate_result_schema`), but found to be **silently disagreeing** with the synthetic-result builder over the `type:` field. Fixed.
3. **Synthetic-result builder (part c)** — already built and already wired into `_cap_out_review_phase` exactly as the plan's target describes. No structural change needed; only the type-string bug above.

No `PhaseOutputResolver` class was built. Consolidating the existing correct functions under one class name would have been renaming for its own sake — the functions already live together in `spec.py`, already share `GATE_RESULT_ARTIFACTS`/`GATE_RESULT_REQUIRED_KEYS` as their common config, and forcing them into a class wouldn't close any actual gap.

## Part (a): path resolution — real duplication found and fixed

`src/services/task_completion/verification.py`'s own module docstring flagged this explicitly: *"The two `_old_name_map` dicts are intentionally duplicated (byte-identical) rather than deduplicated."* Confirmed true — `verify_output_artifact` and `verify_output_survived_commit` had byte-identical 10-entry old-name-alias maps and identical 4-candidate-directory search loops.

Built `resolve_declared_output_path(working_directory, phase_name, declared_output) -> Optional[Path]` in `spec.py`, alongside `OUTPUT_NAME_ALIASES` (the single now-shared alias map). Both callers migrated. Each site's genuinely distinct fallback layer was kept as its own step, not forced into the shared function:
- `verify_output_artifact` still falls back to the feature-gallery archive afterward (a fallback `verify_output_survived_commit` never had).
- `verify_output_survived_commit` still falls back to git history afterward (a fallback `verify_output_artifact` never had).

**Deliberately left unconsolidated**, and noted rather than silently skipped:
- `read_okf_report` (`spec.py`) — a third, narrower search (`.hephaestus/<phase>/` + worktree root only, no `docs/` fallback, no alias map) used at gate-scoring time. Its own docstring already explains why it's narrower on purpose ("checked first, not guessed at... iterating every subdirectory... risked picking up a stale file"). Forcing it onto the broader existence-check search would widen its behavior, not fix a bug.
- `consume_gate_artifacts` (`spec.py`) — its own comment says it "must mirror `read_okf_report`'s candidate order exactly," and traced to confirm it has exactly one caller (`phase_manager.py:1006`), no parallel hand-rolled duplicate. Correctly narrow by the same design as `read_okf_report`, not a second copy of the part-(a) duplication.
- `feature_routes.py`'s `feature_report.html` lookup — a different artifact (an HTML build product of `doc_review`, not a generic phase-declared `.md`/JSON output) with its own 3-candidate search. Confirmed via `git show` against the plan's own named commits (`685258c`, `8772527`) that this is exactly the "report-lookup endpoints" cluster the plan refers to. Genuinely a different concern from "does this phase's declared output exist," not folded in. The plan's third named commit in that cluster, `d06f4ce`, turned out to be a prompt-text fix (correcting what an agent's YAML instructions told it to *read*, not a code search loop) — already resolved, nothing to consolidate.

## Addendum: a real gap found re-checking this item's own prompt doc

Asked to re-read this prompt doc and check for gaps (the same pass applied to §4.8), tracing every caller of `read_okf_report` turned up something the first pass missed: **`resolve_declared_output_path` (existence check) and `read_okf_report` (actual scoring) now disagree on where a report counts as "found," and this is live, not just theoretical.**

`read_okf_report` is used far more heavily than described above — it's `build_phase_output`'s search for every gated phase (the function that actually computes the `score` driving goto/retry/continue) and `verify_gate_result_schema`'s search too, not a minor secondary path. Its candidate set (`.hephaestus/<phase>/<name>` + worktree root — 2 candidates, no `docs/`, no old-name aliases) is strictly narrower than `resolve_declared_output_path`'s (4 candidates including `docs/` and 10 aliases).

Consequence: a report written only to `docs/<name>` (or under an old-name alias) passes `verify_output_artifact`'s existence check (task allowed to complete as "done") and passes `verify_gate_result_schema` too (that function returns "no error" whenever `read_okf_report` finds nothing at all, on the documented assumption that `verify_output_artifact` already caught the missing-file case elsewhere — an assumption this exact gap violates) — then silently mis-scores at actual gate evaluation, landing in each `score_*` function's "result_missing" band as if nothing had been written, sending a genuinely-completed phase back to development.

`qa_validation.yaml` explicitly forbids `docs/` as a write location ("Write ALL reports to Artifacts Path (.hephaestus/) — NOT the project root, NOT ./docs/"), so this is a legacy-compatibility fallback, not a currently-sanctioned path — real-world exposure is probably low, not zero. But wherever it does fire, both hard floors are actively misleading: they say "done" to something the gate is about to treat as not-done.

**Fixed, in a follow-up pass, split across both directions rather than picking just one:**

1. **`docs/` dropped from `resolve_declared_output_path`.** Every gated phase's own prompt explicitly forbids writing there, and nothing was ever going to succeed via that path anyway (the scorer never accepted it) — so accepting it at the existence-check step only ever produced a false "done," never a real success. Rejecting it immediately, with a clear "missing" message while the completing agent still has context to fix it, is strictly better than a confusing async failure downstream. This is a narrowing, and does deliberately change existing behavior: a `docs/`-only report that previously passed `verify_output_artifact` (and then silently failed later) now fails `verify_output_artifact` directly. Given nothing could ever complete successfully via that path before, this can only convert a guaranteed-eventual-failure into an earlier, clearer one — not break a previously-working flow.
2. **Old-name aliases added to `read_okf_report` (and, to keep `consume_gate_artifacts`'s "must mirror exactly" invariant intact, to `consume_gate_artifacts` too).** Unlike `docs/`, an alias is legitimate report content under an old filename, not a forbidden location — nothing forbids it, so making it actually score correctly (rather than rejecting it, which would just relocate the same inconsistency) was the right direction here. Alias resolution is a small, fixed, ten-entry lookup at each of the scorer's existing two candidate locations, not a directory scan — it doesn't reintroduce the stale-file risk `read_okf_report`'s own docstring warns against (that risk is specifically about iterating unknown subdirectories, not trying a second known filename at an already-checked path).

Verified via `tests/test_autopilot_spec.py::TestOutputPathResolutionAgreesWithScoringPathResolution` (both directions, end-to-end: `docs/`-only now rejected at the existence check; an old-name alias now both resolves AND scores cleanly) and a new `TestConsumeGateArtifacts::test_deletes_old_name_alias_too` closing the `consume_gate_artifacts` half of the same consistency requirement.

## Addendum 2: one more instance of the same bug class, found asking "other gaps?"

A third candidate in `resolve_declared_output_path`'s original 4 — flat `.hephaestus/<name>` (no phase subfolder) — had the identical problem: `read_okf_report`'s no-`subdir` path never checked it either (only `.hephaestus/<phase_name>/<name>` and the worktree root). Confirmed live with the same empirical method as `docs/`: a `qa.md` placed only at `.hephaestus/qa.md` (flat) passed `resolve_declared_output_path`, then `build_phase_output` returned `result_missing=True`.

This one is more precise than `docs/`, though, because the flat location genuinely *is* correct for two real cases: `feature_review` (its `GATE_RESULT_SUBDIR` override literally *is* `CONTEXT_DIR_NAME`, so flat `.hephaestus/` is its real, documented scoring location — per `build_phase_output`'s own comment, "Phase 0 artifacts are internal orchestration state, never a git-tracked deliverable"), and every non-gated phase (Feature Architect's `features.json`, etc. — never scored via `build_phase_output` at all, since that function returns `{}` immediately for anything not in `GATED_PHASES`, so there's no scoring-mismatch risk to guard against for those phases in the first place). Blanket-dropping this candidate the way `docs/` was dropped would have broken both of those genuinely-correct cases.

Fixed with a phase-aware condition instead of a blanket removal: `resolve_declared_output_path` now includes the flat-`.hephaestus/` candidate only when `phase_name not in GATED_PHASES or GATE_RESULT_SUBDIR.get(phase_name) == CONTEXT_DIR_NAME` — i.e., skip it for every *other* gated phase (qa_validation, product_validation, scope_review, design_review, architectural_review, adversarial_review), keep it everywhere else. Verified all three cases explicitly: `qa_validation` now rejects a flat-only report, `feature_review` still accepts one, a non-gated phase still accepts one (`TestResolveDeclaredOutputPath::test_does_not_check_flat_hephaestus_for_a_phase_scoped_gated_phase`/`test_still_checks_flat_hephaestus_for_feature_review`/`test_still_checks_flat_hephaestus_for_a_non_gated_phase`).

**One more, smaller imprecision noted but not fixed**: `OUTPUT_NAME_ALIASES` is keyed by filename only, not by (phase, filename) pair — and `architectural_review` and `feature_review` both declare `"review.md"` as their artifact, mapped to the single alias `"architectural_review_report.md"`. This means `feature_review`'s scoring now technically also accepts a file literally named `architectural_review_report.md`, which is semantically another phase's old name, not feature_review's own. Pre-existing in the alias table's structure (not introduced this pass, just extended into one more code path when `read_okf_report` gained alias support above); practically unreachable, since `feature_review` runs in Phase 0's own working directory before any per-feature pipeline (and therefore before `architectural_review`) ever runs, so the two files could never coexist in the same worktree. Not worth a bigger restructuring (phase-scoping the whole alias table) to close a collision that can't actually occur.

Full re-run after this second follow-up: 333 passed, zero regressions.

## Addendum 3: normalized feature_review onto the standard subdirectory convention, on request

Rather than leave `feature_review` permanently carved out (a special phase-aware condition, plus the noted-but-unfixed alias collision above), asked to eliminate both by making it use the same `.hephaestus/<phase_name>/` convention every other gated phase uses, with a unique filename.

**Renamed** `feature_review`'s declared report from `review.md` (flat `.hephaestus/`) to `feature_review.md` (`.hephaestus/feature_review/`) — closing both gaps at once: the phase-aware `include_flat_hephaestus` condition in `resolve_declared_output_path` is no longer needed for any current phase (kept as dead-but-correct infrastructure, not deleted, in case a future phase genuinely needs it), and the `OUTPUT_NAME_ALIASES["review.md"]` collision with `architectural_review` no longer applies to `feature_review` at all, since it no longer shares that filename.

Touched, in order:
- **`config/workflows/feature_architect/02_feature_review.yaml`** — the agent-facing prompt: `outputs:`/`done_definitions:` list, and every instruction telling the agent where to write (`./.hephaestus/feature_review/feature_review.md` instead of flat `./.hephaestus/review.md`; `feature_report.html` moved into the same subdirectory for consistency, since it's declared in the same phase's `outputs:` and would otherwise fail the same existence check once the phase-aware flat-`.hephaestus/` allowance no longer applies to it either).
- **`config/workflows/feature_architect/workflow.yaml`** — `required_output.feature_review` config override, `review.md` → `feature_review.md`.
- **`src/autopilot/spec.py`** — `GATE_RESULT_ARTIFACTS["feature_review"]` renamed; `GATE_RESULT_SUBDIR` emptied (its one entry removed); `build_phase_output`'s `feature_review` branch now calls `read_okf_report` with `phase_name=` like every other gated phase instead of `subdir=CONTEXT_DIR_NAME`; `score_feature_review`'s docstring/messages updated.
- **`src/mcp/autopilot/feature_routes.py`** — `get_workflow_decomposition_review` (serves the report to the UI) and `get_workflow_feature_report` (serves the HTML synopsis, shared with the main pipeline's `doc_review`) both updated to look in the new subdirectory; the Phase-0 `request_changes` handler's live-agent feedback prompt updated to tell the redo agent the new paths.
- **`src/autopilot/orchestrator/__init__.py`** — both `review_src`/`synopsis_src` durability-copy blocks (`run_phase0` and its sibling `finalize_phase0_workflow`) now read from the new subdirectory before archiving into `designs_folder`.

Verification: rewrote `TestResolveDeclaredOutputPath::test_still_checks_flat_hephaestus_for_feature_review` into `test_feature_review_now_uses_the_phase_subdirectory_like_every_other_gated_phase` (asserts the OLD flat location is now rejected and the NEW subdirectory location is accepted). Updated four more tests across `tests/test_phase0_idempotency.py` (both `run_phase0` durability-copy tests), `tests/test_spec.py` (`build_phase_output`'s feature_review-with-FIX scoring test), and `tests/test_task_completion_service.py` (`verify_gate_result_schema`'s feature_review subdir test) that were writing to the old flat location and would otherwise have silently started failing. Full run after this normalization across every affected file (`test_autopilot_spec.py`, `test_project_scoped_repo_resolution.py`, `test_self_review_hook.py`, `test_spec.py`, `test_spec_gate_firing.py`, `test_task_completion_service.py`, `test_update_task_status_ordering.py`, `test_update_task_status_response_shape.py`, `test_advance_phases.py`, `test_phase0_idempotency.py`, `test_review_mode.py`, `test_orchestrator_helpers.py`): 624 passed, zero regressions. ruff clean, `py_compile` clean.

## Addendum 4: added the legacy-location fallback, on request

The "not done" call above (no backward-compatibility bridge for an in-flight Phase 0 run) was revisited: check the old flat location too, for now, explicitly flagged for later removal rather than left as a silent permanent second convention.

Added `_feature_review_legacy_report(working_directory)` in `spec.py` — a one-line wrapper around `read_okf_report(working_directory, "review.md", subdir=CONTEXT_DIR_NAME)`, i.e. exactly the call `build_phase_output`'s `feature_review` branch used before this normalization. Its docstring is explicit about being temporary and names the removal condition (no run started before the normalization can still be active — Phase 0 runs are one-shot and short-lived, so this should be short-lived too). Wired in as a fallback, tried only when the new location comes up empty, at every surface that reads or cleans up `feature_review`'s report:

- **`resolve_declared_output_path`** (existence check) — without this, an in-flight old-style run's "done" claim would be rejected outright before ever reaching scoring.
- **`build_phase_output`** (scoring) — via the helper directly.
- **`verify_gate_result_schema`** (`verification.py`, the schema hard floor) — this one required a small `phase.name == "feature_review"` branch inside an otherwise-generic function, the one place this pass introduced phase-specific logic into shared code; explicitly acceptable here because it's flagged temporary, not a permanent special case.
- **`consume_gate_artifacts`** — not just read, *deleted* too: an unconsumed stale legacy file would otherwise keep resurrecting the exact stale-result goto-loop bug this function exists to prevent, across every retry of an in-flight run still writing to the old location.
- **`feature_routes.py`'s `get_workflow_decomposition_review`** (UI serving) — live-worktree and archived-`designs_folder` candidates both gained the legacy fallback; the response's `"name"` field now reflects whichever file was actually found rather than hardcoding the new name.
- **`orchestrator/__init__.py`'s two durability-copy blocks** (`run_phase0` and `finalize_phase0_workflow`) — without this, an in-flight run's report would never make it into `designs_folder` at all once the worktree is cleaned up, silently losing the audit trail this copy exists to preserve.

`get_workflow_feature_report` (the HTML synopsis endpoint) needed no change — its pre-existing flat-`.hephaestus/feature_report.html` candidate (there for `doc_review`'s own convention) already happens to be the same path `feature_review` used to write to, so the old-location fallback was already present by coincidence.

New `TestFeatureReviewLegacyLocationFallback` (4 tests: existence check falls back, existence check prefers the new location when both exist, scoring falls back, `consume_gate_artifacts` actually deletes the legacy file) plus one more in `tests/test_task_completion_service.py` for `verify_gate_result_schema`'s fallback. Full re-run after this addition across every affected file (adding `test_autopilot_api.py` to the set above): 740 passed, zero regressions.

## Part (b): schema validation — a live type-string mismatch found and fixed

While confirming `validate_gate_result_schema` and `synthetic_clean_result` actually agreed with each other (per the prompt's own instruction to verify this before assuming it), they didn't: `synthetic_clean_result` unconditionally wrote `type: f"{phase_name}_result"`; `validate_gate_result_schema` unconditionally expected `type: phase_name` (bare). Checked against the real, agent-facing documented convention (the literal frontmatter example in each `config/workflows/**/*.yaml`) to determine which was correct — six of the seven gated phases document the bare phase name (`qa_validation.yaml`: `type: qa_validation`), but `feature_review` documents `type: feature_review_result` (`02_feature_review.yaml`), a genuine per-phase inconsistency baked into the prompts, not a typo at one call site.

This is exactly the `type:`-field-mismatch bug class the plan's §4.9 target section names (citing the historical `66401a1` → `b648e90` flip-flop). Fixed by adding one source of truth both functions now consult:

```python
GATE_RESULT_TYPE_OVERRIDE: Dict[str, str] = {"feature_review": "feature_review_result"}

def expected_gate_result_type(phase_name: str) -> str:
    return GATE_RESULT_TYPE_OVERRIDE.get(phase_name, phase_name)
```

**Practical impact**: before this fix, `_cap_out_review_phase`'s own synthetic "clean pass" result would have failed `validate_gate_result_schema` for every gated phase except `feature_review`, had anything ever re-validated it after being written. In the current code path (`_cap_out_review_phase` → `_fire_phase_transition` with `force_continue=True`) that re-validation doesn't currently fire — the cap-out path bypasses `orchestrator.evaluate()` deliberately — so this was **latent, not actively causing the `99d19b6`-class failure today**. It's still a real, worth-fixing inconsistency: any future code path that re-reads a capped-out phase's report (a later goto back to the same phase, a manual re-check, or exactly the re-validation this consolidation was asked to make possible) would have hit it.

## Verification

The plan's own explicitly-named verification target, now fully covered:

- `TestSyntheticCleanResult` (`tests/test_autopilot_spec.py`) already existed for 5 of 7 gated phases (`qa_validation`, `product_validation`, `scope_review`, `architectural_review`, `adversarial_review`) — added the missing `design_review` and `feature_review` cases, completing all 7.
- Added `test_every_result_type_matches_validate_gate_result_schema`, looping every phase in `GATE_RESULT_REQUIRED_KEYS` and asserting `synthetic_clean_result`'s output passes `validate_gate_result_schema` — this is the literal characterization test the plan asked for ("asserting `_cap_out_review_phase`'s synthetic result scores as a clean pass under that phase's real `score_*` function"), extended to also cover the schema-agreement half, not just the scoring half.
- New `TestResolveDeclaredOutputPath` (5 tests): current-name match, old-name-alias fallback, phase-subdirectory-wins-over-root search-order, `docs/` correctly rejected (updated once `docs/` was dropped), not-found returns `None`.
- New `TestOutputPathResolutionAgreesWithScoringPathResolution` (2 tests) and `TestConsumeGateArtifacts::test_deletes_old_name_alias_too` (1 test) — see the existence/scoring-agreement fix above.

**Six pre-existing tests were found asserting the wrong (buggy) expected behavior** — using `qa_validation_result`/`scope_review_result` as fixture input where the real documented YAML says bare `qa_validation`/`scope_review` — across `test_autopilot_spec.py` (3), `test_task_completion_service.py` (1), `test_update_task_status_response_shape.py` (1). Fixed to match the real, authoritative convention (the literal frontmatter example each gated phase's own prompt shows a real agent) rather than the stale expectation. One more pre-existing, unrelated test bug in `test_task_completion_service.py::test_rejects_when_workflow_has_no_working_directory` (a `Mock()` chain that didn't account for the AgentWorktree-recovery code path added after the test was written, causing a `TypeError` unrelated to this item) was also fixed while in the same file — confirmed via diff inspection that it predates this session's edits entirely.

Full run across every affected file (`test_autopilot_spec.py`, `test_project_scoped_repo_resolution.py`, `test_self_review_hook.py`, `test_spec.py`, `test_spec_gate_firing.py`, `test_task_completion_service.py`, `test_update_task_status_ordering.py`, `test_update_task_status_response_shape.py`, `test_advance_phases.py`, plus `test_orchestrator_helpers.py`'s `TestCreatePhaseTaskReviewCap` class for `_cap_out_review_phase` directly): 327+63 passed, zero failures, zero regressions. ruff clean on every touched file (two pre-existing unrelated findings confirmed via `git show HEAD`). `py_compile` clean.

`tests/test_monitor.py` was excluded from this run's file list — a separate, in-progress, uncommitted fix from a parallel session was mid-edit there; not part of this item's scope and not touched.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.8).
- Any other Phase 2 item (§4.10 onward). Nothing found belonging to one of those.
- `read_okf_report`'s narrower search and `feature_report.html`'s lookup (see above) — related but genuinely distinct concerns, not folded into the shared resolver.
- Whether the `feature_review` per-phase `type:` inconsistency should itself be fixed (making all 7 gated phases use the same bare-name convention, editing `02_feature_review.yaml`'s documented prompt) — that's a product/prompt-content decision affecting what real agents are told to write, not a code-consolidation one. `GATE_RESULT_TYPE_OVERRIDE` accommodates it as-is rather than deciding it should change.

No commits — left in the working tree for review.
