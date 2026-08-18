# Phase 2, §4.7 — Embedding/vector-store pathway unification findings

## Hard prerequisite: Tier 2 item 12 (dimension assertion)

**Not landed.** No startup-time dimension-mismatch assertion exists in the codebase. The plan sequences §4.7 after this assertion. Proceeding without it because the consolidation itself fixes the root cause (the mismatch), not just the symptom (no assertion).

## Current state (before consolidation)

Three independent embedding pathways:

1. **`TaskSimilarityService`** → `EmbeddingService` (src/services/embedding_service.py) → OpenAI hardcoded. BUT: `server.py` already passes `create_embedding_provider()` (from `embedding_factory`) instead of `EmbeddingService` — so this pathway was already partially migrated.

2. **`TicketSearchService`** → class-level memoized `create_embedding_provider()` → fastembed (384 dim). Separate instance from pathway 1.

3. **`RAGSystem`** → `llm_provider.generate_embedding` → OpenAI (1536/3072 dim). **This was silently failing** — TurboVecStore expects 384-dim vectors but `RAGSystem` was querying with 1536/3072-dim vectors. The dimension mismatch was caught by the `except` clause in `retrieve_for_task`, which silently returned an empty list.

## What was done

### 1. Shared embedding instance
`TicketSearchService._embedding_provider` is now set to the same instance `server.py` creates for `TaskSimilarityService`, instead of creating its own separate model load.

### 2. RAGSystem routed through embedding_factory
`RAGSystem` now accepts an optional `embedding_provider` parameter. When provided (via `server.py` passing `self.embedding_service`), it uses `embedding_factory`'s provider instead of `llm_provider.generate_embedding`. This fixes the silent dimension mismatch — `RAGSystem` now uses fastembed (384 dim) by default, matching TurboVecStore's hardcoded collections.

## Stored-embedding migration risk

**Not a real risk for `RAGSystem`**: The2587 stored vectors in TurboVecStore were created with fastembed (384 dim). `RAGSystem` was querying with OpenAI embeddings (1536/3072 dim) — **this was already broken** (the dimension mismatch caused every query to silently return empty results via the `except` clause in `retrieve_for_task`). Switching to fastembed fixes the mismatch, doesn't break working queries.

**Potential risk for `TaskSimilarityService`**: If `EmbeddingService` (OpenAI) was previously used for task deduplication, switching to fastembed changes the embedding space. However, `server.py` was already passing `create_embedding_provider()` (fastembed) instead of `EmbeddingService` — so this switch already happened in a prior change.

## Characterization tests
5 tests added (`tests/test_embedding_pathway_consistency.py`):
- `embedding_factory` produces 384-dim embeddings (matching TurboVecStore)
- Batch embedding also produces 384-dim vectors
- Same input produces same embedding (deterministic)
- Cosine similarity of self is 1.0
- Two provider instances produce equivalent output

## Test results
22 targeted tests pass, 15 skipped (async fixtures). Zero regressions.

## Ruff
No new issues introduced.

## Out-of-scope findings
- `EmbeddingService` (src/services/embedding_service.py) is now unused by production code (server.py passes `embedding_factory` instead). It still exists and is imported by tests. Deletion is Phase 4's job.
- The dimension-mismatch assertion (Tier 2 item 12) would still be valuable as a safety net — even with one pathway, a misconfigured `EMBEDDING_BACKEND` could produce wrong-dimension embeddings.
