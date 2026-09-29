"""Git plumbing: async command runner and machine-format parsers."""

from lazysnake.git.branch import Branch, parse_branches
from lazysnake.git.diff import FileDiff, Hunk, LineKind, PatchLine, parse_diff
from lazysnake.git.log import Commit, parse_log
from lazysnake.git.models import FileEntry, RepoSnapshot
from lazysnake.git.runner import Git, GitError
from lazysnake.git.stash import StashEntry, parse_stash
from lazysnake.git.status import merge_in_progress, parse_status
from lazysnake.git.tags import Tag, parse_tags

__all__ = [
    "Branch",
    "Commit",
    "FileDiff",
    "FileEntry",
    "Git",
    "GitError",
    "Hunk",
    "LineKind",
    "PatchLine",
    "RepoSnapshot",
    "StashEntry",
    "Tag",
    "merge_in_progress",
    "parse_branches",
    "parse_diff",
    "parse_log",
    "parse_stash",
    "parse_status",
    "parse_tags",
]
