"""Regex patterns and constants shared by monitor.py and mechanical_recovery.py.

Extracted from monitor.py (SOLID review: agents/monitoring "new findings" --
MechanicalRecoveryDetector reached back into monitor.py via a dynamic
`getattr(monitor_module, name)` lookup to dodge a circular import, since
monitor.py imports MechanicalRecoveryDetector inside __init__. That made
the "extracted collaborator" secretly depend on its own orchestrator
through a back-channel invisible to static analysis, grep, and IDE
navigation -- the opposite of the inversion the decomposition was meant to
produce. Both files now import these names normally from here instead.

monitor.py itself never referenced any of these beyond defining them --
they exist purely for mechanical_recovery.py's detectors -- so this list
is exactly the "shared" subset. MAX_STUCK_TASK_NUDGES stayed in monitor.py:
it's only used there, never by mechanical_recovery.py.
"""

import re

_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# Matches pi's "⚠️ Dangerous command:" confirmation screen and captures the
# command it's asking about. Deliberately anchored to this exact prompt
# shape (not a generic "does the screen contain the word rm" check) to
# avoid false-positive matches on an agent's own reasoning text that merely
# mentions "rm" or "dangerous".
_DANGEROUS_CMD_RE = re.compile(
    r"Dangerous command:\s*\n\s*(\S[^\n]*)", re.IGNORECASE
)

# pi's exact error text when the underlying model hits its per-turn output
# token ceiling mid-generation. Anchored to this specific string (not a
# generic "did generation fail" check) so this detector never fires on an
# agent's own reasoning text that happens to discuss token limits.
_MAX_TOKEN_LIMIT_RE = re.compile(
    r"Error: Model stopped because it reached the maximum output token limit",
    re.IGNORECASE,
)

# Claude session limit detection -- "You've hit your session limit" or similar
# messages that indicate the CLI agent can't actually do work. Anchored to
# that confirmed exact phrase (not the bare fragment "You've hit", which is
# generic enough to risk matching an agent's own reasoning text or an echoed
# task prompt) -- same reasoning as AgentManager._send_initial_prompt_with_retry's
# equivalent check.
_SESSION_LIMIT_RE = re.compile(
    r"(?:you've hit your session limit|session limit|rate limit|too many requests)",
    re.IGNORECASE,
)

# Claude Code's exact message when the account/org hits its configured
# monthly spend cap -- the agent cannot make any more API calls until a
# human raises the limit or the billing period resets. Same failure class
# as a session limit (hard blocker, not recoverable by retrying), so it
# gets identical handling below: fail the task, terminate the agent, and
# pause the workflow only if the phase has no fallback_cli_tool -- a
# configured fallback should get a chance to run instead of sitting paused.
#
# Claude sometimes shows the message as text, other times as an interactive
# menu: "What do you want to do? 1. Stop and wait for limit to reset 2."
# Both patterns are matched here.
_SPEND_LIMIT_RE = re.compile(
    r"(?:you've hit your (?:monthly|weekly) (?:spend )?limit|stop and wait for limit to reset)",
    re.IGNORECASE,
)

# pi's status-line MCP indicator, e.g. "MCP: 0/1 servers". The denominator
# group excludes "0/0" (no servers configured at all -- not a failure) by
# requiring at least one digit that isn't a leading zero. Only observable

# Context overflow: local model hit its context size limit. This is a hard
# blocker for the current model — the agent can't continue without switching
# to a model with a larger context window. Should trigger model fallback.
_CONTEXT_OVERFLOW_RE = re.compile(
    r"(?:exceed_context_size_error|exceeds the available context size)",
    re.IGNORECASE,
)
# via AgentManager.get_agent_output -- get_agent_output returns raw output
# as TUI chrome for every other caller.
_MCP_DISCONNECTED_RE = re.compile(r"MCP:\s*0/[1-9]\d*\s*servers", re.IGNORECASE)

# LLM connection errors that indicate the agent can't reach the API
_CONNECTION_ERROR_RE = re.compile(r"(?:Error:\s*(?:Connection error|Request timed out)|Retry failed after \d+ attempts:\s*Connection error)", re.IGNORECASE)

# OpenRouter's exact 402 error phrasing when a key's credits/weekly limit
# can't cover the requested max_tokens. Anchored to this specific phrase
# (not a generic "credit"/"402" keyword match) to avoid false positives on
# an agent's own reasoning text that happens to discuss billing or HTTP
# codes -- same care check_api_credits already takes for the same reason.
_CREDIT_EXHAUSTED_RE = re.compile(
    r"requires more credits, or fewer max_tokens", re.IGNORECASE
)

# Claude Code's exact rejection when launched with a --model string it
# doesn't recognize (e.g. a stale OpenRouter path baked into a Phase row
# from before default_cli_tool/cli_model changed). This is a hard stop --
# no amount of "just try again" recovers it, and unlike the MCP-disconnect
# case, the agent CANNOT self-remediate: /model is a client-side slash
# command Claude Code's input loop intercepts before it ever reaches the
# model, so no tool call or generated response can invoke it -- only
# literal keystrokes typed into the pane (which is exactly what
# send_message_to_agent does) can.
_BAD_MODEL_ERROR_RE = re.compile(
    r"issue with the selected model", re.IGNORECASE
)

# Matches a complete_my_task/update_task_status tool-call rendering with a
# terminal status (done/failed), e.g. Claude Code's
# "complete_my_task (MCP)(status: "done", ...)". Deliberately requires
# BOTH the tool name and a quoted done/failed status close together (not
# just the tool name alone, which would also match a self-review's
# in_progress re-check) -- see _detect_unconfirmed_task_completion, which
# uses this to catch a completion call that rendered as sent in the
# agent's own transcript but never actually reached the server (dropped
# connection, backend restart mid-call).
#
# DOTALL, not [^\n]-bounded: different CLIs (pi, Codex, ...) pretty-print
# a tool call's arguments across multiple lines rather than Claude Code's
# single-line rendering -- anchoring to one exact layout would silently
# miss every other CLI. This project defaults to Claude Code today, but
# the phase config can select any CLIAgentInterface subclass per-phase.
_COMPLETION_ATTEMPT_RE = re.compile(
    r"(?:complete_my_task|update_task_status).{0,300}?status[\"']?\s*[:=]\s*[\"']?(done|failed)[\"']?",
    re.IGNORECASE | re.DOTALL,
)

def _strip_sgr(text: str) -> str:
    """Strip SGR color escape codes (\\x1b[...m).

    AgentManager._read_transcript_log deliberately KEEPS these when it
    strips other ANSI, since other callers display output to a human and
    want color preserved. Any detector here that compares tmux output
    content across polls (frozen-signature check, repetition-loop line
    counting) must strip them first -- a TUI that re-emits color codes on
    every redraw otherwise makes two reads of identical visible content
    differ byte-for-byte, silently defeating the comparison every time.
    """
    return _SGR_RE.sub("", text)


# How many times _detect_cli_model_fallback/_verify_cli_model_fallback will
# retry an unconfirmed model switch for the same agent before giving up for
# good. Observed live: with no cap, an agent that kept refreezing retried an
# unconfirmed switch 40+ times over 7+ hours -- each retry blindly resent the
# same keystrokes into whatever state the CLI was actually in (the "revert on
# unconfirmed" only patches our own DB record, it never undoes anything in
# the live session), and one of those retries landed on a different, unusable
# catalog entry that broke the session outright.
MAX_FALLBACK_ATTEMPTS = 2


