"""
Runtime daemon — polls the filesystem queue and executes tasks via
the long-horizon autonomous loop.
"""
import json
import logging
import time
from pathlib import Path

from openclaw.adapters.git.repo import ensure_repo, REPO_ROOT
from openclaw.tools.long_horizon_loop import execute_long_horizon

# ── Logging setup ────────────────────────────────────────────────────────────
LOG_PATH = Path("/logs/daemon.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH),
    ],
)
log = logging.getLogger("runtime_engine")

# ── Paths ─────────────────────────────────────────────────────────────────────
QUEUE   = Path("/workspace/queue/tasks.jsonl")
RESULTS = Path("/workspace/results")
RESULTS.mkdir(parents=True, exist_ok=True)


def _load_tasks():
    if not QUEUE.exists():
        return []
    lines = QUEUE.read_text().splitlines()
    tasks = []
    for line in lines:
        line = line.strip()
        if line:
            try:
                tasks.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning(f"Skipping malformed queue line: {line[:80]}")
    return tasks


def _save_result(task_id: str, data: dict):
    path = RESULTS / f"{task_id}.json"
    path.write_text(json.dumps(data, indent=2))
    log.info(f"Result written: {path}")


def _run_task(task: dict):
    task_id = task.get("id", str(int(time.time())))
    project_path = Path(task["project_path"]) if task.get("project_path") else REPO_ROOT
    ensure_repo(project_path)
    log.info(f"─── Task start | id='{task_id}' project='{project_path.name}' prompt='{task.get('prompt','')[:80]}' ───")
    result = execute_long_horizon(task)
    _save_result(task_id, result)
    status = "SUCCESS" if result.get("success") else "FAILED"
    log.info(f"─── Task {status} | id='{task_id}' ───")


def main():
    log.info("Runtime daemon starting")

    seen: set = set()

    while True:
        for task in _load_tasks():
            key = json.dumps(task, sort_keys=True)
            if key not in seen:
                seen.add(key)
                try:
                    _run_task(task)
                except Exception as e:
                    log.exception(f"Unhandled error in task: {e}")
        time.sleep(3)


if __name__ == "__main__":
    main()
