"""
Project management — each project is an isolated git repo under /workspace/projects/.
The CLI selects (or creates) a project before submitting any task; the chosen
project_path is embedded in the task payload and flows through the entire pipeline.
"""
import os
import subprocess
import logging
from pathlib import Path

log = logging.getLogger(__name__)

PROJECTS_ROOT = Path("/workspace/projects")


def list_projects() -> list:
    """Return sorted list of existing project names (dirs with a .git inside)."""
    if not PROJECTS_ROOT.exists():
        return []
    return sorted(
        p.name for p in PROJECTS_ROOT.iterdir()
        if p.is_dir() and (p / ".git").exists()
    )


def create_project(name: str) -> Path:
    """Create a new git-initialised project repo. Returns its path."""
    path = PROJECTS_ROOT / name
    path.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": os.environ.get("GIT_AUTHOR_NAME", "openclaw"),
        "GIT_AUTHOR_EMAIL": os.environ.get("GIT_AUTHOR_EMAIL", "openclaw@local"),
        "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "openclaw"),
        "GIT_COMMITTER_EMAIL": os.environ.get("GIT_COMMITTER_EMAIL", "openclaw@local"),
    }

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    (path / ".gitkeep").touch()
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, env=env)

    log.info(f"Created project: {path}")
    return path


def get_project_path(name: str) -> Path:
    return PROJECTS_ROOT / name
