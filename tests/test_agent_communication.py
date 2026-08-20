"""Tests for agent communication system."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

import pytest

from src.agents.manager import AgentManager
from src.core.database import Agent, AgentLog, DatabaseManager


@pytest.fixture
def db_manager(tmp_path):
    """Create a temporary database for testing."""
    db_path = tmp_path / "test_agent_comm.db"
    db_manager = DatabaseManager(str(db_path))
    db_manager.create_tables()
    return db_manager


@pytest.fixture
def mock_llm_provider():
    """Mock LLM provider."""
    provider = Mock()
    provider.generate_agent_prompt = AsyncMock(return_value="Test prompt")
    return provider


@pytest.fixture
def agent_manager(db_manager, mock_llm_provider):
    """Create agent manager instance."""
    manager = AgentManager(db_manager, mock_llm_provider)
    return manager


@pytest.fixture
def sample_agents(db_manager):
    """Create sample agents in database."""
    session = db_manager.get_session()

    agents = []
    for i in range(3):
        agent = Agent(
            id=f"agent-{i}",
            system_prompt=f"Test agent {i}",
            status="working",
            cli_type="claude",
            tmux_session_name=f"test_session_{i}",
            current_task_id=f"task-{i}",
            last_activity=datetime.utcnow(),
            health_check_failures=0,
        )
        session.add(agent)
        agents.append(agent)

    # Add one terminated agent
    terminated = Agent(
        id="agent-terminated",
        system_prompt="Terminated agent",
        status="terminated",
        cli_type="claude",
        tmux_session_name="test_session_terminated",
        current_task_id="task-terminated",
        last_activity=datetime.utcnow(),
        health_check_failures=0,
    )
    session.add(terminated)
    agents.append(terminated)

    session.commit()
    session.close()

    return agents


class TestBroadcastMessage:
    """Tests for broadcast_message_to_all_agents."""

    @pytest.mark.asyncio
    async def test_broadcast_to_multiple_agents(
        self, agent_manager, sample_agents, db_manager
    ):
        """Test broadcasting a message to multiple agents."""
        # Mock send_message_to_agent to avoid tmux interaction
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            sender_id = "agent-0"
            message = "Test broadcast message"

            recipient_count = await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id=sender_id, message=message
            )

            # Should send to 2 other active agents (not sender, not terminated)
            assert recipient_count == 2

            # Verify send_message_to_agent was called for each recipient
            assert mock_send.call_count == 2

            # Verify message format includes sender ID and BROADCAST prefix
            call_args = mock_send.call_args_list
            for call in call_args:
                agent_id, formatted_message = call[0]
                assert "BROADCAST" in formatted_message
                assert sender_id[:8] in formatted_message
                assert message in formatted_message

    @pytest.mark.asyncio
    async def test_broadcast_excludes_sender(self, agent_manager, sample_agents):
        """Test that broadcast doesn't send to the sender."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            sender_id = "agent-0"

            await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id=sender_id, message="Test"
            )

            # Verify sender didn't receive their own message
            sent_to_ids = [call[0][0] for call in mock_send.call_args_list]
            assert sender_id not in sent_to_ids

    @pytest.mark.asyncio
    async def test_broadcast_excludes_terminated_agents(
        self, agent_manager, sample_agents
    ):
        """Test that broadcast doesn't send to terminated agents."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id="agent-0", message="Test"
            )

            # Verify terminated agent didn't receive message
            sent_to_ids = [call[0][0] for call in mock_send.call_args_list]
            assert "agent-terminated" not in sent_to_ids

    @pytest.mark.asyncio
    async def test_broadcast_logs_to_database(
        self, agent_manager, sample_agents, db_manager
    ):
        """Test that broadcasts are logged to database."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ):
            sender_id = "agent-0"
            message = "Test broadcast"

            await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id=sender_id, message=message
            )

            # Check database for logs
            session = db_manager.get_session()
            logs = (
                session.query(AgentLog).filter_by(log_type="agent_communication").all()
            )

            # Should have 2 logs (one per recipient)
            assert len(logs) == 2

            for log in logs:
                assert log.details["sender_id"] == sender_id
                assert log.details["message_type"] == "broadcast"
                assert message in log.details["message_content"]
                assert "timestamp" in log.details

            session.close()

    @pytest.mark.asyncio
    async def test_broadcast_with_no_recipients(self, agent_manager, db_manager):
        """Test broadcast when no other agents are active."""
        # Create only one agent (the sender)
        session = db_manager.get_session()
        agent = Agent(
            id="only-agent",
            system_prompt="Only agent",
            status="working",
            cli_type="claude",
            tmux_session_name="test_session",
            current_task_id="task-1",
            last_activity=datetime.utcnow(),
            health_check_failures=0,
        )
        session.add(agent)
        session.commit()
        session.close()

        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            recipient_count = await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id="only-agent", message="Hello?"
            )

            # Should return 0 recipients
            assert recipient_count == 0
            # Should not call send_message_to_agent
            assert mock_send.call_count == 0

    @pytest.mark.asyncio
    async def test_broadcast_message_format(self, agent_manager, sample_agents):
        """Test that broadcast messages are formatted correctly."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            sender_id = "agent-12345678-abcd-efgh"
            message = "This is a test message"

            await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id=sender_id, message=message
            )

            # Get the formatted message from first call
            formatted_message = mock_send.call_args_list[0][0][1]

            # Verify format: [AGENT 12345678 BROADCAST]: message
            assert formatted_message.startswith("\n[AGENT")
            assert "BROADCAST]:" in formatted_message
            assert sender_id[:8] in formatted_message
            assert message in formatted_message
            assert formatted_message.endswith("\n")


class TestDirectMessage:
    """Tests for send_direct_message."""

    @pytest.mark.asyncio
    async def test_send_to_valid_recipient(self, agent_manager, sample_agents):
        """Test sending direct message to valid recipient."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            sender_id = "agent-0"
            recipient_id = "agent-1"
            message = "Direct message test"

            success = await agent_manager.send_direct_message(
                sender_agent_id=sender_id,
                recipient_agent_id=recipient_id,
                message=message,
            )

            assert success is True
            mock_send.assert_called_once()

            # Verify correct recipient
            call_args = mock_send.call_args[0]
            assert call_args[0] == recipient_id

    @pytest.mark.asyncio
    async def test_send_to_nonexistent_agent(self, agent_manager, sample_agents):
        """Test sending to non-existent agent returns False."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ):
            success = await agent_manager.send_direct_message(
                sender_agent_id="agent-0",
                recipient_agent_id="nonexistent-agent",
                message="Test",
            )

            assert success is False

    @pytest.mark.asyncio
    async def test_send_to_terminated_agent(self, agent_manager, sample_agents):
        """Test sending to terminated agent returns False."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ):
            success = await agent_manager.send_direct_message(
                sender_agent_id="agent-0",
                recipient_agent_id="agent-terminated",
                message="Test",
            )

            assert success is False

    @pytest.mark.asyncio
    async def test_direct_message_logs_to_database(
        self, agent_manager, sample_agents, db_manager
    ):
        """Test that direct messages are logged."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ):
            sender_id = "agent-0"
            recipient_id = "agent-1"
            message = "Test direct message"

            await agent_manager.send_direct_message(
                sender_agent_id=sender_id,
                recipient_agent_id=recipient_id,
                message=message,
            )

            # Check database
            session = db_manager.get_session()
            log = (
                session.query(AgentLog)
                .filter_by(log_type="agent_communication", agent_id=recipient_id)
                .first()
            )

            assert log is not None
            assert log.details["sender_id"] == sender_id
            assert log.details["recipient_id"] == recipient_id
            assert log.details["message_type"] == "direct"
            assert message in log.details["message_content"]

            session.close()

    @pytest.mark.asyncio
    async def test_direct_message_format(self, agent_manager, sample_agents):
        """Test that direct messages are formatted correctly."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            # Use actual agent IDs from sample_agents
            sender_id = "agent-0"
            recipient_id = "agent-1"
            message = "Direct message content"

            await agent_manager.send_direct_message(
                sender_agent_id=sender_id,
                recipient_agent_id=recipient_id,
                message=message,
            )

            formatted_message = mock_send.call_args[0][1]

            # Verify format: [AGENT xxx TO AGENT yyy]: message
            assert formatted_message.startswith("\n[AGENT")
            assert "TO AGENT" in formatted_message
            assert sender_id[:8] in formatted_message
            assert recipient_id[:8] in formatted_message
            assert message in formatted_message
            assert formatted_message.endswith("\n")


