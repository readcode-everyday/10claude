"""
FastAPI task management API + kanban web UI.
Serves the OpenClaw dashboard on port 18888.
Dispatches tasks to the openclaw gateway instead of the legacy runtime-daemon.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

app = FastAPI(title="OpenClaw Status UI")
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
RESULTS       = Path("/app/workspace/results")
LOG_FILE      = Path("/app/logs/daemon.log")
TASK_STORE    = Path("/app/workspace/queue/task_store.json")
PROJECTS_ROOT = Path("/app/workspace/projects")
STATIC_DIR    = Path(__file__).parent.parent / "static"

_lock = threading.Lock()


# ── openclaw gateway client ─────────────────────────────────────────────────────

OPENCLAW_URL = os.getenv("OPENCLAW_GATEWAY_URL", "http://localhost:18789")

_gateway_token: Optional[str] = None


def _get_token() -> str:
    global _gateway_token
    if _gateway_token:
        return _gateway_token
    # Try env first
    token = os.getenv("OPENCLAW_GATEWAY_TOKEN", "")
    if not token:
        # Fall back to reading from openclaw.json
        try:
            cfg = json.loads((Path.home() / ".openclaw" / "openclaw.json").read_text())
            token = cfg.get("gateway", {}).get("auth", {}).get("token", "")
        except Exception:
            pass
    _gateway_token = token
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


def _invoke(tool: str, args: dict = None, action: str = None, session_key: str = "main") -> Optional[dict]:
    payload: dict = {"tool": tool, "sessionKey": session_key}
    if args:
        payload["args"] = args
    if action:
        payload["action"] = action
    try:
        r = httpx.post(f"{OPENCLAW_URL}/tools/invoke", headers=_headers(), json=payload, timeout=30)
        data = r.json()
        if data.get("ok"):
            return data.get("result")
        log.warning(f"openclaw tool {tool!r} error: {data.get('error')}")
    except Exception as e:
        log.warning(f"openclaw invoke error [{tool}]: {e}")
    return None


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


def _get_result(task_id: str) -> Optional[dict]:
    p = RESULTS / f"{task_id}.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except Exception:
        return None


def _all_tasks() -> list:
    store = _read_store()
    RESULTS.mkdir(parents=True, exist_ok=True)
    all_ids = set(store.keys()) | {p.stem for p in RESULTS.glob("*.json")}
    tasks = []
    for tid in all_ids:
        entry = store.get(tid, {})
        task = {
            "id": tid,
            "prompt": entry.get("prompt", ""),
            "project_path": entry.get("project_path", ""),
            "status": entry.get("status", "unknown"),
            "created_at": entry.get("created_at"),
            "plan": entry.get("plan"),
            "questions": entry.get("questions"),
            "answers": entry.get("answers"),
            "depends_on": entry.get("depends_on"),
        }
        if task["status"] in ("completed", "failed"):
            task["result"] = _get_result(tid)
        tasks.append(task)
    return sorted(tasks, key=lambda t: t.get("created_at") or "", reverse=True)


# ── openclaw Dispatch ──────────────────────────────────────────────────────────

def _build_task_prompt(task: dict) -> str:
    parts = [f"Task: {task['prompt']}"]
    if task.get("project_path"):
        parts.append(f"Project: {task['project_path']}")
    if task.get("answers"):
        parts.append("Answers to clarifying questions:")
        for q, a in task["answers"].items():
            parts.append(f"  - {q}: {a}")
    if task.get("plan"):
        parts.append(f"\nApproved implementation plan:\n{task['plan']}")
    return "\n".join(parts)


def _dispatch_to_openclaw(task: dict) -> Optional[str]:
    """Spawn a dev agent sub-agent for this task. Returns childSessionKey or None."""
    result = _invoke(
        "sessions_spawn",
        args={
            "task": _build_task_prompt(task),
            "agentId": "dev",
            "label": task["id"],
            "runTimeoutSeconds": 3600,
        },
    )
    if result:
        session_key = result.get("childSessionKey") or result.get("sessionKey")
        log.info(f"spawned session {session_key!r} for task {task['id']}")
        return session_key
    log.error(f"failed to spawn sub-agent for task {task['id']}: {result}")
    return None


def _check_session_status(session_key: str) -> Optional[str]:
    """
    Check if an openclaw sub-agent session has completed.
    Returns 'completed', 'failed', or None (still running / unknown).
    """
    result = _invoke("sessions_list", action="json")
    if result is None:
        return None
    # result may be a list or an object with a sessions/subagents key
    sessions = result if isinstance(result, list) else (
        result.get("sessions") or result.get("subagents") or result.get("items") or []
    )
    for s in sessions:
        key = s.get("key") or s.get("sessionKey") or s.get("id") or ""
        if key == session_key or session_key in key:
            st = (s.get("status") or s.get("state") or "").lower()
            if st in ("active", "running", "pending", "queued"):
                return None  # still running
            if st in ("error", "failed", "timeout", "aborted"):
                return "failed"
            # done, completed, archived, ok, etc.
            return "completed"
    # Session not in list → likely auto-archived after completing
    return "completed"


def _write_result(task_id: str, session_key: str, outcome: str):
    RESULTS.mkdir(parents=True, exist_ok=True)
    result = {
        "task_id": task_id,
        "success": outcome == "completed",
        "session_key": session_key,
        "completed_at": datetime.now().isoformat(),
    }
    (RESULTS / f"{task_id}.json").write_text(json.dumps(result, indent=2))


# ── Background Status Poller ────────────────────────────────────────────────────

def _poll_loop():
    """Background thread: poll openclaw for in-progress task completions."""
    while True:
        try:
            store = _read_store()
            for tid, task in list(store.items()):
                if task.get("status") != "in_progress":
                    continue
                session_key = task.get("openclaw_session_key")
                if not session_key:
                    continue
                outcome = _check_session_status(session_key)
                if outcome:
                    with _lock:
                        store2 = _read_store()
                        if store2.get(tid, {}).get("status") == "in_progress":
                            store2[tid]["status"] = outcome
                            store2[tid]["completed_at"] = datetime.now().isoformat()
                            _write_store(store2)
                    _write_result(tid, session_key, outcome)
                    log.info(f"task {tid} → {outcome}")
        except Exception as e:
            log.warning(f"status poller error: {e}")
        time.sleep(15)


threading.Thread(target=_poll_loop, daemon=True, name="openclaw-poller").start()


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
        "plan": f"# Plan\n\nTask: {prompt}\n\n*(Plan generation unavailable — check logs)*",
        "questions": [],
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


def _submit_and_dispatch(task: dict) -> dict:
    """Save task and dispatch to openclaw. Returns updated task dict."""
    _upsert(task)
    session_key = _dispatch_to_openclaw(task)
    with _lock:
        store = _read_store()
        if session_key:
            store[task["id"]]["status"] = "in_progress"
            store[task["id"]]["openclaw_session_key"] = session_key
        else:
            store[task["id"]]["status"] = "failed"
            store[task["id"]]["error"] = "Failed to dispatch to openclaw gateway"
        _write_store(store)
    return store[task["id"]]


@app.post("/api/submit")
def submit(req: SubmitRequest):
    """Submit a task directly without plan review."""
    task = {
        "id": f"task-{uuid.uuid4().hex[:8]}",
        "prompt": req.prompt,
        "project_path": req.project_path,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "depends_on": req.depends_on,
    }
    updated = _submit_and_dispatch(task)
    return {"task_id": task["id"], "status": updated["status"]}


@app.post("/api/confirm/{task_id}")
def confirm(task_id: str, req: ConfirmRequest):
    """Confirm a plan-review task and submit it to openclaw."""
    with _lock:
        store = _read_store()
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        task["answers"] = req.answers
        task["confirmed_at"] = datetime.now().isoformat()
        store[task_id] = task
        _write_store(store)
    updated = _submit_and_dispatch(task)
    return {"status": updated["status"]}


@app.post("/api/retry/{task_id}")
def retry(task_id: str):
    """Re-queue a failed task."""
    with _lock:
        store = _read_store()
        task = store.get(task_id, {"id": task_id, "prompt": "", "status": "pending"})
        result_file = RESULTS / f"{task_id}.json"
        if result_file.exists():
            result_file.unlink()
        task["status"] = "pending"
        task.setdefault("created_at", datetime.now().isoformat())
        task.pop("openclaw_session_key", None)
        task.pop("error", None)
        store[task_id] = task
        _write_store(store)
    updated = _submit_and_dispatch(task)
    return {"status": updated["status"]}


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
    return {"status": "ok", "openclaw_url": OPENCLAW_URL}


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
