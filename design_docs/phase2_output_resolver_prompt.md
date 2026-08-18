# Prompt: Phase 2, §4.9 — declared-output path & schema resolver

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.9 of `docs/AUTOPILOT_REFACTOR_PLAN.md`. Ninth item in this session's Phase 2 sequence — §4.1 through §4.8 are done; read their findings docs for the established rigor and format before starting. **The plan itself sizes this XL and flags it as a larger, riskier consolidation than every prior Phase 2 item — treat it that way: more investigation before committing to an approach, and a real willingness to scope down if the codebase has moved since the plan text was written.**

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.9 (full text) and `design_docs/phase2_termination_findings.md` / `phase2_pause_state_findings.md` for two prior examples of "the plan names a target shape, but the codebase had already partially built something adjacent to it" — the same thing is true here, more so.

## Freshness check — significant drift from the plan text, verify everything below yourself

- **`task_completion_service.py` no longer contains implementation.** It was decomposed in an earlier phase (see its own module docstring, and `design_docs/phase_1b_decomposition.md`) into `src/services/task_completion/` sub-modules — `verify_output_artifact` and friends now live in `src/services/task_completion/verification.py`. The plan's references to `task_completion_service.py` mean this new location; re-verify, don't assume it hasn't moved again since this handoff.
- **`spec.py`** (`src/autopilot/spec.py`) has NOT been decomposed and still holds most of the scoring/schema logic the plan describes.
- **Significant partial infrastructure already exists in `spec.py` that the plan's "Target" section doesn't account for** — read each of these in full before deciding what (if anything) still needs building:
  - `get_phase_required_files(phase, workflow_id)` (~line 139) — already centralizes *which* files a phase must produce (YAML `outputs:` + `workflow.yaml`'s `required_output:` override), with a per-definition cache that itself replaced a real cross-workflow-leak bug (read the comment above `_PHASE_OUTPUT_ARTIFACTS_CACHE` for that history).
  - `synthetic_clean_result(phase_name, run_count)` (~line 1220) — per-phase-shape synthetic "clean pass" result, explicitly built to fix the exact `99d19b6` bug the plan cites (its own docstring names the incident).
  - `validate_gate_result_schema(phase_name, result)` (~line 1348) — checks a gated phase's report `type:` field and required keys against `GATE_RESULT_REQUIRED_KEYS`, returning a human-readable rejection message.
  - `consume_gate_artifacts(phase_name, working_directory)` (~line 1261) — deletes a gated phase's result artifacts after a goto decision, forcing a fresh report on re-run.
  - `_cap_out_review_phase` itself now lives in `src/autopilot/orchestrator/phase_transitions.py` (~line 2522), not `spec.py` — it calls into `synthetic_clean_result` already, which is exactly part (c) of the plan's "Target" ("becomes one call into this resolver's per-phase schema rather than a hand-maintained parallel dict"). **This may already be done.** Verify by reading the call site and confirming there's no separate hand-maintained dict anywhere else still doing the same job.

  **What this means:** parts (b) (schema validator) and (c) (synthetic-result builder) of the plan's three-part target look substantially built already, just as free functions in `spec.py` rather than a class named `PhaseOutputResolver`. Don't assume this from this prompt's own read — confirm it by tracing every caller of these four functions and checking none of them still hand-roll the same logic in parallel. If confirmed, the remaining real work is narrower than the plan text implies.

- **Part (a), path resolution, looks NOT yet consolidated** — `get_phase_required_files` returns *declared file names*, not *resolved paths*. The plan's six-commit cluster (`5616785`, `dcb5b48`, `2078d4d`, `c552fa4`/`6072b49`, `c679e82`) was about a candidate-path *search loop* (trying multiple directories/filename-aliases to find where a file actually landed) — that's a different function from `get_phase_required_files`. Locate it fresh: grep `verify_output_artifact` (`src/services/task_completion/verification.py`) and `spec.py` for the actual file-existence-checking code, and separately locate the parallel three-commit cluster in report-lookup endpoints and phase YAML prompts (`685258c`, `8772527`, `d06f4ce` — search fresh, these commit hashes are old and the files they touched may have moved same as `task_completion_service.py` did).

## Target — three sub-parts, but confirm scope before starting each

1. **Path resolution** (likely still needed, per the freshness check above): one canonical path-resolution function/class, config-driven from each phase's YAML alias list (canonical name + accepted old-name aliases + accepted search roots), replacing the scattered candidate-path search loops. This is probably the bulk of the real remaining work.
2. **Frontmatter/JSON schema validation** (likely already done via `validate_gate_result_schema` — confirm, don't rebuild): if confirmed complete, this sub-part is a no-op for this item; note it as such in findings rather than re-implementing.
3. **Synthetic-result builder wired through the schema** (likely already done via `synthetic_clean_result` — confirm, don't rebuild): same as above.

**If (2) and (3) are both confirmed already-complete**, this item's actual scope is "build the path-resolver for (1), and optionally rename/relocate the existing (2)/(3) functions into one cohesive module or class alongside it for discoverability" — a materially smaller item than the plan's XL sizing implies. **Make this determination explicitly and record it in findings** rather than either quietly doing the smaller scope without saying so, or building a redundant `PhaseOutputResolver` class that just wraps functions that already work. This is exactly the kind of "the plan may be stale, don't build shadow duplicates" situation flagged in §4.6's and §4.7's own findings.

## Verification

The plan's own verification target: a characterization test per gated phase (`qa_validation`, `product_validation`, `scope_review`, `design_review`, `architectural_review`, `adversarial_review`, `feature_review` — confirm this is the full list via `GATE_RESULT_REQUIRED_KEYS` or equivalent) asserting `synthetic_clean_result`'s output, run through that phase's real `score_*` function, scores as a clean pass. Per the plan, this should already be passing for `qa_validation`/`product_validation`/`scope_review` (the three `99d19b6` fixed) — write it anyway, as a regression guard, not because it's expected to fail. If it's currently failing for any phase, that's a live bug worth its own finding, not something to quietly work around.

For whatever path-resolution consolidation you build: characterization tests for the current search behavior (each accepted alias/directory) before consolidating, so the replacement is provably behavior-preserving, not just plausible.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.8).
- Any other Phase 2 item (§4.10 onward). Log anything found belonging to one of those.
- Rebuilding `validate_gate_result_schema`/`synthetic_clean_result`/`consume_gate_artifacts` if the freshness check confirms they're already correct and complete — extend or relocate them, don't rewrite working code to match a class shape the plan describes just for its own sake.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions or this prompt's own freshness-check guesses (all of the above needs your own re-verification, not just mine). `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>`. Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Findings doc (`design_docs/phase2_output_resolver_findings.md` or similar) — given this item's own scope is uncertain until you investigate, lead with a clear statement of what you found already-built vs. what you actually built, before the rest of the usual findings structure. No commits — leave everything in the working tree for review.
