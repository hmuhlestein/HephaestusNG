"""Tests for SDK configuration."""

import os

import pytest

from src.sdk.config import HephaestusConfig


def test_config_defaults():
    """Test default configuration values."""
    # Set env vars for API keys
    os.environ["ANTHROPIC_API_KEY"] = "test-key"

    config = HephaestusConfig()

    assert config.database_path == "./hephaestus.db"
    assert config.qdrant_url == "http://localhost:6333"
    assert config.llm_provider == "anthropic"
    assert config.mcp_port == 8300
    assert config.monitoring_interval == 60

    # Cleanup
    del os.environ["ANTHROPIC_API_KEY"]


def test_config_custom_values():
    """Test custom configuration values."""
    os.environ["OPENAI_API_KEY"] = "test-key"

    config = HephaestusConfig(
        database_path="/custom/path/db.sqlite",
        qdrant_url="http://custom:6333",
        llm_provider="openai",
        llm_model="gpt-4",
        mcp_port=9000,
    )

    assert config.database_path == "/custom/path/db.sqlite"
    assert config.qdrant_url == "http://custom:6333"
    assert config.llm_provider == "openai"
    assert config.llm_model == "gpt-4"
    assert config.mcp_port == 9000

    del os.environ["OPENAI_API_KEY"]


def test_config_validation_missing_api_key_falls_back_to_cli(caplog):
    """A missing API key must not be fatal when default_cli_tool is
    "claude": CLIFallbackChatModel shells out to the claude CLI for real
    LLM calls instead, so refusing to even start the pipeline over a
    missing key here throws away a fallback every downstream caller
    already has. Warn instead of raising."""
    config = HephaestusConfig(llm_provider="openai", default_cli_tool="claude")

    config.validate()

    assert "OPENAI_API_KEY" in caplog.text
    assert "claude CLI" in caplog.text


def test_config_validation_missing_api_key_raises_without_cli_fallback():
    """CLIFallbackChatModel only implements Claude's non-interactive `-p`
    mode -- a non-claude default_cli_tool has no fallback to fall back to,
    so a missing key must still be fatal."""
    config = HephaestusConfig(llm_provider="openai", default_cli_tool="codex")

    with pytest.raises(ValueError, match="OPENAI_API_KEY must be set"):
        config.validate()


def test_config_validation_invalid_provider():
    """Test that validation fails for invalid provider."""
    config = HephaestusConfig(llm_provider="invalid_provider")

    with pytest.raises(ValueError, match="Invalid LLM provider"):
        config.validate()


def test_config_validation_invalid_port():
    """Test that validation fails for invalid port."""
    os.environ["ANTHROPIC_API_KEY"] = "test-key"

    config = HephaestusConfig(mcp_port=80)  # Port too low

    with pytest.raises(ValueError, match="Invalid MCP port"):
        config.validate()

    del os.environ["ANTHROPIC_API_KEY"]


def test_config_to_env_dict():
    """Test converting config to environment dict."""
    os.environ["ANTHROPIC_API_KEY"] = "test-anthropic-key"

    config = HephaestusConfig(
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-5-20250929",
    )

    env_dict = config.to_env_dict()

    assert env_dict["DATABASE_PATH"] == "./hephaestus.db"
    assert env_dict["QDRANT_URL"] == "http://localhost:6333"
    assert env_dict["LLM_PROVIDER"] == "anthropic"
    assert env_dict["LLM_MODEL"] == "claude-sonnet-4-5-20250929"
    assert env_dict["ANTHROPIC_API_KEY"] == "test-anthropic-key"
    assert "MCP_PORT" in env_dict

    del os.environ["ANTHROPIC_API_KEY"]


def test_config_auto_sets_model():
    """Test that default model is set based on provider."""
    os.environ["OPENAI_API_KEY"] = "test-key"

    config = HephaestusConfig(llm_provider="openai")

    assert config.llm_model == "gpt-5"

    del os.environ["OPENAI_API_KEY"]

    os.environ["ANTHROPIC_API_KEY"] = "test-key"

    config = HephaestusConfig(llm_provider="anthropic")

    assert config.llm_model == "claude-sonnet-4-5-20250929"

    del os.environ["ANTHROPIC_API_KEY"]
