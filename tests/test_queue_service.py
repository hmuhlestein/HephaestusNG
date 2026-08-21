"""Unit tests for QueueService."""

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.core.database import Agent, Base, Phase, Task, Workflow
from src.services.queue_service import QueueService


@pytest.fixture
def db_manager():
    """Create a test database manager with in-memory SQLite.

    StaticPool + check_same_thread=False: a bare `sqlite:///:memory:` gives
    each connection checkout its own separate, empty in-memory database
    (fine for every other test here, all single-threaded) -- but
    TestReservationAtomicity's concurrent-threads test needs every thread
    to see the SAME database, which requires pinning the whole engine to
    one shared connection.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # expire_on_commit=False matches the real DatabaseManager (core/database.py) --
    # without it, session_scope()'s commit expires every loaded object's
    # attributes, and reading them after the session closes raises
    # DetachedInstanceError.
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    class TestDatabaseManager:
        def __init__(self):
            self.engine = engine
            self.Session = Session

        def get_session(self):
            return self.Session()

        def create_tables(self):
            Base.metadata.create_all(self.engine)

        @contextmanager
        def session_scope(self):
            session = self.Session()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

    return TestDatabaseManager()


@pytest.fixture
def queue_service(db_manager):
    """Create a QueueService instance with max 3 concurrent agents."""
    return QueueService(db_manager, max_concurrent_agents=3)


def create_test_task(db_manager, task_id=None, priority="medium", status="pending", phase_id=None):
    """Helper to create a test task."""
    session = db_manager.get_session()
    try:
        task = Task(
            id=task_id or str(uuid.uuid4()),
            raw_description="Test task",
            enriched_description="Test task description",
            done_definition="Complete the task",
            status=status,
            priority=priority,
            phase_id=phase_id,
        )
        session.add(task)
        session.commit()
        task_id = task.id
    finally:
        session.close()
    return task_id


def create_test_phase(db_manager, phase_id=None, cli_tool=None, cli_model=None):
    """Helper to create a test phase (with its parent workflow row, required
    by the FK) with an optional per-phase cli_tool/cli_model override."""
    session = db_manager.get_session()
    try:
        workflow_id = str(uuid.uuid4())
        session.add(
            Workflow(id=workflow_id, name="Test workflow", phases_folder_path="/tmp")
        )
        phase = Phase(
            id=phase_id or str(uuid.uuid4()),
            workflow_id=workflow_id,
            order=1,
            name="test_phase",
            description="Test phase",
            done_definitions=[],
            cli_tool=cli_tool,
            cli_model=cli_model,
        )
        session.add(phase)
        session.commit()
        phase_id = phase.id
    finally:
        session.close()
    return phase_id


def create_test_agent(db_manager, agent_id=None, status="working", cli_type="claude", cli_model=None):
    """Helper to create a test agent."""
    session = db_manager.get_session()
    try:
        agent = Agent(
            id=agent_id or str(uuid.uuid4()),
            system_prompt="Test prompt",
            status=status,
            cli_type=cli_type,
            cli_model=cli_model,
        )
        session.add(agent)
        session.commit()
        agent_id = agent.id
    finally:
        session.close()
    return agent_id


class TestGetActiveAgentCount:
    """Tests for get_active_agent_count method."""

    def test_no_agents(self, queue_service):
        """Should return 0 when no agents exist."""
        count = queue_service.get_active_agent_count()
        assert count == 0

    def test_only_active_agents(self, queue_service, db_manager):
        """Should count active agents -- working, idle, starting, and
        stuck. "stuck" counts too: a stuck agent still holds its tmux
        session/task slot, so excluding it from the concurrency cap would
        let the system over-dispatch new agents past max_concurrent_agents
        exactly while stuck agents are already straining resources.
        Only "terminated" (a real end state) is excluded."""
        create_test_agent(db_manager, status="working")
        create_test_agent(db_manager, status="idle")
        create_test_agent(db_manager, status="stuck")
        create_test_agent(db_manager, status="terminated")
        create_test_agent(db_manager, status="terminated")

        count = queue_service.get_active_agent_count()
        assert count == 3  # working + idle + stuck

    def test_all_terminated(self, queue_service, db_manager):
        """Should return 0 when all agents are terminated."""
        create_test_agent(db_manager, status="terminated")
        create_test_agent(db_manager, status="terminated")

        count = queue_service.get_active_agent_count()
        assert count == 0


class TestGetActiveAgentCountForCliModel:
    """get_active_agent_count_for_cli_model is the budget a per-cli/model
    concurrency limit is checked against -- same "stuck must count" gap
    as get_active_agent_count, on the same combo-scoped query."""

    def test_stuck_agent_counts_against_the_combo_budget(self, queue_service, db_manager):
        create_test_agent(db_manager, status="stuck", cli_type="pi", cli_model="qwen-local")

        count = queue_service.get_active_agent_count_for_cli_model("pi", "qwen-local")
        assert count == 1


class TestShouldQueueTask:
    """Tests for should_queue_task method."""

    def test_below_limit(self, queue_service, db_manager):
        """Should return False when below concurrent limit."""
        create_test_agent(db_manager, status="working")
        create_test_agent(db_manager, status="working")

        # 2 agents < 3 max
        assert queue_service.should_queue_task() is False

    def test_at_limit(self, queue_service, db_manager):
        """Should return True when at concurrent limit."""
        create_test_agent(db_manager, status="working")
        create_test_agent(db_manager, status="working")
        create_test_agent(db_manager, status="idle")

        # 3 agents == 3 max
        assert queue_service.should_queue_task() is True

    def test_above_limit(self, queue_service, db_manager):
        """Should return True when above concurrent limit."""
        for _ in range(5):
            create_test_agent(db_manager, status="working")

        # 5 agents > 3 max
        assert queue_service.should_queue_task() is True

    def test_terminated_agents_not_counted(self, queue_service, db_manager):
        """Terminated agents should not count toward limit."""
        create_test_agent(db_manager, status="working")
        create_test_agent(db_manager, status="working")
        create_test_agent(db_manager, status="terminated")
        create_test_agent(db_manager, status="terminated")

        # 2 active agents < 3 max
        assert queue_service.should_queue_task() is False


class TestEnqueueTask:
    """Tests for enqueue_task method."""

    def test_enqueue_task_basic(self, queue_service, db_manager):
        """Should mark task as queued and set timestamp."""
        task_id = create_test_task(db_manager, status="pending")

        queue_service.enqueue_task(task_id)

        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "queued"
            assert task.queued_at is not None
            assert task.queue_position == 1
        finally:
            session.close()

    def test_enqueue_multiple_tasks(self, queue_service, db_manager):
        """Should set correct queue positions for multiple tasks."""
        # Create tasks with different priorities
        task1_id = create_test_task(db_manager, priority="low", status="pending")
        task2_id = create_test_task(db_manager, priority="high", status="pending")
        task3_id = create_test_task(db_manager, priority="medium", status="pending")

        queue_service.enqueue_task(task1_id)
        queue_service.enqueue_task(task2_id)
        queue_service.enqueue_task(task3_id)

        session = db_manager.get_session()
        try:
            task1 = session.query(Task).filter_by(id=task1_id).first()
            task2 = session.query(Task).filter_by(id=task2_id).first()
            task3 = session.query(Task).filter_by(id=task3_id).first()

            # High priority should be position 1
            assert task2.queue_position == 1
            # Medium priority should be position 2
            assert task3.queue_position == 2
            # Low priority should be position 3
            assert task1.queue_position == 3
        finally:
            session.close()

    def test_enqueue_nonexistent_task(self, queue_service):
        """Should handle enqueueing nonexistent task gracefully."""
        # Should not raise exception
        queue_service.enqueue_task("nonexistent-task-id")


class TestGetNextQueuedTask:
    """Tests for get_next_queued_task method."""

    def test_empty_queue(self, queue_service):
        """Should return None when queue is empty."""
        task = queue_service.get_next_queued_task()
        assert task is None

    def test_priority_ordering(self, queue_service, db_manager):
        """Should return highest priority task."""
        # Create tasks in different order
        create_test_task(db_manager, priority="low", status="queued")
        create_test_task(db_manager, priority="medium", status="queued")
        high_id = create_test_task(db_manager, priority="high", status="queued")

        task = queue_service.get_next_queued_task()
        assert task.id == high_id

    def test_fifo_within_same_priority(self, queue_service, db_manager):
        """Should return earliest queued task when priorities are equal."""
        session = db_manager.get_session()
        try:
            # Create tasks with same priority but different queued times
            task1 = Task(
                id=str(uuid.uuid4()),
                raw_description="Task 1",
                done_definition="Done",
                status="queued",
                priority="medium",
                queued_at=datetime.utcnow() - timedelta(minutes=5),
            )
            task2 = Task(
                id=str(uuid.uuid4()),
                raw_description="Task 2",
                done_definition="Done",
                status="queued",
                priority="medium",
                queued_at=datetime.utcnow() - timedelta(minutes=2),
            )
            session.add(task1)
            session.add(task2)
            session.commit()
            task1_id = task1.id
        finally:
            session.close()

        next_task = queue_service.get_next_queued_task()
        # Should get task1 (queued earlier)
        assert next_task.id == task1_id

    def test_boosted_priority_first(self, queue_service, db_manager):
        """Boosted tasks should be returned before high priority tasks."""
        session = db_manager.get_session()
        try:
            # Create high priority task
            high_task = Task(
                id=str(uuid.uuid4()),
                raw_description="High priority",
                done_definition="Done",
                status="queued",
                priority="high",
                queued_at=datetime.utcnow() - timedelta(minutes=10),
            )
            # Create boosted medium priority task
            boosted_task = Task(
                id=str(uuid.uuid4()),
                raw_description="Boosted medium",
                done_definition="Done",
                status="queued",
                priority="medium",
                priority_boosted=True,
                queued_at=datetime.utcnow(),
            )
            session.add(high_task)
            session.add(boosted_task)
            session.commit()
            boosted_id = boosted_task.id
        finally:
            session.close()

        next_task = queue_service.get_next_queued_task()
        # Should get boosted task even though high priority task exists
        assert next_task.id == boosted_id


class TestCliModelConcurrencyLimit:
    """Regression: a local model with a single inference slot (e.g. pi's
    Qwen3.8-27B-UD-Q4_K_XL.gguf) used to have no way to cap concurrency --
    a second agent dispatched onto it just sat frozen waiting its turn
    instead of doing anything. cli_model_concurrency_limits caps active
    agents per (cli_tool, cli_model) combo. This fixture has no fallback
    model configured, so a queued task whose combo is saturated is skipped
    over (not dequeued) rather than starving the whole queue behind it --
    see TestCliModelConcurrencyFallback for the (more common) case where a
    fallback model IS configured."""

    @pytest.fixture
    def limited_queue_service(self, db_manager):
        return QueueService(
            db_manager,
            max_concurrent_agents=10,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
        )

    def test_skips_task_whose_combo_is_at_its_limit(self, limited_queue_service, db_manager):
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        create_test_task(db_manager, priority="high", status="queued", phase_id=phase_id)

        task = limited_queue_service.get_next_queued_task()

        assert task is None

    def test_dispatches_task_once_the_combo_has_a_free_slot(self, limited_queue_service, db_manager):
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, priority="high", status="queued", phase_id=phase_id)

        task = limited_queue_service.get_next_queued_task()

        assert task is not None
        assert task.id == task_id

    def test_falls_through_to_a_different_combo_not_at_its_limit(self, limited_queue_service, db_manager):
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        saturated_phase = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        create_test_task(db_manager, priority="high", status="queued", phase_id=saturated_phase)

        other_phase = create_test_phase(db_manager, cli_tool="claude", cli_model="sonnet")
        other_task_id = create_test_task(db_manager, priority="medium", status="queued", phase_id=other_phase)

        task = limited_queue_service.get_next_queued_task()

        assert task is not None
        assert task.id == other_task_id

    def test_no_limits_configured_is_a_noop(self, queue_service, db_manager):
        """The default (no cli_model_concurrency_limits) fixture must behave
        exactly as before -- no phase lookups, no skipped tasks."""
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, priority="high", status="queued", phase_id=phase_id)

        task = queue_service.get_next_queued_task()

        assert task is not None
        assert task.id == task_id


class TestReservationAtomicity:
    """Regression: get_active_agent_count_for_cli_model's check-then-act was
    not atomic against the other four dispatch call sites that share the
    same limit (process_queue, create_task, restart_task_endpoint,
    bump_task_priority_endpoint, and orchestrator.py's
    create_agent_for_task_direct) -- worktree setup + prompt generation
    take seconds between the check and the real Agent row landing in the
    DB, during which a second, independent dispatch call could run its own
    check, also see room, and double-book a single-inference-slot combo.
    try_reserve_cli_model_slot/release_cli_model_slot close that gap with
    an in-memory reservation counted alongside real active agents, guarded
    by a lock."""

    def test_second_reservation_fails_while_first_is_outstanding(self, db_manager):
        qs = QueueService(db_manager, max_concurrent_agents=10, cli_model_concurrency_limits={"pi/qwen-local": 1})

        first = qs.try_reserve_cli_model_slot("pi", "qwen-local")
        second = qs.try_reserve_cli_model_slot("pi", "qwen-local")

        assert first is True
        assert second is False

    def test_release_frees_the_slot_for_a_later_reservation(self, db_manager):
        qs = QueueService(db_manager, max_concurrent_agents=10, cli_model_concurrency_limits={"pi/qwen-local": 1})

        qs.try_reserve_cli_model_slot("pi", "qwen-local")
        qs.release_cli_model_slot("pi", "qwen-local")
        third = qs.try_reserve_cli_model_slot("pi", "qwen-local")

        assert third is True

    def test_reservation_counts_against_real_active_agents_too(self, db_manager):
        """A real Agent row already occupying the combo's only slot must
        block a reservation just as effectively as a pending reservation
        does -- the two are counted together against the limit."""
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        qs = QueueService(db_manager, max_concurrent_agents=10, cli_model_concurrency_limits={"pi/qwen-local": 1})

        result = qs.try_reserve_cli_model_slot("pi", "qwen-local")

        assert result is False

    def test_unconfigured_combo_always_succeeds_and_reserves_nothing(self, db_manager):
        """No limit configured for this combo -- try_reserve is a pure
        no-op (always True), and release on it must not raise or corrupt
        state for a combo that IS configured."""
        qs = QueueService(db_manager, max_concurrent_agents=10, cli_model_concurrency_limits={"pi/qwen-local": 1})

        result = qs.try_reserve_cli_model_slot("claude", "sonnet")
        qs.release_cli_model_slot("claude", "sonnet")  # must not raise

        assert result is True
        # The configured combo is unaffected.
        assert qs.try_reserve_cli_model_slot("pi", "qwen-local") is True

    def test_release_without_a_prior_reservation_is_a_safe_noop(self, db_manager):
        qs = QueueService(db_manager, max_concurrent_agents=10, cli_model_concurrency_limits={"pi/qwen-local": 1})

        qs.release_cli_model_slot("pi", "qwen-local")  # must not raise or go negative

        assert qs.try_reserve_cli_model_slot("pi", "qwen-local") is True
        assert qs.try_reserve_cli_model_slot("pi", "qwen-local") is False

    def test_concurrent_reservation_attempts_only_let_one_through(self, db_manager):
        """The actual race this fix closes: many threads all seeing 'room'
        under a naive read-then-write check. With the lock, exactly
        `limit` of N concurrent attempts must succeed."""
        import threading

        qs = QueueService(db_manager, max_concurrent_agents=10, cli_model_concurrency_limits={"pi/qwen-local": 2})
        results = []
        results_lock = threading.Lock()

        def attempt():
            ok = qs.try_reserve_cli_model_slot("pi", "qwen-local")
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results.count(True) == 2
        assert results.count(False) == 18


class TestResolveCliModelDispatch:
    """Tests for the consolidated decision+reservation method every
    dispatch call site uses -- resolve_cli_and_model +
    try_reserve_cli_model_slot + resolve_fallback_model rolled into one,
    replacing five independent copies of the same decision tree."""

    def test_no_limits_configured_is_a_full_noop(self, db_manager, queue_service):
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, phase_id=phase_id)
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            result = queue_service.resolve_cli_model_dispatch(session, task)
        finally:
            session.close()

        assert result == (None, None, None, False)

    def test_free_slot_reserves_primary_with_no_override(self, db_manager):
        qs = QueueService(db_manager, max_concurrent_agents=10, cli_model_concurrency_limits={"pi/qwen-local": 1})
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, phase_id=phase_id)
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            cli_override, model_override, reservation, saturated = qs.resolve_cli_model_dispatch(session, task)
        finally:
            session.close()

        assert cli_override is None
        assert model_override is None
        assert reservation == ("pi", "qwen-local")
        assert saturated is False
        # Primary combo's slot is genuinely reserved now.
        assert qs.try_reserve_cli_model_slot("pi", "qwen-local") is False

    def test_saturated_primary_with_fallback_returns_override_and_reserves_fallback(self, db_manager):
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        qs = QueueService(
            db_manager, max_concurrent_agents=10,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi", default_cli_model="qwen-local",
            cli_model_fallback="mimo-v2.5-pro",
        )
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, phase_id=phase_id)
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            cli_override, model_override, reservation, saturated = qs.resolve_cli_model_dispatch(session, task)
        finally:
            session.close()

        assert (cli_override, model_override) == ("pi", "mimo-v2.5-pro")
        assert reservation == ("pi", "mimo-v2.5-pro")
        assert saturated is False

    def test_saturated_with_no_usable_fallback_returns_saturated_true(self, db_manager):
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        qs = QueueService(
            db_manager, max_concurrent_agents=10,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi", default_cli_model="qwen-local",
        )
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, phase_id=phase_id)
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            result = qs.resolve_cli_model_dispatch(session, task)
        finally:
            session.close()

        assert result == (None, None, None, True)


class TestCliModelConcurrencyFallback:
    """When a queued task's primary combo is saturated and a fallback MODEL
    is configured for that cli_tool (e.g. pi's Qwen -> mimo-v2.5-pro, same
    CLI, different model -- CLIAgentInterface.fallback_model's role-based
    target), dispatch onto the fallback instead of stalling the task in the
    queue."""

    @pytest.fixture
    def queue_service_with_fallback(self, db_manager):
        return QueueService(
            db_manager,
            max_concurrent_agents=10,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            cli_model_fallback="mimo-v2.5-pro",
        )

    def test_dispatches_on_fallback_model_when_primary_is_saturated(
        self, queue_service_with_fallback, db_manager
    ):
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, priority="high", status="queued", phase_id=phase_id)

        task = queue_service_with_fallback.get_next_queued_task()

        assert task is not None
        assert task.id == task_id
        assert task._dispatch_cli_override == ("pi", "mimo-v2.5-pro")

    def test_secondary_tier_reads_its_own_fallback_config(self, db_manager):
        """Mirrors CLIAgentInterface.fallback_model's role resolution: a
        non-default (secondary-tier) cli_tool must read
        secondary_cli_model_fallback, not the primary's cli_model_fallback."""
        qs = QueueService(
            db_manager,
            max_concurrent_agents=10,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="claude",  # pi is the secondary tier here
            default_cli_model="local-claude-model",
            cli_model_fallback="opus",  # claude's (primary) fallback -- must NOT be used
            secondary_cli_model_fallback="mimo-v2.5-pro",
        )
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        task_id = create_test_task(db_manager, priority="high", status="queued", phase_id=phase_id)

        task = qs.get_next_queued_task()

        assert task is not None
        assert task.id == task_id
        assert task._dispatch_cli_override == ("pi", "mimo-v2.5-pro")

    def test_skips_when_fallback_combo_is_also_saturated(self, db_manager):
        qs = QueueService(
            db_manager,
            max_concurrent_agents=10,
            cli_model_concurrency_limits={"pi/qwen-local": 1, "pi/mimo-v2.5-pro": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            cli_model_fallback="mimo-v2.5-pro",
        )
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="mimo-v2.5-pro")
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        create_test_task(db_manager, priority="high", status="queued", phase_id=phase_id)

        task = qs.get_next_queued_task()

        assert task is None

    def test_fallback_equal_to_primary_is_not_used(self, db_manager):
        """Same no-op guard as CLIAgentInterface's in-session switch: a
        fallback that happens to equal the primary model isn't a real
        fallback -- must fall through to skip, not loop dispatching onto
        the same saturated combo."""
        qs = QueueService(
            db_manager,
            max_concurrent_agents=10,
            cli_model_concurrency_limits={"pi/qwen-local": 1},
            default_cli_tool="pi",
            default_cli_model="qwen-local",
            cli_model_fallback="qwen-local",
        )
        create_test_agent(db_manager, status="working", cli_type="pi", cli_model="qwen-local")
        phase_id = create_test_phase(db_manager, cli_tool="pi", cli_model="qwen-local")
        create_test_task(db_manager, priority="high", status="queued", phase_id=phase_id)

        task = qs.get_next_queued_task()

        assert task is None


