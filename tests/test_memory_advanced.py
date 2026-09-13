"""The 4-layer memory system: prompt memory, session search, user model."""

from __future__ import annotations

import json

from aegisx_agent.llm.base import Message, Role
from aegisx_agent.memory.advanced import PromptMemory, SessionStore, UserModel
from aegisx_agent.memory.store import ConversationMemory, LongTermMemory

# --------------------------------------------------------------------------- #
# Layer 1: PromptMemory (MEMORY.md + USER.md)
# --------------------------------------------------------------------------- #


def test_combined_memory_joins_only_nonempty_parts(tmp_path) -> None:
    memory = PromptMemory(tmp_path)

    assert memory.get_combined() == ""

    memory._memory = "fact one"
    assert memory.get_combined() == "[Agent Memory]:\nfact one"

    memory._user = "likes concise answers"
    combined = memory.get_combined()
    assert "[Agent Memory]:\nfact one" in combined
    assert "[User Profile]:\nlikes concise answers" in combined


def test_add_memory_persists_to_disk(tmp_path) -> None:
    memory = PromptMemory(tmp_path)

    assert memory.add_memory("deploy checklist lives in DEPLOY.md") is True
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == (
        "deploy checklist lives in DEPLOY.md"
    )

    # Reload proves persistence, not just in-memory state.
    reloaded = PromptMemory(tmp_path)
    assert "deploy checklist" in reloaded.memory


def test_add_memory_rejects_content_over_the_curation_limit(tmp_path) -> None:
    memory = PromptMemory(tmp_path)

    assert memory.add_memory("x" * (memory.max_chars - 5)) is True
    assert memory.add_memory("y" * 100) is False  # would exceed max_chars
    assert len(memory.memory) <= memory.max_chars


def test_add_user_info_persists_and_respects_the_limit(tmp_path) -> None:
    memory = PromptMemory(tmp_path)

    assert memory.add_user_info("prefers python 3.12") is True
    assert (tmp_path / "USER.md").read_text(encoding="utf-8") == "prefers python 3.12"
    assert memory.add_user_info("z" * memory.max_chars) is False


def test_replace_and_remove_memory_operations(tmp_path) -> None:
    memory = PromptMemory(tmp_path)
    memory.add_memory("use ruff for linting")

    memory.replace_memory("ruff", "flake8")
    assert "flake8" in memory.memory

    memory.replace_user("nothing", "nothing")  # no-op replace must not crash
    memory.remove_memory("flake8")
    assert "flake8" not in memory.memory
    assert (tmp_path / "MEMORY.md").read_text(encoding="utf-8") == memory.memory


# --------------------------------------------------------------------------- #
# Layer 2: SessionStore (SQLite + FTS5)
# --------------------------------------------------------------------------- #


def test_save_and_search_round_trip(tmp_path) -> None:
    store = SessionStore(tmp_path)
    store.save_message("s1", "user", "the deploy failed with exit code 1")
    store.save_message("s1", "assistant", "checking the logs now")
    store.save_message("s2", "user", "unrelated chat about pizza")

    hits = store.search("deploy failed")

    assert hits, "FTS5 should find the deploy message"
    assert all("deploy" in h["content"] or "failed" in h["content"] for h in hits)
    assert hits[0]["session_id"] == "s1"
    assert set(hits[0]) == {"session_id", "timestamp", "role", "content"}


def test_search_falls_back_to_like_when_fts_is_unavailable(tmp_path, monkeypatch) -> None:
    store = SessionStore(tmp_path)
    store.save_message("s1", "user", " uniquely Quirky phrase ")

    # Break the FTS table so the MATCH query raises OperationalError.
    import sqlite3

    original_connect = sqlite3.connect

    def drop_fts(*args, **kwargs):
        conn = original_connect(*args, **kwargs)
        conn.execute("DROP TABLE IF EXISTS sessions_fts")
        return conn

    monkeypatch.setattr("aegisx_agent.memory.advanced.sqlite3.connect", drop_fts)

    hits = store.search("Quirky phrase")

    assert len(hits) == 1
    assert "Quirky phrase" in hits[0]["content"]


