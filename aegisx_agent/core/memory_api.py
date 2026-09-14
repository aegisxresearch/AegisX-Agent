"""Public memory operations exposed by :class:`AegisXAgent`."""

from __future__ import annotations

from typing import Any

from aegisx_agent.memory.advanced import PromptMemory, SessionStore, UserModel
from aegisx_agent.memory.store import ConversationMemory, LongTermMemory


class MemoryAPI:
    """Mixin containing long-term, conversation, and session memory APIs."""

    long_term: LongTermMemory
    conversation: ConversationMemory
    session_store: SessionStore
    user_model: UserModel
    prompt_memory: PromptMemory

    def remember(self, category: str, fact: str) -> None:
        """Store a fact in long-term memory."""
        self.long_term.store(category, fact)

    def recall(self, query: str) -> list[dict[str, str]]:
        """Search long-term memory."""
        return self.long_term.search(query)

    def clear_memory(self) -> None:
        """Clear conversation history."""
        self.conversation.clear()

    def search_sessions(self, query: str) -> list[dict[str, Any]]:
        """Search past sessions."""
        return self.session_store.search(query)

    def get_session_stats(self) -> dict[str, Any]:
        """Get session statistics."""
        return self.session_store.get_stats()

    def learn_preference(self, key: str, value: str) -> None:
        """Teach the agent a preference."""
        self.user_model.learn_preference(key, value)
        self.prompt_memory.add_user_info(f"{key}: {value}")
