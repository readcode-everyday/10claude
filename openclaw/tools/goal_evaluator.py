"""
After each sub-goal's DAG completes, evaluates whether done_when is satisfied.
When the DAG reports full success, that is treated as strong evidence and the
LLM acts as a secondary validator — it must explicitly say "false" to override.
"""
import logging
from typing import Dict, Any

from openclaw.adapters.llm.client import call as llm_call

log = logging.getLogger(__name__)


def evaluate_goal(goal: str, done_when: str, evidence: Dict[str, Any]) -> bool:
    # If execution itself failed, don't bother asking the LLM
    if not evidence.get("success"):
        log.info(f"Goal eval | '{goal[:60]}' | execution failed → False")
        return False

    # All nodes succeeded — ask LLM to verify against the done_when criterion
    prompt = f"""A software task just completed successfully.

Goal: {goal}
Success criterion: {done_when}
Execution result: all subtasks completed without errors.

Did this execution satisfy the success criterion?
Answer with a single word: true or false."""

    try:
        out = llm_call(prompt).strip().lower()
        # Only override to False if the LLM explicitly leads with "false"
        if out.startswith("false"):
            log.info(f"Goal eval | '{goal[:60]}' | LLM says not satisfied")
            return False
        log.info(f"Goal eval | '{goal[:60]}' | satisfied ✓")
        return True
    except Exception as e:
        log.warning(f"Goal evaluation LLM call failed ({e}) — trusting execution result")
        return True
