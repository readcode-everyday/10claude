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

import re as _re

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from openclaw.tools import project_manager

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

PROJECTS_ROOT_HOST = Path("/home/wangk/10claude/workspace/projects")


def _build_task_prompt(task: dict) -> str:
    parts = [f"Task: {task['prompt']}"]
    project = task.get("project_path", "").strip()
    if project:
        # project_path is the project name (e.g. "my-app"), not an absolute path
        project_name = Path(project).name  # safe even if someone passes a full path
        project_abs = PROJECTS_ROOT_HOST / project_name
        parts.append(f"Project directory (absolute path on host): {project_abs}")
        parts.append(f"Work inside that directory. All file writes and git commits must happen there.")
    if task.get("answers"):
        parts.append("Answers to clarifying questions:")
        for q, a in task["answers"].items():
            parts.append(f"  - {q}: {a}")
    if task.get("plan"):
        parts.append(f"\nApproved implementation plan:\n{task['plan']}")
    return "\n".join(parts)


DEV_SESSION_KEY = "agent:dev:main"


def _dispatch_background(task: dict):
    """
    Background thread per task: send to dev agent via sessions_send (blocking),
    wait up to 10 min for reply, then mark task complete/failed.
    """
    task_id = task["id"]
    reply = ""
    outcome = "failed"
    try:
        log.info(f"dispatching task {task_id} to {DEV_SESSION_KEY}")
        r = httpx.post(
            f"{OPENCLAW_URL}/tools/invoke",
            headers=_headers(),
            json={
                "tool": "sessions_send",
                "sessionKey": "main",
                "args": {
                    "sessionKey": DEV_SESSION_KEY,
                    "message": _build_task_prompt(task),
                },
            },
            timeout=600,  # 10-minute hard cap; agent should reply by then
        )
        data = r.json()
        if data.get("ok"):
            result_data = data.get("result") or {}
            details = result_data.get("details") or {}
            # Check if sessions_send itself reported an error
            if details.get("status") == "error":
                reply = details.get("error") or "sessions_send returned error"
                outcome = "failed"
            else:
                # Extract the agent's reply text
                reply = (
                    details.get("reply")
                    or result_data.get("reply")
                    or json.dumps(result_data)
                )
                outcome = "completed"
        else:
            reply = data.get("error") or data.get("message") or "Unknown gateway error"
            outcome = "failed"
    except httpx.TimeoutException:
        reply = "Task timed out (10-minute limit exceeded)"
        outcome = "failed"
    except Exception as e:
        reply = str(e)
        outcome = "failed"

    # Update task status atomically
    with _lock:
        store = _read_store()
        if store.get(task_id, {}).get("status") == "in_progress":
            store[task_id]["status"] = outcome
            store[task_id]["completed_at"] = datetime.now().isoformat()
            _write_store(store)

    # Write result file (reply visible in UI)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / f"{task_id}.json").write_text(json.dumps({
        "task_id": task_id,
        "success": outcome == "completed",
        "reply": reply,
        "session_key": DEV_SESSION_KEY,
        "completed_at": datetime.now().isoformat(),
    }, indent=2))
    log.info(f"task {task_id} → {outcome}: {reply[:120]}")


# ── Stale-Task Watchdog ─────────────────────────────────────────────────────────

def _stale_watchdog():
    """
    Background thread: after service restart, background dispatch threads are gone.
    Any task still in_progress after 20 min is declared failed (avoids stuck kanban).
    """
    while True:
        try:
            store = _read_store()
            now = datetime.now()
            for tid, task in list(store.items()):
                if task.get("status") != "in_progress":
                    continue
                started = task.get("confirmed_at") or task.get("created_at")
                if not started:
                    continue
                try:
                    age = (now - datetime.fromisoformat(started)).total_seconds()
                except Exception:
                    continue
                if age > 1200:  # 20 minutes
                    with _lock:
                        store2 = _read_store()
                        if store2.get(tid, {}).get("status") == "in_progress":
                            store2[tid]["status"] = "failed"
                            store2[tid]["error"] = "Timed out (>20 min, likely service restart)"
                            store2[tid]["completed_at"] = now.isoformat()
                            _write_store(store2)
                    log.warning(f"task {tid} timed out after >20 min, marked failed")
        except Exception as e:
            log.warning(f"watchdog error: {e}")
        time.sleep(60)


threading.Thread(target=_stale_watchdog, daemon=True, name="stale-watchdog").start()


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


def _get_anthropic_key() -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return key
    # Fall back to openclaw's stored auth profile
    for profiles_path in [
        Path("/root/.openclaw/agents/personal/agent/auth-profiles.json"),
        Path.home() / ".openclaw/agents/personal/agent/auth-profiles.json",
    ]:
        try:
            data = json.loads(profiles_path.read_text())
            profiles = data.get("profiles", {})
            for v in profiles.values():
                if v.get("provider") == "anthropic" and v.get("token"):
                    return v["token"]
        except Exception:
            pass
    return None


def _generate_plan(prompt: str, feedback: str = None) -> dict:
    try:
        import anthropic
        api_key = _get_anthropic_key()
        if not api_key:
            raise ValueError("No Anthropic API key found")
        client = anthropic.Anthropic(api_key=api_key)
        feedback_section = f"\nAdditional feedback: {feedback}\n" if feedback else ""
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=2048,
            messages=[{"role": "user", "content": _PLAN_PROMPT.format(
                prompt=prompt, feedback_section=feedback_section
            )}],
        )
        raw = msg.content[0].text
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


class CreateProjectRequest(BaseModel):
    name: str


@app.post("/api/projects")
def create_project(req: CreateProjectRequest):
    """Create a new git-initialised project repo."""
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Project name is required")
    if not _re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_\-]*$', name):
        raise HTTPException(400, "Project name must start with a letter/digit and contain only letters, digits, hyphens, underscores")
    if (PROJECTS_ROOT / name).exists():
        raise HTTPException(409, f"Project '{name}' already exists")
    try:
        project_manager.create_project(name)
        return {"name": name, "created": True}
    except Exception as e:
        log.error(f"Failed to create project {name!r}: {e}")
        raise HTTPException(500, str(e))


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
    """
    Mark task in_progress immediately, then dispatch to openclaw in a background
    thread. The background thread blocks on sessions_send and updates status on reply.
    Returns the updated task dict (status=in_progress).
    """
    with _lock:
        store = _read_store()
        store[task["id"]] = task
        store[task["id"]]["status"] = "in_progress"
        store[task["id"]]["openclaw_session_key"] = DEV_SESSION_KEY
        _write_store(store)
        updated = store[task["id"]]

    threading.Thread(
        target=_dispatch_background,
        args=(task,),
        daemon=True,
        name=f"dispatch-{task['id']}",
    ).start()

    return updated


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
