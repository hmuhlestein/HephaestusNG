"""Vector store using turbovec for local vector search with SQLite metadata."""

import asyncio
import hashlib
import json
import logging
import atexit
import signal
import numpy as np
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional
from pathlib import Path

from turbovec import IdMapIndex

logger = logging.getLogger(__name__)


class TurboVecStore:
    """Local vector store using turbovec index + SQLite metadata.

    Replaces Qdrant with a zero-dependency, in-process solution.
    Vectors are compressed 8-16x via TurboQuant quantization.
    """

    COLLECTIONS = {
        "agent_memories": {"dim": 384, "description": "Real-time agent discoveries and learnings"},
        "static_docs": {"dim": 384, "description": "Documentation files and static knowledge"},
        "task_completions": {"dim": 384, "description": "Historical task data and outcomes"},
        "error_solutions": {"dim": 384, "description": "Known error patterns and fixes"},
        "domain_knowledge": {"dim": 384, "description": "CVEs, CWEs, standards, and domain knowledge"},
        "project_context": {"dim": 384, "description": "Current project state and goals"},
        "ticket_embeddings": {"dim": 384, "description": "Ticket tracking system embeddings for semantic search"},
    }

    def __init__(
        self,
        data_dir: str = "data/turbovec",
        collection_prefix: str = "hephaestus",
        bit_width: int = 4,
    ):
        self.data_dir = Path(data_dir)
        self.collection_prefix = collection_prefix
        self.bit_width = bit_width
        self._indices: Dict[str, IdMapIndex] = {}
        self._metadata: Dict[str, Dict[int, Dict[str, Any]]] = {}
        self._str_to_int: Dict[str, int] = {}
        self._write_counts: Dict[str, int] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._flush_interval = 30

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._load_all_collections()
        self._register_shutdown_hooks()
        logger.info(f"Initialized TurboVecStore at {self.data_dir}")

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _register_shutdown_hooks(self):
        atexit.register(self.flush_all)

        def _signal_handler(signum, frame):
            self.flush_all()

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _signal_handler)
            except (OSError, ValueError):
                pass

    def _get_index_path(self, collection: str) -> Path:
        return self.data_dir / f"{self.collection_prefix}_{collection}.tvim"

    def _get_metadata_path(self, collection: str) -> Path:
        return self.data_dir / f"{self.collection_prefix}_{collection}_meta.json"

    def _load_all_collections(self):
        for collection_name, config in self.COLLECTIONS.items():
            index_path = self._get_index_path(collection_name)
            meta_path = self._get_metadata_path(collection_name)

            if index_path.exists():
                try:
                    self._indices[collection_name] = IdMapIndex.load(str(index_path))
                    logger.debug(f"Loaded index for {collection_name}: {len(self._indices[collection_name])} vectors")
                except Exception as e:
                    logger.warning(f"Failed to load index for {collection_name}: {e}")
                    self._indices[collection_name] = IdMapIndex(dim=config["dim"], bit_width=self.bit_width)
            else:
                self._indices[collection_name] = IdMapIndex(dim=config["dim"], bit_width=self.bit_width)

            if meta_path.exists():
                try:
                    with open(meta_path, "r") as f:
                        raw = json.load(f)
                    self._metadata[collection_name] = {int(k): v for k, v in raw.items()}
                except Exception as e:
                    logger.warning(f"Failed to load metadata for {collection_name}: {e}")
                    self._metadata[collection_name] = {}
            else:
                self._metadata[collection_name] = {}

            # Rebuild str→int collision map from loaded metadata
            for int_id, meta in self._metadata[collection_name].items():
                original_id = meta.get("memory_id", "")
                if original_id:
                    self._str_to_int[original_id] = int_id

    def _save_index(self, collection: str):
        index_path = self._get_index_path(collection)
        self._indices[collection].write(str(index_path))

    def _save_metadata(self, collection: str):
        meta_path = self._get_metadata_path(collection)
        raw = {str(k): v for k, v in self._metadata[collection].items()}
        with open(meta_path, "w") as f:
            json.dump(raw, f, indent=2, default=str)

    def _id_to_int(self, point_id: str) -> int:
        h = hashlib.sha256(point_id.encode()).hexdigest()[:16]
        return int(h, 16) % (2**63)

    async def store_memory(
        self,
        collection: str,
        memory_id: str,
        embedding: List[float],
        content: str,
        metadata: Dict[str, Any],
    ) -> bool:
        if collection not in self.COLLECTIONS:
            raise ValueError(f"Unknown collection: {collection}")

        expected_dim = self.COLLECTIONS[collection]["dim"]
        if len(embedding) != expected_dim:
            raise ValueError(
                f"Embedding dimension mismatch for '{collection}': "
                f"expected {expected_dim}, got {len(embedding)}"
            )

        try:
            async with self._get_lock():
                int_id = self._id_to_int(memory_id)

                # Collision detection
                if memory_id in self._str_to_int:
                    existing_int = self._str_to_int[memory_id]
                    if existing_int != int_id:
                        logger.warning(
                            f"Hash collision for id '{memory_id}': "
                            f"existing={existing_int}, new={int_id}"
                        )
                    int_id = existing_int
                else:
                    # Check if new int_id collides with different memory_id
                    if int_id in self._metadata.get(collection, {}):
                        existing_meta = self._metadata[collection].get(int_id, {})
                        if existing_meta.get("memory_id") != memory_id:
                            logger.error(
                                f"ID collision: '{memory_id}' maps to {int_id} "
                                f"but '{existing_meta.get('memory_id')}' already uses it"
                            )
                            return False
                    self._str_to_int[memory_id] = int_id

                vec = np.array([embedding], dtype=np.float32)
                index = self._indices[collection]

                if int_id in index:
                    index.remove(int_id)

                index.add_with_ids(vec, np.array([int_id], dtype=np.uint64))

                self._metadata[collection][int_id] = {
                    "content": content,
                    "memory_id": memory_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    **metadata,
                }

                # Flush on every write to prevent data loss
                self._save_index(collection)
                self._save_metadata(collection)

                logger.debug(f"Stored memory {memory_id} in collection {collection}")
                return True

        except Exception as e:
            logger.error(f"Failed to store memory {memory_id}: {e}")
            return False

    async def search(
        self,
        collection: str,
        query_vector: List[float],
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        score_threshold: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        if collection not in self.COLLECTIONS:
            raise ValueError(f"Unknown collection: {collection}")

        try:
            async with self._get_lock():
                index = self._indices[collection]
                if len(index) == 0:
                    return []

                # Over-fetch to account for post-filter rejection
                fetch_k = min(limit * 3, len(index))
                query = np.array([query_vector], dtype=np.float32)
                scores, ids = index.search(query, k=fetch_k)

                results = []
                for score, int_id in zip(scores[0], ids[0]):
                    int_id = int(int_id)
                    meta = self._metadata[collection].get(int_id)
                    if meta is None:
                        continue

                    if filters:
                        match = True
                        for key, value in filters.items():
                            if meta.get(key) != value:
                                match = False
                                break
                        if not match:
                            continue

                    if score_threshold and float(score) < score_threshold:
                        continue

                    results.append({
                        "id": meta.get("memory_id", str(int_id)),
                        "score": float(score),
                        "content": meta.get("content", ""),
                        "metadata": {k: v for k, v in meta.items() if k != "content"},
                    })

                    if len(results) >= limit:
                        break

                return results

        except Exception as e:
            logger.error(f"Search failed in collection {collection}: {e}")
            return []

    async def search_all_collections(
        self,
        query_vector: List[float],
        limit_per_collection: int = 5,
        total_limit: int = 20,
    ) -> List[Dict[str, Any]]:
        all_results = []

        for collection_name in self.COLLECTIONS.keys():
            results = await self.search(
                collection=collection_name,
                query_vector=query_vector,
                limit=limit_per_collection,
            )
            for result in results:
                result["collection"] = collection_name
                all_results.append(result)

        all_results.sort(key=lambda x: x["score"], reverse=True)
        return all_results[:total_limit]

    def delete_memory(self, collection: str, memory_id: str) -> bool:
        if collection not in self.COLLECTIONS:
            raise ValueError(f"Unknown collection: {collection}")

        try:
            int_id = self._str_to_int.get(memory_id)
            if int_id is None:
                int_id = self._id_to_int(memory_id)

            index = self._indices[collection]

            if int_id in index:
                index.remove(int_id)
                self._metadata[collection].pop(int_id, None)
                self._str_to_int.pop(memory_id, None)
                self._save_index(collection)
                self._save_metadata(collection)
                logger.debug(f"Deleted memory {memory_id} from collection {collection}")
                return True
            return False

        except Exception as e:
            logger.error(f"Failed to delete memory {memory_id}: {e}")
            return False

    def get_collection_stats(self, collection: str) -> Dict[str, Any]:
        if collection not in self.COLLECTIONS:
            raise ValueError(f"Unknown collection: {collection}")

        index = self._indices[collection]
        return {
            "name": collection,
            "vectors_count": len(index),
            "indexed_vectors_count": len(index),
            "status": "green",
            "backend": "turbovec",
            "bit_width": self.bit_width,
        }

    def get_all_stats(self) -> Dict[str, Any]:
        stats = {}
        for collection_name in self.COLLECTIONS.keys():
            stats[collection_name] = self.get_collection_stats(collection_name)
        return stats

    def flush_all(self):
        for collection_name in self.COLLECTIONS.keys():
            self._save_index(collection_name)
            self._save_metadata(collection_name)
        logger.info("Flushed all collections to disk")
