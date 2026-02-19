"""
Persistent world model — tracks goal progress and failure history
across the lifetime of the daemon.
"""
import json
import logging
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

log = logging.getLogger(__name__)

WORLD_FILE = Path("/app/workspace/memory/world_model.json")


def _load() -> Dict[str, Any]:
    if not WORLD_FILE.exists():
        return {"goals": [], "failures": []}
    try:
        return json.loads(WORLD_FILE.read_text())
    except Exception:
        return {"goals": [], "failures": []}


def _save(data: Dict[str, Any]):
    WORLD_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORLD_FILE.write_text(json.dumps(data, indent=2))


def record_failure(info: Dict[str, Any]):
    world = _load()
    world["failures"].append({**info, "timestamp": datetime.utcnow().isoformat()})
    _save(world)
    log.warning(f"Failure recorded: {info.get('goal', info.get('task_id', '?'))[:60]}")


def record_goal_progress(goal: str, status: str):
    world = _load()
    world["goals"].append({
        "goal": goal,
        "status": status,
        "timestamp": datetime.utcnow().isoformat(),
    })
    _save(world)
    log.info(f"Goal progress recorded: '{goal[:60]}' → {status}")
