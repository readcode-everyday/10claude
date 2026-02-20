"""
FastAPI task management API + kanban web UI.
Serves the OpenClaw dashboard on port 18888.

Tasks are written to workspace/queue/task_store.json with status "pending".
Three persistent worker containers (worker-1/2/3) poll the queue every 5 s,
atomically claim tasks, execute them via the claude CLI, and write results.
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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from openclaw.tools import project_manager

app = FastAPI(title="OpenClaw Status UI")
log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
RESULTS          = Path("/app/workspace/results")
LOG_FILE         = Path("/app/logs/daemon.log")
TASK_STORE       = Path("/app/workspace/queue/task_store.json")
QUEUE_DIR        = TASK_STORE.parent
LOCK_FILE        = QUEUE_DIR / ".queue.lock"
PROJECTS_ROOT    = Path("/app/workspace/projects")
PROJECT_HISTORY  = QUEUE_DIR / "project_history.json"
STATIC_DIR       = Path(__file__).parent.parent / "static"

_lock = threading.Lock()


def _fix_queue_permissions():
    """
    status-ui runs as root. Ensure queue files are world-writable so the
    non-root worker containers (uid 1000) can read, write, and lock them.
    """
    try:
        QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(QUEUE_DIR, 0o777)
        LOCK_FILE.touch()
        os.chmod(LOCK_FILE, 0o666)
        if TASK_STORE.exists():
            os.chmod(TASK_STORE, 0o666)
        RESULTS.mkdir(parents=True, exist_ok=True)
        os.chmod(RESULTS, 0o777)
        traces = RESULTS / "traces"
        traces.mkdir(parents=True, exist_ok=True)
        os.chmod(traces, 0o777)
        for f in RESULTS.glob("*.json"):
            os.chmod(f, 0o666)
        for f in traces.glob("*.jsonl"):
            os.chmod(f, 0o666)
    except Exception as e:
        log.warning(f"Permission fix failed: {e}")


_fix_queue_permissions()


# ── Task Store ─────────────────────────────────────────────────────────────────

def _read_project_history() -> dict:
    try:
        return json.loads(PROJECT_HISTORY.read_text()) if PROJECT_HISTORY.exists() else {}
    except Exception:
        return {}


def _touch_project(name: str, created: bool = False):
    """Record a project access in project_history.json."""
    with _lock:
        history = _read_project_history()
        now = datetime.now().isoformat()
        if name not in history:
            history[name] = {"created_at": now, "use_count": 0}
        if created:
            history[name]["created_at"] = now
        history[name]["last_used_at"] = now
        history[name]["use_count"] = history[name].get("use_count", 0) + 1
        PROJECT_HISTORY.write_text(json.dumps(history, indent=2))


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
    try:
        os.chmod(TASK_STORE, 0o666)
    except Exception:
        pass


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
    result_stems = {p.stem for p in RESULTS.glob("*.json")}
    store_ids = set(store.keys())

    tasks = []
    for tid, entry in store.items():
        entry_type = entry.get("type", "task")

        if entry_type == "goal":
            # Build embedded tasks list for the frontend
            task_ids = entry.get("task_ids", [])
            embedded_tasks = []
            for sub_id in task_ids:
                sub = store.get(sub_id, {})
                if sub:
                    embedded_tasks.append({
                        "id":                 sub_id,
                        "title":              sub.get("title", ""),
                        "prompt":             sub.get("prompt", ""),
                        "acceptance_criteria": sub.get("acceptance_criteria", ""),
                        "depends_on":         sub.get("depends_on", []),
                        "status":             sub.get("status", "draft"),
                    })
            tasks.append({
                "id":               tid,
                "type":             "goal",
                "prompt":           entry.get("prompt", ""),
                "project_path":     entry.get("project_path", ""),
                "status":           entry.get("status", "review"),
                "created_at":       entry.get("created_at"),
                "task_ids":         task_ids,
                "tasks":            embedded_tasks,
                "feedback_history": entry.get("feedback_history", []),
            })
        else:
            task = {
                "id":                  tid,
                "type":                entry_type,
                "prompt":              entry.get("prompt", ""),
                "project_path":        entry.get("project_path", ""),
                "status":              entry.get("status", "unknown"),
                "created_at":          entry.get("created_at"),
                "plan":                entry.get("plan"),
                "questions":           entry.get("questions"),
                "answers":             entry.get("answers"),
                "depends_on":          entry.get("depends_on"),
                "worker_id":           entry.get("worker_id"),
                "goal_id":             entry.get("goal_id"),
                "title":               entry.get("title"),
                "acceptance_criteria": entry.get("acceptance_criteria"),
            }
            if task["status"] in ("completed", "failed"):
                result = _get_result(tid)
                task["result"] = result
                if result:
                    task["tool_calls_count"] = result.get("tool_calls_count")
                    task["cost_usd"]         = result.get("cost_usd")
                    task["duration_s"]       = result.get("duration_s")
                    task["num_turns"]        = result.get("num_turns")
            tasks.append(task)

    # Include orphan result files (tasks that were deleted from store but have results)
    for stem in result_stems - store_ids:
        try:
            result = json.loads((RESULTS / f"{stem}.json").read_text())
            tasks.append({
                "id":         stem,
                "type":       "task",
                "prompt":     result.get("prompt", ""),
                "status":     "completed" if result.get("success") else "failed",
                "created_at": result.get("completed_at"),
                "result":     result,
            })
        except Exception:
            pass

    return sorted(tasks, key=lambda t: t.get("created_at") or "", reverse=True)


# ── Stale-Task Watchdog ─────────────────────────────────────────────────────────

def _stale_watchdog():
    """
    After a worker crash, the task stays in_progress forever.
    Declare it failed after 20 minutes so the kanban doesn't get stuck.
    Also auto-completes goals when all their sub-tasks finish.
    """
    while True:
        try:
            store = _read_store()
            now = datetime.now()
            for tid, task in list(store.items()):
                # ── stale in_progress tasks ──────────────────────────────
                if task.get("status") == "in_progress":
                    started = task.get("started_at") or task.get("confirmed_at") or task.get("created_at")
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
                                store2[tid]["status"]       = "failed"
                                store2[tid]["error"]        = "Timed out (>20 min, worker may have crashed)"
                                store2[tid]["completed_at"] = now.isoformat()
                                _write_store(store2)
                        log.warning(f"task {tid} timed out after >20 min, marked failed")

                # ── auto-complete goals ──────────────────────────────────
                elif task.get("type") == "goal" and task.get("status") == "approved":
                    sub_ids = task.get("task_ids", [])
                    if sub_ids and all(
                        store.get(sid, {}).get("status") == "completed"
                        for sid in sub_ids
                    ):
                        with _lock:
                            store2 = _read_store()
                            goal = store2.get(tid, {})
                            if goal.get("status") == "approved":
                                goal["status"]       = "completed"
                                goal["completed_at"] = now.isoformat()
                                store2[tid] = goal
                                _write_store(store2)
                        log.info(f"goal {tid} auto-completed (all sub-tasks done)")
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

_GOAL_QUESTIONS_PROMPT = """\
You are a software engineering planning assistant.