class TestDequeueTask:
    """Tests for dequeue_task method."""

    def test_dequeue_task_basic(self, queue_service, db_manager):
        """Should mark task as assigned and clear queue position."""
        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            task.queue_position = 1
            session.commit()
        finally:
            session.close()

        queue_service.dequeue_task(task_id)

        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "assigned"
            assert task.queue_position is None
        finally:
            session.close()

    def test_dequeue_nonexistent_task(self, queue_service):
        """Should handle dequeueing nonexistent task gracefully."""
        # Should not raise exception
        queue_service.dequeue_task("nonexistent-task-id")

    def test_dequeue_non_queued_task(self, queue_service, db_manager):
        """Should handle dequeueing task that's not queued."""
        task_id = create_test_task(db_manager, status="pending")

        # Should not raise exception
        queue_service.dequeue_task(task_id)

        # Task status should remain unchanged
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "pending"
        finally:
            session.close()


class TestClaimNextQueuedTaskAtomicity:
    """The actual race this method closes: process_queue's dequeue sequence
    moved off the single-threaded event loop into run_in_executor (perf:
    stop process_queue's dispatch chain from blocking the event loop) --
    losing the implicit atomicity a no-await sequence used to get for free
    on that single thread. should_queue_task -> get_next_queued_task ->
    dequeue_task, called separately, is a plain SELECT-then-check-then-write
    with no DB-level locking, so two real concurrent threads racing it could
    both select and dequeue the SAME task. claim_next_queued_task serializes
    the three under _dequeue_lock instead."""

    def test_concurrent_claims_for_one_queued_task_only_let_one_through(self, queue_service, db_manager):
        """Many threads all racing to claim the single queued task -- with
        the lock, exactly one must get it; the rest see an empty queue."""
        import threading

        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()

        results = []
        results_lock = threading.Lock()

        def attempt():
            claimed = queue_service.claim_next_queued_task()
            with results_lock:
                results.append(claimed)

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        claimed_ids = [r.id for r in results if r is not None]
        assert claimed_ids == [task_id]  # exactly one thread claimed it
        assert results.count(None) == 19

        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "assigned"  # dequeue_task actually ran, once
        finally:
            session.close()

    def test_returns_none_when_queue_is_empty(self, queue_service):
        assert queue_service.claim_next_queued_task() is None

    def test_dequeue_task_takes_the_dequeue_lock(self, queue_service, db_manager):
        """Regression: dequeue_task has direct callers besides
        claim_next_queued_task -- task_admin_routes.py's
        bump_task_priority_endpoint and cancel_queued_task_endpoint, async
        FastAPI handlers on the event-loop thread. Those used to run
        UNLOCKED: harmless while everything shared the single event loop,
        but a genuine check-then-write race against an executor-thread
        claim ever since process_queue's DB work moved to
        run_in_executor. dequeue_task must itself hold _dequeue_lock so
        both entry points serialize -- verified here by holding the lock
        in the main thread and asserting a concurrent dequeue_task blocks
        until it's released."""
        import threading

        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()

        finished = threading.Event()
        t = threading.Thread(
            target=lambda: (queue_service.dequeue_task(task_id), finished.set())
        )

        with queue_service._dequeue_lock:
            t.start()
            t.join(timeout=0.5)
            assert t.is_alive(), "dequeue_task ran without holding _dequeue_lock"

        t.join(timeout=5)
        assert finished.is_set(), "dequeue_task never completed after the lock was released"

        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "assigned"
        finally:
            session.close()

    def test_returns_none_when_at_capacity(self, db_manager):
        qs = QueueService(db_manager, max_concurrent_agents=1)
        create_test_agent(db_manager, status="working")
        create_test_task(db_manager, status="queued")

        assert qs.claim_next_queued_task() is None

    def test_claims_and_dequeues_the_next_task(self, queue_service, db_manager):
        task_id = create_test_task(db_manager, status="queued")

        claimed = queue_service.claim_next_queued_task()

        assert claimed is not None
        assert claimed.id == task_id
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "assigned"
        finally:
            session.close()


