"""Data models shared across the git layer and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FileEntry:
    """One path from ``git status --porcelain=v2``."""

    path: str
    orig_path: str | None = None  # previous name, for renames / copies
    index_status: str = "."
    worktree_status: str = "."
    unmerged: bool = False

    @property
    def untracked(self) -> bool:
        return self.index_status == "?"

    @property
    def staged(self) -> bool:
        """Changes recorded in the index (conflicts are their own state)."""
        return not self.untracked and not self.unmerged and self.index_status != "."

    @property
    def unstaged(self) -> bool:
        """Worktree changes relative to the index (includes untracked)."""
        return self.unmerged or self.untracked or self.worktree_status != "."

    @property
    def display(self) -> str:
        """Two-character status code, lazygit style (e.g. ``'M '``)."""
        if self.untracked:
            return "??"
        if self.unmerged:
            return f"{self.index_status}{self.worktree_status}"
        x = self.index_status if self.index_status != "." else " "
        y = self.worktree_status if self.worktree_status != "." else " "
        return f"{x}{y}"

    def __str__(self) -> str:
        name = self.path if self.orig_path is None else f"{self.orig_path} → {self.path}"
        return f"{self.display} {name}"


@dataclass
class RepoSnapshot:
    """Everything one ``git status`` call tells us about the repository."""

    branch: str = ""
    oid: str | None = None
    upstream: str | None = None
    ahead: int = 0
    behind: int = 0
    files: list[FileEntry] = field(default_factory=list)
    rebasing: bool = False
    merging: bool = False
    bisecting: bool = False
    cherry_picking: bool = False

    @property
    def staged_files(self) -> list[FileEntry]:
        return [f for f in self.files if f.staged]

    @property
    def unstaged_files(self) -> list[FileEntry]:
        return [f for f in self.files if f.unstaged]

    @property
    def untracked_files(self) -> list[FileEntry]:
        return [f for f in self.files if f.untracked]

    @property
    def conflict_files(self) -> list[FileEntry]:
        return [f for f in self.files if f.unmerged]

    @property
    def in_conflict(self) -> bool:
        return any(f.unmerged for f in self.files)
