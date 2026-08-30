"""Custom OpenAI-compatible LLM provider.

Works with ANY endpoint that follows the OpenAI chat completions API:
- Together AI
- OpenRouter
- Fireworks AI
- vLLM
- LocalAI
- LM Studio
- Text Generation WebUI
- Any OpenAI-compatible server
"""

from __future__ import annotations

from typing import Any

from utopia_agent.llm.openai_provider import OpenAIProvider


class CustomProvider(OpenAIProvider):
    """Generic OpenAI-compatible provider for any custom endpoint.

    Set UTOPIA_CUSTOM_BASE_URL and UTOPIA_CUSTOM_API_KEY env vars,
    or pass them directly when configuring.
    """

    def __init__(
        self,
        model: str = "default",
        api_key: str = "",
        base_url: str = "",
        **kwargs: Any,
    ) -> None:
        if not base_url:
            raise ValueError(
                "Custom provider requires a base_url. "
                "Examples: https://api.together.xyz/v1, http://localhost:1234/v1"
            )
        super().__init__(
            model=model,
            api_key=api_key or "not-needed",
            base_url=base_url,
            **kwargs,
        )