class TestGetQueueStatus:
    """Tests for get_queue_status method."""

    def test_empty_queue(self, queue_service):
        """Should return correct status for empty queue."""
        status = queue_service.get_queue_status()

        assert status["active_agents"] == 0
        assert status["max_concurrent_agents"] == 3
        assert status["queued_tasks_count"] == 0
        assert status["queued_tasks"] == []
        assert status["slots_available"] == 3
        assert status["at_capacity"] is False

    def test_with_agents_and_tasks(self, queue_service, db_manager):
        """Should return correct status with agents and queued tasks."""
        # Create 2 active agents
        create_test_agent(db_manager, status="working")
        create_test_agent(db_manager, status="idle")

        # Create 2 queued tasks
        task1_id = create_test_task(db_manager, priority="high", status="queued")
        task2_id = create_test_task(db_manager, priority="low", status="queued")

        # Set queue positions
        session = db_manager.get_session()
        try:
            task1 = session.query(Task).filter_by(id=task1_id).first()
            task1.queued_at = datetime.utcnow()
            task1.queue_position = 1
            task2 = session.query(Task).filter_by(id=task2_id).first()
            task2.queued_at = datetime.utcnow()
            task2.queue_position = 2
            session.commit()
        finally:
            session.close()

        status = queue_service.get_queue_status()

        assert status["active_agents"] == 2
        assert status["queued_tasks_count"] == 2
        assert status["slots_available"] == 1
        assert status["at_capacity"] is False
        assert len(status["queued_tasks"]) == 2

    def test_at_capacity(self, queue_service, db_manager):
        """Should indicate when at capacity."""
        # Create 3 active agents (at max)
        for _ in range(3):
            create_test_agent(db_manager, status="working")

        status = queue_service.get_queue_status()

        assert status["active_agents"] == 3
        assert status["slots_available"] == 0
        assert status["at_capacity"] is True


