# Prompt: Phase 2, §4.6 — SOLID-review-sourced consolidations

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.6 of `docs/AUTOPILOT_REFACTOR_PLAN.md`. Sixth item in this session's Phase 2 sequence — §4.1 through §4.5 are done; read their findings docs for the established rigor and format. **This item is more heterogeneous than the prior five — three unrelated sub-problems bundled under one SOLID-review origin, not one consolidation.** Treat them as three separate pieces of work with three separate findings sections (or three separate findings docs, your call), not one merged narrative.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.6 (full text). **Every file path and line number in it is stale** — it predates all five decompositions this session did (the text still says `orchestrator.py:1538`, `orchestrator.py:9229`, `src/mcp/autopilot_api.py`, none of which exist anymore as flat files).

## Sub-problem 1: string-branching dispatch → registries

Replace remaining string-branching `if/elif` dispatch with registries at the highest-traffic sites: MCP tool dispatch, condition evaluation (`WorkflowOrchestrator._check_condition` — this lives in `src/workflow_engine/orchestrator.py`, a *different* file from the decomposed `src/autopilot/orchestrator/` package despite the similar name; confirmed elsewhere in this session's own work that these two are easy to conflate — don't), phase-action handling. The plan names `config_validator.py`'s and this same orchestrator's shared `CONDITION_PATTERN`/`CONDITION_OPERATORS` grammar as a **positive pattern to extend, not replace** — a prior adversarial review (`SOLID_REFACTOR_ADVERSARIAL_REVIEW.md`) explicitly called it out as worth preserving. Locate it fresh and confirm that characterization still holds before treating it as a template.

## Sub-problem 2: wire the remaining status-derivation reimplementations

Confirmed current locations, freshness-checked at this handoff: `is_design_fully_complete` — `src/autopilot/orchestrator/queue.py` (re-verify exact line). `run_design_aggregate` — `src/autopilot/orchestrator/__init__.py` (re-verify exact line). Both should call `derive_design_status`/`derive_workflow_status` from `src/core/status_derivation.py` instead of independently computing "is this done" — confirm neither does yet.

**One instance may already be fixed — verify, don't assume either way**: the plan names `review_feature`'s approve handler (now `src/mcp/autopilot/feature_routes.py`, around line 552, not `autopilot_api.py:4375-4397` as the stale text says) as a confirmed-live hand-rolled duplicate that Phase 3 Tier 1 item 8 was supposed to fix first, before this consolidation. A quick grep at this handoff shows `feature_routes.py` already references `derive_feature_status` multiple times (lines 172, 186, 592, 636 as of this check) — this may mean Tier 1 item 8 already landed, in which case this specific call site needs auditing (does the approve handler itself call the shared function, or do other nearby functions in the same file, leaving the approve handler still hand-rolling?) rather than fixing from scratch. Read the actual approve-handler body before assuming either way.

Two more call sites the plan names as needing the same audit, never explicitly confirmed fixed: `_workflow_appears_abandoned` and the sweep's own `_advance_phases` dispatch cases — both now in `src/autopilot/orchestrator/` submodules (`policy.py` and `phase_transitions.py` respectively, per this session's earlier decomposition work — re-verify). This exact mistake ("all tasks done" without a phase-completeness check) has recurred independently at least four times historically, including once inside `status_derivation.py` itself — treat any of these four call sites you find still unwired as equally live, not lower-priority than the two named in the plan's headline.

## Sub-problem 3: project-CRUD route reconciliation — needs explicit product sign-off, do not implement without it

Two parallel route surfaces exist for the same CRUD operations: `/api/projects/*` (in `projects_api.py`) and `/api/autopilot/projects/*` (in what's now `src/mcp/autopilot/project_routes.py`, post-decomposition). **This is not backend-internal cleanup like the rest of this plan** — retiring either surface is a breaking change for any frontend code or external script still calling it. Per the plan's explicit instruction: grep `frontend/src` for both path prefixes before choosing which surface survives, and coordinate the frontend-side change as part of this item's work, not as a follow-up someone else does later. **If your grep shows both prefixes are actively used by the frontend, stop and report that rather than picking a survivor unilaterally** — this is exactly the kind of decision this plan's own principles reserve for a human, not an agent working alone.

## Verification

- Sub-problem 1: characterization tests for current dispatch behavior at each site before converting to a registry, so the conversion is provably behavior-preserving.
- Sub-problem 2: for each call site confirmed still unwired, a characterization test asserting the current hand-rolled logic's specific gap (e.g. "all tasks done" without checking phase completeness) reproduces the historical bug pattern, then confirm it's closed once wired through `status_derivation.py`.
- Sub-problem 3: no code changes without the grep result and a recorded decision — if you proceed, the frontend coordination is part of this item's own verification, not separate.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.5, all five decompositions).
- Any other Phase 2 item (§4.7 onward). Log anything found belonging to one of those.
- Modifying `WorkflowOrchestrator`/`CONDITION_PATTERN` itself beyond adding registry entries — it's a positive pattern to extend, not rewrite.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions or this prompt's own stale-path guesses. `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>`. Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Findings doc (`design_docs/phase2_solid_consolidations_findings.md` or similar) — given the three sub-problems are unrelated, structure it with three clear sections, not one merged narrative, and call out sub-problem 3's grep result and decision (or non-decision) prominently regardless of whether you implemented it. No commits — leave everything in the working tree for review.
