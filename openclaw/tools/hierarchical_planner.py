"""
Decomposes a high-level goal into ordered sub-goals, each with a done_when
success criterion. This is the multi-turn planning layer above DAG planning.
"""
import json
import logging
from typing import List, Dict, Any

from openclaw.adapters.llm.client import call as llm_call
from openclaw.tools.semantic_retrieval import retrieve_relevant

log = logging.getLogger(__name__)


def decompose_goal(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    memories = retrieve_relevant(task)

    prompt = f"""You are a senior software architect acting as a dev manager.

Goal: {task.get("prompt")}

Past experience:
{json.dumps(memories, indent=2)}

Break this into 2-4 ordered sub-goals. Each sub-goal must have a concrete,
testable done_when success criterion.

Return ONLY a JSON list, no explanation:
[{{"goal": "...", "done_when": "..."}}]"""

    try:
        raw = llm_call(prompt)
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON list found")
        goals = json.loads(raw[start:end])
        if not goals:
            raise ValueError("Empty goal list")
        log.info(f"Decomposed into {len(goals)} sub-goals: {[g['goal'][:40] for g in goals]}")
        return goals
    except Exception as e:
        log.warning(f"Goal decomposition failed ({e}) — single-goal fallback")
        return [{"goal": task.get("prompt"), "done_when": "implementation complete and tests pass"}]