class TestCalculateQueuePosition:
    """Tests for _calculate_queue_position's priority/boost tie-break logic.

    Note: this method currently has zero callers anywhere in src/ (grepped
    for `_calculate_queue_position(` outside its own definition) -- the
    live queue-ordering code path is `_recalculate_queue_positions`, a
    separate, correctly-implemented method using `.order_by(...desc())`
    rather than this one's and_()/or_() boolean-negation approach. These
    tests characterize and fix this method's own logic on its own terms
    (Phase 3 Tier 1 item 4 of docs/AUTOPILOT_REFACTOR_PLAN.md), independent
    of whether/when something ends up calling it.
    """

    def test_higher_priority_task_counts_as_ahead_for_non_boosted_new_task(
        self, queue_service, db_manager
    ):
        """A non-boosted new medium-priority task must count an existing
        non-boosted HIGH-priority queued task as ahead of it (and must NOT
        count an existing non-boosted LOW-priority one) -- the ordinary,
        overwhelmingly common case, since new tasks are essentially never
        pre-boosted (the method's own docstring says so).

        Pre-fix, this was silently broken: `not Task.priority_boosted` is a
        Python `not` on a SQLAlchemy class attribute (always truthy), so it
        always evaluates to the literal `False` at expression-build time --
        not a real SQL predicate. `and_(literal False, ...)` collapses the
        whole containing clause to always-False in the compiled SQL, which
        made the entire priority/queued_at tie-break `or_()` branch
        unreachable for any non-boosted new task -- ahead_count degenerated
        to counting ONLY boosted existing tasks, completely ignoring
        priority level. Verified by inspecting the compiled SQL directly
        during Phase 3 Tier 1 implementation: `tasks.priority_boosted OR
        (false OR false) AND ...`.
        """
        session = db_manager.get_session()
        try:
            high_task = Task(
                id=str(uuid.uuid4()),
                raw_description="High priority, queued",
                done_definition="Done",
                status="queued",
                priority="high",
                priority_boosted=False,
                queued_at=datetime.utcnow() - timedelta(minutes=10),
            )
            low_task = Task(
                id=str(uuid.uuid4()),
                raw_description="Low priority, queued",
                done_definition="Done",
                status="queued",
                priority="low",
                priority_boosted=False,
                queued_at=datetime.utcnow() - timedelta(minutes=10),
            )
            session.add_all([high_task, low_task])
            session.commit()

            new_task = Task(
                id=str(uuid.uuid4()),
                raw_description="New medium-priority task",
                done_definition="Done",
                status="queued",
                priority="medium",
                priority_boosted=False,
                queued_at=datetime.utcnow(),
            )

            # Position is 1-indexed: only the high-priority task should be
            # ahead of a new medium-priority, non-boosted task.
            position = queue_service._calculate_queue_position(session, new_task)
            assert position == 2, (
                f"Expected position 2 (only the high-priority task ahead), got {position}"
            )
        finally:
            session.close()

    def test_boosted_existing_task_still_counts_as_ahead(self, queue_service, db_manager):
        """The one sub-clause that was NOT dead (boosted-existing-task-is-
        always-ahead-of-a-non-boosted-new-task) must keep working after the
        fix -- confirms the fix didn't overcorrect and break the one
        previously-working branch."""
        session = db_manager.get_session()
        try:
            boosted_task = Task(
                id=str(uuid.uuid4()),
                raw_description="Boosted, queued",
                done_definition="Done",
                status="queued",
                priority="low",
                priority_boosted=True,
                queued_at=datetime.utcnow(),
            )
            session.add(boosted_task)
            session.commit()

            new_task = Task(
                id=str(uuid.uuid4()),
                raw_description="New high-priority, non-boosted task",
                done_definition="Done",
                status="queued",
                priority="high",
                priority_boosted=False,
                queued_at=datetime.utcnow(),
            )

            position = queue_service._calculate_queue_position(session, new_task)
            assert position == 2, (
                f"Expected position 2 (boosted low-priority task still ahead "
                f"of a non-boosted high-priority one), got {position}"
            )
        finally:
            session.close()


