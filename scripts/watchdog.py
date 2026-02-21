#!/usr/bin/env python3
"""
OpenClaw Watchdog — host-level systemd service.

Responsibilities:
  1. Hourly git backup of ~/10claude and ~/.openclaw/workspace
     (only commits/pushes when there are actual changes)
  2. Hourly Claude session ping to keep OAuth token warm
  3. Health check every 5 min:
       - All Docker containers (status-ui, worker-1/2/3, caddy)
       - openclaw gateway
       - status-ui HTTP health endpoint
       - Tasks stuck in_progress > 40 min
  4. Thread watchdog: detects dead threads and restarts them
  5. Proactive Telegram alerts on new health issues and resolutions

If this process itself dies, systemd (Restart=always) restarts it.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT             = Path(__file__).resolve().parent.parent   # ~/10claude
OPENCLAW_WS      = Path.home() / ".openclaw" / "workspace"
OPENCLAW_JSON    = Path.home() / ".openclaw" / "openclaw.json"
TASK_STORE       = ROOT / "workspace" / "queue" / "task_store.json"
RESULTS_DIR      = ROOT / "workspace" / "results"

# ── Intervals ─────────────────────────────────────────────────────────────────
BACKUP_INTERVAL  = 3600   # 1 hour
PING_INTERVAL    = 3600   # 1 hour
HEALTH_INTERVAL  = 300    # 5 minutes

# ── Containers to monitor ─────────────────────────────────────────────────────
CONTAINERS = ["status-ui", "worker-1", "worker-2", "worker-3", "caddy"]

# ── Telegram ──────────────────────────────────────────────────────────────────
def _telegram_config():
    """Read bot token and chat ID from ~/.openclaw/openclaw.json."""
    try:
        cfg = json.loads(OPENCLAW_JSON.read_text())
        token = cfg.get("channels", {}).get("telegram", {}).get("botToken", "")
        chat_id = ""
        for b in cfg.get("bindings", []):
            peer_id = b.get("match", {}).get("peer", {}).get("id", "")
            if peer_id.startswith("tg:"):
                chat_id = peer_id[3:]
                break
        return token, chat_id
    except Exception as e:
        log.warning(f"Could not read Telegram config: {e}")
        return "", ""


def send_telegram(msg: str):
    token, chat_id = _telegram_config()
    if not token or not chat_id:
        log.warning("Telegram not configured — skipping alert")
        return
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": msg,
            "parse_mode": "Markdown",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        log.info(f"Telegram: {msg[:80]}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


# ── Git Backup ────────────────────────────────────────────────────────────────
_BACKUP_REPOS = [ROOT, OPENCLAW_WS]


def _git_has_changes(repo: Path) -> bool:
    r = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, timeout=30,
    )
    return bool(r.stdout.strip())


def _git_has_unpushed(repo: Path) -> bool:
    # Get current branch
    branch = subprocess.run(
        ["git", "-C", str(repo), "branch", "--show-current"],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip() or "main"
    r = subprocess.run(
        ["git", "-C", str(repo), "log", f"origin/{branch}..HEAD", "--oneline"],
        capture_output=True, text=True, timeout=30,
    )
    return bool(r.stdout.strip())


def _git_push(repo: Path, branch: str) -> bool:
    for attempt in range(1, 4):
        r = subprocess.run(
            ["git", "-C", str(repo), "push", "origin", branch],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode == 0:
            return True
        log.warning(f"[git-backup] {repo.name}: push attempt {attempt} failed")
        subprocess.run(
            ["git", "-C", str(repo), "pull", "--rebase", "origin", branch],
            capture_output=True, timeout=30,
        )
    return False


def git_backup():
    """Auto-commit tracked changes and push each backup repo."""
    for repo in _BACKUP_REPOS:
        if not (repo / ".git").exists():
            log.info(f"[git-backup] {repo.name}: no .git, skipping")
            continue
        try:
            branch = subprocess.run(
                ["git", "-C", str(repo), "branch", "--show-current"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip() or "main"

            has_changes = _git_has_changes(repo)
            has_unpushed = _git_has_unpushed(repo)

            if not has_changes and not has_unpushed:
                log.info(f"[git-backup] {repo.name}: clean, nothing to do")
                continue

            if has_changes:
                # Stage tracked files only (not untracked — those should be
                # deliberate; use git add -u to avoid committing accidental files)
                subprocess.run(
                    ["git", "-C", str(repo), "add", "-u"],
                    capture_output=True, timeout=30,
                )
                # Also pick up new files that are already tracked by .gitignore
                # (i.e. respect gitignore — only add things not ignored)
                subprocess.run(
                    ["git", "-C", str(repo), "add", "-A"],
                    capture_output=True, timeout=30,
                )
                ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                r = subprocess.run(
                    ["git", "-C", str(repo), "commit", "-m", f"chore: auto-backup {ts}"],
                    capture_output=True, text=True, timeout=30,
                )
                if r.returncode == 0:
                    log.info(f"[git-backup] {repo.name}: committed changes")
                else:
                    # Nothing to commit (already clean after add)
                    log.info(f"[git-backup] {repo.name}: nothing to commit after add")

            if _git_push(repo, branch):
                log.info(f"[git-backup] {repo.name}: pushed ✓")
            else:
                log.error(f"[git-backup] {repo.name}: push failed after 3 attempts")
                send_telegram(
                    f"⚠️ *Git Backup Failed*\n`{repo.name}` could not push after 3 attempts"
                )
        except subprocess.TimeoutExpired:
            log.error(f"[git-backup] {repo.name}: timed out")
        except Exception as e:
            log.error(f"[git-backup] {repo.name}: {e}")


# ── Claude Ping ───────────────────────────────────────────────────────────────
_claude_last_ok: datetime | None = None
_claude_alerted = False


def claude_ping():
    """Ping Claude CLI to keep the OAuth session warm."""
    global _claude_last_ok, _claude_alerted
    try:
        env = {**os.environ}
        # Remove API key so CLI is forced through OAuth (not direct API)
        env.pop("ANTHROPIC_API_KEY", None)
        result = subprocess.run(
            ["claude", "-p", "hi", "--output-format", "json"],
            capture_output=True, text=True, timeout=90, env=env,
        )
        if result.returncode == 0:
            _claude_last_ok = datetime.now()
            _claude_alerted = False
            log.info("[claude-ping] OK")
        else:
            err = (result.stderr or result.stdout)[:120]
            log.error(f"[claude-ping] failed: {err}")
            if not _claude_alerted:
                send_telegram(
                    f"⚠️ *Claude Session Expired*\n"
                    f"Workers may lose auth. Run on host:\n`claude auth login`"
                )
                _claude_alerted = True
    except subprocess.TimeoutExpired:
        log.error("[claude-ping] timed out after 90s")
        if not _claude_alerted:
            send_telegram("⚠️ *Claude Ping Timeout* — session may be stale")
            _claude_alerted = True
    except FileNotFoundError:
        log.error("[claude-ping] claude CLI not found in PATH")


# ── Health Checks ─────────────────────────────────────────────────────────────
_known_issues: set[str] = set()


def _check_containers() -> list[str]:
    issues = []
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=15,
        )
        running = set()
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if parts and "Up" in (parts[1] if len(parts) > 1 else ""):
                running.add(parts[0])
        for name in CONTAINERS:
            if name not in running:
                issues.append(f"container `{name}` is down")
    except FileNotFoundError:
        issues.append("docker not found")
    except Exception as e:
        issues.append(f"docker check error: {e}")
    return issues


def _check_status_ui() -> list[str]:
    try:
        req = urllib.request.Request("http://127.0.0.1:18888/api/health")
        with urllib.request.urlopen(req, timeout=10):
            pass
        return []
    except Exception:
        return ["status-ui not responding at :18888"]


def _check_gateway() -> list[str]:
    try:
        result = subprocess.run(
            ["openclaw", "gateway", "health"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return ["openclaw gateway not responding"]
        return []
    except FileNotFoundError:
        return ["openclaw CLI not found"]
    except Exception as e:
        return [f"openclaw gateway check error: {e}"]


def _check_stuck_tasks() -> list[str]:
    issues = []
    try:
        if not TASK_STORE.exists():
            return []
        store = json.loads(TASK_STORE.read_text())
        now = datetime.now()
        for tid, task in store.items():
            if task.get("status") != "in_progress":
                continue
            updated = task.get("updated_at") or task.get("created_at", "")
            if not updated:
                continue
            try:
                dt = datetime.fromisoformat(updated.replace("Z", "+00:00").replace("+00:00", ""))
                age_min = (now - dt).total_seconds() / 60
                if age_min > 40:
                    title = task.get("title", tid[:12])
                    issues.append(f"task `{title}` stuck in_progress for {int(age_min)}min")
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[health] task store error: {e}")
    return issues


def health_check():
    global _known_issues
    current: list[str] = []
    current += _check_containers()
    current += _check_status_ui()
    current += _check_gateway()
    current += _check_stuck_tasks()

    current_set = set(current)
    new_issues   = current_set - _known_issues
    resolved     = _known_issues - current_set

    if new_issues:
        msg = "🚨 *OpenClaw Health Alert*\n\n" + "\n".join(f"• {i}" for i in sorted(new_issues))
        log.warning(f"[health] new issues: {new_issues}")
        send_telegram(msg)

    if resolved:
        msg = "✅ *Issues Resolved*\n\n" + "\n".join(f"• {i}" for i in sorted(resolved))
        log.info(f"[health] resolved: {resolved}")
        send_telegram(msg)

    _known_issues = current_set

    if not current:
        log.info("[health] all systems OK")


# ── Thread runner + watchdog ───────────────────────────────────────────────────
def _run_periodically(fn, interval: int, initial_delay: int = 0):
    if initial_delay:
        time.sleep(initial_delay)
    while True:
        try:
            fn()
        except Exception as e:
            log.error(f"[{fn.__name__}] unhandled exception: {e}")
        time.sleep(interval)


def _start_thread(fn, interval: int, delay: int = 0) -> threading.Thread:
    t = threading.Thread(
        target=_run_periodically,
        args=(fn, interval, delay),
        name=fn.__name__,
        daemon=True,
    )
    t.start()
    return t


def _watchdog_monitor(threads: dict[str, tuple]):
    """Check every 60s that all managed threads are alive; restart any that died."""
    while True:
        time.sleep(60)
        for name, spec in list(threads.items()):
            fn, interval, t = spec
            if not t.is_alive():
                log.error(f"[watchdog] thread '{name}' died — restarting")
                send_telegram(f"⚠️ *Watchdog*: thread `{name}` died and was restarted")
                new_t = _start_thread(fn, interval)
                threads[name] = (fn, interval, new_t)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("OpenClaw Watchdog starting")
    send_telegram("🟢 *OpenClaw Watchdog* started")

    # (fn, interval_secs, initial_delay_secs)
    specs = {
        "git_backup":   (git_backup,    BACKUP_INTERVAL, 30),
        "claude_ping":  (claude_ping,   PING_INTERVAL,   60),
        "health_check": (health_check,  HEALTH_INTERVAL, 90),
    }

    # Start threads and record them for the watchdog monitor
    threads: dict[str, tuple] = {}
    for name, (fn, interval, delay) in specs.items():
        t = _start_thread(fn, interval, delay)
        threads[name] = (fn, interval, t)

    # Thread monitor
    monitor = threading.Thread(
        target=_watchdog_monitor,
        args=(threads,),
        name="watchdog_monitor",
        daemon=True,
    )
    monitor.start()

    log.info(f"Threads: {list(threads.keys())} + watchdog_monitor")

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Watchdog shutting down")
        send_telegram("🔴 *OpenClaw Watchdog* stopped (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
