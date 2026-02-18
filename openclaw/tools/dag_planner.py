"""
Converts a task into a DAG of parallel subtask nodes.
3 independent nodes run first so workers start in parallel immediately.
"""
import json
import logging
import yaml
from pathlib import Path
from typing import Dict, Any

from openclaw.adapters.llm.client import call as llm_call
from openclaw.tools.semantic_retrieval import retrieve_relevant

log = logging.getLogger(__name__)

_arch_cfg = None


def _arch():
    global _arch_cfg
    if _arch_cfg is None:
        _arch_cfg = yaml.safe_load(Path("/app/config/architecture.yaml").read_text())
    return _arch_cfg


def _default_dag() -> Dict[str, Any]:
    """
    Default DAG: 3 independent nodes (research, implement, tests) run in parallel,
    then integrate depends on all three. Guarantees 3 workers are always busy.
    """
    return {
        "nodes": [
            {"id": "research",   "depends_on": [],                             "action": "reason", "retry": 0},
            {"id": "implement",  "depends_on": [],                             "action": "code",   "retry": 2},
            {"id": "tests",      "depends_on": [],                             "action": "code",   "retry": 1},
            {"id": "integrate",  "depends_on": ["research", "implement", "tests"], "action": "code", "retry": 1},
        ]
    }


def _protected_violation(task: Dict[str, Any]) -> bool:
    prompt = task.get("prompt", "").lower()
    for p in _arch().get("protected_paths", []):
        if p.lower() in prompt:
            return True
    return False


def plan_dag(task: Dict[str, Any]) -> Dict[str, Any]:
    if _protected_violation(task):
        log.warning("Protected path in prompt — using default DAG")
        return _default_dag()

    memories = retrieve_relevant(task)

    prompt = f"""You are an autonomous software planner.

Task: {task.get("prompt")}

Relevant past experience:
{json.dumps(memories, indent=2)}

Return ONLY valid JSON with a DAG. Start with at least 3 independent nodes (depends_on: []) \
so workers run in parallel from the beginning.

Schema:
{{
  "nodes": [
    {{"id": "node_name", "depends_on": ["other_id"], "action": "reason|code|test", "retry": 0}}
  ]
}}

Return only the JSON, no explanation."""

    try:
        raw = llm_call(prompt)
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return _default_dag()
        dag = json.loads(raw[start:end])
        if "nodes" not in dag or len(dag["nodes"]) < 2:
            return _default_dag()
        log.info(f"DAG planned: {[n['id'] for n in dag['nodes']]}")
        return dag
    except Exception as e:
        log.warning(f"DAG planning failed ({e}) — using default")
        return _default_dag()
