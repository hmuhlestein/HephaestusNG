#!/usr/bin/env python3
"""Initialize vector database collections for Hephaestus.

Supports both Qdrant (Docker) and turbovec (local) backends.
Configure via VECTOR_STORE_BACKEND env var.
"""

import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def main():
    """Initialize vector store with all required collections."""
    backend = os.getenv("VECTOR_STORE_BACKEND", "turbovec").lower()

    if backend == "turbovec":
        print("Initializing turbovec (local) vector store...")
        print("No container required - data stored in data/turbovec/")
        print()

        try:
            from src.memory.turbovec_store import TurboVecStore

            data_dir = os.getenv("TURBOVEC_DATA_DIR", "data/turbovec")
            vector_store = TurboVecStore(
                data_dir=data_dir,
                collection_prefix="hephaestus",
            )

            print("turbovec collections initialized successfully!")
            print()

            # Get and display statistics
            stats = vector_store.get_all_stats()
            print("Collection Statistics:")
            for collection_name, collection_stats in stats.items():
                print(f"  - {collection_name}:")
                print(f"      Vectors: {collection_stats.get('vectors_count', 0)}")
                print(f"      Backend: {collection_stats.get('backend', 'unknown')}")

        except Exception as e:
            print(f"Error initializing turbovec: {e}")
            sys.exit(1)

    else:
        print("Initializing Qdrant vector database...")
        print("Make sure Qdrant is running at http://localhost:6333")
        print()

        try:
            from src.memory.vector_store import VectorStoreManager

            vector_store = VectorStoreManager(
                qdrant_url="http://localhost:6333", collection_prefix="hephaestus"
            )

            print("Qdrant collections initialized successfully!")
            print()

            # Get and display statistics
            stats = vector_store.get_all_stats()
            print("Collection Statistics:")
            for collection_name, collection_stats in stats.items():
                print(f"  - {collection_name}:")
                print(f"      Vectors: {collection_stats.get('vectors_count', 0)}")
                print(f"      Status: {collection_stats.get('status', 'unknown')}")

        except Exception as e:
            print(f"Error initializing Qdrant: {e}")
            print("\nMake sure Qdrant is running. You can start it with:")
            print("  docker run -p 6333:6333 qdrant/qdrant")
            sys.exit(1)


if __name__ == "__main__":
    main()