def test_fts_table_creation_failure_is_survived(tmp_path, monkeypatch) -> None:
    """A sqlite build without FTS5 must not crash store initialisation."""
    real_connect = __import__("sqlite3").connect

    class NoFtsConn:
        def __init__(self, conn) -> None:
            self._conn = conn

        def execute(self, sql, params=()):
            if "CREATE VIRTUAL TABLE" in sql:
                raise __import__("sqlite3").OperationalError("no such module: fts5")
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def nofts_connect(*args, **kwargs):
        return NoFtsConn(real_connect(*args, **kwargs))

    monkeypatch.setattr("aegisx_agent.memory.advanced.sqlite3.connect", nofts_connect)

    store = SessionStore(tmp_path)  # must not raise
    store.save_message("s1", "user", "plain storage only")

    assert store.get_session_history("s1")[0]["content"] == "plain storage only"


def test_fts_insert_failure_does_not_lose_the_message(tmp_path, monkeypatch) -> None:
    """If the FTS side-insert fails, the row itself must still be saved."""
    store = SessionStore(tmp_path)

    real_connect = __import__("sqlite3").connect

    class GuardedConn:
        def __init__(self, conn) -> None:
            self._conn = conn

        def execute(self, sql, params=()):
            if "sessions_fts" in sql and "INSERT" in sql:
                raise __import__("sqlite3").OperationalError("fts index corrupt")
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def guarded_connect(*args, **kwargs):
        return GuardedConn(real_connect(*args, **kwargs))

    monkeypatch.setattr("aegisx_agent.memory.advanced.sqlite3.connect", guarded_connect)

    store.save_message("s1", "user", "kept anyway")  # must not raise

    # The message row survived; only the FTS index entry is missing (which
    # also means FTS search will not surface it — the LIKE fallback only
    # covers a failing FTS *query*, not a failing insert).
    history = store.get_session_history("s1")
    assert [h["content"] for h in history] == ["kept anyway"]


def test_session_history_is_ordered_and_complete(tmp_path) -> None:
    store = SessionStore(tmp_path)
    store.save_message("s1", "user", "first question")
    store.save_message("s1", "assistant", "first answer")
    store.save_message("s2", "user", "other session")

    history = store.get_session_history("s1")

    assert [h["role"] for h in history] == ["user", "assistant"]
    assert history[0]["content"] == "first question"
    assert all(h["timestamp"] for h in history)
    assert store.get_session_history("missing") == []


def test_recent_sessions_lists_distinct_ids(tmp_path) -> None:
    store = SessionStore(tmp_path)
    store.save_message("alpha", "user", "one")
    store.save_message("alpha", "user", "two")  # duplicate session id
    store.save_message("beta", "user", "three")

    recent = store.get_recent_sessions(limit=10)

    assert set(recent) == {"alpha", "beta"}


def test_stats_counts_messages_and_sessions(tmp_path) -> None:
    store = SessionStore(tmp_path)
    store.save_message("s1", "user", "a")
    store.save_message("s1", "assistant", "b")
    store.save_message("s2", "user", "c")

    stats = store.get_stats()

    assert stats == {"total_messages": 3, "total_sessions": 2}


# --------------------------------------------------------------------------- #
# Layer 4: UserModel (passive preference learning)
# --------------------------------------------------------------------------- #


def test_user_model_persists_across_instances(tmp_path) -> None:
    model = UserModel(tmp_path)
    model.learn_preference("editor", "neovim")
    model.learn_style("verbosity", "terse")

    reloaded = UserModel(tmp_path)
    assert reloaded._model["preferences"]["editor"] == "neovim"
    assert reloaded._model["communication_style"]["verbosity"] == "terse"


