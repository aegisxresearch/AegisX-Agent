"""FileOperationsTool: read, write, list, search, delete, info — plus risk mapping."""

from __future__ import annotations

from support import run

from aegisx_agent.tools.base import ToolRisk, ToolStatus
from aegisx_agent.tools.file_ops import FileOperationsTool


def _seed(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    return target


def test_read_returns_content_and_truncates_huge_files(tmp_path) -> None:
    target = _seed(tmp_path)
    tool = FileOperationsTool()

    small = run(tool.execute(action="read", path=str(target)))
    assert small.is_success
    assert "alpha" in small.output and "beta" in small.output

    big = tmp_path / "big.txt"
    big.write_text("x" * 50_100, encoding="utf-8")
    truncated = run(tool.execute(action="read", path=str(big)))
    assert truncated.is_success
    assert "truncated" in truncated.output

    missing = run(tool.execute(action="read", path=str(tmp_path / "ghost.txt")))
    assert missing.status is ToolStatus.ERROR
    assert "File not found" in (missing.error or "")

    directory = run(tool.execute(action="read", path=str(tmp_path)))
    assert directory.status is ToolStatus.ERROR
    assert "Not a file" in (directory.error or "")


def test_write_creates_files_and_parent_directories(tmp_path) -> None:
    tool = FileOperationsTool()
    target = tmp_path / "deep" / "new.txt"

    result = run(tool.execute(action="write", path=str(target), content="hello"))

    assert result.is_success
    assert "Successfully wrote 5 bytes" in result.output
    assert target.read_text(encoding="utf-8") == "hello"


def test_list_reports_entries_sorted_dirs_first(tmp_path) -> None:
    _seed(tmp_path)
    (tmp_path / "subdir").mkdir()
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")

    result = run(FileOperationsTool().execute(action="list", path=str(tmp_path)))

    assert result.is_success
    assert "📁 subdir" in result.output
    assert "📄 notes.txt" in result.output
    assert "a.py" in result.output

    empty = run(FileOperationsTool().execute(action="list", path=str(tmp_path / "subdir")))
    assert empty.is_success
    assert "Empty directory" in empty.output

    missing = run(FileOperationsTool().execute(action="list", path=str(tmp_path / "ghost")))
    assert missing.status is ToolStatus.ERROR
    assert "Directory not found" in (missing.error or "")

    not_dir = run(FileOperationsTool().execute(action="list", path=str(tmp_path / "notes.txt")))
    assert not_dir.status is ToolStatus.ERROR
    assert "Not a directory" in (not_dir.error or "")


def test_search_finds_matches_and_reports_none(tmp_path) -> None:
    _seed(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("deep alpha\n", encoding="utf-8")
    tool = FileOperationsTool()

    flat = run(tool.execute(action="search", path=str(tmp_path), pattern="alpha"))
    assert flat.is_success
    assert "notes.txt:1" in flat.output

    recursive = run(
        tool.execute(action="search", path=str(tmp_path), pattern="alpha", recursive=True)
    )
    assert recursive.is_success
    assert "nested.txt:1" in recursive.output

    none = run(tool.execute(action="search", path=str(tmp_path), pattern="zebra"))
    assert none.is_success
    assert "No matches found" in none.output

    no_pattern = run(tool.execute(action="search", path=str(tmp_path), pattern=""))
    assert no_pattern.status is ToolStatus.ERROR
    assert "pattern is required" in (no_pattern.error or "")

    missing = run(tool.execute(action="search", path=str(tmp_path / "ghost"), pattern="x"))
    assert missing.status is ToolStatus.ERROR
    assert "Path not found" in (missing.error or "")


def test_delete_removes_files_and_directories(tmp_path) -> None:
    target = _seed(tmp_path)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "inner.txt").write_text("x", encoding="utf-8")
    tool = FileOperationsTool()

    removed = run(tool.execute(action="delete", path=str(target)))
    assert removed.is_success
    assert "Deleted file" in removed.output
    assert not target.exists()

    removed_tree = run(tool.execute(action="delete", path=str(tree)))
    assert removed_tree.is_success
    assert "Deleted directory" in removed_tree.output
    assert not tree.exists()

    missing = run(tool.execute(action="delete", path=str(target)))
    assert missing.status is ToolStatus.ERROR
    assert "File not found" in (missing.error or "")


def test_info_reports_type_size_and_access(tmp_path) -> None:
    target = _seed(tmp_path)

    file_info = run(FileOperationsTool().execute(action="info", path=str(target)))
    assert file_info.is_success
    assert "Type: File" in file_info.output
    assert "Readable: True" in file_info.output

    dir_info = run(FileOperationsTool().execute(action="info", path=str(tmp_path)))
    assert dir_info.is_success
    assert "Type: Directory" in dir_info.output

    missing = run(FileOperationsTool().execute(action="info", path=str(tmp_path / "ghost")))
    assert missing.status is ToolStatus.ERROR
    assert "Path not found" in (missing.error or "")


def test_unknown_action_missing_path_and_type_errors_are_contained() -> None:
    tool = FileOperationsTool()

    unknown = run(tool.execute(action="teleport", path="x"))
    assert unknown.status is ToolStatus.ERROR
    assert "Unknown action" in (unknown.error or "")

    no_path = run(tool.execute(action="read", path=""))
    assert no_path.status is ToolStatus.ERROR
    assert "Path is required" in (no_path.error or "")

    bad_type = run(tool.execute(action="list", path=".", max_results="many"))
    assert bad_type.status is ToolStatus.ERROR


def test_risk_for_maps_actions_to_declared_risks() -> None:
    tool = FileOperationsTool()

    assert tool.risk_for({"action": "read"}) is ToolRisk.SAFE
    assert tool.risk_for({"action": "write"}) is ToolRisk.CAUTION
    assert tool.risk_for({"action": "delete"}) is ToolRisk.DANGEROUS
    # An unknown action fails closed as dangerous.
    assert tool.risk_for({"action": "teleport"}) is ToolRisk.DANGEROUS


def test_format_size_covers_every_unit() -> None:
    fmt = FileOperationsTool._format_size

    assert fmt(10) == "10.0 B"
    assert fmt(2048) == "2.0 KB"
    assert fmt(1024 * 1024) == "1.0 MB"
    assert fmt(1024**3) == "1.0 GB"
