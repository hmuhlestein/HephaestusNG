"""Regression coverage for TicketService._check_circular_blocking (Phase 3
Tier 2 item 16, docs/AUTOPILOT_REFACTOR_PLAN.md).

The original check only compared the ticket being updated against each
direct candidate blocker's own blocked_by_ticket_ids -- a one-hop check
that caught A<->B but missed any longer chain (A->B->C->A), since the
code's own docstring already admitted.
"""

from datetime import datetime

import pytest

from src.core.database import DatabaseManager, Ticket
from src.services.ticket_service import TicketService


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(str(tmp_path / "test.db"))
    manager.create_tables()
    return manager


def _make_ticket(session, ticket_id, blocked_by_ticket_ids=None):
    session.add(
        Ticket(
            id=ticket_id,
            workflow_id="wf-1",
            created_by_agent_id="agent-1",
            title=ticket_id,
            description="d",
            ticket_type="task",
            priority="medium",
            status="open",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            blocked_by_ticket_ids=blocked_by_ticket_ids or [],
        )
    )


def test_direct_two_hop_cycle_still_detected(db):
    """A blocks B, B (already) blocks A -- the original check's own
    covered case, must keep working."""
    session = db.get_session()
    _make_ticket(session, "A")
    _make_ticket(session, "B", blocked_by_ticket_ids=["A"])
    session.commit()

    with pytest.raises(ValueError, match="Circular blocking detected"):
        TicketService._check_circular_blocking("A", ["B"], session)


def test_three_hop_chain_cycle_is_detected(db):
    """A -> B -> C -> A: a cycle the old pairwise (direct-neighbor-only)
    check could not see, since neither B nor C directly blocks A by
    themselves -- only the full chain closes the loop."""
    session = db.get_session()
    _make_ticket(session, "A")
    _make_ticket(session, "B", blocked_by_ticket_ids=["C"])
    _make_ticket(session, "C", blocked_by_ticket_ids=["A"])
    session.commit()

    # Proposing that A become blocked_by B closes A -> B -> C -> A.
    with pytest.raises(ValueError, match="Circular blocking detected"):
        TicketService._check_circular_blocking("A", ["B"], session)


def test_non_cyclic_chain_is_accepted(db):
    """A -> B -> C, no cycle -- must not raise."""
    session = db.get_session()
    _make_ticket(session, "A")
    _make_ticket(session, "B")
    _make_ticket(session, "C", blocked_by_ticket_ids=["B"])
    session.commit()

    # Should not raise.
    TicketService._check_circular_blocking("A", ["C"], session)


def test_preexisting_cycle_elsewhere_does_not_infinite_loop(db):
    """A pre-existing cycle unrelated to the ticket being checked (X<->Y)
    must not hang the BFS via its own visited-set guard."""
    session = db.get_session()
    _make_ticket(session, "X", blocked_by_ticket_ids=["Y"])
    _make_ticket(session, "Y", blocked_by_ticket_ids=["X"])
    _make_ticket(session, "A")
    session.commit()

    # A's own proposed dependency (on X) is not itself cyclic for A --
    # X/Y's pre-existing cycle is unrelated and must not raise or hang.
    TicketService._check_circular_blocking("A", ["X"], session)