The user wants to achieve this goal:
Goal: {prompt}
Project: {project_path}

Before breaking this into tasks, ask 3–5 focused clarifying questions that will
significantly affect how the work is decomposed.

Focus on decisions like:
- Tech stack / framework choices (if not obvious)
- API style (REST vs GraphQL vs RPC)
- Auth approach (JWT, session, OAuth)
- Testing depth (unit only, integration, e2e)
- Database / storage choices
- Deployment target / constraints
- Performance / scale requirements

Skip questions that are obvious from context or don't affect task decomposition.

Return ONLY valid JSON with no extra text:
{{
  "questions": [
    {{
      "question": "Which web framework should the API use?",
      "options": ["FastAPI (Python) — recommended for speed", "Flask (Python) — simpler", "Express (Node.js)", "Other — specify in feedback"]
    }}
  ]
}}

Each question must have 2–4 options. Mark one as recommended where sensible.\
"""

_DECOMPOSE_PROMPT = """\
You are a software engineering task decomposer.

Goal: {prompt}
Project: {project_path}
{feedback_section}
Break this goal into 3–10 granular sub-tasks that workers can execute.

For each task, write a self-contained prompt that includes:
- Exact file paths and function/class names to create or modify
- API endpoint signatures (method, path, request body, response body as JSON)
- Database schema changes (table names, column types, constraints)
- Shell command to verify success (acceptance criteria)

