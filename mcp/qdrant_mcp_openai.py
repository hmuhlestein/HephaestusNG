#!/usr/bin/env python3
"""Custom Qdrant MCP server using OpenAI embeddings.

This is a custom MCP server that wraps Qdrant with OpenAI embeddings
to match Hephaestus's existing embedding model (text-embedding-3-large, 3072-dim).
"""

import os
import sys
from typing import Any, Dict, List

from fastmcp import FastMCP
from openai import AsyncOpenAI
from qdrant_client import QdrantClient

# Initialize FastMCP
mcp = FastMCP("Qdrant with OpenAI Embeddings")

# Configuration from environment
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
COLLECTION_NAME = os.getenv("COLLECTION_NAME", "hephaestus_agent_memories")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-large")

# Initialize clients
qdrant_client = QdrantClient(url=QDRANT_URL)

if OPENROUTER_API_KEY:
    from openai import AsyncOpenAI

    openai_client = AsyncOpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
elif OPENAI_API_KEY:
    from openai import AsyncOpenAI

    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
else:
    openai_client = None


async def generate_embedding(text: str) -> List[float]:
    """Generate embedding using OpenAI or OpenRouter."""
    if not openai_client:
        raise Exception(
            "No API key available. Set OPENROUTER_API_KEY or OPENAI_API_KEY."
        )
    try:
        response = await openai_client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:8000],  # Limit input length
        )
        return response.data[0].embedding
    except Exception as e:
        raise Exception(f"Failed to generate embedding: {e}")


@mcp.tool()
async def qdrant_find(query: str, limit: int = 5) -> str:
    """Search for relevant information in Qdrant using semantic search.

    Args:
        query: Natural language search query
        limit: Maximum number of results to return (default: 5)

    Returns:
        JSON string with search results containing relevant memories
    """
    try:
        # Generate embedding for query
        query_embedding = await generate_embedding(query)

        # Search Qdrant using query_points (new API)
        results = qdrant_client.query_points(
            collection_name=COLLECTION_NAME,
            query=query_embedding,
            limit=limit,
            with_payload=True,
        ).points

        # Format results
        formatted_results = []
        for i, result in enumerate(results, 1):
            formatted_results.append(
                {
                    "rank": i,
                    "score": round(result.score, 4),
                    "content": result.payload.get("content", ""),
                    "memory_type": result.payload.get("memory_type", "unknown"),
                    "agent_id": result.payload.get("agent_id", "unknown"),
                    "timestamp": result.payload.get("timestamp", ""),
                }
            )

        if not formatted_results:
            return "No relevant memories found for your query."

        # Format as readable text
        output = f"Found {len(formatted_results)} relevant memories:\n\n"
        for r in formatted_results:
            output += f"[{r['rank']}] Score: {r['score']} | Type: {r['memory_type']}\n"
            output += f"    {r['content']}\n"
            output += f"    (Agent: {r['agent_id'][:8]}... | {r['timestamp'][:10]})\n\n"

        return output

    except Exception as e:
        return f"Error searching Qdrant: {str(e)}"


@mcp.tool()
async def qdrant_store(content: str, metadata: Dict[str, Any] = None) -> str:
    """Store information in Qdrant.

    Note: Agents should use the Hephaestus save_memory tool instead.
    This is provided for completeness but is not the recommended method.

    Args:
        content: Content to store
        metadata: Optional metadata dict

    Returns:
        Success message
    """
    return "Please use the Hephaestus 'save_memory' tool instead of qdrant_store for consistency."


if __name__ == "__main__":
    # Validate configuration
    if not OPENAI_API_KEY and not OPENROUTER_API_KEY:
        print(
            "Error: Set OPENROUTER_API_KEY or OPENAI_API_KEY environment variable",
            file=sys.stderr,
        )
        sys.exit(1)

    provider = "OpenRouter" if OPENROUTER_API_KEY else "OpenAI"
    print(f"Starting Qdrant MCP with {provider} embeddings", file=sys.stderr)
    print(f"  Model: {EMBEDDING_MODEL}", file=sys.stderr)
    print(f"  Collection: {COLLECTION_NAME}", file=sys.stderr)
    print(f"  Qdrant: {QDRANT_URL}", file=sys.stderr)

    # Run MCP server
    mcp.run()
