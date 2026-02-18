"""
OpenClaw Dev Manager CLI/TUI.
Run inside the openclaw container: openclaw  (or python -m openclaw.cli)

Accepts a task from the user, submits it to the queue, then streams
daemon log lines until a result appears.
"""
import json
import sys
import time
import uuid
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.text import Text
    _rich = True
except ImportError:
    _rich = False

QUEUE    = Path("/workspace/queue/tasks.jsonl")
RESULTS  = Path("/workspace/results")
LOG_FILE = Path("/logs/daemon.log")

console = Console() if _rich else None


def _print(msg: str, style: str = ""):
    if _rich:
        console.print(msg, style=style)
    else:
        print(msg)


def _submit(task_id: str, prompt: str):
    task = {"id": task_id, "prompt": prompt}
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE.open("a") as f:
        f.write(json.dumps(task) + "\n")


def _poll_result(task_id: str, timeout: int = 600) -> dict | None:
    path = RESULTS / f"{task_id}.json"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return json.loads(path.read_text())
        time.sleep(2)
    return None


def _tail_log(task_id: str, last_pos: int) -> tuple[str, int]:
    """Return new log lines mentioning task_id since last_pos."""
    if not LOG_FILE.exists():
        return "", last_pos
    text = LOG_FILE.read_text()
    new_text = text[last_pos:]
    new_pos = len(text)
    relevant = [l for l in new_text.splitlines() if task_id in l or
                any(k in l for k in ["[worker]", "[loop]", "[executor]", "DAG", "sub-goal"])]
    return "\n".join(relevant), new_pos


def main():
    if _rich:
        console.print(Panel.fit(
            "[bold cyan]OpenClaw Dev Manager[/bold cyan]\n"
            "[dim]Autonomous software engineering agent[/dim]",
            border_style="cyan"
        ))
    else:
        print("=" * 50)
        print("  OpenClaw Dev Manager")
        print("=" * 50)

    try:
        if len(sys.argv) > 1:
            prompt = " ".join(sys.argv[1:])
            _print(f"Task: {prompt}", style="dim")
        else:
            if _rich:
                prompt = Prompt.ask("\n[bold]Enter task[/bold]")
            else:
                prompt = input("\nEnter task: ").strip()

        if not prompt:
            _print("No task provided.", style="red")
            sys.exit(1)

        task_id = f"task-{uuid.uuid4().hex[:8]}"
        _print(f"\n[→] Submitting task [bold]{task_id}[/bold]", style="green")
        _submit(task_id, prompt)
        _print("[→] Task queued — waiting for daemon...\n", style="dim")

        log_pos = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        start = time.time()

        while True:
            # Stream relevant log lines
            new_lines, log_pos = _tail_log(task_id, log_pos)
            if new_lines:
                for line in new_lines.splitlines():
                    # Colour by content
                    if "[worker]" in line:
                        _print(f"  [dim cyan]{line}[/dim cyan]" if _rich else line)
                    elif "FAILED" in line or "failed" in line:
                        _print(f"  [red]{line}[/red]" if _rich else line)
                    elif "complete" in line or "DONE" in line or "SUCCESS" in line:
                        _print(f"  [green]{line}[/green]" if _rich else line)
                    else:
                        _print(f"  [dim]{line}[/dim]" if _rich else line)

            # Check for result
            result_path = RESULTS / f"{task_id}.json"
            if result_path.exists():
                result = json.loads(result_path.read_text())
                elapsed = round(time.time() - start, 1)

                if result.get("success"):
                    _print(f"\n[bold green]✓ Task complete[/bold green] ({elapsed}s)", style="")
                else:
                    _print(f"\n[bold red]✗ Task failed[/bold red] ({elapsed}s)", style="")

                if _rich:
                    console.print_json(json.dumps(result, indent=2))
                else:
                    print(json.dumps(result, indent=2))
                break

            if time.time() - start > 600:
                _print("\n[red]Timeout — no result after 10 minutes.[/red]")
                break

            time.sleep(1)

    except KeyboardInterrupt:
        _print("\n[dim]Interrupted.[/dim]")


if __name__ == "__main__":
    main()
