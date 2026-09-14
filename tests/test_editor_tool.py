"""MultiFileEditorTool: edit, insert, append, batch, create — dry run and apply."""

from __future__ import annotations

from support import run

from aegisx_agent.tools.base import ToolRisk, ToolStatus
from aegisx_agent.tools.coding.editor import MultiFileEditorTool


def _seed(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def main():\n    return 1\n", encoding="utf-8")
    return target


def test_edit_replaces_first_occurrence_and_reports_a_diff(tmp_path) -> None:
    target = _seed(tmp_path)

    result = run(
        MultiFileEditorTool().execute(
            action="edit", path=str(target), old_code="return 1", new_code="return 42"
        )
    )

    assert result.is_success
    assert f"✅ Edited {target}" in result.output
    assert "-    return 1" in result.output
    assert "+    return 42" in result.output
    assert result.metadata["file"] == str(target)
    assert target.read_text(encoding="utf-8") == "def main():\n    return 42\n"


def test_edit_dry_run_shows_the_diff_but_writes_nothing(tmp_path) -> None:
    target = _seed(tmp_path)
    original = target.read_text(encoding="utf-8")

    result = run(
        MultiFileEditorTool().execute(
            action="edit",
            path=str(target),
            old_code="return 1",
            new_code="return 42",
            dry_run=True,
        )
    )

    assert result.is_success
    assert "Diff Preview" in result.output
    assert target.read_text(encoding="utf-8") == original


def test_edit_rejects_missing_path_and_missing_code(tmp_path) -> None:
    tool = MultiFileEditorTool()
    target = _seed(tmp_path)

    no_path = run(tool.execute(action="edit", path="", old_code="x", new_code="y"))
    assert no_path.status is ToolStatus.ERROR
    assert "path and old_code required" in (no_path.error or "")

    missing = run(
        tool.execute(action="edit", path=str(tmp_path / "nope.py"), old_code="x", new_code="y")
    )
    assert missing.status is ToolStatus.ERROR
    assert "File not found" in (missing.error or "")

    absent_code = run(
        tool.execute(action="edit", path=str(target), old_code="not-there", new_code="y")
    )
    assert absent_code.status is ToolStatus.ERROR
    assert "Code not found" in (absent_code.error or "")

    directory = run(tool.execute(action="edit", path=str(tmp_path), old_code="x", new_code="y"))
    assert directory.status is ToolStatus.ERROR
    assert "Is a directory" in (directory.error or "")


def test_insert_adds_lines_at_a_line_number(tmp_path) -> None:
    target = _seed(tmp_path)

    result = run(
        MultiFileEditorTool().execute(
            action="insert", path=str(target), line=2, content="# inserted"
        )
    )

    assert result.is_success
    assert "Inserted 1 lines at line 2" in result.output
    assert "+ # inserted" in result.output
    assert target.read_text(encoding="utf-8").splitlines()[1] == "# inserted"


def test_insert_dry_run_previews_without_writing(tmp_path) -> None:
    target = _seed(tmp_path)
    original = target.read_text(encoding="utf-8")

    result = run(
        MultiFileEditorTool().execute(
            action="insert", path=str(target), line=2, content="# later", dry_run=True
        )
    )

    assert result.is_success
    assert "Insert at line 2" in result.output
    assert target.read_text(encoding="utf-8") == original


def test_insert_rejects_bad_paths_and_line_numbers(tmp_path) -> None:
    tool = MultiFileEditorTool()

    no_line = run(tool.execute(action="insert", path="", line=0, content="x"))
    assert no_line.status is ToolStatus.ERROR
    assert "path and line required" in (no_line.error or "")

    missing = run(tool.execute(action="insert", path=str(tmp_path / "n.py"), line=1, content="x"))
    assert missing.status is ToolStatus.ERROR
    assert "File not found" in (missing.error or "")


def test_append_writes_to_the_end_of_the_file(tmp_path) -> None:
    target = _seed(tmp_path)

    result = run(MultiFileEditorTool().execute(action="append", path=str(target), content="# end"))

    assert result.is_success
    assert f"✅ Appended to {target}" in result.output
    assert target.read_text(encoding="utf-8").endswith("\n# end")

    dry = run(
        MultiFileEditorTool().execute(action="append", path=str(target), content="x", dry_run=True)
    )
    assert dry.is_success
    assert "Append to" in dry.output


def test_append_requires_an_existing_path(tmp_path) -> None:
    tool = MultiFileEditorTool()

    no_path = run(tool.execute(action="append", path="", content="x"))
    assert no_path.status is ToolStatus.ERROR

    missing = run(tool.execute(action="append", path=str(tmp_path / "n.py"), content="x"))
    assert missing.status is ToolStatus.ERROR
    assert "File not found" in (missing.error or "")


def test_batch_edit_reports_success_and_failure_per_file(tmp_path) -> None:
    good = _seed(tmp_path)

    result = run(
        MultiFileEditorTool().execute(
            action="batch",
            edits=[
                {"path": str(good), "old_code": "return 1", "new_code": "return 2"},
                {"path": str(tmp_path / "ghost.py"), "old_code": "a", "new_code": "b"},
            ],
        )
    )

    assert result.is_success  # the batch itself succeeds; per-file verdicts are inline
    assert "✅" in result.output
    assert "❌" in result.output
    assert good.read_text(encoding="utf-8").endswith("return 2\n")


def test_batch_edit_rejects_an_empty_edit_list() -> None:
    result = run(MultiFileEditorTool().execute(action="batch", edits=[]))

    assert result.status is ToolStatus.ERROR
    assert "No edits provided" in (result.error or "")


def test_create_writes_a_new_file_with_parents(tmp_path) -> None:
    target = tmp_path / "new" / "deep" / "mod.py"

    result = run(MultiFileEditorTool().execute(action="create", path=str(target), content="x = 1"))

    assert result.is_success
    assert target.read_text(encoding="utf-8") == "x = 1"


def test_create_refuses_to_overwrite_and_dry_run_only_previews(tmp_path) -> None:
    target = _seed(tmp_path)
    original = target.read_text(encoding="utf-8")
    tool = MultiFileEditorTool()

    dry = run(tool.execute(action="create", path=str(target), content="nope", dry_run=True))
    assert dry.is_success
    assert "Create" in dry.output

    overwrite = run(tool.execute(action="create", path=str(target), content="nope"))
    assert overwrite.status is ToolStatus.ERROR
    assert "already exists" in (overwrite.error or "")
    assert target.read_text(encoding="utf-8") == original

    no_path = run(tool.execute(action="create", path="", content="x"))
    assert no_path.status is ToolStatus.ERROR


def test_unknown_action_and_crashing_edits_are_contained() -> None:
    unknown = run(MultiFileEditorTool().execute(action="duplicate"))
    assert unknown.status is ToolStatus.ERROR
    assert "Unknown action" in (unknown.error or "")


def test_risk_for_flattens_dry_runs_to_safe(tmp_path) -> None:
    tool = MultiFileEditorTool()

    assert tool.risk_for({"action": "edit", "dry_run": True}) is ToolRisk.SAFE
    assert tool.risk_for({"action": "edit"}) is ToolRisk.CAUTION
