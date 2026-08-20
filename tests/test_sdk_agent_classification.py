"""The ticket-tracking exemption must not be handed out by substring match.

_enforce_ticket_tracking_requirement decides who may create a task without a
ticket_id while ticket tracking is enabled. It answered "is this an SDK/root
agent?" with:

    agent_id == "main-session-agent"
    or "sdk" in agent_id.lower()
    or "main" in agent_id.lower()

an unanchored substring test that exempts far more than the docstring beside
it claims -- any agent whose id merely contains "main" (domain-expert,
maintenance-agent) skipped the requirement entirely.

Nothing hit it in practice: real ids are UUID4 hex, which cannot spell "main"
or "sdk" in [0-9a-f], or names like orchestrator-*. It was the next named
agent that would have been the problem. Replaced with one shared, anchored
definition, is_sdk_or_root_agent.
"""

import pytest

from src.core.agent_identity import (
    KNOWN_SYSTEM_AGENTS,
    ROOT_AGENT_ID,
    is_known_system_identity,
    is_root_agent,
    is_sdk_or_root_agent,
)


def _legacy_substring_check(agent_id: str) -> bool:
    """The implementation being replaced, kept so the tightening can be
    asserted as a property rather than a list of hand-picked examples."""
    return (
        agent_id == "main-session-agent"
        or "sdk" in agent_id.lower()
        or "main" in agent_id.lower()
    )


class TestExemptAgents:
    def test_root_session_agent_is_exempt(self):
        assert is_sdk_or_root_agent(ROOT_AGENT_ID)

    @pytest.mark.parametrize("agent_id", ["sdk-agent", "sdk-repair-agent", "sdk-x"])
    def test_sdk_prefixed_agents_are_exempt(self, agent_id):
        assert is_sdk_or_root_agent(agent_id)


class TestNotExempt:
    @pytest.mark.parametrize(
        "agent_id",
        [
            "domain-expert",  # contains "main"
            "maintenance-agent",  # contains "main"
            "my-sdk-helper",  # contains "sdk", not prefixed
            "DOMAIN-EXPERT",  # the old check lowercased first
        ],
    )
    def test_substring_matches_no_longer_grant_the_exemption(self, agent_id):
        assert _legacy_substring_check(agent_id), "precondition: old check exempted it"
        assert not is_sdk_or_root_agent(agent_id)

    @pytest.mark.parametrize(
        "agent_id",
        [
            "78769107-7533-45e4-91e6-6c645073e588",
            "orchestrator-8e7ef1c5",
            "mcp-claude",
            "monitor",
        ],
    )
    def test_ordinary_agents_are_not_exempt(self, agent_id):
        assert not is_sdk_or_root_agent(agent_id)

    def test_mcp_agents_are_not_exempt(self):
        """The rejection message states MCP agents must supply a ticket_id, so
        the mcp- prefix must not be treated as SDK."""
        assert not is_sdk_or_root_agent("mcp-claude")


def test_the_change_is_strictly_a_tightening():
    """No identity that the old check exempted may newly be rejected unless it
    matched only by substring -- i.e. every real system agent keeps its
    previous answer."""
    for agent_id in KNOWN_SYSTEM_AGENTS:
        if _legacy_substring_check(agent_id):
            assert is_sdk_or_root_agent(agent_id), agent_id


def test_no_uuid_can_accidentally_match():
    """Why this was latent rather than live: UUID4 hex uses [0-9a-f-], and
    neither "main" nor "sdk" is spellable in that alphabet."""
    import uuid

    for _ in range(200):
        assert not is_sdk_or_root_agent(str(uuid.uuid4()))


class TestCrossAgentAuthorization:
    """agents_api.py's six 403 guards read "may this caller act on an agent
    other than itself?" and answered it with `"main" not in
    requesting_agent_id.lower()` -- so domain-expert or maintenance-agent
    could read any agent's children, logs, and monitoring, and message or
    nudge them. Same substring defect as the ticket-tracking exemption, on
    authorization rather than a process control.
    """

    def _legacy_cross_agent_check(self, requesting_agent_id: str) -> bool:
        return "main" in requesting_agent_id.lower()

    def test_root_agent_may_act_on_others(self):
        assert is_root_agent(ROOT_AGENT_ID)

    @pytest.mark.parametrize("agent_id", ["domain-expert", "maintenance-agent"])
    def test_substring_lookalikes_may_not(self, agent_id):
        assert self._legacy_cross_agent_check(agent_id), "precondition"
        assert not is_root_agent(agent_id)

    @pytest.mark.parametrize(
        "agent_id", ["orchestrator", "ui-user", "sdk-agent", "monitor"]
    )
    def test_other_system_agents_are_not_granted_cross_agent_access(self, agent_id):
        """Deliberately narrower than is_sdk_or_root_agent: these endpoints
        only ever exempted the root agent, so reusing the SDK predicate here
        would have widened access rather than tightened it."""
        assert not is_root_agent(agent_id)


class TestKnownSystemIdentity:
    """project_routes.py inlined `agent_id not in KNOWN_SYSTEM_AGENTS and not
    agent_id.startswith(("sdk-", "mcp-"))`. Correctly anchored already, so
    this consolidation must be exactly equivalent, not a tightening."""

    @pytest.mark.parametrize("agent_id", sorted(KNOWN_SYSTEM_AGENTS))
    def test_all_known_system_agents_qualify(self, agent_id):
        assert is_known_system_identity(agent_id)

    @pytest.mark.parametrize("agent_id", ["sdk-x", "mcp-claude"])
    def test_sdk_and_mcp_prefixes_qualify(self, agent_id):
        assert is_known_system_identity(agent_id)

    @pytest.mark.parametrize(
        "agent_id", ["78769107-7533-45e4-91e6-6c645073e588", "domain-expert"]
    )
    def test_ordinary_agents_do_not(self, agent_id):
        assert not is_known_system_identity(agent_id)

    def test_mcp_agents_qualify_here_but_not_for_the_ticket_exemption(self):
        """The two predicates answer different questions and must not be
        collapsed: mcp- agents are system identities, but they are exactly
        who the ticket-tracking requirement exists to constrain."""
        assert is_known_system_identity("mcp-claude")
        assert not is_sdk_or_root_agent("mcp-claude")
