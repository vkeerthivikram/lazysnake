"""Merge-conflict helpers: union resolution and bisect state."""

from __future__ import annotations

from lazysnake.git.runner import Git

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


async def bisect_in_progress(git: Git) -> bool:
    """A bisect is underway when BISECT_LOG exists in the git dir.

    Uses the runner's git-dir resolution so linked worktrees (where
    ``.git`` is a file) report correctly.
    """
    return (await git.state_file("BISECT_LOG")).exists()
