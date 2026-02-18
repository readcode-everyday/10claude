"""
FastAPI task management API + kanban web UI.
Serves the OpenClaw dashboard on port 18888.
"""
import json
import logging
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="OpenClaw")
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
QUEUE         = Path("/workspace/queue/tasks.jsonl")
RESULTS       = Path("/workspace/results")
LOG_FILE      = Path("/logs/daemon.log")
TASK_STORE    = Path("/workspace/queue/task_store.json")
CURRENT_TASK  = Path("/workspace/queue/current_task")
PROJECTS_ROOT = Path("/workspace/projects")
STATIC_DIR    = Path(__file__).parent.parent / "static"

_lock = threading.Lock()


# ── Task Store ─────────────────────────────────────────────────────────────────

def _read_store() -> dict:
    if not TASK_STORE.exists():
        return {}
    try:
        return json.loads(TASK_STORE.read_text())
    except Exception:
        return {}


def _write_store(data: dict):
    TASK_STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = TASK_STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(TASK_STORE)


def _upsert(task: dict):
    with _lock:
        store = _read_store()
        store[task["id"]] = task
        _write_store(store)


def _enqueue(task: dict):
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: task[k] for k in ("id", "prompt", "project_path", "depends_on")
               if task.get(k) is not None}
    with QUEUE.open("a") as f:
        f.write(json.dumps(payload) + "\n")


def _queued_ids() -> set:
    if not QUEUE.exists():
        return set()
    ids = set()
    for line in QUEUE.read_text().splitlines():
        try:
            ids.add(json.loads(line.strip())["id"])
        except Exception:
            pass
    return ids


def _running_id() -> Optional[str]:
    try:
        return CURRENT_TASK.read_text().strip() or None
    except Exception:
        return None


def _get_result(task_id: str) -> Optional[dict]:
    p = RESULTS / f"{task_id}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def _compute_status(task_id: str, entry: Optional[dict]) -> str:
    if entry and entry.get("status") in ("review", "cancelled"):
        return entry["status"]
    if _running_id() == task_id:
        return "in_progress"
    r = _get_result(task_id)
    if r is not None:
        return "completed" if r.get("success") else "failed"
    if task_id in _queued_ids():
        return "pending"
    return entry.get("status", "unknown") if entry else "unknown"


def _all_tasks() -> list:
    store = _read_store()
    RESULTS.mkdir(parents=True, exist_ok=True)
    all_ids = (
        set(store.keys())
        | _queued_ids()
        | {p.stem for p in RESULTS.glob("*.json")}
    )
    running = _running_id()
    if running:
        all_ids.add(running)

    tasks = []
    for tid in all_ids:
        entry = store.get(tid, {})
        status = _compute_status(tid, entry)
        task = {
            "id": tid,
            "prompt": entry.get("prompt", ""),
            "project_path": entry.get("project_path", ""),
            "status": status,
            "created_at": entry.get("created_at"),
            "plan": entry.get("plan"),
            "questions": entry.get("questions"),
            "answers": entry.get("answers"),
            "depends_on": entry.get("depends_on"),
        }
        if status in ("completed", "failed"):
            task["result"] = _get_result(tid)
        tasks.append(task)

    return sorted(tasks, key=lambda t: t.get("created_at") or "", reverse=True)


# ── Plan Generation ────────────────────────────────────────────────────────────

_PLAN_PROMPT = """\
You are a software engineering planning assistant.

Task: {prompt}
{feedback_section}
Generate a detailed implementation plan and clarifying questions for the engineer.

Return ONLY valid JSON with no extra text:
{{
  "plan": "# Implementation Plan\\n...",
  "questions": [
    {{
      "question": "Which approach for X?",
      "options": ["Option A — description", "Option B — description (Recommended)"]
    }}
  ]
}}

The plan should be detailed markdown: numbered steps, sub-bullets, code snippets where helpful.
Include 2-3 clarifying questions with 2-4 options each. Mark recommended option with "(Recommended)".\
"""


def _generate_plan(prompt: str, feedback: str = None) -> dict:
    try:
        from openclaw.adapters.llm.client import call as llm_call
        feedback_section = f"\nAdditional feedback: {feedback}\n" if feedback else ""
        raw = llm_call(_PLAN_PROMPT.format(prompt=prompt, feedback_section=feedback_section))
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            return json.loads(match.group())
    except Exception as e:
        log.warning(f"Plan generation failed: {e}")
    return {
        "plan": f"# Plan\n\nTask: {prompt}\n\n*(Plan generation unavailable — check daemon logs)*",
        "questions": []
    }


