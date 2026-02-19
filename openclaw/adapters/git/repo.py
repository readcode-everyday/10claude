"""
Git worktree management. Each parallel worker gets an isolated worktree
so branches never collide during concurrent execution.
"""
import subprocess
import logging
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path("/app/workspace/repo")       # legacy fallback
WORKTREES = Path("/app/workspace/worktrees")  # shared worktree staging area


def ensure_repo(repo_root: Path = None):
    """Bootstrap a bare git repo at repo_root if one doesn't exist."""
    root = repo_root or REPO_ROOT
    if (root / ".git").exists():
        return
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    (root / ".gitkeep").touch()
    subprocess.run(["git", "add", ".gitkeep"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=root, check=True, capture_output=True
    )
    log.info(f"Initialized git repo at {root}")


def create_worktree(name: str, repo_root: Path = None) -> Path:
    """Create an isolated git worktree for a worker. Returns the path."""
    root = repo_root or REPO_ROOT
    ensure_repo(root)
    WORKTREES.mkdir(parents=True, exist_ok=True)
    wt_path = WORKTREES / name

    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=root, capture_output=True
        )

    result = subprocess.run(
        ["git", "worktree", "add", "-b", name, str(wt_path)],
        cwd=root, capture_output=True, text=True
    )
    if result.returncode != 0:
        log.warning(f"worktree add failed: {result.stderr} — falling back to repo root")
        return root

    log.info(f"Created worktree: {wt_path}")
    return wt_path


def remove_worktree(name: str, repo_root: Path = None):
    """Clean up a worktree after a worker finishes."""
    root = repo_root or REPO_ROOT
    wt_path = WORKTREES / name
    subprocess.run(
        ["git", "worktree", "remove", str(wt_path), "--force"],
        cwd=root, capture_output=True
    )
    subprocess.run(["git", "worktree", "prune"], cwd=root, capture_output=True)
    subprocess.run(["git", "branch", "-D", name], cwd=root, capture_output=True)
    log.info(f"Removed worktree: {wt_path}")
