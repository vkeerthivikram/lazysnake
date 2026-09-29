"""Recent-repositories registry: ``~/.config/lazysnake/repos.json``."""

from __future__ import annotations

import json
from pathlib import Path

STORE_PATH = Path.home() / ".config" / "lazysnake" / "repos.json"
MAX_ENTRIES = 10


def load_recent(path: Path | None = None) -> list[str]:
    target = path or STORE_PATH
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(p) for p in data if isinstance(p, str)]


def record_recent(repo_root: Path | str, path: Path | None = None) -> list[str]:
    """Move ``repo_root`` to the front of the list, capped, and persist."""
    target = path or STORE_PATH
    entry = str(Path(repo_root).resolve())
    entries = [p for p in load_recent(target) if p != entry]
    entries.insert(0, entry)
    entries = entries[:MAX_ENTRIES]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(entries, indent=1))
    return entries
