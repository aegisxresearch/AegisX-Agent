"""Configuration for AegisX Agent — supports ANY LLM provider."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from aegisx_agent.security.permissions import PermissionMode
from aegisx_agent.tools.subagent import SubagentProgress


def _split_tool_list(value: str) -> list[str]:
    """Parse a comma-separated env var into tool names.

    ``AEGISX_ALLOWED_TOOLS=shell,execute_code`` is friendlier to type than the
    JSON array pydantic-settings would demand for a ``list[str]`` field.
    """
    return [name.strip() for name in value.split(",") if name.strip()]


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    GROQ = "groq"
    CUSTOM = "custom"  # Any OpenAI-compatible endpoint


class AgentConfig(BaseSettings):
    """Main configuration for AegisX Agent."""

    model_config = SettingsConfigDict(
        env_prefix="AEGISX_",
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

    # Turn budget: graceful stop once one turn consumes this many tokens.
    # 0 or negative disables the guard.
    max_tokens_per_turn: int = Field(
        default=0,
        description="Token budget per turn (AEGISX_MAX_TOKENS_PER_TURN); 0 disables",
    )

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
    # "auto" forces a tool call on the first iteration only for task-shaped
    # requests; "always"/"never" override that judgement.
    force_tools: str = Field(
        default="auto",
        description="Require a tool call on the first iteration: auto|always|never",
    )
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
    vector_store_path: str = Field(default="~/.aegisx/vectorstore", description="Vector store path")

    # === Tool settings ===
    code_execution_enabled: bool = Field(default=True, description="Enable code execution")
    web_search_enabled: bool = Field(default=True, description="Enable web search")
    shell_enabled: bool = Field(default=False, description="Enable shell commands")
    file_operations_enabled: bool = Field(default=True, description="Enable file operations")
    api_call_enabled: bool = Field(default=True, description="Enable API caller")
    db_query_enabled: bool = Field(default=True, description="Enable database queries")
    web_scraper_enabled: bool = Field(default=True, description="Enable web scraping")

    # === Workspace settings ===
    project_context_enabled: bool = Field(
        default=True,
        description="Tell the agent where it is running (cwd, stack, git, AGENTS.md)",
    )

    # === Subagent delegation ===
    subagent_enabled: bool = Field(
        default=True, description="Enable the spawn_subagent delegation tool"
    )
    subagent_max_steps: int = Field(
        default=8, description="Step budget (max iterations) per subagent run"
    )
    subagent_max_depth: int = Field(
        default=2, description="How many generations of nested subagents are allowed"
    )
    subagent_timeout: float = Field(
        default=120.0, description="Wall-clock timeout per subagent run, in seconds"
    )
    subagent_progress: SubagentProgress = Field(
        default=SubagentProgress.STEPS,
        description=(
            "Subagent telemetry level: 'quiet' (silent), 'steps' (start, "
            "per-call, outcome lines), or 'verbose' (also the delegation id "
            "on the outcome line)"
        ),
    )

    # === Safety settings ===
    permission_mode: PermissionMode = Field(
        default=PermissionMode.ASK,
        description="Tool permission mode: allow-all, ask, or read-only",
    )
    allowed_tools: str = Field(
        default="",
        description=(
            "Comma-separated tool names that always run without asking "
            "(e.g. 'execute_code,run_tests')"
        ),
    )
    denied_tools: str = Field(
        default="",
        description="Comma-separated tool names that are always refused",
    )
    audit_log_enabled: bool = Field(
        default=True,
        description="Append every tool decision to <data_dir>/audit.log",
    )

    # === Paths ===
    data_dir: str = Field(default="~/.aegisx", description="Data directory")
    personas_dir: str = Field(default="~/.aegisx/personas", description="Personas directory")

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser()

    @property
    def personas_path(self) -> Path:
        return Path(self.personas_dir).expanduser()

    @property
    def vector_store_path_resolved(self) -> Path:
        return Path(self.vector_store_path).expanduser()

    @property
    def allowed_tool_names(self) -> list[str]:
        """Tools that bypass the permission prompt entirely."""
        return _split_tool_list(self.allowed_tools)

    @property
    def denied_tool_names(self) -> list[str]:
        """Tools that are refused before their risk is even considered."""
        return _split_tool_list(self.denied_tools)

    @property
    def audit_log_path(self) -> Path:
        return self.data_path / "audit.log"

    def get_llm_config(self) -> dict[str, Any]:
        """Get LLM config for the current provider."""
        match self.llm_provider:
            case LLMProvider.OPENAI:
                return {
                    "provider": "openai",
                    "api_key": self.openai_api_key,
                    "model": self.openai_model,
                }
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


def missing_credentials(config: AgentConfig) -> str:
    """Name of the provider that still needs credentials, or ``''`` when ready.

    Single source of truth for "can this config talk to a model", shared by the
    agent (which raises a setup error) and the CLI (which tries to autodetect a
    local server before bothering the user).
    """
    llm = config.get_llm_config()
    provider = str(llm.get("provider", ""))
    if provider in ("ollama", "custom"):
        return "" if str(llm.get("base_url", "")).strip() else provider
    return "" if str(llm.get("api_key", "")).strip() else provider


# === Default Personas ===
DEFAULT_PERSONAS: dict[str, str] = {
    "default": (
        "You are AegisX, a super-powered AI agent. You have access to various tools "
        "including code execution, web search, file operations, and more. "
        "You are helpful, knowledgeable, and always aim to provide the best possible answers. "
        "Think step by step, use tools when needed, and be thorough in your responses."
    ),
    "coder": (
        "You are AegisX Coder, an expert software engineer. You write clean, efficient, "
        "and well-documented code. You follow best practices and design patterns. "
        "When asked to write code, you provide production-quality implementations. "
        "You can debug, refactor, and optimize code across any language or framework."
    ),
    "researcher": (
        "You are AegisX Researcher, a meticulous research assistant. You gather information "
        "from multiple sources, cross-reference facts, and present well-structured findings. "
        "You cite sources, distinguish between confirmed facts and speculation, "
        "and always acknowledge uncertainty when it exists."
    ),
    "analyst": (
        "You are AegisX Analyst, a data and systems analyst. You break down complex problems, "
        "analyze patterns, and provide actionable insights. You think critically, "
        "consider multiple perspectives, and support your conclusions with evidence."
    ),
    "creative": (
        "You are AegisX Creative, an imaginative and expressive AI. You help with writing, "
        "brainstorming, and creative projects. You bring fresh perspectives and "
        "think outside the box while maintaining quality and coherence."
    ),
    "hacker": (
        "You are AegisX Hacker, a security-minded AI assistant. You understand cybersecurity, "
        "penetration testing, and ethical hacking. You help identify vulnerabilities, "
        "write security tools, and educate about defensive security practices."
    ),
    "scientist": (
        "You are AegisX Scientist, a rigorous scientific assistant. You apply the scientific "
        "method, analyze data critically, and present findings with proper methodology. "
        "You help with research design, statistical analysis, and literature review."
    ),
}
