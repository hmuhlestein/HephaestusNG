"""Tests for turbovec store, embedding factory, and store factory."""

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_data_dir(tmp_path):
    """Provide a temporary data directory for TurboVecStore."""
    return str(tmp_path / "turbovec_test")


@pytest.fixture
def store(tmp_data_dir):
    """Create a fresh TurboVecStore in a temp directory."""
    from src.memory.turbovec_store import TurboVecStore

    return TurboVecStore(data_dir=tmp_data_dir, collection_prefix="test")


def _random_embedding(dim: int = 384) -> list[float]:
    """Generate a random normalized embedding."""
    vec = np.random.randn(dim).astype(np.float32)
    return (vec / np.linalg.norm(vec)).tolist()


def _random_id() -> str:
    return str(uuid.uuid4())


# ===========================================================================
# TurboVecStore tests
# ===========================================================================


class TestTurboVecStoreInit:
    def test_creates_data_dir(self, tmp_path):
        data_dir = str(tmp_path / "new_dir")
        assert not Path(data_dir).exists()
        from src.memory.turbovec_store import TurboVecStore

        TurboVecStore(data_dir=data_dir)
        assert Path(data_dir).exists()

    def test_initializes_all_collections(self, store):
        assert len(store.COLLECTIONS) == 7
        for name in store.COLLECTIONS:
            assert name in store._indices
            assert name in store._metadata

    def test_persistence_survives_reload(self, tmp_data_dir):
        from src.memory.turbovec_store import TurboVecStore

        s1 = TurboVecStore(data_dir=tmp_data_dir, collection_prefix="test")
        mem_id = _random_id()
        emb = _random_embedding()
        asyncio.get_event_loop().run_until_complete(
            s1.store_memory("agent_memories", mem_id, emb, "hello", {"tag": "a"})
        )
        del s1

        s2 = TurboVecStore(data_dir=tmp_data_dir, collection_prefix="test")
        assert len(s2._metadata["agent_memories"]) == 1

    def test_custom_bit_width(self, tmp_path):
        from src.memory.turbovec_store import TurboVecStore

        s = TurboVecStore(data_dir=str(tmp_path / "bw"), bit_width=2)
        assert s.bit_width == 2


