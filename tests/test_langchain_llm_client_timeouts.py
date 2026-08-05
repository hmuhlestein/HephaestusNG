"""Regression tests for Conductor LLM-call timeouts in LangChainLLMClient.

analyze_system_coherence runs inside MonitoringLoop's single shared cycle
(via conductor.py). Before this fix, it didn't bound its model.ainvoke()
call with a timeout -- a slow/over-streaming model (mimo can stream a
reasoning trace for minutes and still fail to parse) could block the call
indefinitely, freezing the entire monitoring loop's heartbeat and every
agent's auto-recovery, not just this one call. Observed live:
monitor_heartbeat stopped updating for 20+ minutes after
analyze_system_coherence hung on its final retry attempt. Each call must
bound every attempt with asyncio.wait_for and fall back on timeout, same
as Guardian's GUARDIAN_LLM_TIMEOUT (guardian.py).
"""

import asyncio
import os
from unittest.mock import Mock, patch

import pytest

pytest.importorskip("langchain_core")

from src.core.llm_config import (
    ModelAssignment,
    MultiProviderLLMConfig,
    ProviderConfig,
)
from src.interfaces.langchain_llm_client import LangChainLLMClient


@pytest.fixture
def mock_config():
    return MultiProviderLLMConfig(
        embedding_model="text-embedding-3-small",
        providers={
            "openrouter": ProviderConfig(
                api_key_env="OPENROUTER_API_KEY",
                base_url="https://openrouter.ai/api/v1",
                models=["xiaomi/mimo-v2.5-pro"],
            ),
        },
        model_assignments={
            "conductor_analysis": ModelAssignment(
                provider="openrouter",
                model="xiaomi/mimo-v2.5-pro",
                temperature=0.3,
                max_tokens=2000,
            ),
        },
    )


@pytest.fixture
def client(mock_config):
    with (
        patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}),
        patch("src.interfaces.langchain_llm_client.ChatOpenAI"),
    ):
        return LangChainLLMClient(mock_config)


async def _hang(*args, **kwargs):
    await asyncio.sleep(10)


class TestConductorTimeouts:
    @pytest.mark.asyncio
    async def test_analyze_system_coherence_times_out_instead_of_hanging(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(
            "src.interfaces.langchain_llm_client.CONDUCTOR_LLM_TIMEOUT", 0.05
        )
        hanging_model = Mock()
        hanging_model.ainvoke = _hang
        client._get_model_for_component = Mock(return_value=hanging_model)

        result = await asyncio.wait_for(
            client.analyze_system_coherence(guardian_summaries=[], system_goals={}),
            timeout=5,
        )

        assert result == client._default_coherence_analysis()
