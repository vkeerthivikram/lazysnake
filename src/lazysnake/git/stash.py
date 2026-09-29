"""Stash listing via ``git stash list --format=...``."""

from __future__ import annotations

import re
from dataclasses import dataclass

SEP = "\x1f"
FORMAT = f"%gd{SEP}%H{SEP}%s{SEP}%ci"

_INDEX_RE = re.compile(r"stash@\{(\d+)\}")


@dataclass
class StashEntry:
    index: int
    name: str  # stash@{0}
    sha: str
    subject: str
    when: str


def parse_stash(data: str) -> list[StashEntry]:
    entries: list[StashEntry] = []
    for line in data.splitlines():
        if not line.strip():
            continue
        fields = (line.split(SEP) + [""] * 4)[:4]
        name, sha, subject, when = fields
        m = _INDEX_RE.search(name)
        index = int(m.group(1)) if m else len(entries)
        entries.append(
            StashEntry(
                index=index,
                name=name or f"stash@{{{index}}}",
                sha=sha,
                subject=subject,
                when=when,
            )
        )
    return entries
