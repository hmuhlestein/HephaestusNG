"""Characterization tests for embedding pathways (Phase 2 §4.7).

These verify the embedding output dimensions and consistency across
the unified pathway before and after consolidation. The key invariant:
all pathways must produce embeddings compatible with TurboVecStore's
hardcoded 384-dim collections.
"""

import pytest

from src.memory.embedding_factory import create_embedding_provider


class TestEmbeddingPathwayConsistency:
    """Both pathways must produce 384-dim embeddings (matching TurboVecStore)."""

    @pytest.mark.asyncio
    async def test_embedding_factory_produces_384_dim(self):
        """embedding_factory.create_embedding_provider() produces 384-dim
        embeddings (matching TurboVecStore's hardcoded collections)."""
        provider = create_embedding_provider()
        embedding = await provider.generate_embedding("test task description")

        assert len(embedding) == 384, (
            f"Expected 384-dim embedding from fastembed, got {len(embedding)}"
        )

    @pytest.mark.asyncio
    async def test_embedding_factory_batch_produces_384_dim(self):
        """Batch embedding also produces 384-dim vectors."""
        provider = create_embedding_provider()
        embeddings = await provider.generate_embeddings([
            "first task",
            "second task",
        ])

        assert len(embeddings) == 2
        for emb in embeddings:
            assert len(emb) == 384

    @pytest.mark.asyncio
    async def test_embedding_factory_consistent_output(self):
        """Same input produces same embedding (deterministic)."""
        provider = create_embedding_provider()
        text = "fix the authentication bug in the login flow"

        emb1 = await provider.generate_embedding(text)
        emb2 = await provider.generate_embedding(text)

        assert emb1 == emb2

    @pytest.mark.asyncio
    async def test_embedding_factory_cosine_similarity_self(self):
        """Cosine similarity of an embedding with itself is 1.0."""
        from src.memory.embedding_factory import EmbeddingProvider

        provider = create_embedding_provider()
        emb = await provider.generate_embedding("test text")

        sim = EmbeddingProvider.calculate_cosine_similarity(emb, emb)
        assert abs(sim - 1.0) < 1e-6

    @pytest.mark.asyncio
    async def test_shared_instance_same_output(self):
        """Two calls to create_embedding_provider() return providers that
        produce equivalent output (same model, same dimensions)."""
        provider1 = create_embedding_provider()
        provider2 = create_embedding_provider()

        text = "shared instance test"
        emb1 = await provider1.generate_embedding(text)
        emb2 = await provider2.generate_embedding(text)

        assert len(emb1) == len(emb2) == 384
        # Same model should produce identical output
        assert emb1 == emb2
