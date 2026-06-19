"""Custom OpenRouter client that properly handles provider routing.

Supports optional LiteLLM proxy routing with cost tracking via the 'user' field.
When litellm_proxy_url is set, requests are routed through the LiteLLM proxy
instead of directly to OpenRouter, enabling per-feature cost tracking.
"""

import logging
from typing import Dict, Any, List, Optional
import httpx

logger = logging.getLogger(__name__)


class OpenRouterClient:
    """Direct OpenRouter API client with provider routing support.

    Supports two modes:
    1. Direct OpenRouter (default): base_url = https://openrouter.ai/api/v1
    2. LiteLLM Proxy: base_url = http://deneb-server:4000 (for cost tracking)
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 4000,
        provider_name: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
        litellm_proxy_url: Optional[str] = None,
        litellm_api_key: Optional[str] = None,
        litellm_user: Optional[str] = None,
    ):
        """Initialize OpenRouter client.

        Args:
            api_key: OpenRouter API key
            model: Model to use (e.g., "openai/gpt-oss-120b")
            temperature: Temperature for generation
            max_tokens: Maximum tokens for response
            provider_name: Optional provider to route to (e.g., "Cerebras")
            base_url: OpenRouter API base URL
            litellm_proxy_url: Optional LiteLLM proxy URL (e.g., "http://deneb-server:4000")
            litellm_api_key: Optional virtual key for LiteLLM proxy
            litellm_user: Optional user identifier for cost tracking (e.g., feature name)
        """
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.provider_name = provider_name
        self.litellm_proxy_url = litellm_proxy_url
        self.litellm_api_key = litellm_api_key
        self.litellm_user = litellm_user

        # Use LiteLLM proxy if configured, otherwise direct OpenRouter
        if litellm_proxy_url:
            self.base_url = litellm_proxy_url.rstrip("/")
            logger.info(f"Routing through LiteLLM proxy: {litellm_proxy_url}")
            if litellm_user:
                logger.info(f"Cost tracking enabled for user: {litellm_user}")
        else:
            self.base_url = base_url

        if provider_name:
            logger.info(f"OpenRouter configured to route to {provider_name}")

    def set_user(self, user: str):
        """Set the user identifier for cost tracking.

        Args:
            user: User/feature name for LiteLLM cost tracking
        """
        self.litellm_user = user
        if self.litellm_proxy_url:
            logger.info(f"Cost tracking user set to: {user}")

    async def generate(
        self,
        messages: List[Dict[str, str]],
        response_format: Optional[str] = None
    ) -> Dict[str, Any]:
        """Generate a response from OpenRouter or LiteLLM proxy.

        Args:
            messages: List of message dicts with role and content
            response_format: Optional response format ("json_object" for JSON)

        Returns:
            Response from API with content, provider, usage, and cost info
        """
        # Determine which API key to use
        if self.litellm_proxy_url and self.litellm_api_key:
            api_key = self.litellm_api_key
        else:
            api_key = self.api_key

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # Add OpenRouter-specific headers when not using proxy
        if not self.litellm_proxy_url:
            headers["HTTP-Referer"] = "https://github.com/Ido-Levi/Hephaestus"
            headers["X-Title"] = "Hephaestus - Semi Structured Agentic Framework"

        data = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        # Add user field for LiteLLM cost tracking
        if self.litellm_user and self.litellm_proxy_url:
            data["user"] = self.litellm_user

        # Add provider routing if configured (OpenRouter only)
        if self.provider_name and not self.litellm_proxy_url:
            data["provider"] = {"only": [self.provider_name]}

        # Add response format if specified
        if response_format == "json_object":
            data["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=data,
                )

                if response.status_code == 200:
                    result = response.json()

                    # Extract cost from LiteLLM response headers
                    cost = None
                    if self.litellm_proxy_url:
                        cost_header = response.headers.get("x-litellm-response-cost")
                        if cost_header:
                            try:
                                cost = float(cost_header)
                            except (ValueError, TypeError):
                                pass

                    return {
                        "content": result["choices"][0]["message"]["content"],
                        "provider": result.get("provider", "unknown"),
                        "usage": result.get("usage", {}),
                        "cost": cost,
                        "user": self.litellm_user,
                    }
                else:
                    error_msg = f"API error: {response.status_code} - {response.text[:200]}"
                    logger.error(error_msg)
                    raise Exception(error_msg)

            except httpx.TimeoutException:
                error_msg = "Request timed out"
                logger.error(error_msg)
                raise Exception(error_msg)
            except Exception as e:
                logger.error(f"Request failed: {e}")
                raise

    def get_model_name(self) -> str:
        """Get the model name with provider info."""
        if self.provider_name and not self.litellm_proxy_url:
            return f"{self.model} (via {self.provider_name.lower()})"
        return self.model
