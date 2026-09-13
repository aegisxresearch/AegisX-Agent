"""The ``ingest`` and ``search`` commands the README has always promised.

ChromaDB is not installed in the test environment, so the happy-path tests
stub ``_init_chroma`` — the engine's own chunking, search assembly, and the
CLI's table/empty-state rendering are still exercised for real.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from typer.testing import CliRunner

from aegisx_agent.cli import main as cli
from aegisx_agent.config import AgentConfig, LLMProvider
from aegisx_agent.core import AegisXAgent


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AEGISX_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cli, "_agent", None)
    return CliRunner()


@pytest.fixture()
def agent(tmp_path) -> AegisXAgent:
    return AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path),
            rag_enabled=False,
            web_search_enabled=False,
        )
    )


@pytest.fixture()
def fake_chroma(monkeypatch):
    """Install a stubbed chromadb module and a collection double."""

    class FakeCollection:
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def add(self, ids, documents, metadatas):
            for i, doc in enumerate(documents):
                self.rows.append({"id": ids[i], "content": doc, "metadata": metadatas[i]})

        def count(self) -> int:
            return len(self.rows)

        def query(self, query_texts, n_results):
            documents = [row["content"] for row in self.rows[:n_results]]
            metadatas = [row["metadata"] for row in self.rows[:n_results]]
            distances = [0.1] * len(documents)
            return {
                "documents": [documents],
                "metadatas": [metadatas],
                "distances": [distances],
            }

    collection = FakeCollection()
    engine_calls: list[str] = []

    fake_module = sys.modules.setdefault("chromadb", type(sys)("chromadb"))

    class _StubClient:
        def PersistentClient(self, path):  # noqa: N802
            engine_calls.append(path)
            return self

        def get_or_create_collection(self, name, metadata=None):
            return collection

    fake_module.PersistentClient = _StubClient().PersistentClient  # type: ignore[attr-defined]
    return collection, engine_calls


def test_ingest_a_file_stores_chunks(runner, agent, monkeypatch, tmp_path, fake_chroma):
    collection, _ = fake_chroma
    doc = tmp_path / "notes.md"
    doc.write_text("alpha bravo charlie delta echo foxtrot golf hotel india")
    monkeypatch.setattr(cli, "_agent", agent)

    result = runner.invoke(cli.app, ["ingest", str(doc)])

    assert result.exit_code == 0, result.output
    # Rich wraps long paths at the 80-col console; compare wrap-insensitively.
    assert "chunks stored" in " ".join(result.output.split())
    assert len(collection.rows) >= 1


def test_ingest_a_directory_walks_supported_files(
    runner, agent, monkeypatch, tmp_path, fake_chroma
):
    collection, _ = fake_chroma
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("alpha bravo charlie delta")
    (docs / "b.txt").write_text("echo foxtrot golf hotel")
    monkeypatch.setattr(cli, "_agent", agent)

    result = runner.invoke(cli.app, ["ingest", str(docs)])

    assert result.exit_code == 0, result.output
    assert "chunks stored" in " ".join(result.output.split())
    assert len(collection.rows) >= 2


def test_ingest_without_chromadb_fails_with_install_hint(
    runner, agent, monkeypatch, tmp_path
):
    doc = tmp_path / "notes.md"
    doc.write_text("alpha bravo charlie delta")
    monkeypatch.setattr(cli, "_agent", agent)
    monkeypatch.delitem(sys.modules, "chromadb", raising=False)

    import builtins

    real_import = builtins.__import__

    def no_chromadb(name, *args, **kwargs):
        if name == "chromadb":
            raise ImportError("No module named 'chromadb'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_chromadb)

    result = runner.invoke(cli.app, ["ingest", str(doc)])

    assert result.exit_code == 1
    assert "pip install chromadb" in result.output


def test_search_renders_results_in_a_table(
    runner, agent, monkeypatch, fake_chroma
):
    collection, _ = fake_chroma
    collection.add(
        ids=["x"],
        documents=["The API rate limit is 60 requests per minute."],
        metadatas=[{"source": "docs/api.md"}],
    )
    monkeypatch.setattr(cli, "_agent", agent)

    result = runner.invoke(cli.app, ["search", "rate limit"])

    assert result.exit_code == 0, result.output
    assert "rate limit" in result.output
    assert "docs/api.md" in result.output
    assert "0.90" in result.output  # 1 - 0.1 distance, rendered as score


def test_search_on_an_empty_knowledge_base_suggests_ingest(
    runner, agent, monkeypatch, fake_chroma
):
    monkeypatch.setattr(cli, "_agent", agent)

    result = runner.invoke(cli.app, ["search", "anything"])

    assert result.exit_code == 0, result.output
    assert "empty" in result.output.lower()
    assert "aegisx ingest" in result.output
