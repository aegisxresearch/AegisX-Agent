"""Being able to say where you are: workspace detection and local-model probing."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fake_llm import FakeLLMServer, ollama_tags

from aegisx_agent.llm.autodetect import chat_model, detect_local_provider, ollama_reachable
from aegisx_agent.project import detect_project


def _git_init(path: Path) -> bool:
    """Create a real repository; return False when git is unavailable."""
    try:
        subprocess.run(
            ["git", "init", "-q"], cwd=path, capture_output=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


# === Workspace detection ===


def test_a_plain_directory_is_still_described(tmp_path) -> None:
    context = detect_project(tmp_path)

    assert context.root == tmp_path.resolve()
    assert context.name == tmp_path.name
    assert context.stacks == []
    assert context.git_is_repo is False
    assert "Working directory" in context.to_prompt()


def test_the_working_directory_is_always_stated(tmp_path) -> None:
    """Without this the agent has no idea where its relative paths point."""
    prompt = detect_project(tmp_path).to_prompt()

    assert str(tmp_path.resolve()) in prompt


def test_stacks_are_detected_from_marker_files(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n")

    context = detect_project(tmp_path)

    assert context.stacks == ["Python", "Docker"]


def test_node_and_python_are_not_reported_twice(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / "requirements.txt").write_text("")
    (tmp_path / "package.json").write_text("{}")

    assert detect_project(tmp_path).stacks == ["Python", "Node"]


def test_files_are_counted_but_vendor_dirs_are_skipped(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "left-pad.js").write_text("")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "thing.py").write_text("")

    assert detect_project(tmp_path).file_count == 1


def test_hidden_project_directories_are_counted(tmp_path) -> None:
    """A hidden folder such as ``.github`` is project content, not noise —
    while the ignored ``.git`` directory still stays out of the count."""
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "objects" / "deadbeef").write_text("")

    assert detect_project(tmp_path).file_count == 1


def test_git_branch_and_dirty_count_are_reported(tmp_path) -> None:
    if not _git_init(tmp_path):
        pytest.skip("git is not available")
    (tmp_path / "tracked.txt").write_text("hello")

    context = detect_project(tmp_path)

    assert context.git_is_repo is True
    assert context.git_changed == 1
    assert "Git:" in context.to_prompt()
    assert "1 uncommitted change(s)" in context.to_prompt()


def test_a_repo_without_commits_is_not_called_detached(tmp_path) -> None:
    """A fresh repo has no branch, but calling that "detached" is wrong."""
    if not _git_init(tmp_path):
        pytest.skip("git is not available")

    context = detect_project(tmp_path)

    assert context.git_is_repo is True
    assert context.git_has_commits is False
    assert context.git_label() == "git (no commits yet)"
    assert "no commits yet" in context.to_prompt()
    assert "detached" not in context.to_prompt()


def test_a_clean_tree_says_so(tmp_path) -> None:
    if not _git_init(tmp_path):
        pytest.skip("git is not available")

    assert "working tree clean" in detect_project(tmp_path).to_prompt()


def test_project_instructions_are_loaded(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("# Rules\n\nAlways run the tests.\n")

    context = detect_project(tmp_path)

    assert context.instructions_file == "AGENTS.md"
    assert "Always run the tests." in context.to_prompt()
    assert "Project instructions from AGENTS.md" in context.to_prompt()


def test_long_instructions_are_truncated(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * 20_000)

    context = detect_project(tmp_path)

    assert len(context.instructions) < 4_200
    assert context.instructions.endswith("[truncated]")


def test_a_blank_instructions_file_is_ignored(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("   \n")
    (tmp_path / "CLAUDE.md").write_text("Use tabs.\n")

    context = detect_project(tmp_path)

    assert context.instructions_file == "CLAUDE.md"


def test_a_missing_directory_is_not_an_error(tmp_path) -> None:
    context = detect_project(tmp_path / "gone")

    assert context.file_count == 0
    assert "Working directory" in context.to_prompt()


def test_summary_names_the_folder_stack_and_git(tmp_path) -> None:
    (tmp_path / "pyproject.toml").write_text("")

    summary = detect_project(tmp_path).summary()

    assert tmp_path.name in summary
    assert "Python" in summary
    assert "not a git repo" in summary


def test_the_agent_injects_the_workspace_into_its_prompt(tmp_path) -> None:
    from aegisx_agent.config import AgentConfig, LLMProvider
    from aegisx_agent.core import AegisXAgent
    from aegisx_agent.project import ProjectContext

    (tmp_path / "AGENTS.md").write_text("Run pytest before answering.\n")
    agent = AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path),
            rag_enabled=False,
            web_search_enabled=False,
        ),
        project=ProjectContext(root=tmp_path, stacks=["Python"], instructions_file="AGENTS.md",
                               instructions="Run pytest before answering."),
    )

    prompt = agent._build_system_prompt()

    assert "[Workspace]" in prompt
    assert str(tmp_path.resolve()) in prompt
    assert "Run pytest before answering." in prompt


def test_workspace_context_can_be_switched_off(tmp_path) -> None:
    from aegisx_agent.config import AgentConfig, LLMProvider
    from aegisx_agent.core import AegisXAgent

    agent = AegisXAgent(
        AgentConfig(
            llm_provider=LLMProvider.OLLAMA,
            data_dir=str(tmp_path),
            rag_enabled=False,
            web_search_enabled=False,
            project_context_enabled=False,
        )
    )

    assert agent.get_project() is None
    assert "[Workspace]" not in agent._build_system_prompt()


# === Local provider autodetection ===


@pytest.fixture()
def fake_server():
    server = FakeLLMServer()
    try:
        yield server
    finally:
        server.stop()


def test_ollama_is_found_when_it_answers(fake_server: FakeLLMServer) -> None:
    fake_server.serve_get("/api/tags", {"body": ollama_tags(["llama3.2:latest"])})

    assert ollama_reachable(fake_server.root_url) is True


def test_ollama_is_not_found_when_nothing_listens() -> None:
    # Port 1 is reserved and never has a listener.
    assert ollama_reachable("http://127.0.0.1:1", timeout=0.2) is False


def test_a_server_without_the_route_counts_as_unavailable(fake_server: FakeLLMServer) -> None:
    assert ollama_reachable(fake_server.root_url) is False


def test_the_installed_model_is_chosen(fake_server: FakeLLMServer) -> None:
    fake_server.serve_get("/api/tags", {"body": ollama_tags(["mistral:7b"])})

    assert chat_model(fake_server.root_url) == "mistral:7b"


def test_a_small_chat_model_is_preferred(fake_server: FakeLLMServer) -> None:
    fake_server.serve_get(
        "/api/tags",
        {"body": ollama_tags(["my-custom-70b", "llama3.2:1b", "llama3.1:8b"])},
    )

    assert chat_model(fake_server.root_url) == "llama3.2:1b"


def test_embedding_models_are_never_picked(fake_server: FakeLLMServer) -> None:
    """An embedding model cannot hold a conversation — picking one hangs the first turn."""
    fake_server.serve_get("/api/tags", {"body": ollama_tags(["nomic-embed-text"])})

    assert chat_model(fake_server.root_url) is None


def test_no_models_installed_means_no_detection(fake_server: FakeLLMServer) -> None:
    fake_server.serve_get("/api/tags", {"body": ollama_tags([])})

    assert chat_model(fake_server.root_url) is None


def test_detection_returns_url_and_model(fake_server: FakeLLMServer) -> None:
    fake_server.serve_get("/api/tags", {"body": ollama_tags(["qwen2.5:7b"])})

    assert detect_local_provider(fake_server.root_url) == (fake_server.root_url, "qwen2.5:7b")


def test_detection_is_none_without_a_chat_model(fake_server: FakeLLMServer) -> None:
    fake_server.serve_get("/api/tags", {"body": ollama_tags(["nomic-embed-text"])})

    assert detect_local_provider(fake_server.root_url) is None


def test_the_cli_switches_to_a_local_model_without_asking(
    fake_server: FakeLLMServer, monkeypatch, tmp_path
) -> None:
    """First run with no configuration should not show a wizard if Ollama is up."""
    from aegisx_agent.cli import main as cli
    from aegisx_agent.config import LLMProvider
    from aegisx_agent.llm import autodetect

    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "openai")
    monkeypatch.delenv("AEGISX_OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(
        autodetect,
        "detect_local_provider",
        lambda base_url=autodetect.DEFAULT_OLLAMA_URL: (
            fake_server.root_url,
            "llama3.2:latest",
        ),
    )

    config = cli._get_config()

    assert config.llm_provider is LLMProvider.OLLAMA
    assert config.ollama_model == "llama3.2:latest"
    assert config.ollama_base_url == fake_server.root_url


def test_a_configured_provider_is_left_alone(monkeypatch, tmp_path) -> None:
    from aegisx_agent.cli import main as cli
    from aegisx_agent.config import LLMProvider
    from aegisx_agent.llm import autodetect

    monkeypatch.setenv("AEGISX_LLM_PROVIDER", "openai")
    monkeypatch.setenv("AEGISX_OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(cli, "CONFIG_FILE", tmp_path / "config.json")

    def explode():  # pragma: no cover - must not be called
        raise AssertionError("should not probe for a local model when one is configured")

    monkeypatch.setattr(autodetect, "detect_local_provider", explode)

    assert cli._get_config().llm_provider is LLMProvider.OPENAI


def test_the_probe_is_bounded_so_startup_stays_fast() -> None:
    """Autodetection runs on every launch: it must not stall the CLI."""
    import time

    started = time.perf_counter()
    ollama_reachable("http://127.0.0.1:1", timeout=0.2)

    assert time.perf_counter() - started < 2.0
