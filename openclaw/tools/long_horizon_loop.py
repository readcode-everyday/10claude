"""
The autonomous multi-turn execution loop.
Decomposes a task → for each sub-goal: plan DAG → execute in parallel →
evaluate done_when criterion → checkpoint if major change → advance.
"""
import time
import logging
from typing import Dict, Any

from openclaw.tools.hierarchical_planner import decompose_goal
from openclaw.tools.dag_planner import plan_dag
from openclaw.tools.parallel_executor import execute_dag_parallel
from openclaw.tools.goal_evaluator import evaluate_goal
from openclaw.tools.semantic_memory import store_experience
from openclaw.tools.world_model import record_failure, record_goal_progress

log = logging.getLogger(__name__)


def execute_long_horizon(task: Dict[str, Any]) -> Dict[str, Any]:
    task_id = task.get("id", "?")
    log.info(f"[loop] task='{task_id}' starting long-horizon execution")

    subgoals = decompose_goal(task)
    log.info(f"[loop] {len(subgoals)} sub-goals planned")

    results = []

    for i, sg in enumerate(subgoals, 1):
        goal = sg["goal"]
        done_when = sg.get("done_when", "implementation complete")
        log.info(f"[loop] sub-goal {i}/{len(subgoals)}: '{goal}' | done_when='{done_when}'")

        subtask = {"prompt": goal, "id": f"{task_id}-sg{i}", "project_path": task.get("project_path")}
        dag = plan_dag(subtask)
        result = execute_dag_parallel(subtask, dag)

        if not result.get("success"):
            log.error(f"[loop] sub-goal {i} DAG failed: {result}")
            record_failure({"task_id": task_id, "goal": goal, "error": result})
            return {"success": False, "failed_goal": goal, "detail": result}

        satisfied = evaluate_goal(goal, done_when, result)
        if not satisfied:
            log.error(f"[loop] sub-goal {i} done_when not satisfied")
            record_failure({"task_id": task_id, "goal": goal, "reason": "done_when not satisfied"})
            return {"success": False, "unsatisfied_goal": goal}

        record_goal_progress(goal, "done")
        store_experience(task, dag)
        results.append({"goal": goal, "done_when": done_when, "result": result})
        log.info(f"[loop] sub-goal {i} complete ✓")

        time.sleep(0.5)  # brief pause between sub-goals

    log.info(f"[loop] task='{task_id}' all sub-goals complete")
    return {"success": True, "subgoals": results}