Each worker has access to git, Python, Node. They work inside /app/workspace/projects/{project_path}.
No worker communicates with another — each task must be fully self-contained.

Return ONLY valid JSON:
{{
  "tasks": [
    {{
      "title": "Short imperative title (<=60 chars)",
      "prompt": "Full detailed instructions including all context needed",
      "acceptance_criteria": "Command to run + expected output",
      "depends_on": []
    }}
  ]
}}

depends_on: list of 0-based indices into the tasks array for prerequisites.
Minimise dependencies — prefer parallel tasks where possible.

CRITICAL — every task "prompt" MUST end with these exact git steps:

  P=/app/workspace/projects/{project_path}
  git -C $P add -A
  git -C $P pull --rebase origin main   # sync first, resolve any conflicts
  git -C $P add -A                      # re-stage after rebase
  git -C $P commit -m "feat: <task title>" || true   # ok if nothing to commit
  # Push with retry — if rejected, pull --rebase and retry once
  git -C $P push origin main || (git -C $P pull --rebase origin main && git -C $P push origin main)

Conflict resolution during rebase:
  git -C $P status                   # see conflicted files
  # edit files to resolve conflicts
  git -C $P add <resolved-files>
  git -C $P rebase --continue
  git -C $P push origin main

Never leave changes uncommitted and unpushed — the next dependent task reads the repo state.\
"""


def _get_anthropic_key() -> Optional[str]:
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        return key
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


def _generate_goal_questions(prompt: str, project_path: str) -> list:
    """Call Claude via CLI to generate clarifying questions before decomposition."""
    import subprocess
    full_prompt = _GOAL_QUESTIONS_PROMPT.format(
        prompt=prompt,
        project_path=project_path or "my-project",
    )
    try:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        result = subprocess.run(
            ["claude", "-p", full_prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=60, env=env,
        )
        if result.returncode != 0:
            log.warning(f"Goal questions CLI error: {result.stderr[:200]}")
            return []
        outer = json.loads(result.stdout)
        raw = outer.get("result", "")
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data.get("questions", [])
    except Exception as e:
        log.warning(f"Goal questions generation failed: {e}")
    return []


def _decompose_goal(prompt: str, project_path: str, feedback_history: list, answers: dict = None) -> list:
    """Call Claude via CLI to decompose a goal into sub-tasks. Returns list of task dicts."""
    import subprocess

    feedback_items = feedback_history[-3:] if feedback_history else []
    feedback_section = ""
    if feedback_items:
        feedback_section = "\nPrevious feedback:\n" + "\n".join(f"  - {f}" for f in feedback_items) + "\n"
    if answers:
        lines = "\n".join(f"  - {q}: {a}" for q, a in answers.items())
        feedback_section += f"\nUser decisions (use these in the decomposition):\n{lines}\n"

    full_prompt = _DECOMPOSE_PROMPT.format(
        prompt=prompt,
        project_path=project_path or "my-project",
        feedback_section=feedback_section,
    )

    try:
        # Drop ANTHROPIC_API_KEY — it's an OAuth token that the Python SDK
        # can't use, and it also overrides the valid credentials.json in status-ui.
        # The claude CLI will fall back to credentials.json automatically.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        result = subprocess.run(
            ["claude", "-p", full_prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=120,
            env=env,
        )
        if result.returncode != 0:
            log.warning(f"Goal decomposition CLI error: {result.stderr[:300]}")
            return []
        outer = json.loads(result.stdout)
        raw = outer.get("result", "")
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        if match:
            data = json.loads(match.group())
            return data.get("tasks", [])
    except Exception as e:
        log.warning(f"Goal decomposition failed: {e}")
    return []


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


class CreateProjectRequest(BaseModel):
    name: str


class DecomposeRequest(BaseModel):
    prompt: str
    project_path: str = ""
    answers: dict = {}  # user answers from plan-goal questions


class PlanGoalRequest(BaseModel):
    prompt: str
    project_path: str = ""


class RefineRequest(BaseModel):
    feedback: str


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
    history = _read_project_history()
    projects = []
    for p in PROJECTS_ROOT.iterdir():
        if p.is_dir() and (p / ".git").exists():
            meta = history.get(p.name, {})
            projects.append({
                "name":         p.name,
                "last_used_at": meta.get("last_used_at"),
                "use_count":    meta.get("use_count", 0),
            })
    # Most recently used first; never-used projects sorted alphabetically after
    projects.sort(key=lambda x: (x["last_used_at"] is None, x["last_used_at"] or "", x["name"]))
    projects.reverse()   # newest first (None entries end up last)
    # Re-sort: used projects by last_used_at desc, then unused alphabetically
    used   = sorted([p for p in projects if p["last_used_at"]], key=lambda x: x["last_used_at"], reverse=True)
    unused = sorted([p for p in projects if not p["last_used_at"]], key=lambda x: x["name"])
    return used + unused


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
        _touch_project(name, created=True)
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
        "id":           task_id,
        "prompt":       req.prompt,
        "project_path": req.project_path,
        "status":       "review",
        "created_at":   datetime.now().isoformat(),
        "plan":         data.get("plan", ""),
        "questions":    data.get("questions", []),
        "answers":      {},
        "depends_on":   req.depends_on,
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
        task["plan"]      = data.get("plan", "")
        task["questions"] = data.get("questions", [])
        task["answers"]   = {}
        store[task_id] = task
        _write_store(store)
    return store[task_id]


def _enqueue(task: dict) -> dict:
    """Write task as pending so a worker picks it up."""
    with _lock:
        store = _read_store()
        task["status"] = "pending"
        store[task["id"]] = task
        _write_store(store)
    return task


@app.post("/api/submit")
def submit(req: SubmitRequest):
    """Submit a task directly without plan review."""
    task = {
        "id":           f"task-{uuid.uuid4().hex[:8]}",
        "prompt":       req.prompt,
        "project_path": req.project_path,
        "status":       "pending",
        "created_at":   datetime.now().isoformat(),
        "depends_on":   req.depends_on,
    }
    _enqueue(task)
    return {"task_id": task["id"], "status": "pending"}


@app.post("/api/confirm/{task_id}")
def confirm(task_id: str, req: ConfirmRequest):
    """Confirm a plan-review task and enqueue it for a worker."""
    with _lock:
        store = _read_store()
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        task["answers"]      = req.answers
        task["confirmed_at"] = datetime.now().isoformat()
        task["status"]       = "pending"
        store[task_id] = task
        _write_store(store)
    return {"status": "pending"}


@app.post("/api/plan-goal")
def plan_goal(req: PlanGoalRequest):
    """Generate clarifying questions before decomposing a goal. Returns questions list."""
    questions = _generate_goal_questions(req.prompt, req.project_path)
    return {"questions": questions}


@app.post("/api/decompose")
def decompose(req: DecomposeRequest):
    """Decompose a goal into sub-tasks and save them in draft/review status."""
    goal_id = f"goal-{uuid.uuid4().hex[:8]}"
    now = datetime.now().isoformat()

    # LLM call outside the lock (may be slow)
    raw_tasks = _decompose_goal(req.prompt, req.project_path, [], answers=req.answers or {})

    task_ids = [f"task-{uuid.uuid4().hex[:8]}" for _ in raw_tasks]

    tasks = []
    for i, rt in enumerate(raw_tasks):
        dep_indices = rt.get("depends_on", [])
        dep_ids = [task_ids[j] for j in dep_indices if isinstance(j, int) and 0 <= j < len(task_ids)]
        tasks.append({
            "id":                  task_ids[i],
            "type":                "task",
            "goal_id":             goal_id,
            "title":               rt.get("title", f"Task {i + 1}"),
            "prompt":              rt.get("prompt", ""),
            "acceptance_criteria": rt.get("acceptance_criteria", ""),
            "project_path":        req.project_path,
            "status":              "draft",
            "depends_on":          dep_ids,
            "created_at":          now,
        })

    goal = {
        "id":               goal_id,
        "type":             "goal",
        "prompt":           req.prompt,
        "project_path":     req.project_path,
        "status":           "review",
        "created_at":       now,
        "task_ids":         task_ids,
        "feedback_history": [],
    }

    with _lock:
        store = _read_store()
        store[goal_id] = goal
        for task in tasks:
            store[task["id"]] = task
        _write_store(store)

    if req.project_path:
        _touch_project(req.project_path)

    return {"goal": goal, "tasks": tasks}


@app.post("/api/refine/{goal_id}")
def refine(goal_id: str, req: RefineRequest):
    """Append feedback and re-decompose a goal's sub-tasks."""
    # Read goal info first (outside lock — LLM call below is slow)
    with _lock:
        store = _read_store()
        goal = store.get(goal_id)
        if not goal or goal.get("type") != "goal":
            raise HTTPException(404, "Goal not found")
        feedback_history = list(goal.get("feedback_history", []))
        prompt       = goal["prompt"]
        project_path = goal.get("project_path", "")

    feedback_history.append(req.feedback)

    # Re-decompose (outside lock)
    raw_tasks = _decompose_goal(prompt, project_path, feedback_history)

    task_ids = [f"task-{uuid.uuid4().hex[:8]}" for _ in raw_tasks]
    now = datetime.now().isoformat()
    new_tasks = []
    for i, rt in enumerate(raw_tasks):
        dep_indices = rt.get("depends_on", [])
        dep_ids = [task_ids[j] for j in dep_indices if isinstance(j, int) and 0 <= j < len(task_ids)]
        new_tasks.append({
            "id":                  task_ids[i],
            "type":                "task",
            "goal_id":             goal_id,
            "title":               rt.get("title", f"Task {i + 1}"),
            "prompt":              rt.get("prompt", ""),
            "acceptance_criteria": rt.get("acceptance_criteria", ""),
            "project_path":        project_path,
            "status":              "draft",
            "depends_on":          dep_ids,
            "created_at":          now,
        })

    with _lock:
        store = _read_store()
        goal = store.get(goal_id)
        if not goal:
            raise HTTPException(404, "Goal not found")
        # Remove old draft tasks
        for old_tid in goal.get("task_ids", []):
            store.pop(old_tid, None)
        # Update goal
        goal["feedback_history"] = feedback_history
        goal["task_ids"]         = task_ids
        store[goal_id] = goal
        for task in new_tasks:
            store[task["id"]] = task
        _write_store(store)

    return {"goal": goal, "tasks": new_tasks}


