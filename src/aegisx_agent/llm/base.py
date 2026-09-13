"""Base LLM abstractions for AegisX Agent."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    """Message roles."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass
class Message:
    """A message in the conversation."""

    role: Role
    content: str
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ToolCall:
    """Represents a tool call from the LLM."""

    id: str
    name: str
    arguments: str

    @classmethod
    def from_openai(cls, call: dict[str, Any]) -> ToolCall:
        return cls(
            id=call["id"],
            name=call["function"]["name"],
            arguments=call["function"]["arguments"],
        )

    @classmethod
    def from_anthropic(cls, call: dict[str, Any]) -> ToolCall:
        return cls(
            id=call.get("id", ""),
            name=call["name"],
            arguments=normalise_tool_arguments(call.get("input", {})),
        )


def normalise_tool_arguments(raw: Any) -> str:
    """Return tool-call arguments as a JSON string.

    Providers disagree on the wire format: OpenAI and Anthropic send an object
    for ``arguments``/``input`` while some local runtimes send an already
    encoded string. Both are normalised to a JSON string so the registry can
    parse them consistently.
    """
    if isinstance(raw, str):
        return raw
    if raw is None:
        return "{}"
    return json.dumps(raw)


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    def __init__(self, model: str, **kwargs: Any) -> None:
        self.model = model
        self.config = kwargs

    @abstractmethod
    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """Send a chat completion request."""
        ...

    @abstractmethod
    async def stream_chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ):
        """Stream a chat completion response."""
        ...
