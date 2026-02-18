"""Tests for hierarchical_planner."""
import pytest
from unittest.mock import patch


TASK = {"id": "t1", "prompt": "Build a REST API with authentication"}


def test_decompose_returns_list_of_goals():
    fake = '[{"goal": "scaffold", "done_when": "structure exists"}, {"goal": "implement", "done_when": "tests pass"}]'
    with patch("openclaw.tools.hierarchical_planner.llm_call", return_value=fake):
        with patch("openclaw.tools.hierarchical_planner.retrieve_relevant", return_value=[]):
            from openclaw.tools.hierarchical_planner import decompose_goal
            goals = decompose_goal(TASK)
    assert len(goals) == 2
    assert goals[0]["goal"] == "scaffold"
    assert "done_when" in goals[0]


def test_fallback_on_llm_failure():
    with patch("openclaw.tools.hierarchical_planner.llm_call", side_effect=RuntimeError("no key")):
        with patch("openclaw.tools.hierarchical_planner.retrieve_relevant", return_value=[]):
            from openclaw.tools.hierarchical_planner import decompose_goal
            goals = decompose_goal(TASK)
    assert len(goals) == 1
    assert goals[0]["goal"] == TASK["prompt"]


def test_fallback_on_bad_json():
    with patch("openclaw.tools.hierarchical_planner.llm_call", return_value="not json"):
        with patch("openclaw.tools.hierarchical_planner.retrieve_relevant", return_value=[]):
            from openclaw.tools.hierarchical_planner import decompose_goal
            goals = decompose_goal(TASK)
    assert isinstance(goals, list)
    assert len(goals) >= 1
