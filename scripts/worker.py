#!/usr/bin/env python3
"""
Persistent queue worker with full observability.

Polls /app/workspace/queue/task_store.json every POLL_SECS seconds.
Atomically claims a pending task via flock, executes it with:

    claude --dangerously-skip-permissions -p '<prompt>'
           --output-format stream-json --verbose

Every event (tool calls, thinking, tool results, errors, final result)
is parsed in real time, logged, and saved to a .trace.jsonl file so that
failures can be understood and debugged at the event level — not just from
a pass/fail exit code.

Three of these run in parallel Docker containers (worker-1, worker-2, worker-3).
"""
import fcntl
import json
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

WORKER_ID    = os.environ.get("WORKER_ID", f"worker-{os.getpid()}")
TASK_STORE   = Path("/app/workspace/queue/task_store.json")
LOCK_FILE    = Path("/app/workspace/queue/.queue.lock")
RESULTS_DIR  = Path("/app/workspace/results")
TRACES_DIR   = RESULTS_DIR / "traces"
PROJECTS_DIR = Path("/app/workspace/projects")
POLL_SECS    = 5
TASK_TIMEOUT = 600  # seconds per task


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] [{WORKER_ID}] {msg}", flush=True)


# ── Atomic task claim ──────────────────────────────────────────────────────────

def claim_task() -> dict | None:
    """
    Exclusively lock the queue, find the oldest pending task, mark it
    in_progress with our worker ID, write back, release lock.
    Returns the task dict or None if the queue is empty.
    """
    LOCK_FILE.touch()
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                store = json.loads(TASK_STORE.read_text())
            except Exception:
                return None

            pending = [
                (tid, t) for tid, t in store.items()
                if t.get("status") == "pending"
                and t.get("type", "task") != "goal"     # never claim goal entries
                and _dependencies_met(t, store)          # respect depends_on
            ]
            if not pending:
                return None

            pending.sort(key=lambda x: x[1].get("created_at", ""))
            task_id, task = pending[0]

            task["status"]     = "in_progress"
            task["worker_id"]  = WORKER_ID
            task["started_at"] = datetime.now().isoformat()
            store[task_id] = task

            tmp = TASK_STORE.with_suffix(".tmp")
            tmp.write_text(json.dumps(store, indent=2))
            tmp.rename(TASK_STORE)

            return task
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _dependencies_met(task: dict, store: dict) -> bool:
    """Return True if all depends_on prerequisites are completed."""
    deps = task.get("depends_on") or []
    if isinstance(deps, str):       # backwards compat: single-string format
        deps = [deps] if deps else []
    return all(
        store.get(dep_id, {}).get("status") == "completed"
        for dep_id in deps
    )


def complete_task(task_id: str, outcome: str, result: dict):
    """Update task status in the store and write the result file."""
    LOCK_FILE.touch()
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            try:
                store = json.loads(TASK_STORE.read_text())
            except Exception:
                store = {}

            entry = store.get(task_id, {})
            if entry.get("status") == "in_progress":
                entry["status"]       = outcome
                entry["completed_at"] = datetime.now().isoformat()
                store[task_id] = entry
                tmp = TASK_STORE.with_suffix(".tmp")
                tmp.write_text(json.dumps(store, indent=2))
                tmp.rename(TASK_STORE)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIR / f"{task_id}.json"
    result_path.write_text(json.dumps(result, indent=2))
    try:
        result_path.chmod(0o666)
    except Exception:
        pass


# ── Prompt building ────────────────────────────────────────────────────────────

def build_prompt(task: dict) -> str:
    parts = [task["prompt"]]

    project = task.get("project_path", "").strip()
    if project:
        project_name = Path(project).name
        project_abs  = PROJECTS_DIR / project_name
        if project_abs.is_dir():
            parts.append(f"\nProject directory: {project_abs}")
            parts.append(
                "Work inside that directory. "
                "All file writes and git commits must happen there."
            )

    if task.get("answers"):
        parts.append("\nAnswers to clarifying questions:")
        for q, a in task["answers"].items():
            parts.append(f"  - {q}: {a}")

    # Goal sub-tasks: always commit+push at the end and handle parallel merges
    if task.get("goal_id") and project and project_abs.is_dir():
        p = project_abs
        title = task.get('title', task['prompt'][:60]).replace("'", "")
        parts.append(
            f"\n## Required: commit and push your work when done\n"
            f"After all file changes, run these steps exactly:\n"
            f"  git -C {p} add -A\n"
            f"  git -C {p} pull --rebase origin main\n"
            f"  git -C {p} add -A\n"
            f"  git -C {p} commit -m 'feat: {title}' || true\n"
            f"  # Push with up to 3 retries:\n"
            f"  _ok=false\n"
            f"  for _i in 1 2 3; do\n"
            f"    git -C {p} push origin main && _ok=true && break\n"
            f"    git -C {p} pull --rebase origin main || true\n"
            f"  done\n"
            f"  [ \"$_ok\" = true ] || {{ echo 'PUSH FAILED after 3 attempts'; exit 1; }}\n"
            f"If you hit rebase conflicts: resolve files, git add, git rebase --continue.\n"
            f"Report in your final reply whether push succeeded or failed."
        )

    if task.get("plan"):
        parts.append(f"\nApproved implementation plan:\n{task['plan']}")

    return "\n".join(parts)


