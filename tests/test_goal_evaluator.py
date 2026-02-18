"""Tests for goal_evaluator."""
import pytest
from unittest.mock import patch


def test_returns_true_when_llm_says_true():
    with patch("openclaw.tools.goal_evaluator.llm_call", return_value="true"):
        from openclaw.tools.goal_evaluator import evaluate_goal
        assert evaluate_goal("scaffold project", "directory exists", {"success": True}) is True


def test_returns_false_when_llm_says_false():
    with patch("openclaw.tools.goal_evaluator.llm_call", return_value="false, tests are missing"):
        from openclaw.tools.goal_evaluator import evaluate_goal
        assert evaluate_goal("write tests", "tests pass", {"success": True}) is False


def test_defaults_true_on_llm_failure():
    with patch("openclaw.tools.goal_evaluator.llm_call", side_effect=RuntimeError("no key")):
        from openclaw.tools.goal_evaluator import evaluate_goal
        assert evaluate_goal("some goal", "some criterion", {"success": True}) is True
