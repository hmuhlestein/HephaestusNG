"""Coverage for LangChainLLMClient's provider dispatch (SOLID review 4.8).

_create_model was a ~134-line if/elif over 5 providers and _initialize_models
a parallel ~76-line chain over 4 embedding providers; both are now registry
lookups (_MODEL_BUILDERS / _EMBEDDING_BUILDERS). Only openai/groq/openrouter
had any prior coverage, and none of it pinned the per-provider construction
arguments -- so a branch could have been altered during the move without any
test noticing. These tests pin each builder's contract and the dispatch's
failure modes.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from src.interfaces.langchain_llm_client import (
    _EMBEDDING_BUILDERS,
    _MODEL_BUILDERS,
    LangChainLLMClient,
    ModelAssignment,
)


def _provider(api_key_env="TEST_KEY", base_url=None, api_version=None):
    cfg = MagicMock()
    cfg.api_key_env = api_key_env
    cfg.base_url = base_url
    cfg.api_version = api_version
    return cfg


def _assignment(provider, model="m1", temperature=0.4, max_tokens=1000, **extra):
    a = ModelAssignment(
        provider=provider,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        **extra,
    )
    return a


def _client_with(provider_name, provider_config):
    """A client whose _initialize_models is bypassed, so _create_model can be
    exercised in isolation."""
    config = MagicMock()
    config.providers = {provider_name: provider_config}
    with patch.object(LangChainLLMClient, "_initialize_models"):
        client = LangChainLLMClient(config)
    return client


class TestRegistryCompleteness:
    def test_every_documented_provider_has_a_builder(self):
        assert set(_MODEL_BUILDERS) == {
            "openai",
            "groq",
            "openrouter",
            "azure_openai",
            "google_ai",
        }

    def test_chat_only_providers_share_the_fastembed_embedding_builder(self):
        """openrouter/local/fastembed have no embeddings API of their own."""
        shared = {_EMBEDDING_BUILDERS[k] for k in ("fastembed", "local", "openrouter")}
        assert len(shared) == 1


class TestCreateModelDispatch:
    def test_unknown_provider_returns_none(self):
        client = _client_with("mystery", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}):
            assert client._create_model(_assignment("mystery")) is None

    def test_unconfigured_provider_returns_none(self):
        client = _client_with("openai", _provider())
        assert client._create_model(_assignment("groq")) is None

    def test_missing_api_key_returns_none(self):
        client = _client_with("openai", _provider(api_key_env="ABSENT_KEY"))
        with patch.dict(os.environ, {}, clear=True):
            assert client._create_model(_assignment("openai")) is None

    def test_builder_exception_is_swallowed_into_none(self):
        """A provider package raising during construction must not propagate
        out of _create_model -- initialization continues without that model."""
        client = _client_with("openai", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatOpenAI",
            side_effect=RuntimeError("boom"),
        ):
            assert client._create_model(_assignment("openai")) is None


class TestPerProviderConstruction:
    def test_openai_passes_configured_temperature(self):
        client = _client_with("openai", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatOpenAI"
        ) as Chat:
            client._create_model(_assignment("openai", model="gpt-4o", temperature=0.4))
        assert Chat.call_args.kwargs["temperature"] == 0.4

    def test_gpt5_is_pinned_to_temperature_one(self):
        """GPT-5 rejects any temperature other than 1.0."""
        client = _client_with("openai", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatOpenAI"
        ) as Chat:
            client._create_model(
                _assignment("openai", model="gpt-5-nano", temperature=0.4)
            )
        assert Chat.call_args.kwargs["temperature"] == 1.0

    def test_groq_uses_the_groq_class(self):
        client = _client_with("groq", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatGroq"
        ) as Groq:
            client._create_model(_assignment("groq"))
        assert Groq.call_args.kwargs["groq_api_key"] == "k"

    def test_openrouter_forces_single_provider_routing_and_requests_usage(self):
        client = _client_with(
            "openrouter", _provider(base_url="https://openrouter.ai/api/v1")
        )
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatOpenAI"
        ) as Chat:
            client._create_model(
                _assignment("openrouter", openrouter_provider="cerebras")
            )
        extra = Chat.call_args.kwargs["model_kwargs"]["extra_body"]
        assert extra["provider"] == {"order": ["Cerebras"], "allow_fallbacks": False}
        # Cost tracking depends on usage being returned in the response.
        assert extra["usage"] == {"include": True}

    def test_openrouter_reasoning_off_disables_rather_than_setting_effort(self):
        client = _client_with("openrouter", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatOpenAI"
        ) as Chat:
            client._create_model(_assignment("openrouter", reasoning_effort="off"))
        extra = Chat.call_args.kwargs["model_kwargs"]["extra_body"]
        assert extra["reasoning"] == {"enabled": False}

    def test_openrouter_reasoning_effort_is_lowercased(self):
        client = _client_with("openrouter", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatOpenAI"
        ) as Chat:
            client._create_model(_assignment("openrouter", reasoning_effort="HIGH"))
        extra = Chat.call_args.kwargs["model_kwargs"]["extra_body"]
        assert extra["reasoning"] == {"effort": "high"}

    def test_azure_without_endpoint_returns_none(self):
        """Azure needs a deployment endpoint; without one there is nothing to
        construct, and it must fail closed rather than build a broken client."""
        client = _client_with("azure_openai", _provider(base_url=None))
        with patch.dict(os.environ, {"TEST_KEY": "k"}):
            assert client._create_model(_assignment("azure_openai")) is None

    def test_azure_uses_model_as_deployment_name(self):
        client = _client_with(
            "azure_openai", _provider(base_url="https://x.openai.azure.com")
        )
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.AzureChatOpenAI"
        ) as Azure:
            client._create_model(_assignment("azure_openai", model="my-deployment"))
        kwargs = Azure.call_args.kwargs
        assert kwargs["azure_deployment"] == "my-deployment"
        assert kwargs["api_version"] == "2024-02-01"  # default when unset

    def test_google_ai_uses_the_gemini_class(self):
        client = _client_with("google_ai", _provider())
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.ChatGoogleGenerativeAI"
        ) as Gemini:
            client._create_model(_assignment("google_ai", model="gemini-2.5-flash"))
        assert Gemini.call_args.kwargs["model"] == "gemini-2.5-flash"


class TestEmbeddingDispatch:
    def _config(self, embedding_provider, providers):
        config = MagicMock()
        config.embedding_provider = embedding_provider
        config.embedding_model = "text-embedding-3-small"
        config.providers = providers
        config.model_assignments = {}
        return config

    def test_openai_embeddings_selected(self):
        config = self._config("openai", {"openai": _provider()})
        with patch.dict(os.environ, {"TEST_KEY": "k"}), patch(
            "src.interfaces.langchain_llm_client.OpenAIEmbeddings"
        ) as Emb:
            client = LangChainLLMClient(config)
        Emb.assert_called_once_with(
            model="text-embedding-3-small", openai_api_key="k"
        )
        assert client._embedding_model is Emb.return_value

    @pytest.mark.parametrize("provider", ["openrouter", "local", "fastembed"])
    def test_chat_only_providers_fall_back_to_fastembed(self, provider):
        config = self._config(provider, {})
        fake = MagicMock()
        with patch.dict(
            "sys.modules",
            {"langchain_community.embeddings": MagicMock(FastEmbedEmbeddings=fake)},
        ):
            client = LangChainLLMClient(config)
        assert client._embedding_model is fake.return_value

    def test_unknown_embedding_provider_leaves_model_unset(self):
        config = self._config("nonsense", {})
        client = LangChainLLMClient(config)
        assert client._embedding_model is None

    def test_azure_embeddings_incomplete_config_leaves_model_unset(self):
        config = self._config("azure_openai", {"azure_openai": _provider(base_url=None)})
        with patch.dict(os.environ, {"TEST_KEY": "k"}):
            client = LangChainLLMClient(config)
        assert client._embedding_model is None
