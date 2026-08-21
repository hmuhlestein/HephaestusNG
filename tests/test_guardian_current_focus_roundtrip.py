"""SOLID review 3.7: current_focus and last_claude_message_marker are
written to GuardianAnalysis rows but must also round-trip back out for
the next monitoring cycle to see them -- a real DatabaseManager, not a
mocked session, since get_past_summaries_for_agent's GuardianAnalysis-row
branch had zero test coverage before this (only the AgentLog fallback
branch was ever exercised, with a mocked session that can't catch a
missing/misspelled attribute read).
"""

from datetime import datetime, timedelta

import pytest

from src.core.database import Agent, DatabaseManager, GuardianAnalysis
from src.monitoring.guardian import Guardian
from src.monitoring.guardian_dispatch import GuardianDispatcher


@pytest.fixture
def db_manager(tmp_path):
    db_path = tmp_path / "test.db"
    db = DatabaseManager(str(db_path))
    db.create_tables()
    return db


@pytest.fixture
def dispatcher(db_manager):
    return GuardianDispatcher(
        db_manager=db_manager,
        agent_manager=None,
        config=None,
        guardian=None,
        phase_manager=None,
        auto_restart=None,
        guardian_summaries_cache={},
    )


def test_get_past_summaries_includes_current_focus_and_marker(db_manager, dispatcher):
    with db_manager.session_scope() as session:
        session.add(Agent(id="agent-1", system_prompt="test", cli_type="claude", status="working"))
        session.add(
            GuardianAnalysis(
                agent_id="agent-1",
                timestamp=datetime.utcnow() - timedelta(minutes=5),
                current_phase="implementation",
                trajectory_summary="Working on the login endpoint",
                current_focus="Implementing the POST /login endpoint",
                last_claude_message_marker="wrote the handler",
            )
        )
        session.add(
            GuardianAnalysis(
                agent_id="agent-1",
                timestamp=datetime.utcnow(),
                current_phase="verification",
                trajectory_summary="Running the test suite",
                current_focus="Fixing a failing auth test",
                last_claude_message_marker="test now passes",
            )
        )

    summaries = dispatcher.get_past_summaries_for_agent("agent-1")

    assert len(summaries) == 2
    # Chronological order -- [-1] is the most recent, matching
    # guardian.py's own past_summaries[-1] reads.
    assert summaries[-1]["current_focus"] == "Fixing a failing auth test"
    assert summaries[-1]["last_claude_message_marker"] == "test now passes"
    assert summaries[0]["current_focus"] == "Implementing the POST /login endpoint"


@pytest.mark.asyncio
async def test_build_accumulated_context_carries_forward_current_focus(db_manager):
    guardian = Guardian(
        db_manager=db_manager,
        agent_manager=None,
        llm_provider=None,
    )
    with db_manager.session_scope() as session:
        session.add(Agent(id="agent-1", system_prompt="test", cli_type="claude", status="working"))

    past_summaries = [
        {"current_focus": "Implementing the POST /login endpoint"},
        {"current_focus": "Fixing a failing auth test"},
    ]

    with db_manager.session_scope() as session:
        agent = session.query(Agent).filter_by(id="agent-1").first()
        context = await guardian._build_accumulated_context(agent, past_summaries)

    # Takes the LAST summary's current_focus, not the first.
    assert context["current_focus"] == "Fixing a failing auth test"


@pytest.mark.asyncio
async def test_build_accumulated_context_defaults_current_focus_on_first_cycle(db_manager):
    guardian = Guardian(
        db_manager=db_manager,
        agent_manager=None,
        llm_provider=None,
    )
    with db_manager.session_scope() as session:
        session.add(Agent(id="agent-1", system_prompt="test", cli_type="claude", status="working"))

    with db_manager.session_scope() as session:
        agent = session.query(Agent).filter_by(id="agent-1").first()
        context = await guardian._build_accumulated_context(agent, [])

    assert context["current_focus"] == "Unknown"
