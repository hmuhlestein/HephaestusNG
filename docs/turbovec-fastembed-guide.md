# TurboVec + FastEmbed: Local Vector Search & Embeddings

Zero-dependency, zero-API-key vector search and text embeddings that run entirely on your machine.

## Why Replace Qdrant + OpenAI Embeddings?

| Concern | Qdrant + OpenAI | TurboVec + FastEmbed |
|---------|----------------|---------------------|
| Infrastructure | Docker container required | In-process, no Docker |
| API keys | OpenAI key required | None — runs locally |
| Cost | Per-token embedding costs | Free after install |
| Latency | Network round-trip per embedding | CPU inference, sub-100ms |
| Memory | Uncompressed vectors | 8-16x compression via TurboQuant |
| Privacy | Data sent to OpenAI | Everything stays on-machine |
| Offline | Requires internet | Fully offline capable |

## What They Are

### TurboVec

A Rust-based vector similarity search library. It provides:

- **IdMapIndex**: In-process vector index with custom integer IDs
- **TurboQuant**: 2/3/4-bit quantization for 8-16x memory reduction
- **Persistence**: Save/load indices to disk (`.tvim` files)
- **No server**: Compiles as a Python extension, runs in your process

```python
from turbovec import IdMapIndex
import numpy as np

# Create a 384-dim index with 4-bit quantization
index = IdMapIndex(dim=384, bit_width=4)

# Add vectors with custom IDs
vectors = np.random.randn(100, 384).astype(np.float32)
ids = np.arange(100, dtype=np.uint64)
index.add_with_ids(vectors, ids)

# Search
query = np.random.randn(1, 384).astype(np.float32)
scores, result_ids = index.search(query, k=5)

# Persist to disk
index.write("my_index.tvim")
loaded = IdMapIndex.load("my_index.tvim")
```

### FastEmbed

A Python library for generating text embeddings using quantized ONNX models. It provides:

- **Local inference**: Runs ONNX Runtime on CPU — no GPU required
- **Small models**: `BAAI/bge-small-en-v1.5` is ~130MB, 384-dimensional
- **Batch support**: Embed single texts or batches efficiently
- **No network**: Model downloads once, then runs fully offline

```python
from fastembed import TextEmbedding

model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

# Single embedding
embedding = list(model.embed(["hello world"]))[0]
print(len(embedding))  # 384

# Batch embeddings
texts = ["first doc", "second doc", "third doc"]
embeddings = list(model.embed(texts))
print(len(embeddings))  # 3
```

## Installation

```bash
# TurboVec (requires Rust toolchain)
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
pip install turbovec

# FastEmbed (pure Python + ONNX Runtime)
pip install fastembed
```

First run of FastEmbed will download the model (~130MB). Subsequent runs use the cached version.

## Using Them Together

```python
import asyncio
import numpy as np
from turbovec import IdMapIndex
from fastembed import TextEmbedding

# Initialize
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
index = IdMapIndex(dim=384, bit_width=4)

# Store documents
documents = [
    "The payment module uses Stripe API v3 with webhooks.",
    "Authentication uses JWT tokens with 15-minute expiry.",
    "Database migrations run in CI before deployment.",
]

for i, doc in enumerate(documents):
    embedding = list(model.embed([doc]))[0]
    vec = np.array([embedding], dtype=np.float32)
    index.add_with_ids(vec, np.array([i], dtype=np.uint64))

# Search
query = "how does auth work?"
query_vec = np.array([list(model.embed([query]))[0]], dtype=np.float32)
scores, ids = index.search(query_vec, k=3)

for score, doc_id in zip(scores[0], ids[0]):
    print(f"  [{score:.3f}] {documents[int(doc_id)]}")
```

## Async Wrapper (for Web Services)

FastEmbed and TurboVec are synchronous. Wrap them with `asyncio.to_thread` to avoid blocking your event loop:

```python
import asyncio
from fastembed import TextEmbedding

class EmbeddingProvider:
    def __init__(self):
        self._model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

    async def embed(self, text: str) -> list[float]:
        return await asyncio.to_thread(self._embed_sync, text)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._embed_batch_sync, texts)

    def _embed_sync(self, text: str) -> list[float]:
        return list(self._model.embed([text]))[0].tolist()

    def _embed_batch_sync(self, texts: list[str]) -> list[list[float]]:
        return [e.tolist() for e in self._model.embed(texts)]
```

## Configuration via Environment Variables

