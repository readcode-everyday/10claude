import json
from pathlib import Path
from typing import List, Dict, Any

MEMORY_DIR = Path("/app/workspace/semantic_memory/entries")


def _load_entries() -> List[Dict[str, Any]]:
    entries = []
    for p in MEMORY_DIR.glob("*.json"):
        try:
            entries.append(json.loads(p.read_text()))
        except Exception:
            continue
    return entries


def retrieve_relevant(task: Dict[str, Any], top_k: int = 3) -> List[Dict[str, Any]]:
    """
    Simple keyword similarity retrieval.
    (Production-safe without vector DB.)
    """
    query = task.get("prompt", "").lower()
    scored = []

    for e in _load_entries():
        text = json.dumps(e).lower()
        score = sum(1 for word in query.split() if word in text)
        if score > 0:
            scored.append((score, e))

    scored.sort(reverse=True, key=lambda x: x[0])
    return [e for _, e in scored[:top_k]]
