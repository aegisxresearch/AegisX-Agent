"""RAG search tool — query the agent's knowledge base."""

from __future__ import annotations

from typing import Any

from utopia_agent.tools.base import Tool, ToolResult, ToolStatus


class RAGSearchTool(Tool):
    """Search the agent's document knowledge base."""

    def __init__(self, rag_engine: Any = None) -> None:
        super().__init__(
            name="rag_search",
            description=(
                "Search through the agent's document knowledge base (RAG). "
                "Use this to find information from documents that have been ingested. "
                "Returns the most relevant document chunks for the query."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant documents",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default: 3)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        )
        self._rag_engine = rag_engine

    async def execute(self, **kwargs: Any) -> ToolResult:
        query = kwargs.get("query", "")
        top_k = kwargs.get("top_k", 3)

        if not query:
            return ToolResult(status=ToolStatus.ERROR, output="", error="Query is required")

        if self._rag_engine is None:
            return ToolResult(
                status=ToolStatus.ERROR,
                output="",
                error="RAG engine not initialized. Ingest documents first.",
            )

        try:
            results = await self._rag_engine.search(query, top_k=top_k)
            if not results:
                return ToolResult(
                    status=ToolStatus.SUCCESS,
                    output=f"No relevant documents found for: {query}",
                )

            output = f"RAG results for: {query}\n\n"
            for i, r in enumerate(results, 1):
                output += f"--- Result {i} (score: {r.get('score', 'N/A'):.3f}) ---\n"
                output += f"Source: {r.get('source', 'Unknown')}\n"
                output += f"{r.get('content', '')}\n\n"

            return ToolResult(status=ToolStatus.SUCCESS, output=output.strip())
        except Exception as e:
            return ToolResult(status=ToolStatus.ERROR, output="", error=f"RAG search failed: {e}")