class TestCancelQueuedTask:
    """Tests for cancel_queued_task -- the locked check-and-fail write
    task_admin_routes.py's cancel endpoint delegates to (see the method's
    docstring for the claim race it closes)."""

    def test_cancel_queued_task(self, queue_service, db_manager):
        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            task.queue_position = 1
            session.commit()
        finally:
            session.close()

        outcome, workflow_id = queue_service.cancel_queued_task(task_id)

        assert outcome == "cancelled"
        # No workflow row is attached in this fixture, so workflow_id is
        # None here; the point is that it's threaded through for broadcast.
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "failed"
            assert task.failure_reason == "Cancelled by user from queue"
            assert task.completed_at is not None
        finally:
            session.close()

    def test_cancel_non_queued_task(self, queue_service, db_manager):
        task_id = create_test_task(db_manager, status="assigned")

        outcome, workflow_id = queue_service.cancel_queued_task(task_id)

        assert outcome == "not_queued"
        assert workflow_id is None
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "assigned"
        finally:
            session.close()

    def test_cancel_nonexistent_task(self, queue_service):
        outcome, workflow_id = queue_service.cancel_queued_task("nonexistent-task-id")

        assert outcome == "not_found"
        assert workflow_id is None

    def test_cancel_takes_the_dequeue_lock(self, queue_service, db_manager):
        """Same proof shape as test_dequeue_task_takes_the_dequeue_lock: a
        concurrent cancel must block while the lock is held -- otherwise
        its status=failed write can land inside claim_next_queued_task's
        select-then-dequeue window and dispatch a cancelled task."""
        import threading

        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()

        finished = threading.Event()
        t = threading.Thread(
            target=lambda: (queue_service.cancel_queued_task(task_id), finished.set())
        )

        with queue_service._dequeue_lock:
            t.start()
            t.join(timeout=0.5)
            assert t.is_alive(), "cancel_queued_task ran without holding _dequeue_lock"

        t.join(timeout=5)
        assert finished.is_set(), "cancel_queued_task never completed after the lock was released"


