"""Conversation memory store for Utopia Agent."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from utopia_agent.llm.base import Message, Role


class ConversationMemory:
    """Manages conversation history and context."""

    def __init__(self, max_messages: int = 50, persist_path: str | None = None) -> None:
        self.max_messages = max_messages
        self.persist_path = Path(persist_path).expanduser() if persist_path else None
        self._messages: list[Message] = []
        self._summary: str = ""
        self._loaded = False

        if self.persist_path:
            self._load()

    @property
    def messages(self) -> list[Message]:
        return list(self._messages)

    @property
    def summary(self) -> str:
        return self._summary

    def add(self, message: Message) -> None:
        """Add a message to memory."""
        self._messages.append(message)
        self._trim()
        self._save()

    def add_messages(self, messages: list[Message]) -> None:
        """Add multiple messages."""
        self._messages.extend(messages)
        self._trim()
        self._save()

    def get_context(self, max_tokens_approx: int = 8000) -> list[Message]:
        """Get messages that fit within approximate token budget.

        Includes summary if available, plus most recent messages.
        """
        result: list[Message] = []

        # Include summary as system context if available
        if self._summary:
            result.append(
                Message(
                    role=Role.SYSTEM,
                    content=f"[Previous conversation summary]: {self._summary}",
                )
            )

        # Add messages from most recent, fitting within budget
        # Rough estimate: 1 token per 4 characters
        char_budget = max_tokens_approx * 4
        used_chars = sum(len(m.content) for m in result)

        for msg in reversed(self._messages):
            msg_chars = len(msg.content)
            if used_chars + msg_chars > char_budget:
                break
            result.insert(-len(result) if self._summary else 0, msg)
            used_chars += msg_chars

        return result

    def clear(self) -> None:
        """Clear conversation history."""
        self._messages.clear()
        self._summary = ""
        self._save()

    def set_summary(self, summary: str) -> None:
        """Set conversation summary (for long-term memory compression)."""
        self._summary = summary
        self._save()

    def get_last_n(self, n: int = 10) -> list[Message]:
        """Get last N messages."""
        return self._messages[-n:]

    def _trim(self) -> None:
        """Trim messages to max limit, compressing old ones into summary."""
        if len(self._messages) <= self.max_messages:
            return

        # Keep the most recent messages
        excess = self._messages[: len(self._messages) - self.max_messages]
        self._messages = self._messages[len(excess) :]

        # Build summary from excess messages (simple concatenation)
        excess_text = "\n".join(
            f"{m.role.value}: {m.content[:200]}" for m in excess[-10:]
        )
        if self._summary:
            self._summary = f"{self._summary}\n\n{excess_text}"
        else:
            self._summary = excess_text

        # Truncate summary if too long
        if len(self._summary) > 5000:
            self._summary = self._summary[-5000:]

    def _save(self) -> None:
        """Persist memory to disk."""
        if not self.persist_path:
            return

        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "messages": [
                {"role": m.role.value, "content": m.content} for m in self._messages
            ],
            "summary": self._summary,
        }
        self.persist_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def _load(self) -> None:
        """Load memory from disk."""
        if not self.persist_path or not self.persist_path.exists():
            return

        try:
            data = json.loads(self.persist_path.read_text())
            self._messages = [
                Message(role=Role(m["role"]), content=m["content"])
                for m in data.get("messages", [])
            ]
            self._summary = data.get("summary", "")
            self._loaded = True
        except (json.JSONDecodeError, KeyError):
            pass


class LongTermMemory:
    """Long-term memory for storing facts, preferences, and knowledge."""

    def __init__(self, persist_path: str | None = None) -> None:
        self.persist_path = Path(persist_path).expanduser() if persist_path else None
        self._facts: dict[str, str] = {}  # category -> content
        self._load()

    def store(self, category: str, content: str) -> None:
        """Store a fact or piece of knowledge."""
        if category in self._facts:
            self._facts[category] += f"\n\n{content}"
        else:
            self._facts[category] = content
        self._save()

    def retrieve(self, category: str) -> str | None:
        """Retrieve facts by category."""
        return self._facts.get(category)

    def search(self, query: str) -> list[dict[str, str]]:
        """Simple keyword search across all stored facts."""
        results = []
        query_lower = query.lower()
        for category, content in self._facts.items():
            if query_lower in content.lower() or query_lower in category.lower():
                results.append({"category": category, "content": content})
        return results

    def list_categories(self) -> list[str]:
        return list(self._facts.keys())

    def delete(self, category: str) -> bool:
        if category in self._facts:
            del self._facts[category]
            self._save()
            return True
        return False

    def _save(self) -> None:
        if not self.persist_path:
            return
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        self.persist_path.write_text(
            json.dumps(self._facts, indent=2, ensure_ascii=False)
        )

    def _load(self) -> None:
        if not self.persist_path or not self.persist_path.exists():
            return
        try:
            self._facts = json.loads(self.persist_path.read_text())
        except json.JSONDecodeError:
            pass
