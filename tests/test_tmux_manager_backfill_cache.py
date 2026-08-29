"""Tests for TmuxSessionManager's _transcript_backfill_cache eviction.

Adversarial-review BLOCKER: _transcript_backfill_cache (mirroring
src/agents/output_capture.py's _live_backfill_cache -- see
tests/test_agent_output_capture.py::test_terminated_agent_evicts_live_backfill_cache_entry)
is a manager-lifetime dict keyed by session_name with no eviction. Once a
session is killed its backfill entry is never read again, so kill_session
must evict it.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_TMUX_VIEWER_BACKEND = Path(__file__).resolve().parents[1] / "tools" / "tmux-viewer" / "backend"
if str(_TMUX_VIEWER_BACKEND) not in sys.path:
    sys.path.insert(0, str(_TMUX_VIEWER_BACKEND))

from tmux_manager import TmuxSessionManager  # noqa: E402


@pytest.fixture
def manager():
    mgr = TmuxSessionManager.__new__(TmuxSessionManager)
    mgr.server = MagicMock()
    mgr.session_prefix = "agent"
    mgr._transcript_backfill_cache = {}
    return mgr


def test_kill_session_evicts_backfill_cache_entry(manager):
    session_name = "agent_abc12345_r"
    manager._transcript_backfill_cache[session_name] = "stale cached transcript"

    mock_session = MagicMock()
    manager._find_session = MagicMock(return_value=mock_session)

    manager.kill_session(session_name)

    assert session_name not in manager._transcript_backfill_cache


def test_kill_session_evicts_backfill_cache_entry_even_on_kill_failure(manager):
    """A failed kill_session() call still means the caller is done with
    this session -- the cache entry must not linger just because the
    underlying tmux call raised."""
    session_name = "agent_def67890_r"
    manager._transcript_backfill_cache[session_name] = "stale cached transcript"

    mock_session = MagicMock()
    mock_session.kill_session.side_effect = RuntimeError("boom")
    manager._find_session = MagicMock(return_value=mock_session)

    manager.kill_session(session_name)

    assert session_name not in manager._transcript_backfill_cache
