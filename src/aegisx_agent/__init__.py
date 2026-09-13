"""AegisX Agent — Super-powered Agentic AI with custom LLM support."""

__version__ = "0.1.0"

from aegisx_agent.core import AegisXAgent
from aegisx_agent.config import AgentConfig
from aegisx_agent.agent_loop import AgenticLoop, AgentTrace

__all__ = ["AegisXAgent", "AgentConfig", "AgenticLoop", "AgentTrace"]