# ── Stream-JSON event parsing ──────────────────────────────────────────────────

def _log_tool_call(name: str, inp: dict):
    """Log a single tool call in a human-readable one-liner."""
    if name == "Bash":
        cmd = inp.get("command", "?")
        desc = inp.get("description", "")
        line = cmd[:120]
        if desc:
            line = f"{desc[:60]} | {cmd[:60]}"
        log(f"  [Bash] {line}")
    elif name in ("Write", "Edit", "MultiEdit"):
        path = inp.get("file_path", inp.get("path", "?"))
        log(f"  [{name}] {path}")
    elif name == "Read":
        path = inp.get("file_path", "?")
        log(f"  [Read] {path}")
    elif name == "Glob":
        log(f"  [Glob] {inp.get('pattern','?')}")
    elif name == "Grep":
        log(f"  [Grep] {inp.get('pattern','?')} in {inp.get('path','.')}")
    elif name == "Task":
        log(f"  [Task] {inp.get('description','?')}")
    else:
        log(f"  [{name}]")


# Patterns that indicate real errors (not just file content that contains the word "error")
_ERROR_PATTERNS = (
    "traceback (most recent call last)",
    "error:",
    "errno",
    "exception:",
    "no such file or directory",
    "permission denied",
    "command not found",
    "syntaxerror",
    "nameerror",
    "typeerror",
    "valueerror",
    "importerror",
    "modulenotfounderror",
    "exit code",
    "stderr:",
)


def _check_tool_result_for_errors(block: dict) -> list[str]:
    """
    Extract genuine error strings from a tool_result content block.
    Filters out false positives (e.g. file content that mentions 'error').
    Only flags text that matches known error patterns.
    """
    errors = []
    content = block.get("content", [])
    items = [{"type": "text", "text": content}] if isinstance(content, str) else content
    for c in items:
        if c.get("type") != "text":
            continue
        text = c.get("text", "")
        if not text:
            continue
        lower = text.lower()
        # Must match a specific error pattern, not just contain the word "error"
        if any(pat in lower for pat in _ERROR_PATTERNS):
            errors.append(text[:300])
    return errors


