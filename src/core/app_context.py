"""Process-wide accessor for the running server's shared service singleton.

`src/mcp/server.py`'s `ServerState` (the DB manager, agent manager, RAG
system, LLM provider, queue service, etc.) is the composition root for
nearly everything in the app, but it lived only in the FastAPI route
module — so every other module that needed it (services, routers,
orchestrator) resorted to a lazy, function-body-local
`from src.mcp.server import server_state` to dodge the resulting circular
import. That workaround is itself a Dependency Inversion smell: low-level
services shouldn't need to import the top-level web server module just to
reach shared infrastructure (see docs/SOLID_OO_REVIEW.md findings 1.6/3.11).

This module breaks the cycle: `server.py` registers its `ServerState`
instance here once at import time, and every other consumer imports
`get_app_state()` from here instead of reaching into `src.mcp.server`.
"""

from typing import Any, Optional

_app_state: Optional[Any] = None


def set_app_state(state: Any) -> None:
    """Register the shared ServerState instance. Called once by server.py."""
    global _app_state
    _app_state = state


def get_app_state() -> Any:
    """Return the shared ServerState instance.

    Raises if called before server.py has registered it — a clear error
    here beats an AttributeError on whatever attribute access came next.
    """
    if _app_state is None:
        raise RuntimeError(
            "App state not initialized — set_app_state() must be called "
            "(src.mcp.server does this at import time) before this can be used."
        )
    return _app_state
