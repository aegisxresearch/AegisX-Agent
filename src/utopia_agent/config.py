"""Configuration for Utopia Agent — supports ANY LLM provider."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    GROQ = "groq"
    CUSTOM = "custom"  # Any OpenAI-compatible endpoint


class AgentConfig(BaseSettings):
    """Main configuration for Utopia Agent."""

    model_config = SettingsConfigDict(
        env_prefix="UTOPIA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # === LLM Provider settings ===
    llm_provider: LLMProvider = Field(
        default=LLMProvider.OPENAI,
        description="LLM provider: openai, anthropic, ollama, groq, custom",
    )

    # OpenAI
    openai_api_key: str = Field(default="", description="OpenAI API key")
    openai_model: str = Field(default="gpt-4o", description="OpenAI model")

    # Anthropic
    anthropic_api_key: str = Field(default="", description="Anthropic API key")
    anthropic_model: str = Field(default="claude-sonnet-4-20250514", description="Anthropic model")

    # Ollama (local)
    ollama_base_url: str = Field(default="http://localhost:11434", description="Ollama base URL")
    ollama_model: str = Field(default="llama3.1", description="Ollama model")

    # Groq (fast inference)
    groq_api_key: str = Field(default="", description="Groq API key")
    groq_model: str = Field(default="llama-3.1-70b-versatile", description="Groq model")

    # Custom (ANY OpenAI-compatible endpoint)
    custom_base_url: str = Field(
        default="",
        description="Custom endpoint URL (e.g. https://api.together.xyz/v1)",
    )
    custom_api_key: str = Field(default="", description="Custom endpoint API key")
    custom_model: str = Field(default="default", description="Custom model name")

    # === Agent settings ===
    max_iterations: int = Field(default=15, description="Max tool-calling iterations")
    temperature: float = Field(default=0.7, description="LLM temperature")
    max_tokens: int = Field(default=4096, description="Max tokens per response")
    system_prompt: str = Field(default="", description="Custom system prompt override")
    persona: str = Field(default="default", description="Predefined persona name")

    # === Memory settings ===
    memory_enabled: bool = Field(default=True, description="Enable conversation memory")
    memory_max_messages: int = Field(default=50, description="Max messages in context")

    # === RAG settings ===
    rag_enabled: bool = Field(default=True, description="Enable RAG")
    rag_chunk_size: int = Field(default=512, description="Chunk size for RAG")
    rag_chunk_overlap: int = Field(default=50, description="Chunk overlap for RAG")
    vector_store_path: str = Field(default="~/.utopia/vectorstore", description="Vector store path")

    # === Tool settings ===
    code_execution_enabled: bool = Field(default=True, description="Enable code execution")
    web_search_enabled: bool = Field(default=True, description="Enable web search")
    shell_enabled: bool = Field(default=False, description="Enable shell commands")
    file_operations_enabled: bool = Field(default=True, description="Enable file operations")
    api_call_enabled: bool = Field(default=True, description="Enable API caller")
    db_query_enabled: bool = Field(default=True, description="Enable database queries")
    web_scraper_enabled: bool = Field(default=True, description="Enable web scraping")

    # === Paths ===
    data_dir: str = Field(default="~/.utopia", description="Data directory")
    personas_dir: str = Field(default="~/.utopia/personas", description="Personas directory")

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def personas_path(self) -> Path:
        return Path(self.personas_dir).expanduser()

    @property
    def vector_store_path_resolved(self) -> Path:
        return Path(self.vector_store_path).expanduser()

    def get_llm_config(self) -> dict[str, Any]:
        """Get LLM config for the current provider."""
        match self.llm_provider:
            case LLMProvider.OPENAI:
                return {"provider": "openai", "api_key": self.openai_api_key, "model": self.openai_model}
            case LLMProvider.ANTHROPIC:
                return {
                    "provider": "anthropic",
                    "api_key": self.anthropic_api_key,
                    "model": self.anthropic_model,
                }
            case LLMProvider.OLLAMA:
                return {
                    "provider": "ollama",
                    "base_url": self.ollama_base_url,
                    "model": self.ollama_model,
                }
            case LLMProvider.GROQ:
                return {"provider": "groq", "api_key": self.groq_api_key, "model": self.groq_model}
            case LLMProvider.CUSTOM:
                return {
                    "provider": "custom",
                    "base_url": self.custom_base_url,
                    "api_key": self.custom_api_key,
                    "model": self.custom_model,
                }


# === Default Personas ===
DEFAULT_PERSONAS: dict[str, str] = {
    "default": (
        "You are Utopia, a super-powered AI agent. You have access to various tools "
        "including code execution, web search, file operations, and more. "
        "You are helpful, knowledgeable, and always aim to provide the best possible answers. "
        "Think step by step, use tools when needed, and be thorough in your responses."
    ),
    "coder": (
        "You are Utopia Coder, an expert software engineer. You write clean, efficient, "
        "and well-documented code. You follow best practices and design patterns. "
        "When asked to write code, you provide production-quality implementations. "
        "You can debug, refactor, and optimize code across any language or framework."
    ),
    "researcher": (
        "You are Utopia Researcher, a meticulous research assistant. You gather information "
        "from multiple sources, cross-reference facts, and present well-structured findings. "
        "You cite sources, distinguish between confirmed facts and speculation, "
        "and always acknowledge uncertainty when it exists."
    ),
    "analyst": (
        "You are Utopia Analyst, a data and systems analyst. You break down complex problems, "
        "analyze patterns, and provide actionable insights. You think critically, "
        "consider multiple perspectives, and support your conclusions with evidence."
    ),
    "creative": (
        "You are Utopia Creative, an imaginative and expressive AI. You help with writing, "
        "brainstorming, and creative projects. You bring fresh perspectives and "
        "think outside the box while maintaining quality and coherence."
    ),
    "hacker": (
        "You are Utopia Hacker, a security-minded AI assistant. You understand cybersecurity, "
        "penetration testing, and ethical hacking. You help identify vulnerabilities, "
        "write security tools, and educate about defensive security practices."
    ),
    "scientist": (
        "You are Utopia Scientist, a rigorous scientific assistant. You apply the scientific "
        "method, analyze data critically, and present findings with proper methodology. "
        "You help with research design, statistical analysis, and literature review."
    ),
}
