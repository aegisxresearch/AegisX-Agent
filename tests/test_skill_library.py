"""Skill library: markdown round-trip, capture thresholds, usage statistics."""

from __future__ import annotations

from support import run

from aegisx_agent.skills.manager import SkillManager
from aegisx_agent.skills.skill import Skill


def test_skill_markdown_round_trip(tmp_path) -> None:
    manager = SkillManager(tmp_path)
    manager.create_skill(
        name="Fix Flaky Test",
        description="Stabilise a flaky pytest suite",
        steps=["Reproduce the failure", "Freeze the clock", "Re-run 50 times"],
        tags=["testing"],
        category="qa",
    )

    reloaded = SkillManager(tmp_path).get("Fix Flaky Test")

    assert reloaded is not None
    assert reloaded.description == "Stabilise a flaky pytest suite"
    assert reloaded.steps == [
        "Reproduce the failure",
        "Freeze the clock",
        "Re-run 50 times",
    ]
    assert reloaded.category == "qa"
    assert reloaded.tags == ["testing"]


def test_from_markdown_ignores_empty_tag_list() -> None:
    skill = Skill.from_markdown(
        "# Skill: bare\n\n**Description:** nothing fancy\n**Tags:**\n\n## Steps\n\n1. do it\n"
    )

    assert skill.name == "bare"
    assert skill.tags == []
    assert skill.steps == ["do it"]


def test_usage_statistics_survive_reload(tmp_path) -> None:
    manager = SkillManager(tmp_path)
    manager.create_skill("deploy k8s", "Roll out a release", ["apply manifests"])

    assert manager.record_use("deploy k8s", success=True) is True
    assert manager.record_use("deploy k8s", success=False) is True
    assert manager.record_use("missing skill", success=True) is False

    reloaded = SkillManager(tmp_path).get("deploy k8s")

    assert reloaded is not None
    assert reloaded.use_count == 2
    assert reloaded.success_count == 1
    assert reloaded.success_rate == 0.5


def test_search_matches_name_description_and_tags(tmp_path) -> None:
    manager = SkillManager(tmp_path)
    manager.create_skill("deploy k8s", "Roll out a release", ["apply"], tags=["k8s"])
    manager.create_skill("write docs", "Draft the README", ["outline"], tags=["docs"])

    assert [skill.name for skill in manager.search("k8s")] == ["deploy k8s"]
    assert [skill.name for skill in manager.search("readme")] == ["write docs"]
    assert [skill.name for skill in manager.search("nothing here")] == []


def test_update_skill_bumps_version_and_persists(tmp_path) -> None:
    manager = SkillManager(tmp_path)
    manager.create_skill("release", "Cut a release", ["tag"])

    updated = manager.update_skill("release", steps=["tag", "publish"])

    assert updated is not None
    assert updated.version == "1.0.1"

    reloaded = SkillManager(tmp_path).get("release")
    assert reloaded is not None
    assert reloaded.steps == ["tag", "publish"]


def test_delete_skill_removes_file_and_entry(tmp_path) -> None:
    manager = SkillManager(tmp_path)
    manager.create_skill("temporary", "Goes away", ["step"])
    assert (tmp_path / "temporary.md").exists()

    assert manager.delete_skill("temporary") is True
    assert manager.delete_skill("temporary") is False
    assert not (tmp_path / "temporary.md").exists()


def test_should_create_skill_thresholds(tmp_path) -> None:
    manager = SkillManager(tmp_path)

    assert manager.should_create_skill(
        tool_call_count=5, had_error_recovery=False, had_user_correction=False
    )
    assert manager.should_create_skill(
        tool_call_count=1, had_error_recovery=True, had_user_correction=False
    )
    assert manager.should_create_skill(
        tool_call_count=1, had_error_recovery=False, had_user_correction=True
    )
    assert not manager.should_create_skill(
        tool_call_count=1, had_error_recovery=False, had_user_correction=False
    )


def test_skill_tool_lists_searches_and_loads(tmp_path) -> None:
    from aegisx_agent.tools.skill_tool import SkillTool

    manager = SkillManager(tmp_path)
    manager.create_skill("deploy k8s", "Roll out a release", ["apply manifests"], tags=["k8s"])
    tool = SkillTool(manager)

    listing = run(tool.execute(action="list"))
    assert listing.is_success
    assert "deploy k8s" in listing.output

    found = run(tool.execute(action="search", query="k8s"))
    assert found.is_success
    assert "deploy k8s" in found.output

    loaded = run(tool.execute(action="load", name="deploy k8s"))
    assert loaded.is_success
    assert "Step 1: apply manifests" in loaded.output

    stored = manager.get("deploy k8s")
    assert stored is not None
    assert stored.use_count == 1


def test_skill_tool_rejects_bad_requests(tmp_path) -> None:
    from aegisx_agent.tools.base import ToolStatus
    from aegisx_agent.tools.skill_tool import SkillTool

    tool = SkillTool(SkillManager(tmp_path))

    assert run(tool.execute(action="nope")).status is ToolStatus.ERROR
    assert run(tool.execute(action="load")).status is ToolStatus.ERROR
    assert run(tool.execute(action="search")).status is ToolStatus.ERROR
    assert run(tool.execute(action="load", name="ghost")).status is ToolStatus.ERROR
