"""Regression: VectorStoreManager.search called a Qdrant method that no longer exists.

`QdrantClient.search` was removed in qdrant-client 1.x (1.18 is what's
installed here) in favour of `query_points`. The call raised AttributeError,
which the surrounding `except Exception` caught and logged as "Search failed
in collection ...", returning [] -- so semantic search on the Qdrant backend
silently produced no results at all, indistinguishable to every caller from
"nothing matched". Memory/RAG lookups degraded to zero context rather than
failing visibly.

Only deployments using the documented VECTOR_STORE_BACKEND=qdrant fallback
were affected; the default backend is turbovec. Found via mypy's
[attr-defined] category once c38f143 unblocked it.

The existing tests/test_vector_store.py is a single integration test gated on
an OpenAI API key, so this path had no coverage at all. These use a mocked
client so they run everywhere.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.memory.vector_store import VectorStoreManager


def _point(pid="p1", score=0.9, content="hello", **payload):
    return SimpleNamespace(
        id=pid, score=score, payload={"content": content, **payload}
    )


@pytest.fixture
def store():
    with patch("src.memory.vector_store.QdrantClient"):
        manager = VectorStoreManager(qdrant_url="http://localhost:6333")
    manager.client = MagicMock()
    return manager


@pytest.fixture
def collection(store):
    return next(iter(store.COLLECTIONS))


class TestUsesTheCurrentQdrantApi:
    @pytest.mark.asyncio
    async def test_calls_query_points_not_the_removed_search(self, store, collection):
        store.client.query_points.return_value = SimpleNamespace(points=[_point()])

        await store.search(collection=collection, query_vector=[0.1, 0.2])

        store.client.query_points.assert_called_once()
        assert store.client.query_points.call_args.kwargs["query"] == [0.1, 0.2]

    @pytest.mark.asyncio
    async def test_does_not_call_search_at_all(self, store, collection):
        """The guard that would have caught this: a client exposing only the
        removed method must never be reached."""
        store.client.query_points.return_value = SimpleNamespace(points=[])

        await store.search(collection=collection, query_vector=[0.1])

        store.client.search.assert_not_called()

    @pytest.mark.asyncio
    async def test_results_are_read_off_the_response_points(self, store, collection):
        """query_points returns a QueryResponse, not a bare list -- iterating
        the response object itself would silently yield nothing."""
        store.client.query_points.return_value = SimpleNamespace(
            points=[_point(pid="a", score=0.75, content="body", kind="note")]
        )

        results = await store.search(collection=collection, query_vector=[0.1])

        assert results == [
            {
                "id": "a",
                "score": 0.75,
                "content": "body",
                "metadata": {"kind": "note"},
            }
        ]

    @pytest.mark.asyncio
    async def test_limit_and_threshold_are_forwarded(self, store, collection):
        store.client.query_points.return_value = SimpleNamespace(points=[])

        await store.search(
            collection=collection,
            query_vector=[0.1],
            limit=3,
            score_threshold=0.42,
        )

        kwargs = store.client.query_points.call_args.kwargs
        assert kwargs["limit"] == 3
        assert kwargs["score_threshold"] == 0.42

    @pytest.mark.asyncio
    async def test_an_empty_result_is_not_mistaken_for_a_failure(
        self, store, collection
    ):
        store.client.query_points.return_value = SimpleNamespace(points=[])

        assert await store.search(collection=collection, query_vector=[0.1]) == []

    @pytest.mark.asyncio
    async def test_unknown_collection_still_raises(self, store):
        with pytest.raises(ValueError, match="Unknown collection"):
            await store.search(collection="not-a-collection", query_vector=[0.1])


def test_installed_qdrant_client_no_longer_has_search():
    """Pins the reason for the change. If a future pin restores `search`,
    this fails and the migration can be revisited deliberately rather than
    being assumed."""
    from qdrant_client import QdrantClient

    assert not hasattr(QdrantClient, "search")
    assert hasattr(QdrantClient, "query_points")
