"""ReAct planning: plan serialisation, prompt rendering, and LLM response parsing."""

from __future__ import annotations

import json

from aegisx_agent.planning.react import ExecutionPlan, PlanBuilder, PlanStep

# --------------------------------------------------------------------------- #
# PlanStep / ExecutionPlan serialisation
# --------------------------------------------------------------------------- #


def test_plan_step_to_dict_keeps_every_field() -> None:
    step = PlanStep(
        step_number=2,
        thought="fetch the data",
        action="web_search",
        action_input={"query": "rate limits"},
        observation="found it",
        status="completed",
    )

    assert step.to_dict() == {
        "step": 2,
        "thought": "fetch the data",
        "action": "web_search",
        "action_input": {"query": "rate limits"},
        "observation": "found it",
        "status": "completed",
    }


def test_add_step_numbers_steps_and_returns_them() -> None:
    plan = ExecutionPlan(goal="ship the release")

    first = plan.add_step("tag the repo", action="shell", action_input={"cmd": "git tag"})
    second = plan.add_step("write notes")

    assert first.step_number == 1
    assert second.step_number == 2
    assert plan.steps == [first, second]
    assert plan.steps[0].status == "pending"


def test_plan_to_dict_round_trip_shape() -> None:
    plan = ExecutionPlan(goal="ship it", status="running", current_step=1)
    plan.add_step("tag", action="shell", action_input={"cmd": "git tag"})
    plan.steps[0].observation = "tagged"
    plan.steps[0].status = "completed"

    data = plan.to_dict()

    assert data["goal"] == "ship it"
    assert data["status"] == "running"
    assert data["current_step"] == 1
    assert data["steps"] == [plan.steps[0].to_dict()]


# --------------------------------------------------------------------------- #
# to_prompt rendering
# --------------------------------------------------------------------------- #


def test_to_prompt_renders_status_icons_actions_and_results() -> None:
    plan = ExecutionPlan(goal="investigate flaky tests")
    plan.add_step("read the CI log", action="read_file", action_input={"path": "ci.log"})
    plan.steps[0].status = "completed"
    plan.steps[0].observation = "timeout at step 12"
    plan.add_step("rerun locally")
    plan.steps[1].status = "running"
    plan.add_step("fix and push")
    plan.steps[2].status = "failed"
    plan.add_step("celebrate")  # stays pending

    prompt = plan.to_prompt()

    assert prompt.startswith("Plan for: investigate flaky tests")
    assert "Status: pending" in prompt
    assert "Step 1: ✅" in prompt
    assert "Step 2: 🔄" in prompt
    assert "Step 3: ❌" in prompt
    assert "Step 4: ⏳" in prompt
    assert "Thought: read the CI log" in prompt
    assert "Action: read_file({'path': 'ci.log'})" in prompt
    assert "Result: timeout at step 12" in prompt


def test_to_prompt_truncates_long_observations() -> None:
    plan = ExecutionPlan(goal="g")
    plan.add_step("think")
    plan.steps[0].observation = "x" * 500

    prompt = plan.to_prompt()

    assert "Result: " + "x" * 200 in prompt
    assert "x" * 201 not in prompt


def test_to_prompt_skips_missing_action_and_observation() -> None:
    plan = ExecutionPlan(goal="g")
    plan.add_step("pure reasoning step")

    prompt = plan.to_prompt()

    assert "Action:" not in prompt
    assert "Result:" not in prompt


# --------------------------------------------------------------------------- #
# PlanBuilder.build_planning_prompt
# --------------------------------------------------------------------------- #


def test_prompt_lists_the_available_tools() -> None:
    prompt = PlanBuilder().build_planning_prompt("clean the data", ["shell", "read_file"])

    assert "Available tools: shell, read_file" in prompt
    assert "Goal: clean the data" in prompt
    assert prompt.strip().endswith("Provide the plan as JSON:")


def test_prompt_without_tools_says_reasoning_only() -> None:
    prompt = PlanBuilder().build_planning_prompt("think deeply", [])

    assert "Available tools: none (reasoning only)" in prompt


# --------------------------------------------------------------------------- #
# PlanBuilder.parse_plan
# --------------------------------------------------------------------------- #


def _steps_payload() -> dict:
    return {
        "goal": "ignored, the caller supplies the goal",
        "steps": [
            {"thought": "search first", "action": "web_search", "action_input": {"q": "x"}},
            {"thought": "then reason about it", "action": None, "action_input": None},
        ],
    }


def test_parse_plan_reads_clean_json() -> None:
    plan = PlanBuilder().parse_plan(json.dumps(_steps_payload()), goal="my goal")

    assert plan.goal == "my goal"
    assert len(plan.steps) == 2
    assert plan.steps[0].action == "web_search"
    assert plan.steps[0].action_input == {"q": "x"}
    assert plan.steps[1].action is None
    assert plan.steps[0].status == "pending"


def test_parse_plan_extracts_json_surrounded_by_prose() -> None:
    response = (
        "Sure! Here is my plan:\n\n"
        + json.dumps(_steps_payload())
        + "\n\nHope that helps!"
    )

    plan = PlanBuilder().parse_plan(response, goal="g")

    assert len(plan.steps) == 2
    assert plan.steps[0].thought == "search first"


def test_parse_plan_falls_back_to_a_single_step_on_garbage() -> None:
    plan = PlanBuilder().parse_plan("I cannot produce JSON today, sorry.", goal="g")

    assert len(plan.steps) == 1
    assert plan.steps[0].thought == "I cannot produce JSON today, sorry."
    assert plan.steps[0].action is None


def test_parse_plan_falls_back_on_broken_json() -> None:
    plan = PlanBuilder().parse_plan('{"steps": [{"thought": "unterminated', goal="g")

    assert len(plan.steps) == 1
    assert '{"steps": [{"thought": "unterminated' in plan.steps[0].thought


def test_parse_plan_with_empty_steps_yields_an_empty_plan() -> None:
    plan = PlanBuilder().parse_plan('{"steps": []}', goal="g")

    assert plan.steps == []
    assert plan.goal == "g"


def test_parse_plan_survives_a_steps_field_that_is_not_a_list() -> None:
    """LLMs sometimes emit "steps": "string" — must degrade, not crash."""
    plan = PlanBuilder().parse_plan('{"steps": "just do the thing"}', goal="g")

    assert len(plan.steps) >= 1  # fallback path, not AttributeError


def test_parse_plan_survives_non_dict_step_entries() -> None:
    plan = PlanBuilder().parse_plan('{"steps": ["step one", "step two"]}', goal="g")

    assert len(plan.steps) >= 1  # fallback path, not AttributeError