@app.post("/api/approve/{goal_id}")
def approve(goal_id: str):
    """Approve a goal — transition all draft sub-tasks to pending."""
    with _lock:
        store = _read_store()
        goal = store.get(goal_id)
        if not goal or goal.get("type") != "goal":
            raise HTTPException(404, "Goal not found")

        count = 0
        for tid in goal.get("task_ids", []):
            task = store.get(tid)
            if task and task.get("status") == "draft":
                task["status"] = "pending"
                store[tid] = task
                count += 1

        goal["status"] = "approved"
        store[goal_id] = goal
        _write_store(store)

    return {"approved": True, "task_count": count}


@app.post("/api/retry/{task_id}")
def retry(task_id: str):
    """Re-queue a failed task."""
    with _lock:
        store = _read_store()
        task = store.get(task_id, {"id": task_id, "prompt": "", "status": "pending"})
        result_file = RESULTS / f"{task_id}.json"
        if result_file.exists():
            result_file.unlink()
        task["status"]     = "pending"
        task["worker_id"]  = None
        task["started_at"] = None
        task.setdefault("created_at", datetime.now().isoformat())
        task.pop("error", None)
        store[task_id] = task
        _write_store(store)
    return {"status": "pending"}


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


@app.get("/api/trace/{task_id}", response_class=PlainTextResponse)
def trace(task_id: str, tail: int = 0):
    """Return the raw stream-json trace for a task (newline-delimited JSON events)."""
    trace_file = RESULTS / "traces" / f"{task_id}.trace.jsonl"
    if not trace_file.exists():
        raise HTTPException(404, f"No trace for task {task_id}")
    lines = trace_file.read_text().splitlines()
    if tail:
        lines = lines[-tail:]
    return "\n".join(lines)


@app.get("/api/logs", response_class=PlainTextResponse)
def logs(tail: int = 200):
    if not LOG_FILE.exists():
        return "No logs yet."
    lines = LOG_FILE.read_text().splitlines()
    return "\n".join(lines[-tail:])


@app.get("/api/health")
def health():
    return {"status": "ok", "workers": ["worker-1", "worker-2", "worker-3"]}


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