class TestPauseQueuedTask:
    """Tests for pause_queued_task -- same shape and same claim-vs-mutate
    race as cancel_queued_task, reached via pause_task_endpoint's "queued"
    case instead of the cancel endpoint."""

    def test_pause_queued_task(self, queue_service, db_manager):
        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            task.queue_position = 1
            session.commit()
        finally:
            session.close()

        paused = queue_service.pause_queued_task(task_id)

        assert paused is True
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "blocked"
            assert task.queue_position is None
        finally:
            session.close()

    def test_pause_non_queued_task(self, queue_service, db_manager):
        task_id = create_test_task(db_manager, status="assigned")

        paused = queue_service.pause_queued_task(task_id)

        assert paused is False
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "assigned"
        finally:
            session.close()

    def test_pause_nonexistent_task(self, queue_service):
        assert queue_service.pause_queued_task("nonexistent-task-id") is False

    def test_pause_takes_the_dequeue_lock(self, queue_service, db_manager):
        """Same proof shape as test_cancel_takes_the_dequeue_lock: a
        concurrent pause must block while the lock is held -- otherwise its
        status=blocked write can land inside claim_next_queued_task's
        select-then-dequeue window and dispatch a task the user just paused."""
        import threading

        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()

        finished = threading.Event()
        t = threading.Thread(
            target=lambda: (queue_service.pause_queued_task(task_id), finished.set())
        )

        with queue_service._dequeue_lock:
            t.start()
            t.join(timeout=0.5)
            assert t.is_alive(), "pause_queued_task ran without holding _dequeue_lock"

        t.join(timeout=5)
        assert finished.is_set(), "pause_queued_task never completed after the lock was released"


