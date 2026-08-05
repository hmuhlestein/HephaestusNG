"""DEPRECATED: OpenRouter client wrapper.

This module has been replaced by direct OpenRouter integration in
src/interfaces/langchain_llm_client.py which uses the _invoke_and_record
helper to capture cost data from OpenRouter responses.

This file is dead code and will be removed in a future cleanup.

See docs/COST_TRACKING_DESIGN.md for the current architecture.
"""

import logging

logger = logging.getLogger(__name__)

# Module kept for backward compatibility during transition
# All actual OpenRouter interaction is in src/interfaces/langchain_llm_client.py