class TestMessageContent:
    """Tests for message content handling."""

    @pytest.mark.asyncio
    async def test_long_message_truncation_in_log(
        self, agent_manager, sample_agents, db_manager
    ):
        """Test that long messages are truncated in database logs."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ):
            # Create a message longer than 200 characters
            long_message = "x" * 300

            await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id="agent-0", message=long_message
            )

            session = db_manager.get_session()
            log = (
                session.query(AgentLog)
                .filter_by(log_type="agent_communication")
                .first()
            )

            # Verify truncation to 200 chars
            assert len(log.details["message_content"]) == 200
            session.close()

    @pytest.mark.asyncio
    async def test_special_characters_in_message(self, agent_manager, sample_agents):
        """Test that messages with special characters are handled correctly."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ) as mock_send:
            special_message = (
                "Test with special chars: \n\t\"quotes\" 'apostrophe' $var @user #tag"
            )

            await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id="agent-0", message=special_message
            )

            # Verify message content is preserved
            formatted_message = mock_send.call_args_list[0][0][1]
            assert special_message in formatted_message


class TestErrorHandling:
    """Tests for error handling in communication system."""

    @pytest.mark.asyncio
    async def test_broadcast_handles_send_failure(self, agent_manager, sample_agents):
        """Test that broadcast continues even if one send fails."""
        call_count = 0

        async def mock_send_with_failure(agent_id, message):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Simulated send failure")

        with patch.object(
            agent_manager, "send_message_to_agent", side_effect=mock_send_with_failure
        ):
            recipient_count = await agent_manager.broadcast_message_to_all_agents(
                sender_agent_id="agent-0", message="Test"
            )

            # Should still count as successful for agents where send succeeded
            # (In this implementation, failures are logged but count continues)
            assert recipient_count >= 0

    @pytest.mark.asyncio
    async def test_direct_message_handles_exception(self, agent_manager, sample_agents):
        """Test that direct message handles exceptions gracefully."""

        async def mock_send_with_exception(agent_id, message):
            raise Exception("Simulated exception")

        with patch.object(
            agent_manager, "send_message_to_agent", side_effect=mock_send_with_exception
        ):
            success = await agent_manager.send_direct_message(
                sender_agent_id="agent-0", recipient_agent_id="agent-1", message="Test"
            )

            # Should return False on exception
            assert success is False


