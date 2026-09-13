"""RAG engine — document ingestion, chunking, and vector retrieval."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class RAGEngine:
    """Retrieval-Augmented Generation engine using ChromaDB."""

    def __init__(
        self,
        persist_dir: str = "~/.aegisx/vectorstore",
        chunk_size: int = 512,
        chunk_overlap: int = 50,
    ) -> None:
        self.persist_dir = str(Path(persist_dir).expanduser())
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self._collection: Any = None
        self._client: Any = None

    def _init_chroma(self) -> None:
        """Lazy-init ChromaDB."""
        if self._collection is not None:
            return

        try:
            import chromadb

            Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
            self._client = chromadb.PersistentClient(path=self.persist_dir)
            self._collection = self._client.get_or_create_collection(
                name="aegisx_documents",
                metadata={"hnsw:space": "cosine"},
            )
        except ImportError:
            raise ImportError(
                "ChromaDB is required for RAG. Install with: pip install chromadb"
            )

    async def ingest_text(
        self,
        text: str,
        source: str = "user_input",
        metadata: dict[str, Any] | None = None,
    ) -> int:
        """Ingest raw text into the vector store. Returns number of chunks."""
        self._init_chroma()

        chunks = self._chunk_text(text)
        if not chunks:
            return 0

        ids = []
        documents = []
        metadatas = []

        for i, chunk in enumerate(chunks):
            chunk_id = hashlib.md5(f"{source}:{i}:{chunk[:100]}".encode()).hexdigest()  # noqa: S324
            ids.append(chunk_id)
            documents.append(chunk)
            metadatas.append(
                {"source": source, "chunk_index": i, **(metadata or {})}
            )

        self._collection.add(
            ids=ids,
            documents=documents,
            metadatas=metadatas,
        )
        return len(chunks)

    async def ingest_file(self, file_path: str) -> int:
        """Ingest a file (supports .txt, .md, .py, .json, .pdf)."""
        p = Path(file_path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = p.suffix.lower()
        if suffix == ".pdf":
            text = self._extract_pdf(str(p))
        else:
            text = p.read_text(encoding="utf-8", errors="replace")

        return await self. ingest_text(text, source=str(p))

    async def ingest_directory(self, dir_path: str, extensions: list[str] | None = None) -> int:
        """Ingest all matching files in a directory."""
        p = Path(dir_path).expanduser()
        if not p.is_dir():
            raise NotADirectoryError(f"Not a directory: {dir_path}")

        exts = extensions or [".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".toml"]
        total_chunks = 0

        for file in p.rglob("*"):
            if file.is_file() and file.suffix.lower() in exts:
                try:
                    chunks = await self.ingest_file(str(file))
                    total_chunks += chunks
                except Exception:
                    pass  # Skip unreadable files

        return total_chunks

    async def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Search the vector store for relevant chunks."""
        self._init_chroma()

        if self._collection.count() == 0:
            return []

        results = self._collection.query(
            query_texts=[query],
            n_results=min(top_k, self._collection.count()),
        )

        output = []
        if results and results["documents"]:
            for i, doc in enumerate(results["documents"][0]):
                meta = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else 0
                output.append(
                    {
                        "content": doc,
                        "source": meta.get("source", "Unknown"),
                        "score": 1 - distance,  # Convert distance to similarity
                        "metadata": meta,
                    }
                )
        return output

    async def get_stats(self) -> dict[str, Any]:
        """Get vector store statistics."""
        self._init_chroma()
        count = self._collection.count()
        return {
            "total_chunks": count,
            "persist_dir": self.persist_dir,
        }

    async def clear(self) -> None:
        """Clear the entire vector store."""
        self._init_chroma()
        self._client.delete_collection("aegisx_documents")
        self._collection = self._client.get_or_create_collection(
            name="aegisx_documents",
            metadata={"hnsw:space": "cosine"},
        )

    def _chunk_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks."""
        if len(text) <= self.chunk_size:
            return [text] if text.strip() else []

        chunks = []
        start = 0
        while start < len(text):
            end = start + self.chunk_size
            chunk = text[start:end]

            # Try to break at sentence boundary
            if end < len(text):
                last_period = chunk.rfind(".")
                last_newline = chunk.rfind("\n")
                break_at = max(last_period, last_newline)
                if break_at > self.chunk_size * 0.3:
                    chunk = chunk[: break_at + 1]
                    end = start + break_at + 1

            if chunk.strip():
                chunks.append(chunk.strip())
            start = end - self.chunk_overlap

        return chunks

    @staticmethod
    def _extract_pdf(file_path: str) -> str:
        """Extract text from a PDF file."""
        try:
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            text_parts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    text_parts.append(text)
            return "\n\n".join(text_parts)
        except ImportError:
            raise ImportError("pypdf is required for PDF support. Install with: pip install pypdf")
