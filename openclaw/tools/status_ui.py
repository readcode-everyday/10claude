"""
FastAPI observability + task submission API.
Runs inside the openclaw container on port 18888.
"""
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="OpenClaw Status API")
log = logging.getLogger(__name__)

QUEUE   = Path("/workspace/queue/tasks.jsonl")
RESULTS = Path("/workspace/results")
LOG_FILE = Path("/logs/daemon.log")


# ── Models ────────────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    prompt: str
    id: str = None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/submit")
def submit(req: TaskRequest):
    """Submit a new task to the filesystem queue."""
    task_id = req.id or f"task-{int(time.time())}"
    task = {"id": task_id, "prompt": req.prompt}
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE.open("a") as f:
        f.write(json.dumps(task) + "\n")
    log.info(f"Task submitted via API: {task_id}")
    return {"task_id": task_id, "status": "queued"}


@app.get("/queue")
def queue():
    """List all tasks currently in the queue."""
    if not QUEUE.exists():
        return []
    tasks = []
    for line in QUEUE.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                tasks.append(json.loads(line))
            except Exception:
                pass
    return tasks


@app.get("/tasks")
def tasks():
    """List completed task IDs (have a result file)."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    return [p.stem for p in sorted(RESULTS.glob("*.json"))]


@app.get("/results/{task_id}")
def result(task_id: str):
    """Fetch the result JSON for a completed task."""
    p = RESULTS / f"{task_id}.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="Task result not found")
    return json.loads(p.read_text())


@app.get("/logs", response_class=PlainTextResponse)
def logs(tail: int = 100):
    """Return the last N lines of the daemon log."""
    if not LOG_FILE.exists():
        return "No logs yet."
    lines = LOG_FILE.read_text().splitlines()
    return "\n".join(lines[-tail:])


@app.get("/health")
def health():
    return {"status": "ok"}
