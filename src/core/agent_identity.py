"""Canonical answers to "who is this agent?".

These questions gate real controls -- who may create a task without a ticket,
who may read another agent's logs -- and the answers had been re-implemented
inline at each call site, drifting as they went (SOLID review 1.19). Two of
those copies used unanchored substring tests, so any agent whose id merely
contained "main" or "sdk" passed:

    "sdk" in agent_id.lower() or "main" in agent_id.lower()

Nothing exploited that, because real ids are UUID4 hex -- neither "main" nor
"sdk" is spellable in [0-9a-f] -- or names like `orchestrator-*`. A future
named agent would have been the problem, silently and on the permissive side.

This module lives in src/core deliberately: src/mcp/server/_shared.py imports
src/mcp/agents_api.py, so the two files that most need these predicates cannot
import them from each other without a cycle. src/core imports nothing from
src/mcp, so this is cycle-free by construction.
"""

#: The root session agent. Exempt from controls that apply to spawned agents,
#: and permitted to act on agents other than itself.
ROOT_AGENT_ID = "main-session-agent"

#: Prefix identifying an agent started by the SDK.
SDK_AGENT_PREFIX = "sdk-"

#: Prefix identifying an agent acting through the MCP tool surface.
MCP_AGENT_PREFIX = "mcp-"

#: Named agents that are part of the system itself rather than spawned work.
KNOWN_SYSTEM_AGENTS = {
    ROOT_AGENT_ID,
    "sdk-agent",
    "system",
    "ui-user",
    "sdk-repair-agent",
    "orchestrator",
    "monitor",
    "pi-extension",
}


def is_root_agent(agent_id: str) -> bool:
    """Whether `agent_id` is the root session agent.

    Anchored equality, not a substring test: `domain-expert` and
    `maintenance-agent` both contain "main" and must not qualify.
    """
    return agent_id == ROOT_AGENT_ID


def is_sdk_or_root_agent(agent_id: str) -> bool:
    """Whether `agent_id` is the root session agent or an SDK-started one.

    Deliberately excludes the mcp- prefix: MCP agents are exactly the callers
    the ticket-tracking requirement exists to constrain.
    """
    return is_root_agent(agent_id) or agent_id.startswith(SDK_AGENT_PREFIX)


def is_known_system_identity(agent_id: str) -> bool:
    """Whether `agent_id` is a system agent or carries an SDK/MCP prefix --
    i.e. an identity the system issues rather than a spawned work agent.

    Broader than is_sdk_or_root_agent: this one includes mcp- agents, and is
    the "does this identity need a registered Agent row?" question rather than
    the "may this identity skip a control?" one.
    """
    return agent_id in KNOWN_SYSTEM_AGENTS or agent_id.startswith(
        (SDK_AGENT_PREFIX, MCP_AGENT_PREFIX)
    )
