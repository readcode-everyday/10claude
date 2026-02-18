"""
Long-term experience store. Saves task+DAG pairs after successful runs
so the planner can learn from past executions.
"""
import json
import logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger(__name__)

ENTRIES = Path("/workspace/semantic_memory/entries")


def store_experience(task: dict, dag: dict):
    try:
        ENTRIES.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "task": task,
            "dag": dag,
        }
        ts = int(datetime.utcnow().timestamp() * 1000)
        path = ENTRIES / f"{ts}.json"
        path.write_text(json.dumps(entry, indent=2))
        log.debug(f"Experience stored: {path.name}")
    except Exception as e:
        log.warning(f"store_experience failed: {e}")