# ── Models ─────────────────────────────────────────────────────────────────────

class PlanRequest(BaseModel):
    prompt: str
    project_path: str = ""
    depends_on: Optional[str] = None
    feedback: Optional[str] = None


class SubmitRequest(BaseModel):
    prompt: str
    project_path: str = ""
    depends_on: Optional[str] = None


class ConfirmRequest(BaseModel):
    answers: dict = {}
    feedback: Optional[str] = None


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    html = STATIC_DIR / "index.html"
    if not html.exists():
        return PlainTextResponse("UI not found — static/index.html missing", status_code=404)
    return FileResponse(html)


@app.get("/api/tasks")
def list_tasks():
    return _all_tasks()


@app.get("/api/projects")
def list_projects():
    if not PROJECTS_ROOT.exists():
        return []
    return sorted(
        p.name for p in PROJECTS_ROOT.iterdir()
        if p.is_dir() and (p / ".git").exists()
    )


@app.post("/api/plan")
def plan(req: PlanRequest):
    """Generate a plan (with questions) and save it in review status."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    data = _generate_plan(req.prompt, req.feedback)
    task = {
        "id": task_id,
        "prompt": req.prompt,
        "project_path": req.project_path,
        "status": "review",
        "created_at": datetime.now().isoformat(),
        "plan": data.get("plan", ""),
        "questions": data.get("questions", []),
        "answers": {},
        "depends_on": req.depends_on,
    }
    _upsert(task)
    return task


@app.post("/api/replan/{task_id}")
def replan(task_id: str, req: PlanRequest):
    """Regenerate the plan for an existing review task with new feedback."""
    with _lock:
        store = _read_store()
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        data = _generate_plan(task["prompt"], req.feedback)
        task["plan"] = data.get("plan", "")
        task["questions"] = data.get("questions", [])
        task["answers"] = {}
        store[task_id] = task
        _write_store(store)
    return store[task_id]


@app.post("/api/submit")
def submit(req: SubmitRequest):
    """Submit a task directly without plan review."""
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    task = {
        "id": task_id,
        "prompt": req.prompt,
        "project_path": req.project_path,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "depends_on": req.depends_on,
    }
    _upsert(task)
    _enqueue(task)
    return {"task_id": task_id, "status": "queued"}


@app.post("/api/confirm/{task_id}")
def confirm(task_id: str, req: ConfirmRequest):
    """Confirm a plan-review task and submit it to the execution queue."""
    with _lock:
        store = _read_store()
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        task["answers"] = req.answers
        task["status"] = "pending"
        task["confirmed_at"] = datetime.now().isoformat()
        store[task_id] = task
        _write_store(store)
    _enqueue(task)
    return {"status": "queued"}


@app.post("/api/retry/{task_id}")
def retry(task_id: str):
    """Re-queue a failed task."""
    store = _read_store()
    task = store.get(task_id, {"id": task_id, "prompt": ""})
    result_file = RESULTS / f"{task_id}.json"
    if result_file.exists():
        result_file.unlink()
    task["status"] = "pending"
    task.setdefault("created_at", datetime.now().isoformat())
    _upsert(task)
    _enqueue(task)
    return {"status": "requeued"}


@app.delete("/api/tasks/{task_id}")
def cancel(task_id: str):
    """Cancel a task."""
    with _lock:
        store = _read_store()
        entry = store.get(task_id, {"id": task_id, "prompt": ""})
        entry["status"] = "cancelled"
        store[task_id] = entry
        _write_store(store)
    return {"status": "cancelled"}


@app.get("/api/logs", response_class=PlainTextResponse)
def logs(tail: int = 200):
    if not LOG_FILE.exists():
        return "No logs yet."
    lines = LOG_FILE.read_text().splitlines()
    return "\n".join(lines[-tail:])


@app.get("/api/health")
def health():
    return {"status": "ok", "running": _running_id()}


# ── Legacy compatibility ───────────────────────────────────────────────────────

@app.post("/submit")
def legacy_submit(req: SubmitRequest):
    return submit(req)


@app.get("/tasks")
def legacy_tasks():
    return [t["id"] for t in _all_tasks() if t["status"] == "completed"]


@app.get("/results/{task_id}")
def legacy_result(task_id: str):
    r = _get_result(task_id)
    if not r:
        raise HTTPException(404, "Not found")
    return r