```bash
# Vector store backend (turbovec or qdrant)
VECTOR_STORE_BACKEND=turbovec

# Embedding backend (fastembed or openai)
EMBEDDING_BACKEND=fastembed

# FastEmbed model (default: BAAI/bge-small-en-v1.5)
FASTEMBED_MODEL=BAAI/bge-small-en-v1.5

# TurboVec storage directory
TURBOVEC_DATA_DIR=data/turbovec

# Bit width for quantization (2, 3, or 4; default: 4)
TURBOVEC_BIT_WIDTH=4
```

## Performance Characteristics

### TurboVec

| Metric | Value |
|--------|-------|
| Write latency | <1ms per vector (in-memory) |
| Search latency | <10ms for 10K vectors |
| Memory per vector (4-bit) | ~48 bytes (vs 1536 bytes raw float32 @ 384-dim) |
| Compression ratio | 8-16x vs raw float32 |
| Max tested scale | Millions of vectors |

### FastEmbed (BAAI/bge-small-en-v1.5)

| Metric | Value |
|--------|-------|
| Embedding dimension | 384 |
| Model size | ~130MB |
| Single text latency | ~20-50ms on CPU |
| Batch throughput | ~100-200 texts/sec on CPU |
| MTEB benchmark | 62.17 (good for retrieval) |

### Compared to OpenAI text-embedding-3-large

| Metric | OpenAI | FastEmbed |
|--------|--------|-----------|
| Dimension | 3072 | 384 |
| Latency | 100-300ms (network) | 20-50ms (local) |
| Cost | ~$0.00013/1K tokens | Free |
| Quality (MTEB) | ~64.6 | ~62.17 |
| Privacy | Data leaves machine | Stays local |

## Migrating from OpenAI + Qdrant

1. **Install**: `pip install turbovec fastembed`
2. **Set env vars**: `VECTOR_STORE_BACKEND=turbovec EMBEDDING_BACKEND=fastembed`
3. **Update config**: Change embedding dimension from 3072 to 384
4. **Re-index**: Existing Qdrant collections use 3072-dim vectors and are incompatible with 384-dim. You need to re-embed and re-index all stored data.
5. **Update downstream code**: Any code that hard-codes `3072` as the embedding dimension must change to `384`.

```bash
# Example: full switch
export VECTOR_STORE_BACKEND=turbovec
export EMBEDDING_BACKEND=fastembed
export TURBOVEC_DATA_DIR=data/turbovec
export FASTEMBED_MODEL=BAAI/bge-small-en-v1.5

# Re-initialize collections
python scripts/init_qdrant.py  # Works for both backends
```

## Fallback Behavior

Both factories fall back gracefully:

- `create_embedding_provider()`: If fastembed is not installed, falls back to OpenAI (requires `OPENAI_API_KEY`)
- `create_vector_store()`: If turbovec is not installed, falls back to Qdrant (requires Docker)

```python
from src.memory.embedding_factory import create_embedding_provider
from src.memory.store_factory import create_vector_store

# These respect EMBEDDING_BACKEND and VECTOR_STORE_BACKEND env vars
provider = create_embedding_provider()  # fastembed or openai
store = create_vector_store()           # turbovec or qdrant
```

## Available FastEmbed Models

| Model | Dim | Size | Quality | Speed |
|-------|-----|------|---------|-------|
| `BAAI/bge-small-en-v1.5` | 384 | 130MB | Good | Fast |
| `BAAI/bge-base-en-v1.5` | 768 | 440MB | Better | Medium |
| `BAAI/bge-large-en-v1.5` | 1024 | 1.3GB | Best | Slow |
| `sentence-transformers/all-MiniLM-L6-v2` | 384 | 90MB | Good | Fastest |

Set via: `FASTEMBED_MODEL=BAAI/bge-base-en-v1.5`

## Troubleshooting

**turbovec install fails**: Ensure Rust toolchain is installed (`rustc --version` must work). On macOS: `xcode-select --install` may also be needed.

**fastembed download stalls**: Model downloads from HuggingFace. If behind a proxy, set `HF_HUB_DISABLE_TELEMETRY=1` and configure `HTTP_PROXY`/`HTTPS_PROXY`.

**Dimension mismatch errors**: All vectors in a collection must have the same dimension. If you switch models (e.g., from `bge-small` to `bge-base`), you must create new collections with the updated dimension.

**Memory usage**: FastEmbed loads the ONNX model into memory. `bge-small-en-v1.5` uses ~200MB RAM. For memory-constrained environments, use `all-MiniLM-L6-v2`.
