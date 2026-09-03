"""Tests for ticket search functionality (Wave 2 - Search & Intelligence)."""

import asyncio

import pytest


@pytest.fixture
def event_loop():
    """Create an event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
async def setup_test_data():
    """Set up test data for ticket search tests."""
    # This fixture would create test workflows, agents, board configs, and tickets
    # For now, this is a placeholder for the structure
    pass


class TestTicketSearchService:
    """Test ticket search functionality."""

    @pytest.mark.asyncio
    async def test_semantic_search(self):
        """Test semantic search using Qdrant."""
        # This test would require:
        # 1. A test workflow
        # 2. Test tickets indexed in Qdrant
        # 3. Qdrant running locally
        pytest.skip("Integration test - requires Qdrant and test data")

    @pytest.mark.asyncio
    async def test_keyword_search_handles_fts5_special_characters(self, db_manager):
        """Regression: keyword_search passed raw search text straight
        through as the FTS5 MATCH argument. FTS5's query grammar treats a
        colon as a column-filter ("title:foo"), and AND/OR/NOT/NEAR,
        hyphens, and unbalanced quotes all have operator meaning too --
        any of that in the search text either breaks the query outright or
        silently changes what it matches. Observed live: searching for a
        ticket titled "...capped-notice run counters..." raised "no such
        column: notice", not because any table has that column, but
        because FTS5 parsed part of the phrase as a column-filter
        expression."""
        import uuid

        from src.core.database import Ticket
        from src.services.ticket_search_service import TicketSearchService

        workflow_id = f"wf-{uuid.uuid4()}"
        with db_manager.session_scope() as session:
            session.add(
                Ticket(
                    id=f"ticket-{uuid.uuid4()}",
                    workflow_id=workflow_id,
                    created_by_agent_id="agent-x",
                    title="Orchestrator: capped-notice run counters appear scoped to parent design session",
                    description="Found during forensics review",
                    ticket_type="bug",
                    priority="medium",
                    status="backlog",
                )
            )

        results = await TicketSearchService.keyword_search(
            keywords="capped-notice run counters scoped to parent design session",
            workflow_id=workflow_id,
        )

        assert len(results) == 1
        assert "capped-notice" in results[0]["title"]

    @pytest.mark.asyncio
    async def test_keyword_search_tolerates_reserved_fts5_syntax(self, db_manager):
        """Search text containing FTS5's own reserved words/operators
        (title:, AND/OR/NOT, unbalanced quotes, parens) must not raise --
        it should just search for those as literal words."""
        from src.services.ticket_search_service import TicketSearchService

        for keywords in ['title:foo', 'a AND b', 'a OR "b', 'NEAR(a b)', '-exclude me', '(unbalanced']:
            results = await TicketSearchService.keyword_search(
                keywords=keywords, workflow_id="wf-does-not-exist",
            )
            assert results == []

    @pytest.mark.asyncio
    async def test_keyword_search_with_empty_query_returns_all_matching_filters(
        self, db_manager
    ):
        """Regression: every "check for open bug tickets" self-check in this
        project's own workflow prompts calls search_tickets with only
        structural filters and no free-text query -- but _fts5_query("")
        returns '""' (an empty phrase literal), which FTS5 MATCH matches
        ZERO rows against, not "everything". A pure filter query (no query
        text) must fall back to a direct, unranked listing instead of
        silently returning nothing."""
        import uuid

        from src.core.database import Ticket
        from src.services.ticket_search_service import TicketSearchService

        workflow_id = f"wf-{uuid.uuid4()}"
        with db_manager.session_scope() as session:
            session.add(Ticket(
                id=f"ticket-{uuid.uuid4()}", workflow_id=workflow_id,
                created_by_agent_id="agent-x", title="Unresolved bug",
                description="d", ticket_type="bug", priority="medium",
                status="backlog", is_resolved=False,
            ))
            session.add(Ticket(
                id=f"ticket-{uuid.uuid4()}", workflow_id=workflow_id,
                created_by_agent_id="agent-x", title="Already shipped bug",
                description="d", ticket_type="bug", priority="medium",
                status="shipped", is_resolved=True,
            ))

        results = await TicketSearchService.keyword_search(
            keywords="", workflow_id=workflow_id, filters={"ticket_type": "bug"},
        )

        assert {r["title"] for r in results} == {"Unresolved bug", "Already shipped bug"}

    @pytest.mark.asyncio
    async def test_keyword_search_is_resolved_filter(self, db_manager):
        """The actual gap this fix closes: an agent's self-check needs
        "give me open bug tickets" -- a resolution-state filter, not a
        literal board-column string match (which is what "status" is).
        Works with or without a free-text query."""
        import uuid

        from src.core.database import Ticket
        from src.services.ticket_search_service import TicketSearchService

        workflow_id = f"wf-{uuid.uuid4()}"
        with db_manager.session_scope() as session:
            session.add(Ticket(
                id=f"ticket-{uuid.uuid4()}", workflow_id=workflow_id,
                created_by_agent_id="agent-x", title="Needs action",
                description="d", ticket_type="bug", priority="medium",
                status="backlog", is_resolved=False,
            ))
            session.add(Ticket(
                id=f"ticket-{uuid.uuid4()}", workflow_id=workflow_id,
                created_by_agent_id="agent-x", title="Won't fix, resolved",
                description="d", ticket_type="bug", priority="medium",
                status="wontfix", is_resolved=True,
            ))

        results = await TicketSearchService.keyword_search(
            keywords="", workflow_id=workflow_id,
            filters={"ticket_type": "bug", "is_resolved": False},
        )

        assert [r["title"] for r in results] == ["Needs action"]

    @pytest.mark.asyncio
    async def test_semantic_search_with_empty_query_returns_immediately(self):
        """An empty/whitespace query has no meaningful direction to embed --
        unlike keyword_search's deterministic empty-phrase-matches-nothing
        behavior, a degenerate embedding still returns SOME top-K vector
        hits, essentially arbitrary ones. Must short-circuit BEFORE ever
        calling the embedding provider -- asserting the return value alone
        isn't enough, since an unmocked provider call failing into the
        except-block's keyword_search fallback (itself called with the same
        empty string) would coincidentally also return [] for the wrong
        reason; assert the provider is never even reached."""
        from unittest.mock import patch

        from src.services.ticket_search_service import TicketSearchService

        with patch.object(
            TicketSearchService, "_get_embedding_provider"
        ) as mock_provider:
            assert await TicketSearchService.semantic_search(
                query_text="", workflow_id="wf-anything",
            ) == []
            assert await TicketSearchService.semantic_search(
                query_text="   ", workflow_id="wf-anything",
            ) == []
            mock_provider.assert_not_called()

    @pytest.mark.asyncio
    async def test_hybrid_search(self):
        """Test hybrid search (semantic + keyword)."""
        # This test would verify:
        # 1. Both searches are executed
        # 2. Results are merged using RRF
        # 3. Combined scores are calculated correctly
        pytest.skip("Integration test - requires full setup")

    @pytest.mark.asyncio
    async def test_find_related_tickets(self):
        """Test finding related tickets for duplicate detection."""
        # This test would verify:
        # 1. Similar tickets are found
        # 2. Similarity scores are >= 0.9 for duplicates
        # 3. Relation types are correctly classified
        pytest.skip("Integration test - requires test data")

    @pytest.mark.asyncio
    async def test_index_ticket(self):
        """Test indexing a ticket in Qdrant."""
        pytest.skip("Integration test - requires Qdrant")

    @pytest.mark.asyncio
    async def test_reindex_ticket(self):
        """Test reindexing an existing ticket."""
        pytest.skip("Integration test - requires Qdrant and test data")


class TestGetVectorStoreDedup:
    """ServerState.initialize() seeds TicketSearchService._vector_store from
    server_state.vector_store (mirroring _embedding_provider) so the whole
    process shares one TurboVecStore. _get_vector_store()'s lazy-construct
    fallback should only fire -- and log loudly -- when that seeding never
    happened."""

    def teardown_method(self):
        from src.services.ticket_search_service import TicketSearchService

        TicketSearchService._vector_store = None

    def test_uses_the_pre_seeded_store_without_constructing_a_duplicate(self):
        from unittest.mock import patch

        from src.services.ticket_search_service import TicketSearchService

        sentinel_store = object()
        TicketSearchService._vector_store = sentinel_store

        with patch(
            "src.services.ticket_search_service.create_vector_store"
        ) as mock_create:
            assert TicketSearchService._get_vector_store() is sentinel_store
            mock_create.assert_not_called()

    def test_logs_a_warning_and_constructs_one_when_never_seeded(self, caplog):
        from unittest.mock import patch

        from src.services.ticket_search_service import TicketSearchService

        TicketSearchService._vector_store = None
        sentinel_store = object()

        with patch(
            "src.services.ticket_search_service.create_vector_store",
            return_value=sentinel_store,
        ) as mock_create:
            with caplog.at_level("WARNING"):
                result = TicketSearchService._get_vector_store()

        assert result is sentinel_store
        mock_create.assert_called_once()
        assert any(
            "never seeded" in record.message for record in caplog.records
        )


class TestTicketServiceIntegration:
    """Test TicketService integration with embeddings."""

    @pytest.mark.asyncio
    async def test_create_ticket_with_embedding(self):
        """Test that ticket creation generates embeddings."""
        # This test would verify:
        # 1. Ticket is created
        # 2. Embedding is generated
        # 3. Embedding is stored in Qdrant
        # 4. Similar tickets are found
        pytest.skip("Integration test - requires full setup")

    @pytest.mark.asyncio
    async def test_update_ticket_regenerates_embedding(self):
        """Test that title/description updates trigger reindexing."""
        # This test would verify:
        # 1. Update with title change triggers reindex
        # 2. Update with description change triggers reindex
        # 3. Other field updates don't trigger reindex
        pytest.skip("Integration test - requires test data")

    @pytest.mark.asyncio
    async def test_comment_reindexing_every_5(self):
        """Test that every 5th comment triggers reindexing."""
        # This test would verify:
        # 1. Comments 1-4 don't trigger reindex
        # 2. 5th comment triggers reindex
        # 3. Comments 6-9 don't trigger reindex
        # 4. 10th comment triggers reindex
        pytest.skip("Integration test - requires test data")


class TestMCPEndpoints:
    """Test MCP search and stats endpoints."""

    @pytest.mark.asyncio
    async def test_search_endpoint_hybrid_default(self):
        """Test that hybrid search is the default mode."""
        # This test would verify:
        # 1. Default search_type is "hybrid"
        # 2. Results include both semantic and keyword matches
        # 3. Scores are combined correctly
        pytest.skip("Integration test - requires MCP server running")

    @pytest.mark.asyncio
    async def test_search_endpoint_with_filters(self):
        """Test search with various filters."""
        # This test would verify filters work:
        # 1. status filter
        # 2. priority filter
        # 3. ticket_type filter
        # 4. assigned_agent_id filter
        # 5. Multiple filters combined
        pytest.skip("Integration test - requires MCP server and test data")

    @pytest.mark.asyncio
    async def test_stats_endpoint(self):
        """Test ticket statistics endpoint."""
        # This test would verify:
        # 1. All statistics are calculated correctly
        # 2. by_status, by_type, by_priority aggregations
        # 3. blocked_count, resolved_count
        # 4. Average calculations
        # 5. Time-based metrics (today, last 7 days)
        pytest.skip("Integration test - requires MCP server and test data")


# Note: These are placeholder tests showing the structure.
# Full integration tests would require:
# 1. Test database setup with fixtures
# 2. Qdrant running locally or mocked
# 3. Sample workflows, agents, and tickets
# 4. Cleanup after tests


class TestFts5QuerySanitization:
    """Unit coverage for _fts5_query -- the sanitizer that keeps arbitrary
    search text from being parsed as FTS5 query grammar (see its own
    docstring for the exact live failure this closes)."""

    def test_quotes_every_token(self):
        from src.services.ticket_search_service import _fts5_query

        assert _fts5_query("capped-notice run counters") == '"capped" "notice" "run" "counters"'

    def test_neutralizes_column_filter_syntax(self):
        from src.services.ticket_search_service import _fts5_query

        assert _fts5_query("title:foo") == '"title" "foo"'

    def test_neutralizes_boolean_operators(self):
        from src.services.ticket_search_service import _fts5_query

        assert _fts5_query("a AND b OR NOT c") == '"a" "AND" "b" "OR" "NOT" "c"'

    def test_empty_input_produces_a_valid_empty_query(self):
        from src.services.ticket_search_service import _fts5_query

        assert _fts5_query("") == '""'
        assert _fts5_query("   ") == '""'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
