"""Unified vector store interface with pluggable backends.

Supports:
- Qdrant (network-based, requires Docker container)
- turbovec (local, in-process, zero dependencies)

Configure via environment variable:
    VECTOR_STORE_BACKEND=turbovec  # or qdrant (default: turbovec)
    TURBOVEC_DATA_DIR=data/turbovec
    QDRANT_URL=http://localhost:6333
"""

import os
import logging
from typing import List, Dict, Any, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class VectorStoreProtocol(Protocol):
    """Protocol that all vector store backends must implement."""

    async def store_memory(
        self,
        collection: str,
        memory_id: str,
        embedding: List[float],
        content: str,
        metadata: Dict[str, Any],
    ) -> bool: ...

    async def search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]: ...

    async def search_all_collections(
        self,
        query_vector: List[float],
        limit_per_collection: int = 5,
        total_limit: int = 20,
    ) -> List[Dict[str, Any]]: ...

    def delete_memory(self, collection: str, memory_id: str) -> bool: ...

    def get_collection_stats(self, collection: str) -> Dict[str, Any]: ...

    def get_all_stats(self) -> Dict[str, Any]: ...


def create_vector_store() -> VectorStoreProtocol:
    """Create and return the configured vector store backend.

    Reads VECTOR_STORE_BACKEND env var to determine which backend to use.
    Falls back to Qdrant if turbovec is not available.

    Returns:
        Vector store instance implementing VectorStoreProtocol
    """
    backend = os.getenv("VECTOR_STORE_BACKEND", "turbovec").lower()

    if backend == "turbovec":
        try:
            from src.memory.turbovec_store import TurboVecStore

            data_dir = os.getenv("TURBOVEC_DATA_DIR", "data/turbovec")
            prefix = os.getenv("QDRANT_COLLECTION_PREFIX", "hephaestus")

            store = TurboVecStore(
                data_dir=data_dir,
                collection_prefix=prefix,
            )
            logger.info(f"Using turbovec backend (data: {data_dir})")
            return store

        except ImportError as e:
            logger.warning(f"turbovec not available: {e}. Falling back to Qdrant.")
            backend = "qdrant"

    if backend == "qdrant":
        from src.memory.vector_store import VectorStoreManager

        qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
        prefix = os.getenv("QDRANT_COLLECTION_PREFIX", "hephaestus")

        store = VectorStoreManager(
            qdrant_url=qdrant_url,
            collection_prefix=prefix,
        )
        logger.info(f"Using Qdrant backend (url: {qdrant_url})")
        return store

    raise ValueError(f"Unknown VECTOR_STORE_BACKEND: {backend}. Use 'qdrant' or 'turbovec'.")
