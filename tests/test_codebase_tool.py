"""CodebaseTool against a real temporary project tree.

Structure, find, read, search, deps, and summary all run on real files —
the tool's whole job is filesystem awareness, so that is what is tested.
"""

from __future__ import annotations

import json

from support import run

from aegisx_agent.tools.base import ToolStatus
from aegisx_agent.tools.coding.codebase import CodebaseTool


def _make_project(tmp_path):
    (tmp_path / "app.py").write_text("def main():\n    return 42\n", encoding="utf-8")
    (tmp_path / "utils.py").write_text("VALUE = 'seven'\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("just notes\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "helper.py").write_text("from app import main\n", encoding="utf-8")
    (tmp_path / ".hidden").write_text("nope\n", encoding="utf-8")
    return tmp_path


def test_structure_lists_a_real_tree(tmp_path) -> None:
    _make_project(tmp_path)

    result = run(CodebaseTool().execute(action="structure", path=str(tmp_path)))

    assert result.is_success
    assert "app.py" in result.output
    assert "pkg/" in result.output
    assert "helper.py" in result.output
    assert "Total:" in result.output
    # Dotfiles are ignored.
    assert ".hidden" not in result.output


def test_structure_respects_max_results_and_reports_truncation(tmp_path) -> None:
    _make_project(tmp_path)

    result = run(
        CodebaseTool().execute(action="structure", path=str(tmp_path), max_results=2)
    )

    assert result.is_success
    assert "items shown" in result.output


def test_structure_rejects_a_missing_path(tmp_path) -> None:
    result = run(CodebaseTool().execute(action="structure", path=str(tmp_path / "ghost")))

    assert result.status is ToolStatus.ERROR
    assert "Path not found" in (result.error or "")


def test_find_matches_files_by_name_and_reports_no_matches(tmp_path) -> None:
    _make_project(tmp_path)

    found = run(CodebaseTool().execute(action="find", path=str(tmp_path), query="helper"))
    assert found.is_success
    assert "helper.py" in found.output

    none = run(CodebaseTool().execute(action="find", path=str(tmp_path), query="zzz"))
    assert none.is_success
    assert "No files found" in none.output

    missing_query = run(CodebaseTool().execute(action="find", path=str(tmp_path), query=""))
    assert missing_query.status is ToolStatus.ERROR
    assert "Query required" in (missing_query.error or "")


def test_read_numbers_lines_and_truncates_long_files(tmp_path) -> None:
    _make_project(tmp_path)
    big = tmp_path / "big.py"
    big.write_text("\n".join(f"line {i}" for i in range(1, 506)), encoding="utf-8")

    small = run(CodebaseTool().execute(action="read", path=str(tmp_path / "app.py")))
    assert small.is_success
    assert "1 │" in small.output
    assert "return 42" in small.output

    truncated = run(CodebaseTool().execute(action="read", path=str(big)))
    assert truncated.is_success
    assert "more lines" in truncated.output

    missing = run(CodebaseTool().execute(action="read", path=str(tmp_path / "nope.py")))
    assert missing.status is ToolStatus.ERROR
    assert "File not found" in (missing.error or "")

    directory = run(CodebaseTool().execute(action="read", path=str(tmp_path)))
    assert directory.status is ToolStatus.ERROR
    assert "Not a file" in (directory.error or "")


def test_search_content_finds_matches_with_file_and_line(tmp_path) -> None:
    _make_project(tmp_path)

    result = run(CodebaseTool().execute(action="search", path=str(tmp_path), query="import"))

    assert result.is_success
    assert "helper.py:1" in result.output
    assert "import" in result.output

    none = run(CodebaseTool().execute(action="search", path=str(tmp_path), query="quantum"))
    assert none.is_success
    assert "No matches" in none.output

    missing_query = run(CodebaseTool().execute(action="search", path=str(tmp_path), query=""))
    assert missing_query.status is ToolStatus.ERROR


def test_deps_reads_python_node_and_go_manifests(tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("rich>=13.0.0\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"left-pad": "1.0.0"}, "devDependencies": {"jest": "2"}}),
        encoding="utf-8",
    )
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")

    result = run(CodebaseTool().execute(action="deps", path=str(tmp_path)))

    assert result.is_success
    assert "requirements.txt" in result.output
    assert "left-pad: 1.0.0" in result.output
    assert "jest (dev): 2" in result.output
    assert "go.mod" in result.output


def test_deps_falls_back_to_raw_text_for_invalid_package_json(tmp_path) -> None:
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")

    result = run(CodebaseTool().execute(action="deps", path=str(tmp_path)))

    assert result.is_success
    assert "package.json" in result.output
    assert "{not json" in result.output


def test_deps_reports_when_no_manifests_exist(tmp_path) -> None:
    result = run(CodebaseTool().execute(action="deps", path=str(tmp_path)))

    assert result.is_success
    assert "No dependency files" in result.output


def test_summary_counts_files_lines_and_configs(tmp_path) -> None:
    _make_project(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "noext").write_text("x\n", encoding="utf-8")

    result = run(CodebaseTool().execute(action="summary", path=str(tmp_path)))

    assert result.is_success
    assert "Total files:" in result.output
    assert "Code lines:" in result.output
    assert ".py" in result.output
    assert "(no ext)" in result.output
    assert "pyproject.toml" in result.output
    assert "README.md" in result.output


def test_an_unknown_action_is_an_error() -> None:
    result = run(CodebaseTool().execute(action="teleport"))

    assert result.status is ToolStatus.ERROR
    assert "Unknown action" in (result.error or "")


def test_an_argument_type_error_is_contained_as_a_tool_error(tmp_path) -> None:
    """A non-integer max_results raises TypeError inside — it must become an error result."""

    result = run(
        CodebaseTool().execute(action="structure", path=str(tmp_path), max_results="many")
    )

    assert result.status is ToolStatus.ERROR
    assert result.error  # the exception text is surfaced, not swallowed


def test_format_size_covers_all_units() -> None:
    fmt = CodebaseTool._format_size

    assert fmt(512) == "512B"
    assert fmt(2048) == "2KB"
    assert fmt(2048 * 1024) == "2MB"
    assert fmt(2048 * 1024 * 1024) == "2GB"