class TestResetQueuedTaskToPending:
    """Tests for reset_queued_task_to_pending -- same shape and same
    claim-vs-mutate race as cancel_queued_task/pause_queued_task, reached
    via stop_workflow's "queued" case (stop_workflow resets its
    assigned/in_progress tasks to "pending" too, for a clean state to
    resume from)."""

    def test_reset_queued_task(self, queue_service, db_manager):
        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            task.queue_position = 1
            session.commit()
        finally:
            session.close()

        reset = queue_service.reset_queued_task_to_pending(task_id)

        assert reset is True
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "pending"
            assert task.queue_position is None
        finally:
            session.close()

    def test_reset_non_queued_task(self, queue_service, db_manager):
        task_id = create_test_task(db_manager, status="in_progress")

        reset = queue_service.reset_queued_task_to_pending(task_id)

        assert reset is False
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "in_progress"
        finally:
            session.close()

    def test_reset_nonexistent_task(self, queue_service):
        assert queue_service.reset_queued_task_to_pending("nonexistent-task-id") is False

    def test_reset_takes_the_dequeue_lock(self, queue_service, db_manager):
        """Same proof shape as test_cancel_takes_the_dequeue_lock: a
        concurrent reset must block while the lock is held -- otherwise its
        status=pending write can land inside claim_next_queued_task's
        select-then-dequeue window and dispatch a task the stop just reset."""
        import threading

        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()

        finished = threading.Event()
        t = threading.Thread(
            target=lambda: (queue_service.reset_queued_task_to_pending(task_id), finished.set())
        )

        with queue_service._dequeue_lock:
            t.start()
            t.join(timeout=0.5)
            assert t.is_alive(), "reset_queued_task_to_pending ran without holding _dequeue_lock"

        t.join(timeout=5)
        assert finished.is_set(), "reset_queued_task_to_pending never completed after the lock was released"


