"""Unit tests for the TaskSimilarityService."""

import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from src.core.database import DatabaseManager, Task
from src.memory.embedding_factory import EmbeddingProvider
from src.services.task_similarity_service import TaskSimilarityService


class TestTaskSimilarityService:
    """Test cases for TaskSimilarityService."""

    def _setup_query_mock(self, session, return_value):
        """Helper to setup the query chain mock."""
        query_mock = Mock()
        query_mock.filter.return_value = query_mock
        query_mock.all.return_value = return_value
        session.query.return_value = query_mock

    @pytest.fixture
    def mock_db_manager(self):
        """Create a mock database manager."""
        db_manager = Mock(spec=DatabaseManager)
        session = Mock()
        db_manager.get_session.return_value = session
        # session_scope() is a @contextmanager method -- mock it as one so
        # `with self.db_manager.session_scope() as session:` yields the
        # same session mock the tests configure query expectations on.
        db_manager.session_scope.return_value.__enter__ = Mock(return_value=session)
        db_manager.session_scope.return_value.__exit__ = Mock(return_value=False)
        return db_manager, session

    @pytest.fixture
    def mock_embedding_service(self):
        """Create a mock embedding service.

        Spec'd on EmbeddingProvider (src.memory.embedding_factory) -- the
        actual class TaskSimilarityService is wired up with in production
        (via create_embedding_provider()), which only exposes
        calculate_cosine_similarity, not a batch variant.
        """
        service = Mock(spec=EmbeddingProvider)
        return service

    @pytest.fixture
    def similarity_service(self, mock_db_manager, mock_embedding_service):
        """Create a TaskSimilarityService instance for testing."""
        db_manager, _ = mock_db_manager
        with patch("src.services.task_similarity_service.get_config") as mock_config:
            config = Mock()
            config.database_path = ":memory:"
            config.task_similarity_threshold = 0.7
            config.task_related_threshold = 0.4
            mock_config.return_value = config
            service = TaskSimilarityService(db_manager, mock_embedding_service)
            return service

    @pytest.fixture
    def sample_task(self):
        """Create a sample task with embedding."""
        task = Mock(spec=Task)
        task.id = "task-123"
        task.enriched_description = "Implement user authentication"
        task.raw_description = "Add login functionality"
        task.status = "in_progress"
        task.created_at = datetime.now()
        task.embedding = [0.1] * 3072  # Simple embedding
        return task

    @pytest.mark.asyncio
    async def test_check_duplicates_exact_match(
        self, similarity_service, mock_db_manager, mock_embedding_service, sample_task
    ):
        """Test detection of exact duplicate (similarity = 1.0)."""
        _, session = mock_db_manager

        # Setup existing task
        sample_task.embedding = [0.1] * 3072
        self._setup_query_mock(session, [sample_task])

        # Mock perfect similarity
        mock_embedding_service.calculate_cosine_similarity.side_effect = [1.0]

        # Check for duplicates
        result = await similarity_service.check_for_duplicates(
            "Implement user authentication", [0.1] * 3072
        )

        # Verify duplicate detected
        assert result["is_duplicate"] is True
        assert result["duplicate_of"] == "task-123"
        assert result["duplicate_description"] == "Implement user authentication"
        assert result["max_similarity"] == 1.0
        assert result["related_tasks"] == []

    @pytest.mark.asyncio
    async def test_check_duplicates_above_threshold(
        self, similarity_service, mock_db_manager, mock_embedding_service, sample_task
    ):
        """Test detection of duplicate above threshold (similarity > 0.7)."""
        _, session = mock_db_manager

        self._setup_query_mock(session, [sample_task])
        mock_embedding_service.calculate_cosine_similarity.side_effect = [0.85]

        result = await similarity_service.check_for_duplicates(
            "Setup user login system", [0.2] * 3072
        )

        assert result["is_duplicate"] is True
        assert result["duplicate_of"] == "task-123"
        assert result["max_similarity"] == 0.85

    @pytest.mark.asyncio
    async def test_check_duplicates_below_threshold(
        self, similarity_service, mock_db_manager, mock_embedding_service, sample_task
    ):
        """Test no duplicate when below threshold (similarity < 0.7)."""
        _, session = mock_db_manager

        self._setup_query_mock(session, [sample_task])
        mock_embedding_service.calculate_cosine_similarity.side_effect = [0.65]

        result = await similarity_service.check_for_duplicates(
            "Different task entirely", [0.5] * 3072
        )

        assert result["is_duplicate"] is False
        assert result["duplicate_of"] is None
        assert result["max_similarity"] == 0.65

    @pytest.mark.asyncio
    async def test_find_related_tasks(
        self, similarity_service, mock_db_manager, mock_embedding_service
    ):
        """Test finding related tasks (0.4 < similarity < 0.7)."""
        _, session = mock_db_manager

        # Create multiple tasks
        task1 = Mock(
            id="task-1",
            enriched_description="Auth system",
            status="done",
            created_at=datetime.now(),
            embedding=[0.1] * 3072,
        )
        task2 = Mock(
            id="task-2",
            enriched_description="User profiles",
            status="pending",
            created_at=datetime.now(),
            embedding=[0.2] * 3072,
        )
        task3 = Mock(
            id="task-3",
            enriched_description="Database setup",
            status="done",
            created_at=datetime.now(),
            embedding=[0.3] * 3072,
        )

        self._setup_query_mock(session, [task1, task2, task3])

        # Mock similarities: task1 related, task2 not related, task3 related
        mock_embedding_service.calculate_cosine_similarity.side_effect = [
            0.55,
            0.3,
            0.45,
        ]

        result = await similarity_service.check_for_duplicates(
            "User management system", [0.4] * 3072
        )

        assert result["is_duplicate"] is False
        assert len(result["related_tasks"]) == 2
        assert "task-1" in result["related_tasks"]
        assert "task-3" in result["related_tasks"]
        assert "task-2" not in result["related_tasks"]

        # Check details are sorted by similarity
        details = result["related_tasks_details"]
        assert details[0]["task_id"] == "task-1"  # Higher similarity
        assert details[0]["similarity"] == 0.55
        assert details[1]["task_id"] == "task-3"
        assert details[1]["similarity"] == 0.45

    @pytest.mark.asyncio
    async def test_no_related_or_duplicates(
        self, similarity_service, mock_db_manager, mock_embedding_service, sample_task
    ):
        """Test when no duplicates or related tasks found (similarity < 0.4)."""
        _, session = mock_db_manager

        self._setup_query_mock(session, [sample_task])
        mock_embedding_service.calculate_cosine_similarity.side_effect = [0.2]

        result = await similarity_service.check_for_duplicates(
            "Completely unrelated task", [0.9] * 3072
        )

        assert result["is_duplicate"] is False
        assert result["duplicate_of"] is None
        assert result["related_tasks"] == []
        assert result["max_similarity"] == 0.2

    @pytest.mark.asyncio
    async def test_multiple_related_tasks_sorted(
        self, similarity_service, mock_db_manager, mock_embedding_service
    ):
        """Test that multiple related tasks are sorted by similarity."""
        _, session = mock_db_manager

        # Create tasks with varying similarities
        tasks = [
            Mock(
                id=f"task-{i}",
                enriched_description=f"Task {i}",
                status="done",
                created_at=datetime.now(),
                embedding=[i * 0.1] * 3072,
            )
            for i in range(5)
        ]

        self._setup_query_mock(session, tasks)

        # Similarities: mix of related and not
        mock_embedding_service.calculate_cosine_similarity.side_effect = [
            0.45,
            0.6,
            0.35,
            0.5,
            0.42,
        ]

        result = await similarity_service.check_for_duplicates("New task", [0.5] * 3072)

        # Should have 4 related tasks (all with similarity between 0.4 and 0.7)
        assert len(result["related_tasks"]) == 4

        # Check sorted order
        details = result["related_tasks_details"]
        similarities = [d["similarity"] for d in details]
        assert similarities == sorted(similarities, reverse=True)
        assert details[0]["similarity"] == 0.6
        assert details[-1]["similarity"] == 0.42

    @pytest.mark.asyncio
    async def test_exclude_failed_tasks(
        self, similarity_service, mock_db_manager, mock_embedding_service
    ):
        """Test that failed tasks are excluded from comparison."""
        _, session = mock_db_manager

        # Mock query to filter out failed tasks
        query_mock = Mock()
        filter_mock = Mock()

        session.query.return_value = query_mock
        query_mock.filter.return_value = filter_mock
        filter_mock.all.return_value = []

        result = await similarity_service.check_for_duplicates("New task", [0.5] * 3072)

        # Verify filter was called with correct parameters
        query_mock.filter.assert_called_once()
        query_mock.filter.call_args[0]

        # Should filter for non-null embeddings and exclude failed/duplicated
        assert result["is_duplicate"] is False
        assert result["related_tasks"] == []

    @pytest.mark.asyncio
    async def test_exclude_duplicated_tasks(self, similarity_service, mock_db_manager):
        """Test that already duplicated tasks are excluded."""
        _, session = mock_db_manager

        # Setup returns no tasks (all filtered out)
        self._setup_query_mock(session, [])

        result = await similarity_service.check_for_duplicates("New task", [0.5] * 3072)

        assert result["is_duplicate"] is False
        assert result["related_tasks"] == []
        assert result["max_similarity"] == 0.0

    @pytest.mark.asyncio
    async def test_empty_database(self, similarity_service, mock_db_manager):
        """Test behavior with no existing tasks."""
        _, session = mock_db_manager

        self._setup_query_mock(session, [])

        result = await similarity_service.check_for_duplicates(
            "First task ever", [0.5] * 3072
        )

        assert result["is_duplicate"] is False
        assert result["duplicate_of"] is None
        assert result["related_tasks"] == []
        assert result["max_similarity"] == 0.0

    @pytest.mark.asyncio
    async def test_store_embedding_success(self, similarity_service, mock_db_manager):
        """Test successful embedding storage."""
        _, session = mock_db_manager

        task = Mock(spec=Task)
        session.query().filter_by().first.return_value = task

        embedding = [0.5] * 3072
        await similarity_service.store_task_embedding("task-123", embedding)

        # Verify embedding was set and committed
        assert task.embedding == embedding

    @pytest.mark.asyncio
    async def test_store_embedding_with_related(
        self, similarity_service, mock_db_manager
    ):
        """Test storing embedding with related task IDs."""
        _, session = mock_db_manager

        task = Mock(spec=Task)
        session.query().filter_by().first.return_value = task

        embedding = [0.5] * 3072
        related_ids = ["task-1", "task-2", "task-3"]

        await similarity_service.store_task_embedding(
            "task-123", embedding, related_task_ids=related_ids
        )

        assert task.embedding == embedding
        assert task.related_task_ids == related_ids

    @pytest.mark.asyncio
    async def test_store_embedding_with_duplicate_info(
        self, similarity_service, mock_db_manager
    ):
        """Test storing embedding with duplicate information."""
        _, session = mock_db_manager

        task = Mock(spec=Task)
        session.query().filter_by().first.return_value = task

        await similarity_service.store_task_embedding(
            "task-123",
            [0.5] * 3072,
            duplicate_of="task-original",
            similarity_score=0.95,
        )

        assert task.duplicate_of_task_id == "task-original"
        assert task.similarity_score == 0.95

    @pytest.mark.asyncio
    async def test_handle_null_embeddings(
        self, similarity_service, mock_db_manager, mock_embedding_service
    ):
        """Test skipping tasks without embeddings."""
        _, session = mock_db_manager

        # Create tasks with and without embeddings
        task1 = Mock(id="task-1", embedding=None)
        task2 = Mock(id="task-2", embedding=[0.1] * 3072)
        task3 = Mock(id="task-3", embedding="")  # Empty string

        self._setup_query_mock(session, [task1, task2, task3])
        mock_embedding_service.calculate_cosine_similarity.side_effect = [0.5]

        await similarity_service.check_for_duplicates("New task", [0.5] * 3072)

        # Should only compare with task2 -- one call, since task1/task3 have
        # no usable embedding and are filtered out before comparison.
        mock_embedding_service.calculate_cosine_similarity.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_json_stored_embeddings(
        self, similarity_service, mock_db_manager, mock_embedding_service
    ):
        """Test handling embeddings stored as JSON strings."""
        _, session = mock_db_manager

        # Create task with JSON string embedding
        task = Mock(
            id="task-1",
            enriched_description="Task",
            status="done",
            created_at=datetime.now(),
        )
        task.embedding = json.dumps([0.1] * 3072)

        self._setup_query_mock(session, [task])
        mock_embedding_service.calculate_cosine_similarity.side_effect = [0.5]

        await similarity_service.check_for_duplicates("New task", [0.5] * 3072)

        # Should parse and use the JSON embedding
        mock_embedding_service.calculate_cosine_similarity.assert_called_once()
        embedding_arg = mock_embedding_service.calculate_cosine_similarity.call_args[0][1]
        assert embedding_arg == [0.1] * 3072

    @pytest.mark.asyncio
    async def test_error_handling_returns_safe_default(
        self, similarity_service, mock_db_manager
    ):
        """Test that errors return safe defaults."""
        _, session = mock_db_manager

        # Cause an error
        session.query.side_effect = Exception("Database error")

        result = await similarity_service.check_for_duplicates("New task", [0.5] * 3072)

        # Should return safe defaults
        assert result["is_duplicate"] is False
        assert result["duplicate_of"] is None
        assert result["related_tasks"] == []
        assert "error" in result
        assert "Database error" in result["error"]

    @pytest.mark.asyncio
    async def test_limit_related_tasks_to_ten(
        self, similarity_service, mock_db_manager, mock_embedding_service
    ):
        """Test that related tasks are limited to top 10."""
        _, session = mock_db_manager

        # Create 20 tasks
        tasks = [
            Mock(
                id=f"task-{i}",
                enriched_description=f"Task {i}",
                status="done",
                created_at=datetime.now(),
                embedding=[i * 0.01] * 3072,
            )
            for i in range(20)
        ]

        self._setup_query_mock(session, tasks)

        # All with similarity between 0.4 and 0.7
        similarities = [0.4 + (i * 0.01) for i in range(20)]
        mock_embedding_service.calculate_cosine_similarity.side_effect = similarities

        result = await similarity_service.check_for_duplicates("New task", [0.5] * 3072)

        # Should limit to 10 related tasks
        assert len(result["related_tasks"]) == 10
        assert len(result["related_tasks_details"]) == 10


