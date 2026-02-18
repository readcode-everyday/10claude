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
QUEUE        = Path("/workspace/queue/tasks.jsonl")
RESULTS      = Path("/workspace/results")
CURRENT_TASK = Path("/workspace/queue/current_task")  # read by status_ui
RESULTS.mkdir(parents=True, exist_ok=True)


def _load_tasks():
    if not QUEUE.exists():
        return []
    tasks = []
    for line in QUEUE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                tasks.append(json.loads(line))
            except json.JSONDecodeError:
                log.warning(f"Skipping malformed queue line: {line[:80]}")
    return tasks


def _can_run(task: dict) -> bool:
    """Check that the prerequisite task (if any) has completed successfully."""
    dep = task.get("depends_on")
    if not dep:
        return True
    result_file = RESULTS / f"{dep}.json"
    if not result_file.exists():
        return False
    try:
        return json.loads(result_file.read_text()).get("success", False)
    except Exception:
        return False


def _save_result(task_id: str, data: dict):
    path = RESULTS / f"{task_id}.json"
    path.write_text(json.dumps(data, indent=2))
    log.info(f"Result written: {path}")


def _run_task(task: dict):
    task_id = task.get("id", str(int(time.time())))
    project_path = Path(task["project_path"]) if task.get("project_path") else REPO_ROOT
    ensure_repo(project_path)

    # Signal to the UI which task is running
    CURRENT_TASK.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_TASK.write_text(task_id)

    log.info(f"─── Task start | id='{task_id}' project='{project_path.name}' prompt='{task.get('prompt','')[:80]}' ───")
    try:
        result = execute_long_horizon(task)
        _save_result(task_id, result)
        status = "SUCCESS" if result.get("success") else "FAILED"
        log.info(f"─── Task {status} | id='{task_id}' ───")
    finally:
        CURRENT_TASK.unlink(missing_ok=True)


def main():
    log.info("Runtime daemon starting")

    seen: set = set()

    while True:
        for task in _load_tasks():
            key = json.dumps(task, sort_keys=True)
            if key in seen:
                continue
            if not _can_run(task):
                continue  # prerequisite not done yet — retry next poll
            seen.add(key)
            try:
                _run_task(task)
            except Exception as e:
                log.exception(f"Unhandled error in task: {e}")
        time.sleep(3)


if __name__ == "__main__":
    main()
