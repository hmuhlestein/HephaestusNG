# Prompt: Phase 2, §4.7 — embedding/vector-store pathway unification

Paste this to the implementing agent as-is.

---

Execute Phase 2, §4.7 of `docs/AUTOPILOT_REFACTOR_PLAN.md`. Seventh item in this session's Phase 2 sequence — §4.1 through §4.6 are done; read their findings docs for the established rigor and format before starting.

## Read first

`docs/AUTOPILOT_REFACTOR_PLAN.md` §4.7 (full text, short). **Do your own freshness check** — this section predates this session's decompositions; re-locate `embedding_factory.py`, `TaskSimilarityService`, `TicketSearchService`, `RAGSystem`, and `TurboVecStore` fresh rather than trusting any implied path.

## Hard prerequisite — verify before starting

The plan explicitly sequences this item **after** Phase 3 Tier 2 item 12 (the dimension-mismatch startup assertion) — check `docs/AUTOPILOT_REFACTOR_PLAN.md`'s current Tier 2 list for that item's status. If it hasn't landed yet, that's a blocker: land it first (it's a small, independent safety-net fix, not part of this consolidation), or explicitly flag that you're proceeding without it and why.

## Target — two sub-changes

1. **Share one embedding-provider instance** between `TaskSimilarityService` and `TicketSearchService` instead of the current two independent long-lived model loads (`TaskSimilarityService` uses `embedding_factory.py`'s `EMBEDDING_BACKEND`/`FASTEMBED_MODEL`-driven provider directly; `TicketSearchService` has its own separate lazily-memoized instance of the same configurable stack). Find both call sites, confirm they're really instantiating independently, and give them a shared instance instead.
2. **Route `RAGSystem.retrieve_for_task` through `embedding_factory.create_embedding_provider()`** instead of its current `llm_provider.generate_embedding` path (governed by a different config surface, `hephaestus_config.yaml`'s `llm.embedding_provider`, which has to agree with `TurboVecStore`'s hardcoded 384-dim collections with nothing currently checking that it does — that's the exact gap Tier 2 item 12's assertion guards against as a symptom-fix; this item removes the root cause by collapsing to one pathway).

## Verification

- Characterization tests for current embedding output on a fixed input, for both pathways, before consolidating — confirm the unified pathway produces embeddings of the same dimensionality and (as close as practical) semantic behavior as what `RAGSystem` currently gets from `llm_provider.generate_embedding`. A silent embedding-space change would be a real regression (existing stored vectors would no longer be comparable to newly-generated ones) — if you find this is a real risk, say so explicitly rather than proceeding quietly; it may mean this item needs a migration step for already-stored embeddings, which would be a significant scope expansion worth flagging before implementing, not deciding unilaterally.
- Confirm `TaskSimilarityService`/`TicketSearchService` still produce equivalent results after sharing the instance.

## Explicitly out of scope

- Anything already shipped (§4.1 through §4.6, all five decompositions).
- Any other Phase 2 item (§4.8 onward). Log anything found belonging to one of those.
- Building the dimension-mismatch assertion itself if Tier 2 item 12 hasn't landed — that's a separate, smaller fix; land it first or flag the gap, don't fold it into this item's diff.

## Quality bar, matching every prior target this session

Adversarial review against HEAD, not assumptions. `ruff check` clean on every touched file — verify pre-existing findings via `git show HEAD~1 -- <file>`. Full targeted-test verification plus a full-suite gate against the pristine-HEAD baseline (strict subset of pre-existing failures, zero regressions). Findings doc (`design_docs/phase2_embedding_unification_findings.md` or similar) for anything out of scope, especially the stored-embedding-migration question above if it turns out to be real. No commits — leave everything in the working tree for review.