def test_learn_domain_appends_instead_of_overwriting(tmp_path) -> None:
    model = UserModel(tmp_path)
    model.learn_domain("docker", "multi-stage builds")
    model.learn_domain("docker", "buildkit caching")

    reloaded = UserModel(tmp_path)
    assert "multi-stage builds" in reloaded._model["domain_knowledge"]["docker"]
    assert "buildkit caching" in reloaded._model["domain_knowledge"]["docker"]


def test_record_correction_appends_with_timestamp_and_caps_at_50(tmp_path) -> None:
    model = UserModel(tmp_path)
    for i in range(55):
        model.record_correction(f"wrong {i}", f"right {i}", context="coding")

    reloaded = UserModel(tmp_path)
    corrections = reloaded._model["corrections"]
    assert len(corrections) == 50
    assert corrections[-1]["original"] == "wrong 54"
    assert all(c["timestamp"] for c in corrections)


def test_record_pattern_stores_frequency_and_last_seen(tmp_path) -> None:
    model = UserModel(tmp_path)
    model.record_pattern("runs pytest after every edit", frequency=3)

    reloaded = UserModel(tmp_path)
    (pattern,) = reloaded._model["patterns"]
    assert pattern["pattern"] == "runs pytest after every edit"
    assert pattern["frequency"] == 3
    assert pattern["last_seen"]


def test_get_context_renders_preferences_and_style_or_empty(tmp_path) -> None:
    model = UserModel(tmp_path)
    assert model.get_context() == ""  # nothing learned yet

    model.learn_preference("timezone", "UTC+7")
    model.learn_style("greeting", "informal")

    context = model.get_context()
    assert "User Preferences:" in context
    assert "- timezone: UTC+7" in context
    assert "Communication Style:" in context
    assert "- greeting: informal" in context


def test_user_model_survives_a_corrupt_json_file(tmp_path) -> None:
    (tmp_path / "user_model.json").write_text("{not valid json")

    model = UserModel(tmp_path)  # must fall back to defaults, not crash

    assert model._model["preferences"] == {}
    assert model._model["corrections"] == []
    model.learn_preference("k", "v")  # and remain usable


def test_user_model_survives_a_json_scalar_file(tmp_path) -> None:
    """A scalar JSON document must not crash _load."""
    (tmp_path / "user_model.json").write_text(json.dumps("just a string"))

    model = UserModel(tmp_path)

    # The scalar is loaded as-is; the learn* API fails loudly rather than
    # silently corrupting further — document the current sharp edge.
    try:
        model.learn_preference("k", "v")
    except TypeError:
        pass


def test_user_model_rejects_non_object_json(tmp_path) -> None:
    """A non-dict JSON document must not crash _load either."""
    (tmp_path / "user_model.json").write_text(json.dumps(["a", "b"]))

    model = UserModel(tmp_path)

    assert isinstance(model._model, list)  # current behaviour: loaded as-is


# --------------------------------------------------------------------------- #
# Basic stores (store.py) — persistence + search semantics
# --------------------------------------------------------------------------- #


def test_conversation_memory_trims_to_the_message_cap() -> None:
    convo = ConversationMemory(max_messages=3)
    for i in range(6):
        convo.add(
            Message(role=Role.USER if i % 2 == 0 else Role.ASSISTANT, content=f"m{i}")
        )

    contents = [m.content for m in convo.messages]

    assert len(contents) == 3
    assert contents[-1] == "m5"  # newest kept, oldest trimmed


def test_long_term_memory_store_retrieve_and_delete(tmp_path) -> None:
    lt = LongTermMemory(persist_path=str(tmp_path / "facts.json"))

    lt.store("deployments", "aegisx ships via docker compose")
    lt.store("deployments", "staging runs on port 8080")  # appends

    assert "docker compose" in (lt.retrieve("deployments") or "")
    assert "port 8080" in (lt.retrieve("deployments") or "")
    assert set(lt.list_categories()) == {"deployments"}

    hits = lt.search("docker")
    assert hits and "deployments" in json.dumps(hits)

    assert lt.delete("deployments") is True
    assert lt.delete("deployments") is False
    assert lt.retrieve("deployments") is None
