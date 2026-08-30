"""Factory for creating LLM providers."""

from __future__ import annotations

from typing import Any

from utopia_agent.llm.base import LLMProvider


def create_llm_provider(config: dict[str, Any]) -> LLMProvider:
    """Create an LLM provider from configuration.

    Supports: openai, anthropic, ollama, groq, custom (any OpenAI-compatible).
    """
    provider_name = config.get("provider", "openai")

    match provider_name:
        case "openai":
            from utopia_agent.llm.openai_provider import OpenAIProvider

            return OpenAIProvider(
                model=config.get("model", "gpt-4o"),
                api_key=config.get("api_key", ""),
            )
        case "anthropic":
            from utopia_agent.llm.anthropic_provider import AnthropicProvider

            return AnthropicProvider(
                model=config.get("model", "claude-sonnet-4-20250514"),
                api_key=config.get("api_key", ""),
            )
        case "ollama":
            from utopia_agent.llm.ollama_provider import OllamaProvider

            return OllamaProvider(
                model=config.get("model", "llama3.1"),
                base_url=config.get("base_url", "http://localhost:11434"),
            )
        case "groq":
            from utopia_agent.llm.groq_provider import GroqProvider

            return GroqProvider(
                model=config.get("model", "llama-3.1-70b-versatile"),
                api_key=config.get("api_key", ""),
            )
        case "custom":
            from utopia_agent.llm.custom_provider import CustomProvider

            return CustomProvider(
                model=config.get("model", "default"),
                api_key=config.get("api_key", "not-needed"),
                base_url=config.get("base_url", ""),
            )
        case _:
            raise ValueError(
                f"Unknown provider: '{provider_name}'. "
                f"Supported: openai, anthropic, ollama, groq, custom"
            )