class TestStoreMemory:
    @pytest.mark.asyncio
    async def test_basic_store(self, store):
        mem_id = _random_id()
        emb = _random_embedding()
        result = await store.store_memory(
            "agent_memories", mem_id, emb, "test content", {"type": "test"}
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_unknown_collection_raises(self, store):
        with pytest.raises(ValueError, match="Unknown collection"):
            await store.store_memory("nonexistent", _random_id(), _random_embedding(), "", {})

    @pytest.mark.asyncio
    async def test_wrong_dimension_raises(self, store):
        with pytest.raises(ValueError, match="dimension mismatch"):
            await store.store_memory("agent_memories", _random_id(), [0.1, 0.2], "", {})

    @pytest.mark.asyncio
    async def test_upsert_updates_existing(self, store):
        mem_id = _random_id()
        emb = _random_embedding()
        await store.store_memory("agent_memories", mem_id, emb, "v1", {"version": 1})
        await store.store_memory("agent_memories", mem_id, emb, "v2", {"version": 2})
        assert len(store._metadata["agent_memories"]) == 1

    @pytest.mark.asyncio
    async def test_persists_index_and_metadata(self, tmp_data_dir):
        from src.memory.turbovec_store import TurboVecStore

        store = TurboVecStore(data_dir=tmp_data_dir)
        mem_id = _random_id()
        await store.store_memory("agent_memories", mem_id, _random_embedding(), "persist", {})

        index_path = Path(tmp_data_dir) / "hephaestus_agent_memories.tvim"
        meta_path = Path(tmp_data_dir) / "hephaestus_agent_memories_meta.json"
        assert index_path.exists()
        assert meta_path.exists()

    @pytest.mark.asyncio
    async def test_metadata_stored_correctly(self, store):
        mem_id = _random_id()
        emb = _random_embedding()
        await store.store_memory(
            "agent_memories", mem_id, emb, "content here", {"tag": "x", "score": 42}
        )
        int_id = store._str_to_int[mem_id]
        meta = store._metadata["agent_memories"][int_id]
        assert meta["content"] == "content here"
        assert meta["memory_id"] == mem_id
        assert meta["tag"] == "x"
        assert meta["score"] == 42
        assert "timestamp" in meta


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_results(self, store):
        emb = _random_embedding()
        mem_id = _random_id()
        await store.store_memory("agent_memories", mem_id, emb, "searchable", {})
        results = await store.search("agent_memories", emb, limit=5)
        assert len(results) >= 1
        assert results[0]["id"] == mem_id
        assert results[0]["content"] == "searchable"

    @pytest.mark.asyncio
    async def test_search_empty_collection(self, store):
        results = await store.search("agent_memories", _random_embedding())
        assert results == []

    @pytest.mark.asyncio
    async def test_search_with_filter(self, store):
        emb1 = _random_embedding()
        emb2 = _random_embedding()
        await store.store_memory("agent_memories", _random_id(), emb1, "a", {"cat": "x"})
        await store.store_memory("agent_memories", _random_id(), emb2, "b", {"cat": "y"})

        results = await store.search(
            "agent_memories", emb1, limit=10, filters={"cat": "x"}
        )
        assert all(r["metadata"].get("cat") == "x" for r in results)

    @pytest.mark.asyncio
    async def test_search_limit(self, store):
        for _ in range(10):
            await store.store_memory(
                "agent_memories", _random_id(), _random_embedding(), "item", {}
            )
        results = await store.search("agent_memories", _random_embedding(), limit=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_search_unknown_collection_raises(self, store):
        with pytest.raises(ValueError, match="Unknown collection"):
            await store.search("nonexistent", _random_embedding())

    @pytest.mark.asyncio
    async def test_search_score_threshold(self, store):
        emb = _random_embedding()
        await store.store_memory("agent_memories", _random_id(), emb, "match", {})
        results = await store.search(
            "agent_memories", emb, limit=10, score_threshold=0.9999
        )
        for r in results:
            assert r["score"] >= 0.9999


class TestSearchAllCollections:
    @pytest.mark.asyncio
    async def test_searches_all_collections(self, store):
        emb_mem = _random_embedding()
        emb_doc = _random_embedding()
        await store.store_memory("agent_memories", _random_id(), emb_mem, "mem", {})
        await store.store_memory("static_docs", _random_id(), emb_doc, "doc", {})

        results = await store.search_all_collections(emb_mem, limit_per_collection=5)
        collections_found = {r["collection"] for r in results}
        assert "agent_memories" in collections_found

    @pytest.mark.asyncio
    async def test_respects_total_limit(self, store):
        for _ in range(5):
            await store.store_memory("agent_memories", _random_id(), _random_embedding(), "", {})
            await store.store_memory("static_docs", _random_id(), _random_embedding(), "", {})

        results = await store.search_all_collections(
            _random_embedding(), limit_per_collection=5, total_limit=4
        )
        assert len(results) <= 4

    @pytest.mark.asyncio
    async def test_sorted_by_score_desc(self, store):
        emb = _random_embedding()
        await store.store_memory("agent_memories", _random_id(), emb, "a", {})
        await store.store_memory("static_docs", _random_id(), _random_embedding(), "b", {})

        results = await store.search_all_collections(emb, limit_per_collection=5)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)


class TestDeleteMemory:
    def test_delete_existing(self, store):
        mem_id = _random_id()
        asyncio.get_event_loop().run_until_complete(
            store.store_memory("agent_memories", mem_id, _random_embedding(), "del", {})
        )
        assert store.delete_memory("agent_memories", mem_id) is True
        assert len(store._metadata["agent_memories"]) == 0

    def test_delete_nonexistent(self, store):
        assert store.delete_memory("agent_memories", "no-such-id") is False

    def test_delete_unknown_collection_raises(self, store):
        with pytest.raises(ValueError, match="Unknown collection"):
            store.delete_memory("nonexistent", "x")

    def test_delete_removes_from_str_to_int(self, store):
        mem_id = _random_id()
        asyncio.get_event_loop().run_until_complete(
            store.store_memory("agent_memories", mem_id, _random_embedding(), "", {})
        )
        assert mem_id in store._str_to_int
        store.delete_memory("agent_memories", mem_id)
        assert mem_id not in store._str_to_int


class TestGetStats:
    def test_collection_stats(self, store):
        stats = store.get_collection_stats("agent_memories")
        assert stats["name"] == "agent_memories"
        assert stats["backend"] == "turbovec"
        assert stats["vectors_count"] == 0
        assert stats["status"] == "green"

    def test_all_stats(self, store):
        stats = store.get_all_stats()
        assert len(stats) == len(store.COLLECTIONS)
        for name in store.COLLECTIONS:
            assert name in stats

    def test_stats_unknown_collection_raises(self, store):
        with pytest.raises(ValueError, match="Unknown collection"):
            store.get_collection_stats("nonexistent")


class TestFlushAll:
    def test_flush_creates_files(self, store):
        mem_id = _random_id()
        asyncio.get_event_loop().run_until_complete(
            store.store_memory("agent_memories", mem_id, _random_embedding(), "", {})
        )
        store.flush_all()
        for name in store.COLLECTIONS:
            assert store._get_index_path(name).exists()
            assert store._get_metadata_path(name).exists()


class TestIdMapping:
    @pytest.mark.asyncio
    async def test_str_to_int_mapping(self, store):
        mem_id = _random_id()
        await store.store_memory("agent_memories", mem_id, _random_embedding(), "", {})
        assert mem_id in store._str_to_int
        int_id = store._str_to_int[mem_id]
        assert isinstance(int_id, int)
        assert 0 <= int_id < 2**63

    @pytest.mark.asyncio
    async def test_deterministic_hash(self, store):
        mem_id = "fixed-id-for-test"
        h1 = store._id_to_int(mem_id)
        h2 = store._id_to_int(mem_id)
        assert h1 == h2

    @pytest.mark.asyncio
    async def test_different_ids_different_hashes(self, store):
        h1 = store._id_to_int("id-a")
        h2 = store._id_to_int("id-b")
        assert h1 != h2


# ===========================================================================
# FastEmbed embedding provider tests
# ===========================================================================


class TestFastEmbedProvider:
    @pytest.fixture
    def provider(self):
        from src.memory.embedding_factory import FastEmbedEmbeddingProvider

        return FastEmbedEmbeddingProvider()

    @pytest.mark.asyncio
    async def test_single_embedding(self, provider):
        emb = await provider.generate_embedding("hello world")
        assert isinstance(emb, list)
        assert len(emb) == 384
        assert all(isinstance(x, float) for x in emb)

    @pytest.mark.asyncio
    async def test_batch_embeddings(self, provider):
        texts = ["hello", "world", "test"]
        embs = await provider.generate_embeddings(texts)
        assert len(embs) == 3
        for emb in embs:
            assert len(emb) == 384

    @pytest.mark.asyncio
    async def test_deterministic(self, provider):
        text = "deterministic test input"
        e1 = await provider.generate_embedding(text)
        e2 = await provider.generate_embedding(text)
        assert e1 == e2

    @pytest.mark.asyncio
    async def test_different_inputs_different_embeddings(self, provider):
        e1 = await provider.generate_embedding("cats are great")
        e2 = await provider.generate_embedding("dogs are great")
        assert e1 != e2

    def test_get_dim(self, provider):
        assert provider.get_dim() == 384

    def test_default_model(self, provider):
        assert provider.model_name == "BAAI/bge-small-en-v1.5"


class TestCreateEmbeddingProvider:
    def test_fastembed_default(self, monkeypatch):
        monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
        from src.memory.embedding_factory import create_embedding_provider

        provider = create_embedding_provider()
        assert provider.get_dim() == 384

    def test_fastembed_explicit(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BACKEND", "fastembed")
        from src.memory.embedding_factory import create_embedding_provider

        provider = create_embedding_provider()
        assert provider.get_dim() == 384

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BACKEND", "nonexistent")
        from src.memory.embedding_factory import create_embedding_provider

        with pytest.raises(ValueError, match="Unknown EMBEDDING_BACKEND"):
            create_embedding_provider()


# ===========================================================================
# Store factory tests
# ===========================================================================


class TestCreateVectorStore:
    def test_turbovec_default(self, monkeypatch):
        monkeypatch.delenv("VECTOR_STORE_BACKEND", raising=False)
        from src.memory.store_factory import create_vector_store

        store = create_vector_store()
        assert type(store).__name__ == "TurboVecStore"

    def test_turbovec_explicit(self, monkeypatch):
        monkeypatch.setenv("VECTOR_STORE_BACKEND", "turbovec")
        from src.memory.store_factory import create_vector_store

        store = create_vector_store()
        assert type(store).__name__ == "TurboVecStore"

    def test_unknown_backend_raises(self, monkeypatch):
        monkeypatch.setenv("VECTOR_STORE_BACKEND", "nonexistent")
        from src.memory.store_factory import create_vector_store

        with pytest.raises(ValueError, match="Unknown VECTOR_STORE_BACKEND"):
            create_vector_store()


class TestVectorStoreProtocol:
    def test_turbovec_satisfies_protocol(self, store):
        from src.memory.store_factory import VectorStoreProtocol

        assert isinstance(store, VectorStoreProtocol)


# ===========================================================================
# Integration: embedding → store roundtrip
# ===========================================================================


class TestIntegrationRoundtrip:
    @pytest.mark.asyncio
    async def test_embed_store_search(self, tmp_path):
        from src.memory.embedding_factory import FastEmbedEmbeddingProvider
        from src.memory.turbovec_store import TurboVecStore

        provider = FastEmbedEmbeddingProvider()
        store = TurboVecStore(data_dir=str(tmp_path / "rt"))

        text = "turbovec is a local vector search engine"
        emb = await provider.generate_embedding(text)
        mem_id = _random_id()

        await store.store_memory("agent_memories", mem_id, emb, text, {"source": "test"})

        query_emb = await provider.generate_embedding("local vector search")
        results = await store.search("agent_memories", query_emb, limit=5)
        assert len(results) >= 1
        assert results[0]["id"] == mem_id
        assert results[0]["content"] == text

    @pytest.mark.asyncio
    async def test_embed_delete_search(self, tmp_path):
        from src.memory.embedding_factory import FastEmbedEmbeddingProvider
        from src.memory.turbovec_store import TurboVecStore

        provider = FastEmbedEmbeddingProvider()
        store = TurboVecStore(data_dir=str(tmp_path / "del_rt"))

        emb = await provider.generate_embedding("to be deleted")
        mem_id = _random_id()
        await store.store_memory("agent_memories", mem_id, emb, "delete me", {})

        store.delete_memory("agent_memories", mem_id)
        results = await store.search("agent_memories", emb, limit=5)
        assert all(r["id"] != mem_id for r in results)

    @pytest.mark.asyncio
    async def test_cross_collection_search(self, tmp_path):
        from src.memory.embedding_factory import FastEmbedEmbeddingProvider
        from src.memory.turbovec_store import TurboVecStore

        provider = FastEmbedEmbeddingProvider()
        store = TurboVecStore(data_dir=str(tmp_path / "cross"))

        text = "error handling best practices"
        emb = await provider.generate_embedding(text)
        await store.store_memory("error_solutions", _random_id(), emb, text, {})
        await store.store_memory(
            "domain_knowledge", _random_id(), _random_embedding(), "other", {}
        )

        query = await provider.generate_embedding("how to handle errors")
        results = await store.search_all_collections(query, limit_per_collection=3)
        assert any(r["collection"] == "error_solutions" for r in results)
