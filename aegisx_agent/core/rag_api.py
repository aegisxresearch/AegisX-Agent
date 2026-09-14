"""Public RAG operations exposed by :class:`AegisXAgent`."""

from __future__ import annotations

from typing import Any

from aegisx_agent.rag.engine import RAGEngine


class RAGAPI:
    """Mixin containing the agent's document knowledge-base API."""

    _rag_engine: RAGEngine | None

    async def ingest_document(self, file_path: str) -> int:
        """Ingest a document into the knowledge base."""
        if not self._rag_engine:
            raise RuntimeError("RAG is disabled. Enable it in config.")
        return await self._rag_engine.ingest_file(file_path)

    async def ingest_text(self, text: str, source: str = "user_input") -> int:
        """Ingest raw text into the knowledge base."""
        if not self._rag_engine:
            raise RuntimeError("RAG is disabled. Enable it in config.")
        return await self._rag_engine.ingest_text(text, source=source)

    async def search_knowledge(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Search the knowledge base."""
        if not self._rag_engine:
            raise RuntimeError("RAG is disabled. Enable it in config.")
        return await self._rag_engine.search(query, top_k=top_k)
