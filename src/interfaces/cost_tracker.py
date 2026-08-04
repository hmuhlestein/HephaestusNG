"""DEPRECATED: Cost tracking via LiteLLM proxy.

This module has been replaced by src/core/cost_derivation.py and
src/services/cost_collection_service.py which provide direct cost
collection from CLI tools (pi, Claude Code, OpenCode) and OpenRouter.

The CostTracker class queried a LiteLLM proxy's spend endpoints, but
this project talks to OpenRouter directly — no proxy sits in front of it.
This file is dead code and will be removed in a future cleanup.

See docs/COST_TRACKING_DESIGN.md for the current architecture.
"""

import logging

logger = logging.getLogger(__name__)

# Module kept for backward compatibility during transition
# All actual cost tracking logic is in src/core/cost_derivation.py
