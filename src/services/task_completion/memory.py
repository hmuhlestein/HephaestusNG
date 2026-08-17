"""Memory persistence for task-completion learnings.

Extracted from src.services.task_completion_service.TaskCompletionService
per design_docs/phase_1b_decomposition.md section 4.4.
"""

import logging
import uuid

logger = logging.getLogger(__name__)


async def record_learnings(
    session,
    agent_id: str,
    task_id: str,
    key_learnings: list,
    code_changes: list,
) -> None:
    """Embed and persist each reported learning as a Memory."""
    from src.core.app_context import get_app_state
    from src.core.database import Memory

    server_state = get_app_state()

    for learning in key_learnings:
        embedding = await server_state.llm_provider.generate_embedding(learning)

        memory_id = str(uuid.uuid4())
        await server_state.vector_store.store_memory(
            collection="agent_memories",
            memory_id=memory_id,
            embedding=embedding,
            content=learning,
            metadata={
                "agent_id": agent_id,
                "task_id": task_id,
                "memory_type": "learning",
                "code_changes": code_changes,
            },
        )

        memory = Memory(
            id=memory_id,
            agent_id=agent_id,
            content=learning,
            memory_type="learning",
            embedding_id=memory_id,
            related_task_id=task_id,
            related_files=code_changes,
        )
        session.add(memory)
