"""RAGEngine: chunking, ingestion, search assembly, and failure paths.

ChromaDB is not installed in the test environment, so chromadb is stubbed
with an in-memory collection double. The engine's real logic — chunking,
IDs/metadata assembly, similarity scoring, error handling — runs for real.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from support import run

from aegisx_agent.rag.engine import RAGEngine


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """Stub chromadb and return (engine, client)."""

    class FakeCollection:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []
            self.added: list[dict[str, Any]] = []

        def add(self, ids, documents, metadatas) -> None:
            self.added.append(
                {
                    "ids": list(ids),
                    "documents": list(documents),
                    "metadatas": list(metadatas),
                }
            )
            for i, doc in enumerate(documents):
                self.rows.append({"id": ids[i], "content": doc, "metadata": metadatas[i]})

        def count(self) -> int:
            return len(self.rows)

        def query(self, query_texts, n_results):
            n = min(n_results, len(self.rows))
            documents = [row["content"] for row in self.rows[:n]]
            metadatas = [row["metadata"] for row in self.rows[:n]]
            distances = [0.25, 0.5, 0.75, 0.9][:n]
            return {
                "documents": [documents],
                "metadatas": [metadatas],
                "distances": [distances],
            }

    class FakeClient:
        def __init__(self) -> None:
            self.collection = FakeCollection()
            self.deleted: list[str] = []
            self.collection_name = ""

        def PersistentClient(self, path):  # noqa: N802
            self.path = path
            return self

        def get_or_create_collection(self, name, metadata=None):
            self.collection_name = name
            return self.collection

        def delete_collection(self, name) -> None:
            self.deleted.append(name)
            self.collection = FakeCollection()

    client = FakeClient()
    fake_module = type(sys)("chromadb")
    fake_module.PersistentClient = client.PersistentClient
    monkeypatch.setitem(sys.modules, "chromadb", fake_module)

    engine = RAGEngine(
        persist_dir=str(tmp_path / "vectors"), chunk_size=64, chunk_overlap=8
    )
    return engine, client


# --------------------------------------------------------------------------- #
# Chunking (pure logic, no stub needed)
# --------------------------------------------------------------------------- #


def test_short_text_is_one_chunk_and_whitespace_only_is_none() -> None:
    engine = RAGEngine(chunk_size=100)

    assert engine._chunk_text("a short note") == ["a short note"]
    assert engine._chunk_text("   \n  ") == []
    assert engine._chunk_text("") == []


def test_long_text_splits_at_sentence_boundaries() -> None:
    engine = RAGEngine(chunk_size=60, chunk_overlap=5)
    text = ". ".join(f"Sentence {i} says something useful" for i in range(8)) + "."

    chunks = engine._chunk_text(text)

    assert len(chunks) > 1
    assert all(len(c) <= 60 for c in chunks), [len(c) for c in chunks]
    # Chunks end at sentence boundaries where one was available in-window.
    assert sum(c.endswith(".") for c in chunks) >= 1


def test_long_text_without_boundaries_still_covers_the_input() -> None:
    engine = RAGEngine(chunk_size=32, chunk_overlap=4)
    text = "x" * 100  # no periods, no newlines

    chunks = engine._chunk_text(text)

    assert len(chunks) >= 3
    assert sum(len(c) for c in chunks) >= 90  # no meaningful loss


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #


def test_ingest_text_stores_chunked_documents_with_metadata(store) -> None:
    engine, client = store

    count = run(
        engine.ingest_text("word " * 40, source="notes.md", metadata={"topic": "x"})
    )

    assert count >= 2
    assert client.collection_name == "aegisx_documents"
    first = client.collection.added[0]
    assert len(first["ids"]) == len(set(first["ids"]))  # ids unique
    assert all(
        meta["source"] == "notes.md" and meta["topic"] == "x"
        for meta in first["metadatas"]
    )
    assert [meta["chunk_index"] for meta in first["metadatas"]] == list(
        range(len(first["documents"]))
    )


def test_ingest_text_of_whitespace_returns_zero_and_touches_nothing(store) -> None:
    engine, client = store

    assert run(engine.ingest_text("   \n\t ")) == 0
    assert client.collection.added == []


def test_ingest_file_reads_text_and_uses_the_path_as_source(store, tmp_path) -> None:
    engine, client = store
    doc = tmp_path / "notes.md"
    doc.write_text("hello world, this is a document body")

    count = run(engine.ingest_file(str(doc)))

    assert count == 1
    assert client.collection.rows[0]["metadata"]["source"] == str(doc)


def test_ingest_file_raises_on_a_missing_file(store, tmp_path) -> None:
    engine, _ = store

    with pytest.raises(FileNotFoundError):
        run(engine.ingest_file(str(tmp_path / "nope.txt")))


def test_ingest_directory_walks_supported_extensions_recursively(store, tmp_path) -> None:
    engine, client = store
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("alpha bravo charlie")
    (docs / "b.txt").write_text("delta echo foxtrot")
    (docs / "skip.bin").write_text("binary noise")  # unsupported extension
    nested = docs / "nested"
    nested.mkdir()
    (nested / "c.py").write_text("print('hi')")

    total = run(engine.ingest_directory(str(docs)))

    assert total == 3
    sources = {row["metadata"]["source"] for row in client.collection.rows}
    assert sources == {str(docs / "a.md"), str(docs / "b.txt"), str(nested / "c.py")}


def test_ingest_directory_honours_custom_extensions(store, tmp_path) -> None:
    engine, client = store
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.yaml").write_text("key: value")
    (docs / "b.md").write_text("not wanted this time")

    total = run(engine.ingest_directory(str(docs), extensions=[".yaml"]))

    assert total == 1
    assert client.collection.rows[0]["metadata"]["source"].endswith("a.yaml")


def test_ingest_directory_continues_past_an_unreadable_file(
    store, tmp_path, monkeypatch
) -> None:
    engine, client = store
    docs = tmp_path / "docs"
    docs.mkdir()
    good = docs / "good.txt"
    good.write_text("readable body")
    bad = docs / "bad.txt"
    bad.write_text("unreadable body")

    real_read = Path.read_text

    def flaky_read(self, *args, **kwargs):
        if self.name == "bad.txt":
            raise PermissionError("nope")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read)

    total = run(engine.ingest_directory(str(docs)))

    assert total == 1  # only the good file
    assert client.collection.rows[0]["metadata"]["source"] == str(good)


def test_ingest_directory_raises_on_a_missing_directory(store, tmp_path) -> None:
    engine, _ = store

    with pytest.raises(NotADirectoryError):
        run(engine.ingest_directory(str(tmp_path / "void")))


def test_unsupported_suffix_falls_back_to_text_reading(store, tmp_path) -> None:
    """Only .pdf is special-cased; everything else goes through read_text."""
    engine, _ = store
    doc = tmp_path / "data.csv"
    doc.write_text("a,b\n1,2\n")

    assert run(engine.ingest_file(str(doc))) == 1


# --------------------------------------------------------------------------- #
# Search / stats / clear
# --------------------------------------------------------------------------- #


def test_search_scores_similarity_and_carries_metadata(store) -> None:
    engine, _ = store
    run(engine.ingest_text("chunk one body", source="s1"))
    run(engine.ingest_text("chunk two body", source="s2"))

    results = run(engine.search("anything", top_k=2))

    assert len(results) == 2
    assert results[0]["score"] == 0.75  # 1 - 0.25
    assert results[1]["score"] == 0.5  # 1 - 0.5
    assert {r["source"] for r in results} == {"s1", "s2"}
    assert results[0]["metadata"]["source"] == "s1"


def test_search_top_k_is_capped_by_collection_size(store) -> None:
    engine, _ = store
    run(engine.ingest_text("only one chunk", source="s"))

    results = run(engine.search("anything", top_k=10))

    assert len(results) == 1


def test_search_on_an_empty_store_returns_empty_list(store) -> None:
    engine, _ = store

    assert run(engine.search("anything")) == []


def test_stats_reports_count_and_directory(store, tmp_path) -> None:
    engine, _ = store
    run(engine.ingest_text("body " * 40, source="s"))

    stats = run(engine.get_stats())

    assert stats["total_chunks"] >= 2
    assert stats["persist_dir"] == str(tmp_path / "vectors")


def test_clear_deletes_and_recreates_the_collection(store) -> None:
    engine, client = store
    run(engine.ingest_text("some body " * 10, source="s"))
    assert client.collection.count() > 0

    run(engine.clear())

    assert client.deleted == ["aegisx_documents"]
    assert client.collection.count() == 0


# --------------------------------------------------------------------------- #
# Missing-dependency errors
# --------------------------------------------------------------------------- #


def test_missing_chromadb_raises_with_the_install_hint(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(sys.modules, "chromadb", None)  # import -> ImportError
    engine = RAGEngine(persist_dir=str(tmp_path / "v"))

    with pytest.raises(ImportError, match="pip install chromadb"):
        run(engine.ingest_text("hello"))


def test_extract_pdf_without_pypdf_raises_with_the_install_hint(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setitem(sys.modules, "pypdf", None)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really a pdf")

    with pytest.raises(ImportError, match="pip install pypdf"):
        run(RAGEngine(persist_dir=str(tmp_path / "v")).ingest_file(str(pdf)))


def test_ingest_pdf_joins_page_texts_and_skips_blank_pages(
    store, tmp_path, monkeypatch
) -> None:
    """The .pdf branch: extracted page text flows into the normal pipeline."""
    engine, client = store
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake but structurally plausible")

    class FakePage:
        def __init__(self, text: str | None) -> None:
            self._text = text

        def extract_text(self) -> str | None:
            return self._text

    class FakeReader:
        def __init__(self, path: str) -> None:
            self.pages = [
                FakePage("page one body"),
                FakePage(None),  # blank page must be skipped
                FakePage("page two body"),
            ]

    fake_pypdf = type(sys)("pypdf")
    fake_pypdf.PdfReader = FakeReader
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    count = run(engine.ingest_file(str(pdf)))

    assert count == 1
    row = client.collection.rows[0]
    assert row["content"] == "page one body\n\npage two body"
    assert row["metadata"]["source"] == str(pdf)


def test_lazy_init_runs_only_once(store) -> None:
    engine, client = store
    engine._init_chroma()
    engine._init_chroma()  # second call must be a no-op

    assert client.collection is not None
