"""Regression tests for the OpenRouter-unset CLI fallback.

Every LLMProviderInterface method (enrich_task, classify_complexity,
resolve_ticket_clarification, generate_agent_prompt, analyze_agent_trajectory
[Guardian], analyze_system_coherence [Conductor]) already degrades to a dumb
static default when _get_model_for_component returns None -- but every
configured model_assignment in hephaestus_config.yaml uses provider:
openrouter, so a missing OPENROUTER_API_KEY silently degraded ALL of them at
once. _create_model now returns a CLIFallbackChatModel instead of None in
that one case, so those callers get a real answer from the locally
authenticated CLI tool instead of a canned default. Embeddings are
unaffected -- they already default to local fastembed, no API key involved.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

pytest.importorskip("langchain_core")

from src.core.llm_config import ModelAssignment, MultiProviderLLMConfig, ProviderConfig
from src.interfaces.langchain_llm_client import (
    CLI_FALLBACK_TIMEOUT,
    CLIFallbackChatModel,
    LangChainLLMClient,
)


@pytest.fixture
def mock_config():
    return MultiProviderLLMConfig(
        embedding_model="text-embedding-3-small",
        providers={
            "openrouter": ProviderConfig(
                api_key_env="OPENROUTER_API_KEY",
                base_url="https://openrouter.ai/api/v1",
                models=["xiaomi/mimo-v2.5"],
            ),
            "openai": ProviderConfig(
                api_key_env="OPENAI_API_KEY",
                models=["gpt-4-turbo-preview"],
            ),
        },
        model_assignments={
            "task_enrichment": ModelAssignment(provider="openrouter", model="xiaomi/mimo-v2.5"),
            "agent_prompts": ModelAssignment(provider="openai", model="gpt-4-turbo-preview"),
        },
    )


class TestCreateModelFallsBackOnlyForOpenRouter:
    def test_missing_openrouter_key_returns_cli_fallback_model(self, mock_config):
        with patch.dict(os.environ, {}, clear=True):
            client = LangChainLLMClient.__new__(LangChainLLMClient)
            client.config = mock_config
            model = client._create_model(mock_config.model_assignments["task_enrichment"])
        assert isinstance(model, CLIFallbackChatModel)

    def test_missing_non_openrouter_key_still_returns_none(self, mock_config):
        """Not broadened beyond what was asked -- only openrouter's missing
        key triggers the CLI fallback; any other provider's missing key
        keeps the original "return None, caller uses its static default"
        behavior."""
        with patch.dict(os.environ, {}, clear=True):
            client = LangChainLLMClient.__new__(LangChainLLMClient)
            client.config = mock_config
            model = client._create_model(mock_config.model_assignments["agent_prompts"])
        assert model is None

    def test_present_openrouter_key_builds_real_model_not_fallback(self, mock_config):
        with (
            patch.dict(os.environ, {"OPENROUTER_API_KEY": "real-key"}),
            patch("src.interfaces.langchain_llm_client.ChatOpenAI") as mock_chat,
        ):
            client = LangChainLLMClient.__new__(LangChainLLMClient)
            client.config = mock_config
            model = client._create_model(mock_config.model_assignments["task_enrichment"])
        assert not isinstance(model, CLIFallbackChatModel)
        mock_chat.assert_called_once()


class TestCLIFallbackChatModelAinvoke:
    def _fake_proc(self, returncode=0, stdout=b"the answer", stderr=b""):
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        proc.returncode = returncode
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        return proc

    @pytest.mark.asyncio
    async def test_success_returns_stdout_as_content(self):
        model = CLIFallbackChatModel("claude", "sonnet")
        proc = self._fake_proc(stdout=b"  {\"ok\": true}  ")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as create:
            resp = await model.ainvoke([Mock(type="system", content="be terse"), Mock(type="human", content="hi")])
        assert resp.content == '{"ok": true}'
        assert resp.response_metadata == {}
        args = create.call_args.args
        assert args[:4] == ("claude", "-p", "--model", "sonnet")
        assert "--dangerously-skip-permissions" in args

    @pytest.mark.asyncio
    async def test_runs_outside_this_repos_own_cwd(self):
        """The backend runs from HephaestusNG's own repo root -- without an
        explicit cwd, the subprocess would inherit it and Claude Code would
        auto-load THIS repo's project-level CLAUDE.md (commit policy, SOLID
        review conventions, output style) into what's supposed to be a
        generic completion for an arbitrary managed project."""
        import tempfile

        model = CLIFallbackChatModel("claude", "sonnet")
        proc = self._fake_proc()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)) as create:
            await model.ainvoke([Mock(type="human", content="hi")])
        cwd = create.call_args.kwargs.get("cwd")
        assert cwd == tempfile.gettempdir()
        assert cwd != os.getcwd()

    @pytest.mark.asyncio
    async def test_prompt_sent_via_stdin_not_argv(self):
        """Large prompts (accumulated Guardian context, task history) must
        not go through argv -- OS ARG_MAX and shell-escaping risk."""
        model = CLIFallbackChatModel("claude", "sonnet")
        proc = self._fake_proc()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            await model.ainvoke([Mock(type="system", content="sys"), Mock(type="human", content="the actual prompt")])
        sent = proc.communicate.call_args.args[0]
        assert b"the actual prompt" in sent

    @pytest.mark.asyncio
    async def test_nonzero_exit_raises_with_stderr(self):
        model = CLIFallbackChatModel("claude", "sonnet")
        proc = self._fake_proc(returncode=1, stderr=b"auth failed")
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with pytest.raises(RuntimeError, match="auth failed"):
                await model.ainvoke([Mock(type="human", content="hi")])

    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_raises(self):
        model = CLIFallbackChatModel("claude", "sonnet")
        proc = MagicMock()
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        proc.kill = MagicMock()
        proc.wait = AsyncMock()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            with pytest.raises(RuntimeError, match=f"timed out after {CLI_FALLBACK_TIMEOUT}s"):
                await model.ainvoke([Mock(type="human", content="hi")])
        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsupported_cli_tool_raises_not_implemented(self):
        model = CLIFallbackChatModel("pi", "some-model")
        with pytest.raises(NotImplementedError, match="pi"):
            await model.ainvoke([Mock(type="human", content="hi")])
