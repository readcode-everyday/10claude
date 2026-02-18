"""
Routes each DAG node to the configured coding agent (claude or codex).
Each node gets its own isolated git worktree.
"""
import logging
import yaml
from pathlib import Path
from typing import Tuple

from openclaw.adapters.git.repo import create_worktree, remove_worktree, REPO_ROOT

log = logging.getLogger(__name__)

_cfg = None


def _config() -> dict:
    global _cfg
    if _cfg is None:
        _cfg = yaml.safe_load(Path("/app/config/coding_agents.yaml").read_text())
    return _cfg


def execute_node(task: dict, node: dict) -> Tuple[bool, str]:
    agent = _config().get("default", "claude")
    node_id = node["id"]
    action = node.get("action", "code")
    task_id = task.get("id", "task")

    prompt = (
        f"Task: {task.get('prompt')}\n\n"
        f"Your specific subtask: {node_id} ({action})\n"
        f"Focus only on this subtask. Be concise and complete."
    )

    log.info(f"[worker] node='{node_id}' action='{action}' agent='{agent}' task='{task_id}'")

    repo_root = Path(task.get("project_path", str(REPO_ROOT)))
    wt_name = f"{task_id}-{node_id}"
    wt_path = str(create_worktree(wt_name, repo_root))

    try:
        if agent == "claude":
            from openclaw.adapters.coder.claude_cli import run
        elif agent == "codex":
            from openclaw.adapters.coder.codex_cli import run
        else:
            log.error(f"Unknown agent: {agent}")
            return False, f"Unknown agent: {agent}"

        output = run(prompt, cwd=wt_path)
        log.info(f"[worker] DONE node='{node_id}' output_len={len(output)}")
        return True, output

    except Exception as e:
        log.error(f"[worker] FAILED node='{node_id}': {e}")
        return False, str(e)

    finally:
        remove_worktree(wt_name, repo_root)
