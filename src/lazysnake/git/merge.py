"""Merge-conflict helpers: union resolution and bisect state."""

from __future__ import annotations

from pathlib import Path

OURS = "<<<<<<<"
BASE = "|||||||"  # diff3 style only; absent in default merge style
SEP = "======="
THEIRS = ">>>>>>>"


def union_resolve(content: str) -> tuple[str, int]:
    """Resolve conflict markers by keeping *both* sides.

    Returns ``(merged_text, conflict_count)``. Each conflict block's ours
    section is kept, then theirs — the same result as
    ``git merge-file --union``. Handles both the default two-way marker
    style and diff3 (whose ``|||||||`` base section is dropped).
    Content without markers is returned intact.
    """
    out: list[str] = []
    state = "outer"  # outer -> ours -> (base) -> theirs -> outer
    conflicts = 0
    for line in content.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.startswith(OURS) and state == "outer":
            state = "ours"
            conflicts += 1
        elif stripped.startswith(BASE) and state == "ours":
            state = "base"  # diff3: the common ancestor section
        elif stripped == SEP and state in ("ours", "base"):
            state = "theirs"
        elif stripped.startswith(THEIRS) and state == "theirs":
            state = "outer"
        elif state == "base":
            pass  # base lines are dropped in a union merge
        else:
            out.append(line)
    return "".join(out), conflicts


def bisect_in_progress(repo_root: Path | str) -> bool:
    """A bisect is underway when .git/BISECT_LOG exists."""
    return (Path(repo_root) / ".git" / "BISECT_LOG").exists()
