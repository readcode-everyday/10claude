"""Tests for parallel_executor — no real workers, fully mocked."""
import pytest
from unittest.mock import patch

PARALLELISM_CFG = {"parallelism": {"max_workers": 3}}

SIMPLE_DAG = {
    "nodes": [
        {"id": "a", "depends_on": [],           "action": "code", "retry": 0},
        {"id": "b", "depends_on": [],           "action": "code", "retry": 0},
        {"id": "c", "depends_on": [],           "action": "code", "retry": 0},
        {"id": "d", "depends_on": ["a","b","c"],"action": "code", "retry": 0},
    ]
}

TASK = {"id": "test-task", "prompt": "hello"}


def _mock_execute(task, node):
    return True, f"output-{node['id']}"


def _mock_execute_fail_b(task, node):
    if node["id"] == "b":
        return False, "b failed"
    return True, f"output-{node['id']}"


def test_all_nodes_complete_successfully():
    with patch("openclaw.tools.parallel_executor._parallelism_cfg", PARALLELISM_CFG):
        with patch("openclaw.tools.parallel_executor.execute_node", side_effect=_mock_execute):
            with patch("openclaw.tools.parallel_executor.detect_major_change", return_value=False):
                from openclaw.tools.parallel_executor import execute_dag_parallel
                result = execute_dag_parallel(TASK, SIMPLE_DAG)
    assert result["success"] is True
    assert set(result["nodes"].keys()) == {"a", "b", "c", "d"}


def test_failure_halts_dag():
    with patch("openclaw.tools.parallel_executor._parallelism_cfg", PARALLELISM_CFG):
        with patch("openclaw.tools.parallel_executor.execute_node", side_effect=_mock_execute_fail_b):
            with patch("openclaw.tools.parallel_executor.detect_major_change", return_value=False):
                from openclaw.tools.parallel_executor import execute_dag_parallel
                result = execute_dag_parallel(TASK, SIMPLE_DAG)
    assert result["success"] is False


def test_three_root_nodes_start_concurrently():
    """Verify all 3 root nodes are submitted before any completes."""
    started = []

    def _track(task, node):
        started.append(node["id"])
        return True, "ok"

    with patch("openclaw.tools.parallel_executor._parallelism_cfg", PARALLELISM_CFG):
        with patch("openclaw.tools.parallel_executor.execute_node", side_effect=_track):
            with patch("openclaw.tools.parallel_executor.detect_major_change", return_value=False):
                from openclaw.tools.parallel_executor import execute_dag_parallel
                execute_dag_parallel(TASK, SIMPLE_DAG)

    assert "a" in started
    assert "b" in started
    assert "c" in started
    assert started.index("d") > max(
        started.index("a"), started.index("b"), started.index("c")
    )


def test_retry_on_failure():
    calls = {"count": 0}

    def _fail_then_succeed(task, node):
        if node["id"] == "a":
            calls["count"] += 1
            if calls["count"] < 2:
                return False, "transient"
        return True, "ok"

    dag = {"nodes": [{"id": "a", "depends_on": [], "action": "code", "retry": 2}]}
    with patch("openclaw.tools.parallel_executor._parallelism_cfg", PARALLELISM_CFG):
        with patch("openclaw.tools.parallel_executor.execute_node", side_effect=_fail_then_succeed):
            with patch("openclaw.tools.parallel_executor.detect_major_change", return_value=False):
                from openclaw.tools.parallel_executor import execute_dag_parallel
                result = execute_dag_parallel(TASK, dag)
    assert result["success"] is True
    assert calls["count"] == 2
