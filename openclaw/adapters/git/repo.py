"""
Git worktree management. Each parallel worker gets an isolated worktree
so branches never collide during concurrent execution.
"""
import subprocess
import logging
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path("/workspace/repo")
WORKTREES = Path("/workspace/worktrees")


def ensure_repo():
    """Bootstrap a bare git repo at /workspace/repo if one doesn't exist."""
    if (REPO_ROOT / ".git").exists():
        return
    REPO_ROOT.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=REPO_ROOT, check=True, capture_output=True)
    # Need at least one commit for worktree add to work
    (REPO_ROOT / ".gitkeep").touch()
    subprocess.run(["git", "add", ".gitkeep"], cwd=REPO_ROOT, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=REPO_ROOT, check=True, capture_output=True
    )
    log.info(f"Initialized git repo at {REPO_ROOT}")


def create_worktree(name: str) -> Path:
    """Create an isolated git worktree for a worker. Returns the path."""
    ensure_repo()
    WORKTREES.mkdir(parents=True, exist_ok=True)
    wt_path = WORKTREES / name

    # Remove stale worktree if it exists
    if wt_path.exists():
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path), "--force"],
            cwd=REPO_ROOT, capture_output=True
        )

    result = subprocess.run(
        ["git", "worktree", "add", "-b", name, str(wt_path)],
        cwd=REPO_ROOT, capture_output=True, text=True
    )
    if result.returncode != 0:
        log.warning(f"worktree add failed: {result.stderr} — falling back to REPO_ROOT")
        return REPO_ROOT

    log.info(f"Created worktree: {wt_path}")
    return wt_path


def remove_worktree(name: str):
    """Clean up a worktree after a worker finishes."""
    wt_path = WORKTREES / name
    subprocess.run(
        ["git", "worktree", "remove", str(wt_path), "--force"],
        cwd=REPO_ROOT, capture_output=True
    )
    # Also prune stale refs
    subprocess.run(["git", "worktree", "prune"], cwd=REPO_ROOT, capture_output=True)
    # Delete the branch
    subprocess.run(
        ["git", "branch", "-D", name],
        cwd=REPO_ROOT, capture_output=True
    )
    log.info(f"Removed worktree: {wt_path}")


