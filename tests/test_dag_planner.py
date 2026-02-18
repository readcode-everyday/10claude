"""Tests for dag_planner — runs without API keys or /app/config paths."""
import pytest
from unittest.mock import patch

ARCH_CFG = {"protected_paths": ["/app/config", "/workspace/memory"]}
TASK = {"id": "t1", "prompt": "Write a hello world Python script"}


def test_default_dag_structure():
    """Default DAG must have 3+ parallel root nodes for parallel execution."""
    from openclaw.tools.dag_planner import _default_dag
    dag = _default_dag()
    assert "nodes" in dag
    root_nodes = [n for n in dag["nodes"] if not n.get("depends_on")]
    assert len(root_nodes) >= 3, "Need at least 3 parallel root nodes"


def test_plan_dag_returns_valid_dag_on_llm_failure():
    """When LLM fails, plan_dag must return a usable default DAG."""
    with patch("openclaw.tools.dag_planner._arch_cfg", ARCH_CFG):
        with patch("openclaw.tools.dag_planner.llm_call", side_effect=RuntimeError("no key")):
            with patch("openclaw.tools.dag_planner.retrieve_relevant", return_value=[]):
                from openclaw.tools.dag_planner import plan_dag
                dag = plan_dag(TASK)
    assert "nodes" in dag
    assert len(dag["nodes"]) >= 2


def test_plan_dag_parses_valid_llm_json():
    """When LLM returns valid JSON, plan_dag uses it."""
    fake_response = """
    {
      "nodes": [
        {"id": "step1", "depends_on": [], "action": "code", "retry": 0},
        {"id": "step2", "depends_on": [], "action": "code", "retry": 0}
      ]
    }
    """
    with patch("openclaw.tools.dag_planner._arch_cfg", ARCH_CFG):
        with patch("openclaw.tools.dag_planner.llm_call", return_value=fake_response):
            with patch("openclaw.tools.dag_planner.retrieve_relevant", return_value=[]):
                from openclaw.tools.dag_planner import plan_dag
                dag = plan_dag(TASK)
    assert dag["nodes"][0]["id"] == "step1"


def test_plan_dag_falls_back_on_bad_json():
    """Malformed LLM JSON triggers fallback."""
    with patch("openclaw.tools.dag_planner._arch_cfg", ARCH_CFG):
        with patch("openclaw.tools.dag_planner.llm_call", return_value="not json at all"):
            with patch("openclaw.tools.dag_planner.retrieve_relevant", return_value=[]):
                from openclaw.tools.dag_planner import plan_dag
                dag = plan_dag(TASK)
    assert "nodes" in dag


def test_protected_path_uses_default():
    """Tasks mentioning protected paths must use the safe default DAG."""
    task = {"id": "t2", "prompt": "modify /app/config/architecture.yaml"}
    with patch("openclaw.tools.dag_planner._arch_cfg", ARCH_CFG):
        with patch("openclaw.tools.dag_planner.retrieve_relevant", return_value=[]):
            from openclaw.tools.dag_planner import plan_dag
            dag = plan_dag(task)
    assert "nodes" in dag
