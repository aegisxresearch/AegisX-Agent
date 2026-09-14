"""Advanced 4-layer memory system inspired by Hermes Agent.

Layer 1: Prompt Memory (MEMORY.md + USER.md) — always loaded
Layer 2: Session Search (SQLite + FTS5) — on-demand retrieval
Layer 3: Skills (procedural memory) — progressive disclosure
Layer 4: User Model — passive preference learning
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any


class PromptMemory:
    """Layer 1: Always-on memory injected into system prompt.

    Two files: MEMORY.md (facts) and USER.md (user preferences).
    Total limit: ~3575 characters to force curation.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.memory_file = data_dir / "MEMORY.md"
        self.user_file = data_dir / "USER.md"
        self.max_chars = 3575
        self._memory = self._load(self.memory_file)
        self._user = self._load(self.user_file)

    @property
    def memory(self) -> str:
        return self._memory

    def get_combined(self) -> str:
        """Get combined memory for system prompt."""
        parts = []
        if self._memory.strip():
            parts.append(f"[Agent Memory]:\n{self._memory}")
        if self._user.strip():
            parts.append(f"[User Profile]:\n{self._user}")
        return "\n\n".join(parts)

    def add_memory(self, content: str) -> bool:
        """Add to agent memory. Returns False if over limit."""
        new = f"{self._memory}\n\n{content}".strip()
        if len(new) > self.max_chars:
            return False
        self._memory = new
        self._save(self.memory_file, self._memory)
        return True

    def add_user_info(self, content: str) -> bool:
        """Add to user profile."""
        new = f"{self._user}\n\n{content}".strip()
        if len(new) > self.max_chars:
            return False
        self._user = new
        self._save(self.user_file, self._user)
        return True

    def replace_memory(self, old: str, new: str) -> None:
        self._memory = self._memory.replace(old, new)
        self._save(self.memory_file, self._memory)

    def replace_user(self, old: str, new: str) -> None:
        self._user = self._user.replace(old, new)
        self._save(self.user_file, self._user)

    def remove_memory(self, text: str) -> None:
        self._memory = self._memory.replace(text, "").strip()
        self._save(self.memory_file, self._memory)

    def _load(self, path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        return ""

    def _save(self, path: Path, content: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class SessionStore:
    """Layer 2: Session persistence with SQLite + FTS5 search.

    Every session is saved and searchable.
    Uses LLM summarization for relevant context injection.
    """

    def __init__(self, data_dir: Path) -> None:
        self.db_path = data_dir / "sessions.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)
        # Create FTS5 index for full-text search
        try:
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts
                USING fts5(content, session_id, timestamp)
            """)
        except sqlite3.OperationalError:
            pass  # FTS5 already exists
        conn.commit()
        conn.close()

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Save a message to the session store."""
        conn = sqlite3.connect(str(self.db_path))
        timestamp = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, role, content, metadata)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, timestamp, role, content, json.dumps(metadata or {})),
        )
        # Also insert into FTS
        try:
            conn.execute(
                "INSERT INTO sessions_fts (content, session_id, timestamp) VALUES (?, ?, ?)",
                (content, session_id, timestamp),
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()
        conn.close()

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Search past sessions using FTS5."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            rows = conn.execute(
                "SELECT s.session_id, s.timestamp, s.role, s.content "
                "FROM sessions s "
                "JOIN sessions_fts f ON s.content = f.content "
                "WHERE sessions_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Fallback to LIKE search
            rows = conn.execute(
                "SELECT session_id, timestamp, role, content "
                "FROM sessions WHERE content LIKE ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()

        results = []
        for row in rows:
            results.append({
                "session_id": row[0],
                "timestamp": row[1],
                "role": row[2],
                "content": row[3],
            })
        conn.close()
        return results

    def get_session_history(self, session_id: str) -> list[dict[str, str]]:
        """Get full history for a session."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT timestamp, role, content FROM sessions "
            "WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        ).fetchall()
        conn.close()
        return [{"timestamp": r[0], "role": r[1], "content": r[2]} for r in rows]

    def get_recent_sessions(self, limit: int = 10) -> list[str]:
        """Get recent session IDs."""
        conn = sqlite3.connect(str(self.db_path))
        rows = conn.execute(
            "SELECT DISTINCT session_id FROM sessions "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return [r[0] for r in rows]

    def get_stats(self) -> dict[str, Any]:
        """Get session store statistics."""
        conn = sqlite3.connect(str(self.db_path))
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        sessions = conn.execute("SELECT COUNT(DISTINCT session_id) FROM sessions").fetchone()[0]
        conn.close()
        return {"total_messages": total, "total_sessions": sessions}


class UserModel:
    """Layer 4: Passive user preference learning.

    Tracks user preferences, communication style, and domain knowledge
    across sessions without explicit user input.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.model_file = data_dir / "user_model.json"
        self._model = self._load()

    def _load(self) -> dict[str, Any]:
        if self.model_file.exists():
            try:
                return json.loads(self.model_file.read_text())  # type: ignore[no-any-return]
            except json.JSONDecodeError:
                pass
        return {
            "preferences": {},
            "communication_style": {},
            "domain_knowledge": {},
            "corrections": [],
            "patterns": [],
        }

    def _save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.model_file.write_text(json.dumps(self._model, indent=2, ensure_ascii=False))

    def learn_preference(self, key: str, value: str) -> None:
        """Learn a user preference."""
        self._model["preferences"][key] = value
        self._save()

    def learn_style(self, key: str, value: str) -> None:
        """Learn communication style preference."""
        self._model["communication_style"][key] = value
        self._save()

    def learn_domain(self, topic: str, detail: str) -> None:
        """Learn domain knowledge."""
        if topic in self._model["domain_knowledge"]:
            self._model["domain_knowledge"][topic] += f"\n{detail}"
        else:
            self._model["domain_knowledge"][topic] = detail
        self._save()

    def record_correction(self, original: str, corrected: str, context: str) -> None:
        """Record when user corrects the agent."""
        self._model["corrections"].append({
            "original": original,
            "corrected": corrected,
            "context": context,
            "timestamp": datetime.now().isoformat(),
        })
        # Keep only last 50 corrections
        self._model["corrections"] = self._model["corrections"][-50:]
        self._save()

    def record_pattern(self, pattern: str, frequency: int = 1) -> None:
        """Record a detected user pattern."""
        self._model["patterns"].append({
            "pattern": pattern,
            "frequency": frequency,
            "last_seen": datetime.now().isoformat(),
        })
        self._save()

    def get_context(self) -> str:
        """Get user model as context string for system prompt."""
        parts = []
        if self._model["preferences"]:
            prefs = "\n".join(f"- {k}: {v}" for k, v in self._model["preferences"].items())
            parts.append(f"User Preferences:\n{prefs}")
        if self._model["communication_style"]:
            styles = "\n".join(f"- {k}: {v}" for k, v in self._model["communication_style"].items())
            parts.append(f"Communication Style:\n{styles}")
        return "\n\n".join(parts) if parts else ""
