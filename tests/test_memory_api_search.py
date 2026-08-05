"""Regression coverage for src.mcp.memory_api.search_memory.

Both vector store backends (turbovec_store.py, vector_store.py's Qdrant
wrapper) already normalize their results to {"id", "score", "content",
"metadata": {...}} -- neither has a "payload" key. search_memory read
r.get("payload", {}).get("content", "") / r.get("payload", {}).get(
"memory_type", ""), which always returned an empty dict, silently
dropping content/memory_type from every single result regardless of
backend. Confirmed live: hephaestus_search_memory reported "Found 10
memories" but rendered every one as "- []" (empty memory_type, empty
content).
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.mcp.memory_api import SearchMemoryRequest, search_memory


def _make_server_state(search_results):
    state = AsyncMock()
    state.llm_provider.generate_embedding = AsyncMock(return_value=[0.1, 0.2])
    state.vector_store.search = AsyncMock(return_value=search_results)
    return state


@pytest.mark.asyncio
async def test_search_memory_extracts_content_and_type_from_real_backend_shape():
    backend_results = [
        {
            "id": "mem-1",
            "score": 0.92,
            "content": "Used PyJWT because design.md's sample code uses jwt.InvalidTokenError.",
            "metadata": {"memory_type": "decision", "project_id": "proj-1"},
        }
    ]
    state = _make_server_state(backend_results)

    with patch("src.mcp.memory_api._get_server_state", return_value=state):
        response = await search_memory(
            SearchMemoryRequest(query="JWT library choice"), agent_id=None
        )

    assert response.total == 1
    result = response.results[0]
    assert result["content"] == (
        "Used PyJWT because design.md's sample code uses jwt.InvalidTokenError."
    )
    assert result["memory_type"] == "decision"
    assert result["id"] == "mem-1"


@pytest.mark.asyncio
async def test_search_memory_metadata_excludes_memory_type_duplication():
    """The formatted "metadata" field already surfaces memory_type as its
    own top-level key -- it shouldn't also duplicate project_id-adjacent
    bookkeeping fields that were only ever meant to be filtered on."""
    backend_results = [
        {
            "id": "mem-1",
            "score": 0.5,
            "content": "c",
            "metadata": {"memory_type": "learning", "project_id": "proj-1", "tags": ["auth"]},
        }
    ]
    state = _make_server_state(backend_results)

    with patch("src.mcp.memory_api._get_server_state", return_value=state):
        response = await search_memory(
            SearchMemoryRequest(query="q"), agent_id=None
        )

    metadata = response.results[0]["metadata"]
    assert metadata["project_id"] == "proj-1"
    assert metadata["tags"] == ["auth"]
