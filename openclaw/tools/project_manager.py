"""
Project management — each project is an isolated git repo under /workspace/projects/.
The CLI selects (or creates) a project before submitting any task; the chosen
project_path is embedded in the task payload and flows through the entire pipeline.
"""
import json
import logging
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

CLAUDE_MD_TEMPLATE = Path("/app/config/CLAUDE.md")

log = logging.getLogger(__name__)

PROJECTS_ROOT = Path("/app/workspace/projects")


def _github_create_repo(name: str) -> str | None:
    """
    Create a private GitHub repo via REST API.
    Returns an HTTPS remote URL with token embedded, or None on failure.
    """
    token = os.environ.get("GITHUB_TOKEN", "")
    user  = os.environ.get("GITHUB_USER", "")
    if not token or not user:
        log.warning("GITHUB_TOKEN or GITHUB_USER not set — skipping GitHub repo creation")
        return None

    payload = json.dumps({"name": name, "private": True, "auto_init": False}).encode()
    req = urllib.request.Request(
        "https://api.github.com/user/repos",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            log.info(f"GitHub repo created: {data.get('html_url')}")
            return f"https://{token}@github.com/{user}/{name}.git"
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        log.warning(f"GitHub repo creation failed {e.code}: {body[:200]}")
        return None
    except Exception as e:
        log.warning(f"GitHub repo creation error: {e}")
        return None


def list_projects() -> list:
    """Return sorted list of existing project names (dirs with a .git inside)."""
    if not PROJECTS_ROOT.exists():
        return []
    return sorted(
        p.name for p in PROJECTS_ROOT.iterdir()
        if p.is_dir() and (p / ".git").exists()
    )


def create_project(name: str) -> Path:
    """Create a new git-initialised project repo, push to GitHub. Returns its path."""
    path = PROJECTS_ROOT / name
    path.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME":    os.environ.get("GIT_AUTHOR_NAME", "openclaw"),
        "GIT_AUTHOR_EMAIL":   os.environ.get("GIT_AUTHOR_EMAIL", "openclaw@local"),
        "GIT_COMMITTER_NAME": os.environ.get("GIT_COMMITTER_NAME", "openclaw"),
        "GIT_COMMITTER_EMAIL":os.environ.get("GIT_COMMITTER_EMAIL", "openclaw@local"),
    }

    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    (path / ".gitkeep").touch()
    subprocess.run(["git", "add", ".gitkeep"], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True, env=env)

    # Inject agent rules so Claude Code reads them automatically
    if CLAUDE_MD_TEMPLATE.exists():
        shutil.copy(CLAUDE_MD_TEMPLATE, path / "CLAUDE.md")
        subprocess.run(["git", "add", "CLAUDE.md"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add agent rules"], cwd=path, check=True, capture_output=True, env=env)

    # Create GitHub remote and push
    remote_url = _github_create_repo(name)
    if remote_url:
        subprocess.run(["git", "remote", "add", "origin", remote_url],
                       cwd=path, check=True, capture_output=True)
        result = subprocess.run(["git", "push", "-u", "origin", "main"],
                                cwd=path, capture_output=True, env=env)
        if result.returncode == 0:
            log.info(f"Pushed initial commits to GitHub: {name}")
        else:
            log.warning(f"Push to GitHub failed: {result.stderr.decode()[:200]}")

    # If running inside Docker as root, chown to the host user so the dev agent
    # (which runs on the host as the real user) can write to the project.
    host_uid = os.environ.get("HOST_UID")
    host_gid = os.environ.get("HOST_GID")
    if host_uid and host_gid:
        try:
            subprocess.run(
                ["chown", "-R", f"{host_uid}:{host_gid}", str(path)],
                check=True, capture_output=True,
            )
            log.info(f"chown {host_uid}:{host_gid} {path}")
        except Exception as e:
            log.warning(f"chown failed (non-fatal): {e}")

    log.info(f"Created project: {path}")
    return path


def get_project_path(name: str) -> Path:
    return PROJECTS_ROOT / name
