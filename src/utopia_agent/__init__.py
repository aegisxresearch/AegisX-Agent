"""Utopia Agent — Super-powered Agentic AI with custom LLM support."""

__version__ = "0.1.0"

from utopia_agent.core import UtopiaAgent
from utopia_agent.config import AgentConfig
from utopia_agent.agent_loop import AgenticLoop, AgentTrace

__all__ = ["UtopiaAgent", "AgentConfig", "AgenticLoop", "AgentTrace"]