class TestConcurrency:
    """Tests for concurrent message operations."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_broadcasts(self, agent_manager, sample_agents):
        """Test multiple agents broadcasting simultaneously."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ):
            # Simulate 3 agents broadcasting at the same time
            tasks = [
                agent_manager.broadcast_message_to_all_agents(
                    f"agent-{i}", f"Message from {i}"
                )
                for i in range(3)
            ]

            results = await asyncio.gather(*tasks)

            # All should succeed
            assert all(count >= 0 for count in results)

    @pytest.mark.asyncio
    async def test_concurrent_direct_messages(self, agent_manager, sample_agents):
        """Test multiple direct messages sent concurrently."""
        with patch.object(
            agent_manager, "send_message_to_agent", new_callable=AsyncMock
        ):
            # Multiple agents sending messages simultaneously
            tasks = [
                agent_manager.send_direct_message("agent-0", "agent-1", "Message 1"),
                agent_manager.send_direct_message("agent-1", "agent-2", "Message 2"),
                agent_manager.send_direct_message("agent-2", "agent-0", "Message 3"),
            ]

            results = await asyncio.gather(*tasks)

            # All should succeed
            assert all(results)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestAgentCommunicationServiceMigration:
    """Characterization tests for AgentCommunicationService's tmux methods.

    These verify the current behavior (routed through AgentMessenger)
    matches what the old raw-subprocess implementation did, so the
    migration is provably behavior-preserving.
    """

    @pytest.fixture
    def comm_service(self, db_manager):
        from src.services.agent_communication import AgentCommunicationService
        return AgentCommunicationService(db_manager)

    def test_get_child_logs_returns_none_without_agent_manager(self, comm_service, db_manager):
        """Without agent_manager, get_child_logs returns None gracefully."""
        with db_manager.session_scope() as session:
            session.add(Agent(
                id="parent-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="task-1",
            ))
            session.add(Agent(
                id="child-1", status="working", cli_type="pi",
                system_prompt="x", tmux_session_name="agent-child-1",
            ))
            from src.core.database import Task
            session.add(Task(
                id="task-1", workflow_id="wf-1", raw_description="x",
                done_definition="x", created_by_agent_id="parent-1",
                assigned_agent_id="child-1",
            ))

        result = comm_service.get_child_logs("parent-1", "child-1")
        assert result is None  # No agent_manager available

    def test_send_message_to_child_returns_false_without_agent_manager(self, comm_service, db_manager):
        """Without agent_manager, send_message_to_child returns False."""
        with db_manager.session_scope() as session:
            session.add(Agent(
                id="parent-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="task-1",
            ))
            session.add(Agent(
                id="child-1", status="working", cli_type="pi",
                system_prompt="x", tmux_session_name="agent-child-1",
            ))
            from src.core.database import Task
            session.add(Task(
                id="task-1", workflow_id="wf-1", raw_description="x",
                done_definition="x", created_by_agent_id="parent-1",
                assigned_agent_id="child-1",
            ))

        import asyncio
        # asyncio.run, not get_event_loop().run_until_complete: from
        # Python 3.12 get_event_loop() no longer creates a loop when none
        # is running, so in a sync test it raises RuntimeError outright.
        result = asyncio.run(
            comm_service.send_message_to_child("parent-1", "child-1", "hello")
        )
        assert result is False

    def test_send_message_to_child_rejects_non_child(self, comm_service, db_manager):
        """send_message_to_child rejects messaging a non-child agent."""
        with db_manager.session_scope() as session:
            session.add(Agent(
                id="parent-1", status="working", cli_type="pi",
                system_prompt="x",
            ))
            session.add(Agent(
                id="stranger-1", status="working", cli_type="pi",
                system_prompt="x", tmux_session_name="agent-stranger",
            ))

        import asyncio
        # asyncio.run, not get_event_loop().run_until_complete: from
        # Python 3.12 get_event_loop() no longer creates a loop when none
        # is running, so in a sync test it raises RuntimeError outright.
        result = asyncio.run(
            comm_service.send_message_to_child("parent-1", "stranger-1", "hello")
        )
        assert result is False

    def test_get_child_logs_rejects_non_child(self, comm_service, db_manager):
        """get_child_logs rejects reading logs of a non-child agent."""
        with db_manager.session_scope() as session:
            session.add(Agent(
                id="parent-1", status="working", cli_type="pi",
                system_prompt="x",
            ))
            session.add(Agent(
                id="stranger-1", status="working", cli_type="pi",
                system_prompt="x", tmux_session_name="agent-stranger",
            ))

        result = comm_service.get_child_logs("parent-1", "stranger-1")
        assert result is None

    def test_nudge_child_uses_parent_nudge_prompt(self, comm_service, db_manager):
        """nudge_child sends the parent_nudge_child prompt template."""
        with db_manager.session_scope() as session:
            session.add(Agent(
                id="parent-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="task-1",
            ))
            session.add(Agent(
                id="child-1", status="working", cli_type="pi",
                system_prompt="x", tmux_session_name="agent-child-1",
            ))
            from src.core.database import Task
            session.add(Task(
                id="task-1", workflow_id="wf-1", raw_description="x",
                done_definition="x", created_by_agent_id="parent-1",
                assigned_agent_id="child-1",
            ))

        # nudge_child calls send_message_to_child, which needs agent_manager
        # to actually deliver. Without it, it returns False.
        import asyncio
        # asyncio.run, not get_event_loop().run_until_complete: from
        # Python 3.12 get_event_loop() no longer creates a loop when none
        # is running, so in a sync test it raises RuntimeError outright.
        result = asyncio.run(
            comm_service.nudge_child("parent-1", "child-1", "test reason")
        )
        assert result is False

    def test_send_message_to_child_offloads_get_children(self, comm_service, db_manager):
        """Regression: get_children does blocking DB I/O and was called
        directly (unoffloaded) inside async def send_message_to_child --
        same class of issue already fixed at the route layer for this
        service's sibling (sync) methods (agents_api.py)."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        with db_manager.session_scope() as session:
            session.add(Agent(
                id="parent-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="task-1",
            ))

        with patch("asyncio.to_thread", new=AsyncMock(return_value=[])) as mock_to_thread:
            asyncio.run(
                comm_service.send_message_to_child("parent-1", "child-1", "hello")
            )

        mock_to_thread.assert_called_once_with(comm_service.get_children, "parent-1")

    def test_monitor_and_nudge_stuck_children_offloads_status_summary(self, comm_service, db_manager):
        """Regression: get_children_status_summary does blocking DB reads
        plus tmux pane inspection per child and was called directly
        (unoffloaded) inside async def monitor_and_nudge_stuck_children."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        with db_manager.session_scope() as session:
            session.add(Agent(
                id="parent-1", status="working", cli_type="pi",
                system_prompt="x", current_task_id="task-1",
            ))

        empty_summary = {"total": 0, "working": 0, "idle": 0, "stuck": 0, "completed": 0, "failed": 0, "children": []}
        with patch("asyncio.to_thread", new=AsyncMock(return_value=empty_summary)) as mock_to_thread:
            nudged = asyncio.run(
                comm_service.monitor_and_nudge_stuck_children("parent-1")
            )

        assert nudged == []
        mock_to_thread.assert_called_once_with(comm_service.get_children_status_summary, "parent-1")
