"""Groq LLM provider — ultra-fast inference via Groq Cloud."""

from __future__ import annotations

from typing import Any

from utopia_agent.llm.openai_provider import OpenAIProvider


class GroqProvider(OpenAIProvider):
    """Groq provider. Uses OpenAI-compatible API with Groq endpoints."""

    def __init__(
        self, model: str = "llama-3.1-70b-versatile", api_key: str = "", **kwargs: Any
    ) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=kwargs.get("base_url", "https://api.groq.com/openai/v1"),
            **kwargs,
        )