class TestClaimNextQueuedTaskDefenseInDepth:
    """claim_next_queued_task must not hand out a task its own dequeue_task
    call failed to actually claim -- e.g. if the task was deleted between
    get_next_queued_task's select and dequeue_task's write. Simulated here
    by deleting the task from inside a monkeypatched should_queue_task-less
    window: patch dequeue_task itself to simulate the failure, since the
    real race requires two threads and is already covered by the
    lock-blocking tests above -- this test is about claim_next_queued_task's
    OWN handling of a False return, independent of why dequeue_task failed."""

    def test_returns_none_when_dequeue_fails(self, queue_service, db_manager, monkeypatch):
        task_id = create_test_task(db_manager, status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            session.commit()
        finally:
            session.close()

        monkeypatch.setattr(queue_service, "dequeue_task", lambda tid: False)

        assert queue_service.claim_next_queued_task() is None

        # Not left half-claimed: the task's status is untouched since the
        # (mocked) dequeue_task never actually wrote anything.
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.status == "queued"
        finally:
            session.close()


class TestBoostTaskPriority:
    """Tests for boost_task_priority method."""

    def test_boost_queued_task(self, queue_service, db_manager):
        """Should boost a queued task's priority."""
        task_id = create_test_task(db_manager, priority="low", status="queued")
        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            task.queued_at = datetime.utcnow()
            task.queue_position = 5
            session.commit()
        finally:
            session.close()

        result = queue_service.boost_task_priority(task_id)

        assert result is True

        session = db_manager.get_session()
        try:
            task = session.query(Task).filter_by(id=task_id).first()
            assert task.priority_boosted is True
            assert task.queue_position == 1
        finally:
            session.close()

    def test_boost_non_queued_task(self, queue_service, db_manager):
        """Should fail to boost a non-queued task."""
        task_id = create_test_task(db_manager, status="pending")

        result = queue_service.boost_task_priority(task_id)

        assert result is False

    def test_boost_nonexistent_task(self, queue_service):
        """Should fail to boost nonexistent task."""
        result = queue_service.boost_task_priority("nonexistent-task-id")

        assert result is False

    def test_boost_moves_to_front(self, queue_service, db_manager):
        """Boosted task should be returned first by get_next_queued_task."""
        # Create multiple queued tasks
        create_test_task(db_manager, priority="high", status="queued")
        low_id = create_test_task(db_manager, priority="low", status="queued")

        # Boost the low priority task
        queue_service.boost_task_priority(low_id)

        # Should get the boosted low priority task first
        next_task = queue_service.get_next_queued_task()
        assert next_task.id == low_id


class TestGetQueuedTasks:
    """Tests for get_queued_tasks method."""

    def test_empty_queue(self, queue_service):
        """Should return empty list when no queued tasks."""
        tasks = queue_service.get_queued_tasks()
        assert tasks == []

    def test_ordered_by_priority(self, queue_service, db_manager):
        """Should return tasks ordered by priority."""
        # Create tasks in random order
        low_id = create_test_task(db_manager, priority="low", status="queued")
        high_id = create_test_task(db_manager, priority="high", status="queued")
        medium_id = create_test_task(db_manager, priority="medium", status="queued")

        tasks = queue_service.get_queued_tasks()

        assert len(tasks) == 3
        assert tasks[0].id == high_id
        assert tasks[1].id == medium_id
        assert tasks[2].id == low_id

    def test_boosted_tasks_first(self, queue_service, db_manager):
        """Boosted tasks should appear first."""
        high_id = create_test_task(db_manager, priority="high", status="queued")
        low_id = create_test_task(db_manager, priority="low", status="queued")

        # Boost the low priority task
        queue_service.boost_task_priority(low_id)

        tasks = queue_service.get_queued_tasks()

        assert len(tasks) == 2
        # Boosted task should be first
        assert tasks[0].id == low_id
        assert tasks[0].priority_boosted is True
        # Regular high priority task should be second
        assert tasks[1].id == high_id


class TestProjectScopedQueue:
    """Each active project gets its own independent max_concurrent_agents
    budget -- required once more than one project can be active at once
    (multi-project concurrency), otherwise one project's queue depth /
    agent count can starve another's out of the single global cap."""

    def _make_project_task_agent(
        self, db_manager, project_id, workflow_id, agent_status="working"
    ):
        from src.core.database import AutopilotProject, Workflow

        session = db_manager.get_session()
        try:
            if not session.query(AutopilotProject).filter_by(id=project_id).first():
                session.add(
                    AutopilotProject(
                        id=project_id, name=project_id, base_dir=f"/tmp/{project_id}"
                    )
                )
            if not session.query(Workflow).filter_by(id=workflow_id).first():
                session.add(
                    Workflow(
                        id=workflow_id,
                        name=workflow_id,
                        status="active",
                        project_id=project_id,
                        phases_folder_path="/tmp",
                    )
                )
            session.commit()

            task = Task(
                id=str(uuid.uuid4()),
                workflow_id=workflow_id,
                raw_description="r",
                enriched_description="r",
                done_definition="d",
                status="in_progress",
            )
            session.add(task)
            session.commit()

            agent = Agent(
                id=str(uuid.uuid4()),
                system_prompt="p",
                status=agent_status,
                cli_type="claude",
                current_task_id=task.id,
            )
            session.add(agent)
            session.commit()
            return task.id, agent.id
        finally:
            session.close()

    def test_active_agent_count_scoped_to_project(self, queue_service, db_manager):
        self._make_project_task_agent(db_manager, "proj-a", "wf-a")
        self._make_project_task_agent(db_manager, "proj-a", "wf-a2")
        self._make_project_task_agent(db_manager, "proj-b", "wf-b")

        assert queue_service.get_active_agent_count("proj-a") == 2
        assert queue_service.get_active_agent_count("proj-b") == 1
        # Unscoped (no project_id) still counts globally -- unchanged.
        assert queue_service.get_active_agent_count() == 3

    def test_should_queue_task_is_per_project_not_global(self, db_manager):
        """A project at its OWN cap must queue, even while another active
        project is well under the SAME cap -- proves each project has an
        independent budget, not a shared global one."""
        queue_service = QueueService(db_manager, max_concurrent_agents=2)

        self._make_project_task_agent(db_manager, "proj-busy", "wf-busy")
        self._make_project_task_agent(db_manager, "proj-busy", "wf-busy2")
        self._make_project_task_agent(db_manager, "proj-quiet", "wf-quiet")

        assert queue_service.should_queue_task("proj-busy") is True
        assert queue_service.should_queue_task("proj-quiet") is False

    def test_get_next_queued_task_scoped_to_project(self, queue_service, db_manager):
        from src.core.database import AutopilotProject, Workflow

        session = db_manager.get_session()
        try:
            session.add(AutopilotProject(id="proj-a", name="a", base_dir="/tmp/a"))
            session.add(AutopilotProject(id="proj-b", name="b", base_dir="/tmp/b"))
            session.add(
                Workflow(
                    id="wf-a", name="a", status="active", project_id="proj-a",
                    phases_folder_path="/tmp",
                )
            )
            session.add(
                Workflow(
                    id="wf-b", name="b", status="active", project_id="proj-b",
                    phases_folder_path="/tmp",
                )
            )
            session.commit()
        finally:
            session.close()

        task_a = Task(
            id=str(uuid.uuid4()), workflow_id="wf-a", raw_description="r",
            done_definition="d", status="queued", priority="medium",
        )
        task_b = Task(
            id=str(uuid.uuid4()), workflow_id="wf-b", raw_description="r",
            done_definition="d", status="queued", priority="medium",
        )
        session = db_manager.get_session()
        try:
            session.add(task_a)
            session.add(task_b)
            session.commit()
        finally:
            session.close()

        next_a = queue_service.get_next_queued_task("proj-a")
        assert next_a.id == task_a.id

        next_b = queue_service.get_next_queued_task("proj-b")
        assert next_b.id == task_b.id
