"""Parser for ``git status --porcelain=v2 --branch -z``.

Record shapes (NUL-terminated when ``-z`` is used)::

    # branch.head <name | (detached) | (initial)>
    # branch.oid <sha | (initial)>
    # branch.upstream <upstream>
    # branch.ab +<ahead> -<behind>
    1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
    2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path>\0<origPath>
    u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
    ? <path>
    ! <path>            (only with --ignored; we never ask for it)
"""

from __future__ import annotations

import re
from pathlib import Path

from lazysnake.git.models import FileEntry, RepoSnapshot

_AB_RE = re.compile(r"^# branch\.ab \+(\d+) -(\d+)$")


def merge_in_progress(repo_root: Path | str) -> bool:
    """A merge is underway when ``MERGE_HEAD`` exists in the git dir.

    Porcelain v2 does not report merge state, so callers check this
    alongside ``parse_status``. (Linked worktrees store ``.git`` as a
    file — not handled yet.)
    """
    return (Path(repo_root) / ".git" / "MERGE_HEAD").exists()


def parse_status(data: str) -> RepoSnapshot:
    """Parse the NUL-separated output of porcelain-v2 status into a snapshot."""
    snap = RepoSnapshot()
    records = data.split("\0")
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record:
            continue
        if record.startswith("# branch.head "):
            snap.branch = record.removeprefix("# branch.head ")
        elif record.startswith("# branch.oid "):
            oid = record.removeprefix("# branch.oid ")
            snap.oid = None if oid == "(initial)" else oid
        elif record.startswith("# branch.upstream "):
            snap.upstream = record.removeprefix("# branch.upstream ")
        elif record.startswith("# branch.ab "):
            m = _AB_RE.match(record)
            if m:
                snap.ahead, snap.behind = int(m.group(1)), int(m.group(2))
        elif record.startswith("# rebase."):
            snap.rebasing = True
        elif record.startswith("1 "):
            fields = record.split(" ", 8)
            if len(fields) == 9:
                snap.files.append(_entry(fields[1], fields[8]))
        elif record.startswith("2 "):
            fields = record.split(" ", 9)
            if len(fields) == 10:
                entry = _entry(fields[1], fields[9])
                # With -z the original path is the next NUL-separated record.
                entry.orig_path = records[i] if i < len(records) else None
                i += 1
                snap.files.append(entry)
        elif record.startswith("u "):
            fields = record.split(" ", 10)
            if len(fields) == 11:
                entry = _entry(fields[1], fields[10], unmerged=True)
                snap.files.append(entry)
        elif record.startswith("? "):
            snap.files.append(FileEntry(path=record[2:], index_status="?", worktree_status="?"))
        # "! " ignored entries never appear without --ignored.
    return snap


def _entry(xy: str, path: str, *, unmerged: bool = False) -> FileEntry:
    x, y = xy[0], xy[1] if len(xy) > 1 else "."
    return FileEntry(
        path=path,
        index_status=x,
        worktree_status=y,
        unmerged=unmerged,
    )
