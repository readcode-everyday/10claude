"""
Parallel DAG executor with dependency resolution, per-node retry,
and automatic checkpoint/snapshot after a successful run.
"""
import time
import logging
import yaml
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Any

from openclaw.tools.coding_agent_router import execute_node
from openclaw.tools.progress_memory import detect_major_change, checkpoint_code, snapshot_workspace

log = logging.getLogger(__name__)

_parallelism_cfg = None


def _max_workers() -> int:
    global _parallelism_cfg
    if _parallelism_cfg is None:
        _parallelism_cfg = yaml.safe_load(
            Path("/app/config/runtime_parallelism.yaml").read_text()
        )
    return _parallelism_cfg.get("parallelism", {}).get("max_workers", 3)


class _RetryPolicy:
    def __init__(self, retries: int, base_delay: float = 2.0):
        self.retries = retries
        self.base_delay = base_delay

    def backoff(self, attempt: int) -> float:
        return self.base_delay * (2 ** attempt)


def _run_with_retry(task: Dict[str, Any], node: Dict[str, Any]) -> tuple:
    retries = node.get("retry", 0)
    policy = _RetryPolicy(retries)

    for attempt in range(retries + 1):
        success, output = execute_node(task, node)
        if success:
            return True, output
        if attempt < retries:
            delay = policy.backoff(attempt)
            log.warning(f"node='{node['id']}' attempt {attempt+1}/{retries+1} failed — retry in {delay}s")
            time.sleep(delay)

    return False, output


def execute_dag_parallel(task: Dict[str, Any], dag: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a DAG with full parallelism. Independent nodes run concurrently;
    dependent nodes wait only until their specific dependencies finish.

    Loop structure (avoids premature deadlock detection):
      1. Submit all newly-runnable nodes
      2. If nothing is pending → true deadlock
      3. Collect any completed futures; mark failures immediately
      4. Sleep briefly if nothing completed this cycle
    """
    nodes = {n["id"]: n for n in dag["nodes"]}
    completed: Dict[str, bool] = {}   # node_id → True/False
    pending: Dict[str, Any] = {}      # node_id → Future
    workers = _max_workers()

    log.info(f"DAG execution started | nodes={list(nodes)} | max_workers={workers}")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while len(completed) < len(nodes):

            # ── 1. Submit all nodes whose deps are satisfied ──────────────
            for node_id, node in nodes.items():
                if node_id in completed or node_id in pending:
                    continue
                deps = node.get("depends_on", [])
                if all(completed.get(d) is True for d in deps):
                    log.info(f"[executor] submitting node='{node_id}'")
                    pending[node_id] = pool.submit(_run_with_retry, task, node)

            # ── 2. True deadlock: nothing running, nothing left to submit ─
            if not pending:
                unfinished = [n for n in nodes if n not in completed]
                log.error(f"DAG deadlock — unfinished: {unfinished}")
                return {"success": False, "error": "deadlock", "unfinished": unfinished}

            # ── 3. Collect completed futures ──────────────────────────────
            collected = False
            for node_id, fut in list(pending.items()):
                if fut.done():
                    success, output = fut.result()
                    completed[node_id] = success
                    del pending[node_id]
                    status = "succeeded" if success else "FAILED"
                    log.info(f"[executor] node='{node_id}' {status}")
                    if not success:
                        # Cancel remaining work and report failure
                        for f in pending.values():
                            f.cancel()
                        return {"success": False, "failed_nodes": [node_id], "nodes": completed}
                    collected = True

            # ── 4. Yield if nothing finished this cycle ───────────────────
            if not collected:
                time.sleep(0.1)

    all_ok = all(completed.values())
    if all_ok:
        log.info("DAG complete — checking for major change")
        if detect_major_change():
            checkpoint_code("auto-checkpoint: post-DAG major change")
            snapshot_workspace()

    return {"success": all_ok, "nodes": completed}
