"""AegisX Agent — autonomous pentest and bug-hunting agent."""

__version__ = "0.1.0"

from aegisx_agent.core import AegisXAgent
from aegisx_agent.core.config import AgentConfig
from aegisx_agent.core.loop import AgenticLoop, AgentTrace

__all__ = ["AegisXAgent", "AgentConfig", "AgenticLoop", "AgentTrace"]