class TestPhaselessDuplicateCheckUsesRealQuery:
    """Regression test for the phase_id=None branch of check_for_duplicates.

    This class's fixtures (unlike TestTaskSimilarityService's) exercise a
    real SQLite session, not a fully-stubbed query-chain mock. That's
    deliberate: the bug this guards against was `Task.phase_id is None`
    (a Python identity check on a SQLAlchemy class attribute, which is
    never None, so the expression is always the literal `False` -- SQLAlchemy
    compiles filter(False) to a clause that matches zero rows, always). A
    mocked `.filter()` that just returns itself regardless of its argument
    cannot distinguish that from a correct `.is_(None)` predicate -- only a
    real query against real rows can prove the fix actually filters
    anything.
    """

    @pytest.fixture
    def real_similarity_service(self, db_manager):
        from src.memory.embedding_factory import EmbeddingProvider

        with patch("src.services.task_similarity_service.get_config") as mock_config:
            config = Mock()
            config.database_path = ":memory:"
            config.task_similarity_threshold = 0.7
            config.task_related_threshold = 0.4
            mock_config.return_value = config
            embedding_service = Mock(spec=EmbeddingProvider)
            embedding_service.calculate_cosine_similarity.return_value = 0.99
            service = TaskSimilarityService(db_manager, embedding_service)
            return service, embedding_service

    def _insert_phaseless_task(self, db_manager, task_id, description, embedding):
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id=task_id,
                    raw_description=description,
                    done_definition="done",
                    status="in_progress",
                    phase_id=None,
                    embedding=embedding,
                )
            )

    @pytest.mark.asyncio
    async def test_finds_duplicate_among_phaseless_tasks(
        self, db_manager, real_similarity_service
    ):
        """A pre-fix `Task.phase_id is None` filter matches nothing, ever --
        even an exact-duplicate phase-less task would be invisible to this
        check. Confirm it's actually found now."""
        service, _ = real_similarity_service
        self._insert_phaseless_task(
            db_manager, "task-phaseless-1", "Implement user authentication", [0.1] * 3072
        )

        result = await service.check_for_duplicates(
            "Implement user authentication", [0.1] * 3072, phase_id=None
        )

        assert result["is_duplicate"] is True
        assert result["duplicate_of"] == "task-phaseless-1"

    @pytest.mark.asyncio
    async def test_ignores_phased_tasks_when_checking_phaseless(
        self, db_manager, real_similarity_service
    ):
        """A task that DOES belong to a phase must never surface as a
        duplicate for a phase-less check -- the existing phase_id-provided
        branch already guarantees the reverse; this is the phase_id=None
        branch's own half of the same guarantee."""
        service, _ = real_similarity_service
        with db_manager.session_scope() as session:
            session.add(
                Task(
                    id="task-phase-1",
                    raw_description="Implement user authentication",
                    done_definition="done",
                    status="in_progress",
                    phase_id="phase-abc",
                    embedding=[0.1] * 3072,
                )
            )

        result = await service.check_for_duplicates(
            "Implement user authentication", [0.1] * 3072, phase_id=None
        )

        assert result["is_duplicate"] is False
