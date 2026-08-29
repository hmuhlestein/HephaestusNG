"""Regression: LLMConfig.validate() used to hard-fail startup (raising
ValueError, which run_server.py turns into sys.exit(1)) whenever
OPENROUTER_API_KEY was unset and llm_provider == "openrouter" -- even
though langchain_llm_client.py's _create_model already has a graceful
fallback for exactly this case: it degrades to CLIFallbackChatModel
(drives the configured CLI tool directly) instead of calling OpenRouter.
That runtime fallback exists specifically so the system can run fully
API-key-free with the CLI itself acting as arbitrator/guardian/etc., but
this startup check was stricter than it and crashed the server before
_create_model ever got a chance to use it -- the graceful path existed
but was unreachable. Observed live: `heph restart` failed outright with
"OPENROUTER_API_KEY is required" after the key was intentionally
commented out of .env to force CLI-fallback mode.

openai/anthropic have no such runtime fallback, so their missing-key
checks must keep hard-failing exactly as before.
"""

from src.core.simple_config import LLMConfig


def _llm_config(provider: str) -> LLMConfig:
    cfg = LLMConfig({"llm": {"default_provider": provider}})
    cfg.openai_api_key = None
    cfg.anthropic_api_key = None
    cfg.openrouter_api_key = None
    return cfg


class TestOpenRouterMissingKeyFallsBackInsteadOfFailing:
    def test_validate_does_not_raise_when_openrouter_key_missing(self):
        cfg = _llm_config("openrouter")

        assert cfg.validate() is True

    def test_validate_logs_info_when_openrouter_key_missing(self, caplog):
        """Informational, not a warning -- the CLI fallback handles this
        case, so it isn't something gone wrong."""
        import logging

        cfg = _llm_config("openrouter")

        with caplog.at_level(logging.INFO, logger="src.core.simple_config"):
            cfg.validate()

        assert "OPENROUTER_API_KEY not set" in caplog.text
        assert "CLI tool" in caplog.text

    def test_validate_passes_cleanly_when_openrouter_key_present(self):
        cfg = _llm_config("openrouter")
        cfg.openrouter_api_key = "sk-or-v1-present"

        assert cfg.validate() is True


class TestOtherProvidersStillHardFailWithNoFallback:
    def test_openai_without_key_still_raises(self):
        cfg = _llm_config("openai")

        try:
            cfg.validate()
            assert False, "expected ValueError"
        except ValueError as e:
            assert "OPENAI_API_KEY" in str(e)

    def test_anthropic_without_key_still_raises(self):
        cfg = _llm_config("anthropic")

        try:
            cfg.validate()
            assert False, "expected ValueError"
        except ValueError as e:
            assert "ANTHROPIC_API_KEY" in str(e)