def run_task(task: dict):
    task_id = task["id"]
    prompt  = build_prompt(task)
    log(f"Starting task {task_id}: {task['prompt'][:80]!r}")

    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = TRACES_DIR / f"{task_id}.trace.jsonl"

    # Execution state
    final_result  = ""
    outcome       = "failed"
    tool_calls    = []      # list of {"tool": name, "input": {}}
    errors_seen   = []      # tool errors observed mid-run
    cost_usd      = 0.0
    duration_s    = 0.0
    num_turns     = 0
    timed_out     = False

    cmd = [
        "claude",
        "--dangerously-skip-permissions",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd="/app",
        )
    except FileNotFoundError:
        complete_task(task_id, "failed", {
            "task_id": task_id, "success": False,
            "reply": "claude CLI not found in PATH",
            "worker_id": WORKER_ID,
            "completed_at": datetime.now().isoformat(),
        })
        return

    # ── Timeout thread ──────────────────────────────────────────────────────
    _done = threading.Event()

    def _timeout_killer():
        if not _done.wait(TASK_TIMEOUT):
            try:
                proc.kill()
            except Exception:
                pass

    timer = threading.Thread(target=_timeout_killer, daemon=True)
    timer.start()

    # ── Stream and parse events ─────────────────────────────────────────────
    try:
        with open(trace_path, "w") as trace_f:
            for raw_line in proc.stdout:
                line = raw_line.rstrip()
                if not line:
                    continue

                # Save every raw event to the trace file
                trace_f.write(line + "\n")
                trace_f.flush()

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # Non-JSON output (e.g. progress spinners) — log and skip
                    log(f"  [raw] {line[:120]}")
                    continue

                etype = event.get("type")

                # ── system init ───────────────────────────────────────────
                if etype == "system" and event.get("subtype") == "init":
                    model = event.get("model", "?")
                    n_tools = len(event.get("tools", []))
                    log(f"  [init] model={model}  tools={n_tools}")

                # ── assistant message (thinking + tool calls) ─────────────
                elif etype == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "").strip()
                            # Only log non-empty thinking snippets
                            if text:
                                snippet = text[:160].replace("\n", " ")
                                log(f"  [think] {snippet}")
                        elif btype == "tool_use":
                            name = block.get("name", "?")
                            inp  = block.get("input", {})
                            tool_calls.append({"tool": name, "input": inp})
                            _log_tool_call(name, inp)

                # ── user message (tool results) ───────────────────────────
                elif etype == "user":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_result":
                            errs = _check_tool_result_for_errors(block)
                            for e in errs:
                                errors_seen.append(e)
                                log(f"  [err] {e[:140]}")

                # ── final result ──────────────────────────────────────────
                elif etype == "result":
                    subtype     = event.get("subtype", "unknown")
                    cost_usd    = event.get("total_cost_usd", 0.0)
                    duration_s  = event.get("duration_ms", 0) / 1000
                    num_turns   = event.get("num_turns", 0)
                    final_result = event.get("result") or ""

                    if subtype == "success":
                        outcome = "completed"
                        log(
                            f"  [result] success | {num_turns} turns | "
                            f"{duration_s:.1f}s | ${cost_usd:.4f}"
                        )
                    else:
                        outcome = "failed"
                        log(
                            f"  [result] {subtype} | {num_turns} turns | "
                            f"{duration_s:.1f}s | ${cost_usd:.4f}"
                        )
                        if not final_result:
                            final_result = f"Execution error: {subtype}"

        proc.wait(timeout=10)

        # If claude returned non-zero but we didn't get a result event
        if proc.returncode != 0 and outcome != "failed":
            stderr = proc.stderr.read()
            if stderr.strip():
                final_result = stderr.strip()
            outcome = "failed"

        # If we got no result event at all (e.g. startup error)
        if not final_result and outcome == "failed":
            stderr = proc.stderr.read()
            final_result = stderr.strip() or "Claude exited without producing a result"

    except Exception as exc:
        final_result = f"Worker error: {exc}"
        outcome = "failed"
        try:
            proc.kill()
        except Exception:
            pass
    finally:
        _done.set()   # cancel the timeout timer
        timer.join(timeout=2)

    # Detect timeout from the kill
    if proc.returncode == -9 and outcome == "failed" and not final_result:
        final_result = f"Task killed after {TASK_TIMEOUT}s timeout"
        timed_out = True

    # ── Build tool-call summary for the UI ─────────────────────────────────
    tool_summary = {}
    for tc in tool_calls:
        tool_summary[tc["tool"]] = tool_summary.get(tc["tool"], 0) + 1
    tool_summary_str = "  ".join(f"{k}×{v}" for k, v in tool_summary.items())

    # Final reply shown in UI: result text + summary line
    reply_parts = []
    if final_result:
        reply_parts.append(final_result)
    if tool_summary_str:
        reply_parts.append(f"\n---\nTools: {tool_summary_str}  |  Turns: {num_turns}  |  Cost: ${cost_usd:.4f}  |  Time: {duration_s:.1f}s")
    if errors_seen:
        reply_parts.append(f"Errors encountered ({len(errors_seen)}):\n" + "\n".join(f"  • {e[:140]}" for e in errors_seen[:5]))
    reply = "\n".join(reply_parts) or "(no output)"

    result_doc = {
        "task_id":          task_id,
        "success":          outcome == "completed",
        "reply":            reply,
        "worker_id":        WORKER_ID,
        "completed_at":     datetime.now().isoformat(),
        "tool_calls_count": len(tool_calls),
        "tool_summary":     tool_summary,
        "cost_usd":         cost_usd,
        "duration_s":       duration_s,
        "num_turns":        num_turns,
        "timed_out":        timed_out,
        "errors_seen":      errors_seen[:10],
        "trace_file":       str(trace_path),
    }

    complete_task(task_id, outcome, result_doc)
    log(
        f"Task {task_id} → {outcome} | "
        f"{len(tool_calls)} tool calls | "
        f"{duration_s:.1f}s | ${cost_usd:.4f}"
    )


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)

    log(f"Worker started. Polling every {POLL_SECS}s for pending tasks...")
    log(f"Stream-JSON mode: full event trace saved to {TRACES_DIR}/")

    while True:
        try:
            task = claim_task()
        except Exception as e:
            log(f"Error claiming task: {e}")
            time.sleep(POLL_SECS)
            continue

        if task is None:
            time.sleep(POLL_SECS)
            continue

        try:
            run_task(task)
        except Exception as e:
            log(f"Unhandled error on task {task['id']}: {e}")
            try:
                complete_task(task["id"], "failed", {
                    "task_id":      task["id"],
                    "success":      False,
                    "reply":        f"Worker crash: {e}",
                    "worker_id":    WORKER_ID,
                    "completed_at": datetime.now().isoformat(),
                })
            except Exception:
                pass


if __name__ == "__main__":
    main()
