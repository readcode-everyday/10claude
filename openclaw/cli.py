"""
OpenClaw Dev Manager CLI/TUI.
Run inside the openclaw container: openclaw  (or python -m openclaw.cli)

On startup: asks whether to create a new project or continue an existing one.
Then accepts a task, submits it to the queue with the chosen project_path,
and streams daemon log lines until a result appears.
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
    from rich.text import Text
    _rich = True
except ImportError:
    _rich = False

from openclaw.tools.project_manager import list_projects, create_project, get_project_path

QUEUE    = Path("/workspace/queue/tasks.jsonl")
RESULTS  = Path("/workspace/results")
LOG_FILE = Path("/logs/daemon.log")

console = Console() if _rich else None


def _print(msg: str, style: str = ""):
    if _rich:
        console.print(msg, style=style)
    else:
        print(msg)


def _select_project() -> str:
    """Interactive project selector. Returns the chosen project path as a string."""
    projects = list_projects()

    if _rich:
        console.print("\n[bold]Projects[/bold]")
        for i, name in enumerate(projects, 1):
            console.print(f"  [cyan]{i}.[/cyan] {name}")
        console.print(f"  [cyan]n.[/cyan] Create new project")
        choice = Prompt.ask(
            "\nSelect",
            default="n" if not projects else "1"
        ).strip().lower()
    else:
        print("\nProjects:")
        for i, name in enumerate(projects, 1):
            print(f"  {i}. {name}")
        print("  n. Create new project")
        choice = input(f"\nSelect [{('n' if not projects else '1')}]: ").strip().lower() or \
                 ("n" if not projects else "1")

    if choice == "n" or not projects:
        if _rich:
            name = Prompt.ask("Project name").strip()
        else:
            name = input("Project name: ").strip()
        if not name:
            _print("No project name given.", style="red")
            sys.exit(1)
        path = create_project(name)
        _print(f"\n[→] Created project [bold]{name}[/bold] at {path}", style="green")
        return str(path)

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(projects):
            raise ValueError
        name = projects[idx]
    except (ValueError, IndexError):
        _print(f"Invalid selection: {choice!r}", style="red")
        sys.exit(1)

    path = get_project_path(name)
    _print(f"\n[→] Project: [bold]{name}[/bold]", style="cyan")
    return str(path)


def _submit(task_id: str, prompt: str, project_path: str):
    task = {"id": task_id, "prompt": prompt, "project_path": project_path}
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE.open("a") as f:
        f.write(json.dumps(task) + "\n")


def _tail_log(task_id: str, last_pos: int) -> tuple:
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
        # ── Project selection ────────────────────────────────────────────────
        project_path = _select_project()

        # ── Task input ───────────────────────────────────────────────────────
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
        _submit(task_id, prompt, project_path)
        _print("[→] Task queued — waiting for daemon...\n", style="dim")

        log_pos = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
        start = time.time()

        while True:
            new_lines, log_pos = _tail_log(task_id, log_pos)
            if new_lines:
                for line in new_lines.splitlines():
                    if "[worker]" in line:
                        _print(f"  [dim cyan]{line}[/dim cyan]" if _rich else line)
                    elif "FAILED" in line or "failed" in line:
                        _print(f"  [red]{line}[/red]" if _rich else line)
                    elif "complete" in line or "DONE" in line or "SUCCESS" in line:
                        _print(f"  [green]{line}[/green]" if _rich else line)
                    else:
                        _print(f"  [dim]{line}[/dim]" if _rich else line)

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
