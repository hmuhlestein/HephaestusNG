"""Regression coverage for validate_embedding_dimension_compatibility
(Phase 3 Tier 2 item 12, docs/AUTOPILOT_REFACTOR_PLAN.md).

No startup-time check existed comparing the configured embedding
provider's output dimension against the vector store's collection
dimension -- a mismatch (e.g. EMBEDDING_BACKEND=openai's 3072-dim output
against VECTOR_STORE_BACKEND=turbovec's 384-dim collections) was only
ever caught by store_memory's own per-call ValueError guard, which every
current caller (rag.py, memory_api.py) wraps in a bare except that logs
and continues -- so a systemic misconfiguration failed every single
memory save silently, from the very first call.
"""

import pytest

from src.memory.store_factory import validate_embedding_dimension_compatibility


class _FakeTurboVecStore:
    """Shape-matches TurboVecStore: COLLECTIONS values keyed 'dim'."""

    COLLECTIONS = {
        "agent_memories": {"dim": 384, "description": "x"},
        "static_docs": {"dim": 384, "description": "x"},
    }


class _FakeQdrantStore:
    """Shape-matches VectorStoreManager: COLLECTIONS values keyed 'size'."""

    COLLECTIONS = {
        "agent_memories": {"size": 3072, "description": "x"},
    }


class _FakeStoreWithNoCollections:
    pass


def test_matching_dimensions_does_not_raise():
    validate_embedding_dimension_compatibility(_FakeTurboVecStore(), 384)


def test_mismatched_dimensions_raises_with_both_values_named():
    with pytest.raises(ValueError, match="384") as exc_info:
        validate_embedding_dimension_compatibility(_FakeTurboVecStore(), 3072)
    assert "3072" in str(exc_info.value)


def test_qdrant_style_size_key_is_also_checked():
    with pytest.raises(ValueError, match="3072"):
        validate_embedding_dimension_compatibility(_FakeQdrantStore(), 384)

    # Matching dims for the Qdrant-style store must not raise.
    validate_embedding_dimension_compatibility(_FakeQdrantStore(), 3072)


def test_store_with_no_collections_attribute_is_a_noop():
    """Defensive: an unrecognized store shape shouldn't crash startup --
    it just means this check can't say anything, not that something's
    wrong."""
    validate_embedding_dimension_compatibility(_FakeStoreWithNoCollections(), 384)
