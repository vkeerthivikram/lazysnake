"""Worktree listing via ``git worktree list --porcelain``."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Worktree:
    path: str
    head: str = ""
    branch: str = ""  # refs/heads/… empty for detached
    bare: bool = False
    detached: bool = False
    is_main: bool = False  # first worktree in the list

    @property
    def branch_short(self) -> str:
        return self.branch.removeprefix("refs/heads/") if self.branch else "(detached)"


def parse_worktrees(data: str) -> list[Worktree]:
    """Parse porcelain blocks separated by blank lines."""
    worktrees: list[Worktree] = []
    current: Worktree | None = None
    for line in data.splitlines():
        if not line.strip():
            current = None
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = Worktree(path=value, is_main=not worktrees)
            worktrees.append(current)
        elif current is None:
            continue
        elif key == "HEAD":
            current.head = value
        elif key == "branch":
            current.branch = value
        elif key == "bare":
            current.bare = True
        elif key == "detached":
            current.detached = True
    return worktrees
