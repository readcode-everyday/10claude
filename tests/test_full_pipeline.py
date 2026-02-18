"""
Integration test: full pipeline from task through to result.
All external calls (LLM, claude CLI, config files) are mocked.
"""
import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

TASK = {"id": "integ-test-001", "prompt": "Write a hello world Python script"}
ARCH_CFG = {"protected_paths": []}
PARALLELISM_CFG = {"parallelism": {"max_workers": 3}}

FAKE_GOALS = '[{"goal": "write hello.py", "done_when": "file contains print statement"}]'
FAKE_DAG = json.dumps({
    "nodes": [
        {"id": "research",  "depends_on": [],                              "action": "reason", "retry": 0},
        {"id": "implement", "depends_on": [],                              "action": "code",   "retry": 0},
        {"id": "tests",     "depends_on": [],                              "action": "code",   "retry": 0},
        {"id": "integrate", "depends_on": ["research","implement","tests"],"action": "code",   "retry": 0},
    ]
})


def _mock_llm(prompt, system=None):
    p = prompt.lower()
    if "senior" in p or "break" in p or "sub-goal" in p:
        return FAKE_GOALS
    if "planner" in p or "parallel" in p or "dag" in p:
        return FAKE_DAG
    return "true"  # goal evaluator


def _mock_execute(task, node):
    return True, f"[mock] completed {node['id']}"


def test_full_pipeline_succeeds(tmp_path):
    # Patch the local `llm_call` names in each module (imported by-value)
    with patch("openclaw.tools.hierarchical_planner.llm_call", side_effect=_mock_llm), \
         patch("openclaw.tools.dag_planner.llm_call", side_effect=_mock_llm), \
         patch("openclaw.tools.goal_evaluator.llm_call", side_effect=_mock_llm), \
         patch("openclaw.tools.dag_planner._arch_cfg", ARCH_CFG), \
         patch("openclaw.tools.parallel_executor._parallelism_cfg", PARALLELISM_CFG), \
         patch("openclaw.tools.parallel_executor.execute_node", side_effect=_mock_execute), \
         patch("openclaw.tools.parallel_executor.detect_major_change", return_value=False), \
         patch("openclaw.tools.semantic_retrieval.MEMORY_DIR", tmp_path), \
         patch("openclaw.tools.world_model.WORLD_FILE", tmp_path / "world.json"), \
         patch("openclaw.tools.semantic_memory.ENTRIES", tmp_path / "entries"):
        from openclaw.tools.long_horizon_loop import execute_long_horizon
        result = execute_long_horizon(TASK)

    assert result["success"] is True, f"Expected success, got: {result}"
    assert "subgoals" in result
    assert len(result["subgoals"]) == 1


def test_pipeline_records_failure_on_dag_fail(tmp_path):
    def _fail(task, node):
        return False, "simulated failure"

    with patch("openclaw.tools.hierarchical_planner.llm_call", side_effect=_mock_llm), \
         patch("openclaw.tools.dag_planner.llm_call", side_effect=_mock_llm), \
         patch("openclaw.tools.goal_evaluator.llm_call", side_effect=_mock_llm), \
         patch("openclaw.tools.dag_planner._arch_cfg", ARCH_CFG), \
         patch("openclaw.tools.parallel_executor._parallelism_cfg", PARALLELISM_CFG), \
         patch("openclaw.tools.parallel_executor.execute_node", side_effect=_fail), \
         patch("openclaw.tools.parallel_executor.detect_major_change", return_value=False), \
         patch("openclaw.tools.semantic_retrieval.MEMORY_DIR", tmp_path), \
         patch("openclaw.tools.world_model.WORLD_FILE", tmp_path / "world.json"), \
         patch("openclaw.tools.semantic_memory.ENTRIES", tmp_path / "entries"):
        from openclaw.tools.long_horizon_loop import execute_long_horizon
        result = execute_long_horizon(TASK)

    assert result["success"] is False
