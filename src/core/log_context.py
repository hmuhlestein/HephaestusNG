"""Structured log context filter.

Usage:
    from src.core.log_context import log_context, set_log_context

    # Set context for a scope (e.g. at the start of a request handler)
    set_log_context(project="proj-540", task="be10213e", agent="8d8e6b9f")

    # Or use as a context manager
    with log_context(workflow="2b4ce9e4", phase="development"):
        logger.info("This will include workflow and phase in the prefix")

    # Clear when done
    clear_log_context()
"""

import logging
from contextvars import ContextVar
from typing import Optional

# Context variables - these are per-task (asyncio) or per-thread
_ctx_project: ContextVar[str] = ContextVar("ctx_project", default="")
_ctx_workflow: ContextVar[str] = ContextVar("ctx_workflow", default="")
_ctx_phase: ContextVar[str] = ContextVar("ctx_phase", default="")
_ctx_task: ContextVar[str] = ContextVar("ctx_task", default="")
_ctx_agent: ContextVar[str] = ContextVar("ctx_agent", default="")


def _octet(val: str) -> str:
    """Return first 8 chars of a UUID-like string, or the full string if shorter."""
    if not val:
        return ""
    return val[:8]


def set_log_context(
    *,
    project: Optional[str] = None,
    workflow: Optional[str] = None,
    phase: Optional[str] = None,
    task: Optional[str] = None,
    agent: Optional[str] = None,
) -> None:
    """Set structured log context for the current execution scope."""
    if project is not None:
        _ctx_project.set(project)
    if workflow is not None:
        _ctx_workflow.set(workflow)
    if phase is not None:
        _ctx_phase.set(phase)
    if task is not None:
        _ctx_task.set(task)
    if agent is not None:
        _ctx_agent.set(agent)


def clear_log_context() -> None:
    """Clear all log context for the current execution scope."""
    _ctx_project.set("")
    _ctx_workflow.set("")
    _ctx_phase.set("")
    _ctx_task.set("")
    _ctx_agent.set("")


def get_log_prefix() -> str:
    """Build the structured prefix string from current context.

    Returns e.g. \"[proj-540|wf-2b4ce9|dev|t-be1021|a-8d8e6b]\" or "" if no context.
    """
    parts = []
    proj = _ctx_project.get()
    if proj:
        parts.append(_octet(proj))
    wf = _ctx_workflow.get()
    if wf:
        parts.append(_octet(wf))
    phase = _ctx_phase.get()
    if phase:
        parts.append(phase[:16] if len(phase) > 16 else phase)
    task = _ctx_task.get()
    if task:
        parts.append(_octet(task))
    agent = _ctx_agent.get()
    if agent:
        parts.append(_octet(agent))
    if not parts:
        return ""
    return "[" + "|".join(parts) + "] "


class StructuredContextFilter(logging.Filter):
    """Logging filter that injects structured context into log records.

    Adds a `ctx_prefix` attribute to each record, which the format string
    can reference as %(ctx_prefix)s.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx_prefix = get_log_prefix()
        return True


class ContextFormatter(logging.Formatter):
    """Formatter that prepends the structured context prefix to messages.

    Works with any existing format string — the prefix is prepended to
    the message itself, so no format string changes are needed.
    """

    def format(self, record: logging.LogRecord) -> str:
        prefix = getattr(record, "ctx_prefix", "")
        if prefix:
            record.msg = f"{prefix}{record.msg}"
            record.args = record.args  # no-op, but keeps the record consistent
        return super().format(record)