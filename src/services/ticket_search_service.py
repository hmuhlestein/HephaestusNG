"""Service for searching tickets using hybrid (semantic + keyword) approach."""

import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.sql import text

from src.core.database import Ticket, TicketComment, get_db
from src.memory.embedding_factory import create_embedding_provider
from src.memory.store_factory import create_vector_store

logger = logging.getLogger(__name__)

# Configurable vector store collection for tickets (turbovec key, 384-dim).
TICKET_COLLECTION = "ticket_embeddings"


class TicketSearchService:
    """Service for comprehensive ticket search (semantic + keyword)."""

    # Configurable backends (python-only by default: turbovec + fastembed), shared
    # with the rest of the system instead of the old hardcoded Qdrant + OpenAI stack.
    _vector_store = None
    _embedding_provider = None

    @classmethod
    def _get_vector_store(cls):
        """Get or create the configurable vector store (turbovec by default)."""
        if cls._vector_store is None:
            cls._vector_store = create_vector_store()
        return cls._vector_store

    @classmethod
    def _get_embedding_provider(cls):
        """Get or create the configurable embedding provider (fastembed by default)."""
        if cls._embedding_provider is None:
            cls._embedding_provider = create_embedding_provider()
        return cls._embedding_provider

    @staticmethod
    def _ticket_text(title: str, description: str, tags: List[str]) -> str:
        """Compose the text embedded for a ticket."""
        tag_str = " ".join(tags) if tags else ""
        return f"{title}\n\n{description}\n\n{tag_str}".strip()

    @staticmethod
    async def semantic_search(
        query_text: str,
        workflow_id: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Semantic search using the configurable vector store (turbovec).

        Gracefully degrades to keyword search if the vector store is unavailable.

        Args:
            query_text: Natural language search query
            workflow_id: Workflow to search within
            limit: Max number of results
            filters: Optional filters (status, priority, type, etc.)

        Returns:
            List of ticket search results with relevance scores
        """
        try:
            # Generate query embedding via the configurable provider (fastembed)
            provider = TicketSearchService._get_embedding_provider()
            query_embedding = await provider.generate_embedding(query_text)

            # Vector search via the configurable store (turbovec). Scope to the workflow
            # in the store; apply the richer (list/equality) filters in Python below.
            store = TicketSearchService._get_vector_store()
            hits = await store.search(
                collection=TICKET_COLLECTION,
                query_vector=query_embedding,
                limit=limit * 3,  # over-fetch to survive post-filtering
                filters={"workflow_id": workflow_id},
                score_threshold=0.3,  # better recall on cosine similarity
            )

            def _passes(meta: Dict[str, Any]) -> bool:
                if not filters:
                    return True
                for key in ("status", "priority", "ticket_type"):
                    if key in filters:
                        want, val = filters[key], meta.get(key)
                        if isinstance(want, list):
                            if val not in want:
                                return False
                        elif val != want:
                            return False
                if (
                    "assigned_agent_id" in filters
                    and meta.get("assigned_agent_id") != filters["assigned_agent_id"]
                ):
                    return False
                if (
                    "is_blocked" in filters
                    and meta.get("is_blocked") != filters["is_blocked"]
                ):
                    return False
                return True

            results = []
            for hit in hits:
                meta = hit.get("metadata", {})
                if meta.get("workflow_id") != workflow_id or not _passes(meta):
                    continue
                desc = meta.get("description", "") or ""
                results.append(
                    {
                        "ticket_id": meta.get("ticket_id", hit.get("id")),
                        "title": meta.get("title", ""),
                        "description": desc,
                        "status": meta.get("status"),
                        "priority": meta.get("priority"),
                        "ticket_type": meta.get("ticket_type"),
                        "relevance_score": hit.get("score", 0.0),
                        "matched_in": ["semantic"],
                        "preview": (desc[:200] + "...") if len(desc) > 200 else desc,
                        "created_at": meta.get("created_at"),
                        "assigned_agent_id": meta.get("assigned_agent_id"),
                        "tags": meta.get("tags", []),
                    }
                )
                if len(results) >= limit:
                    break

            logger.info(f"Semantic search returned {len(results)} results")
            return results

        except Exception as e:
            logger.warning(
                f"Semantic search failed, falling back to keyword-only search: {e}"
            )
            # Gracefully degrade to keyword search
            try:
                return await TicketSearchService.keyword_search(
                    keywords=query_text,
                    workflow_id=workflow_id,
                    limit=limit,
                    filters=filters,
                )
            except Exception as fallback_error:
                logger.error(f"Keyword fallback search also failed: {fallback_error}")
                return []

    @staticmethod
    async def keyword_search(
        keywords: str,
        workflow_id: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Keyword-based search using SQLite FTS5.

        Args:
            keywords: Search keywords
            workflow_id: Workflow to search within
            limit: Max number of results
            filters: Optional filters

        Returns:
            List of ticket search results with rank scores
        """
        try:
            with get_db() as db:
                # Build FTS5 query
                # Use FTS5 MATCH syntax
                fts_query = keywords

                # Query FTS5 with JOIN to tickets table
                sql = text(
                    """
                    SELECT
                        t.id as ticket_id,
                        t.title,
                        t.description,
                        t.status,
                        t.priority,
                        t.ticket_type,
                        t.created_at,
                        t.assigned_agent_id,
                        t.tags,
                        fts.rank as relevance_score
                    FROM ticket_fts fts
                    JOIN tickets t ON fts.ticket_id = t.id
                    WHERE fts.ticket_fts MATCH :query
                      AND t.workflow_id = :workflow_id
                    ORDER BY fts.rank
                    LIMIT :limit
                """
                )

                result = db.execute(
                    sql,
                    {"query": fts_query, "workflow_id": workflow_id, "limit": limit},
                )

                rows = result.fetchall()

                # Apply additional filters if provided
                results = []
                for row in rows:
                    # Check filters
                    if filters:
                        if "status" in filters:
                            if isinstance(filters["status"], list):
                                if row.status not in filters["status"]:
                                    continue
                            elif row.status != filters["status"]:
                                continue

                        if "priority" in filters:
                            if isinstance(filters["priority"], list):
                                if row.priority not in filters["priority"]:
                                    continue
                            elif row.priority != filters["priority"]:
                                continue

                        if "ticket_type" in filters:
                            if isinstance(filters["ticket_type"], list):
                                if row.ticket_type not in filters["ticket_type"]:
                                    continue
                            elif row.ticket_type != filters["ticket_type"]:
                                continue

                    results.append(
                        {
                            "ticket_id": row.ticket_id,
                            "title": row.title,
                            "description": row.description,
                            "status": row.status,
                            "priority": row.priority,
                            "ticket_type": row.ticket_type,
                            "relevance_score": abs(float(row.relevance_score))
                            if row.relevance_score
                            else 0.0,  # FTS5 rank is negative
                            "matched_in": ["keyword"],
                            "preview": row.description[:200] + "..."
                            if len(row.description) > 200
                            else row.description,
                            "created_at": row.created_at.isoformat() + "Z"
                            if hasattr(row.created_at, "isoformat")
                            else str(row.created_at),
                            "assigned_agent_id": row.assigned_agent_id,
                            "tags": json.loads(row.tags) if row.tags else [],
                        }
                    )

                logger.info(f"Keyword search returned {len(results)} results")
                return results

        except Exception as e:
            logger.error(f"Keyword search failed: {e}")
            return []

    @staticmethod
    async def hybrid_search(
        query: str,
        workflow_id: str,
        limit: int = 10,
        filters: Optional[Dict[str, Any]] = None,
        include_comments: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Hybrid search combining semantic (70%) + keyword (30%) with RRF.

        This is the DEFAULT search mode.

        Args:
            query: Search query (natural language)
            workflow_id: Workflow to search within
            limit: Max number of results
            filters: Optional filters
            include_comments: Whether to search comments too

        Returns:
            List of ticket search results sorted by combined relevance
        """
        start_time = time.time()

        # Execute both searches
        semantic_results = await TicketSearchService.semantic_search(
            query_text=query,
            workflow_id=workflow_id,
            limit=limit * 2,  # Get more to merge
            filters=filters,
        )

        keyword_results = await TicketSearchService.keyword_search(
            keywords=query, workflow_id=workflow_id, limit=limit * 2, filters=filters
        )

        # Merge using Reciprocal Rank Fusion (RRF)
        # combined_score = (semantic_score * 0.7) + (keyword_score * 0.3)
        ticket_scores = {}

        # Add semantic scores (70% weight)
        for idx, result in enumerate(semantic_results):
            ticket_id = result["ticket_id"]
            # RRF: score = 1 / (k + rank), k=60 is standard
            rrf_score = 1.0 / (60 + idx + 1)
            ticket_scores[ticket_id] = {
                "semantic_score": rrf_score * 0.7,
                "keyword_score": 0.0,
                "data": result,
            }

        # Add keyword scores (30% weight)
        for idx, result in enumerate(keyword_results):
            ticket_id = result["ticket_id"]
            rrf_score = 1.0 / (60 + idx + 1)

            if ticket_id in ticket_scores:
                ticket_scores[ticket_id]["keyword_score"] = rrf_score * 0.3
                # Merge matched_in
                ticket_scores[ticket_id]["data"]["matched_in"] = ["semantic", "keyword"]
            else:
                ticket_scores[ticket_id] = {
                    "semantic_score": 0.0,
                    "keyword_score": rrf_score * 0.3,
                    "data": result,
                }

        # Calculate combined scores and sort
        merged_results = []
        for ticket_id, scores in ticket_scores.items():
            combined_score = scores["semantic_score"] + scores["keyword_score"]
            result_data = scores["data"]
            result_data["relevance_score"] = combined_score
            merged_results.append(result_data)

        # Sort by combined score (descending)
        merged_results.sort(key=lambda x: x["relevance_score"], reverse=True)

        # Return top limit results
        final_results = merged_results[:limit]

        search_time_ms = int((time.time() - start_time) * 1000)
        logger.info(
            f"Hybrid search for '{query[:50]}...' in workflow {workflow_id}: "
            f"{len(final_results)} results in {search_time_ms}ms "
            f"(from {len(semantic_results)} semantic + {len(keyword_results)} keyword)"
        )
        return final_results

    @staticmethod
    async def find_related_tickets(
        ticket_id: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """
        Find semantically similar tickets for duplicate detection and context.

        Args:
            ticket_id: Ticket to find related tickets for
            limit: Max number of related tickets

        Returns:
            List of related tickets with similarity scores
        """
        try:
            # Get ticket embedding from database
            with get_db() as db:
                ticket = db.query(Ticket).filter_by(id=ticket_id).first()
                if not ticket:
                    logger.warning(f"Ticket not found: {ticket_id}")
                    return []

                if not ticket.embedding:
                    logger.warning(f"Ticket {ticket_id} has no embedding")
                    return []

                query_embedding = ticket.embedding
                ticket_workflow_id = ticket.workflow_id

            # Search the configurable store for similar tickets (exclude this ticket)
            store = TicketSearchService._get_vector_store()
            hits = await store.search(
                collection=TICKET_COLLECTION,
                query_vector=query_embedding,
                limit=limit + 1,  # +1 because we'll filter out the query ticket
                filters={"workflow_id": ticket_workflow_id},
            )

            # Format results and classify relation type
            results = []
            for hit in hits:
                meta = hit.get("metadata", {})
                hid = meta.get("ticket_id", hit.get("id"))
                # Skip the query ticket itself
                if hid == ticket_id:
                    continue

                score = hit.get("score", 0.0)
                # Classify relation type based on similarity score
                if score >= 0.9:
                    relation_type = "duplicate"
                elif score >= 0.7:
                    relation_type = "related"
                elif score >= 0.5:
                    relation_type = "similar"
                else:
                    continue  # Skip low similarity

                results.append(
                    {
                        "ticket_id": hid,
                        "title": meta.get("title", ""),
                        "similarity_score": score,
                        "relation_type": relation_type,
                        "status": meta.get("status"),
                        "priority": meta.get("priority"),
                    }
                )

            logger.info(f"Found {len(results)} related tickets for {ticket_id}")
            return results[:limit]

        except Exception as e:
            logger.error(f"Find related tickets failed: {e}")
            return []

    @staticmethod
    async def index_ticket(
        ticket_id: str,
        title: str,
        description: str,
        comments: List[str],
        workflow_id: str,
        ticket_type: str,
        priority: str,
        status: str,
        tags: List[str],
        created_at: str,
        updated_at: str,
        created_by_agent_id: str,
        assigned_agent_id: Optional[str],
        is_blocked: bool,
    ) -> str:
        """
        Index ticket in the configurable vector store (turbovec).

        Args:
            All ticket metadata for payload

        Returns:
            Embedding ID (the ticket_id key in the vector store)
        """
        try:
            # Generate embedding via the configurable provider (fastembed)
            provider = TicketSearchService._get_embedding_provider()
            embedding = await provider.generate_embedding(
                TicketSearchService._ticket_text(title, description, tags)
            )

            # Prepare payload (stored as vector metadata)
            payload = {
                "ticket_id": ticket_id,
                "workflow_id": workflow_id,
                "title": title,
                "description": description,
                "ticket_type": ticket_type,
                "priority": priority,
                "status": status,
                "tags": tags,
                "created_at": created_at,
                "updated_at": updated_at,
                "created_by_agent_id": created_by_agent_id,
                "assigned_agent_id": assigned_agent_id,
                "comment_texts": comments,
                "is_blocked": is_blocked,
            }

            # Store in the configurable vector store (turbovec), keyed by ticket_id
            store = TicketSearchService._get_vector_store()
            await store.store_memory(
                collection=TICKET_COLLECTION,
                memory_id=ticket_id,
                embedding=embedding,
                content=description,
                metadata=payload,
            )

            logger.info(f"Indexed ticket {ticket_id} in {TICKET_COLLECTION}")
            return ticket_id

        except Exception as e:
            logger.error(f"Failed to index ticket {ticket_id}: {e}")
            raise

    @staticmethod
    async def reindex_ticket(ticket_id: str) -> str:
        """
        Regenerate and update embedding for existing ticket.

        Called when title/description changes or every 5 comments.

        Args:
            ticket_id: Ticket to reindex

        Returns:
            New embedding ID
        """
        try:
            # Fetch ticket from database and extract all needed data while in session
            with get_db() as db:
                ticket = db.query(Ticket).filter_by(id=ticket_id).first()
                if not ticket:
                    raise ValueError(f"Ticket not found: {ticket_id}")

                # Get comments
                comments = (
                    db.query(TicketComment)
                    .filter_by(ticket_id=ticket_id)
                    .order_by(TicketComment.created_at.desc())
                    .limit(5)
                    .all()
                )

                comment_texts = [c.comment_text for c in comments]

                # Extract all ticket data while still in session
                title = ticket.title
                description = ticket.description
                tags = ticket.tags or []
                workflow_id = ticket.workflow_id
                ticket_type = ticket.ticket_type
                priority = ticket.priority
                status = ticket.status
                created_at = ticket.created_at.isoformat() + "Z"
                updated_at = ticket.updated_at.isoformat() + "Z"
                created_by_agent_id = ticket.created_by_agent_id
                assigned_agent_id = ticket.assigned_agent_id
                is_blocked = bool(
                    ticket.blocked_by_ticket_ids
                    and len(ticket.blocked_by_ticket_ids) > 0
                )

            # Generate new embedding via the configurable provider (fastembed)
            provider = TicketSearchService._get_embedding_provider()
            embedding = await provider.generate_embedding(
                TicketSearchService._ticket_text(title, description, tags)
            )

            payload = {
                "ticket_id": ticket_id,
                "workflow_id": workflow_id,
                "title": title,
                "description": description,
                "ticket_type": ticket_type,
                "priority": priority,
                "status": status,
                "tags": tags,
                "created_at": created_at,
                "updated_at": updated_at,
                "created_by_agent_id": created_by_agent_id,
                "assigned_agent_id": assigned_agent_id,
                "comment_texts": comment_texts,
                "is_blocked": is_blocked,
            }

            # Update in the configurable vector store (turbovec)
            store = TicketSearchService._get_vector_store()
            await store.store_memory(
                collection=TICKET_COLLECTION,
                memory_id=ticket_id,
                embedding=embedding,
                content=description,
                metadata=payload,
            )

            # Update ticket record in database
            with get_db() as db:
                ticket = db.query(Ticket).filter_by(id=ticket_id).first()
                ticket.embedding = embedding
                ticket.embedding_id = ticket_id
                ticket.updated_at = datetime.utcnow()
                db.commit()

            logger.info(f"Reindexed ticket {ticket_id} in {TICKET_COLLECTION}")
            return ticket_id

        except Exception as e:
            logger.error(f"Failed to reindex ticket {ticket_id}: {e}")
            raise
