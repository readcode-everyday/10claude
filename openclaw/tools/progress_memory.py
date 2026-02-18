"""
Detects major code changes and creates git checkpoints + workspace snapshots.
Runs automatically after every successful DAG execution.
"""
import subprocess
import logging
import yaml
from pathlib import Path
from datetime import datetime

from openclaw.adapters.git.repo import REPO_ROOT

log = logging.getLogger(__name__)

_cfg = None


def _config() -> dict:
    global _cfg
    if _cfg is None:
        _cfg = yaml.safe_load(Path("/app/config/major_change.yaml").read_text())
    return _cfg


def detect_major_change(repo_root: Path = None) -> bool:
    """Return True if git diff stats exceed configured thresholds."""
    root = repo_root or REPO_ROOT
    try:
        result = subprocess.run(
            ["git", "diff", "--stat", "HEAD"],
            capture_output=True, text=True, cwd=root
        )
        diff = result.stdout
        cfg = _config()["thresholds"]

        files_changed = sum(1 for line in diff.splitlines() if "|" in line)
        insertions = sum(
            int(p.split()[0]) for p in diff.splitlines()
            if "insertion" in p
        ) if "insertion" in diff else 0
        deletions = sum(
            int(p.split()[0]) for p in diff.splitlines()
            if "deletion" in p
        ) if "deletion" in diff else 0

        major = (
            files_changed >= cfg.get("files_changed", 5)
            or insertions >= cfg.get("insertions", 50)
            or deletions >= cfg.get("deletions", 20)
        )
        if major:
            log.info(f"Major change detected: files={files_changed} +{insertions} -{deletions}")
        return major
    except Exception as e:
        log.warning(f"detect_major_change failed: {e}")
        return False


def checkpoint_code(msg: str, repo_root: Path = None):
    """Commit all changes in the repo as a checkpoint."""
    root = repo_root or REPO_ROOT
    try:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", msg], cwd=root, capture_output=True)
        log.info(f"Checkpoint committed: {msg}")
    except Exception as e:
        log.warning(f"checkpoint_code failed: {e}")


def snapshot_workspace():
    """Create a timestamped tarball of /workspace as a full snapshot."""
    try:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = Path("/backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / f"snapshot-{ts}.tgz"
        subprocess.run(
            ["tar", "czf", str(dest), "/workspace"],
            check=True, capture_output=True
        )
        log.info(f"Workspace snapshot: {dest}")
    except Exception as e:
        log.warning(f"snapshot_workspace failed: {e}")
