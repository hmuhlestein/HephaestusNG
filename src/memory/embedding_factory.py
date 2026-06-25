"""Unified embedding provider with pluggable backends.

Supports:
- OpenAI (network-based, requires API key)
- fastembed (local, in-process, zero network calls)

Configure via environment variable:
    EMBEDDING_BACKEND=fastembed  # or openai (default: fastembed)
    FASTEMBED_MODEL=BAAI/bge-small-en-v1.5
"""

import asyncio
import os
import logging
from typing import List, Optional
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class EmbeddingProvider(ABC):
    @abstractmethod
    async def generate_embedding(self, text: str) -> List[float]: ...

    @abstractmethod
    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]: ...

    @abstractmethod
    def get_dim(self) -> int: ...

    @staticmethod
    def calculate_cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """Cosine similarity between two embedding vectors (-1..1). Concrete + static on
        the base so every provider shares one implementation, callers never hardcode the
        math, and it works without instantiating (loading) an embedding model."""
        import numpy as np
        if not vec1 or not vec2 or len(vec1) != len(vec2):
            return 0.0
        a = np.array(vec1, dtype=np.float32)
        b = np.array(vec2, dtype=np.float32)
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na == 0 or nb == 0:
            return 0.0
        return float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model: str = "text-embedding-3-large", api_key: Optional[str] = None):
        import openai
        self.model = model
        self.client = openai.OpenAI(api_key=api_key or os.getenv("OPENAI_API_KEY"))
        self._dim = 3072

    async def generate_embedding(self, text: str) -> List[float]:
        return await asyncio.to_thread(
            self._client_embed, text[:8000]
        )

    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        return await asyncio.to_thread(
            self._client_embed_batch, [t[:8000] for t in texts]
        )

    def _client_embed(self, text: str) -> List[float]:
        response = self.client.embeddings.create(
            model=self.model, input=text, encoding_format="float"
        )
        return response.data[0].embedding

    def _client_embed_batch(self, texts: List[str]) -> List[List[float]]:
        response = self.client.embeddings.create(
            model=self.model, input=texts, encoding_format="float"
        )
        return [item.embedding for item in response.data]

    def get_dim(self) -> int:
        return self._dim


class FastEmbedEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        self._dim: Optional[int] = None

    def get_dim(self) -> int:
        if self._dim is None:
            test = list(self._model.embed(["test"]))[0]
            self._dim = len(test)
        return self._dim

    async def generate_embedding(self, text: str) -> List[float]:
        return await asyncio.to_thread(self._embed_single, text)

    async def generate_embeddings(self, texts: List[str]) -> List[List[float]]:
        return await asyncio.to_thread(self._embed_batch, texts)

    def _embed_single(self, text: str) -> List[float]:
        return list(self._model.embed([text]))[0].tolist()

    def _embed_batch(self, texts: List[str]) -> List[List[float]]:
        return [emb.tolist() for emb in self._model.embed(texts)]


def create_embedding_provider() -> EmbeddingProvider:
    backend = os.getenv("EMBEDDING_BACKEND", "fastembed").lower()

    if backend == "fastembed":
        try:
            model_name = os.getenv("FASTEMBED_MODEL", "BAAI/bge-small-en-v1.5")
            provider = FastEmbedEmbeddingProvider(model_name=model_name)
            logger.info(f"Using fastembed backend (model: {model_name}, dim: {provider.get_dim()})")
            return provider
        except ImportError as e:
            logger.warning(f"fastembed not available: {e}. Falling back to OpenAI.")
            backend = "openai"

    if backend == "openai":
        model = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")
        provider = OpenAIEmbeddingProvider(model=model)
        logger.info(f"Using OpenAI backend (model: {model})")
        return provider

    raise ValueError(f"Unknown EMBEDDING_BACKEND: {backend}. Use 'openai' or 'fastembed'.")
