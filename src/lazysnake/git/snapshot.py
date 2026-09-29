"""Full-state snapshots for undo/redo.

A snapshot captures everything a mutating git action is about to disturb:

* ``HEAD`` position,
* the index, as a tree (``git write-tree``),
* the tracked+untracked worktree content (non-ignored), as a commit built
  through a temporary index so the real one is never touched,
* the stash list.

Restore replays all four: reset to HEAD, remove files the snapshot says
were gone, reload the snapshot index, overlay the snapshot worktree via
``git restore -s <snapshot> -W`` (which also resurrects untracked files),
and repair the stash list. The snapshot chain is anchored on
``refs/lazysnake/snapshots`` so the objects stay reachable for the session.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lazysnake.git.runner import Git, GitError

SNAPSHOT_REF = "refs/lazysnake/snapshots"


@dataclass
class Snapshot:
    head: str | None  # HEAD sha, None when the repo had no commits
    index_tree: str | None  # None when the index was conflicted/unreadable
    worktree_commit: str
    stashes: list[str] = field(default_factory=list)  # newest first, as git lists


async def capture(git: Git, previous_snapshot: str | None) -> Snapshot | None:
    """Snapshot the repository state.

    ``previous_snapshot`` is the commit of the prior snapshot in the chain
    (parent of the new one) — pass the app's last snapshot commit. Returns
    ``None`` only if git is too broken to snapshot at all.
    """
    # HEAD (may be unborn).
    head: str | None = None
    try:
        out = await git.run("rev-parse", "--verify", "-q", "HEAD", check=False)
        if out.strip():
            head = out.strip()
    except GitError:
        head = None

    # Index tree; conflicted indexes cannot be written as-is.
    index_tree: str | None = None
    try:
        index_tree = (await git.run("write-tree")).strip()
    except GitError:
        index_tree = None

    # Worktree content via a temporary index (real index untouched):
    # seed from HEAD, then absorb worktree changes incl. untracked files.
    with tempfile.NamedTemporaryFile(
        prefix="lazysnake-snap-", suffix=".index", delete=False
    ) as fh:
        tmp_index = Path(fh.name)
    env = {"GIT_INDEX_FILE": str(tmp_index)}
    try:
        if head:
            await git.run("read-tree", "HEAD", env_extra=env)
        else:
            await git.run("read-tree", "--empty", env_extra=env)
        await git.run("add", "-A", "--", ":/", env_extra=env)
        tree = (await git.run("write-tree", env_extra=env)).strip()
    except GitError:
        tmp_index.unlink(missing_ok=True)
        return None
    tmp_index.unlink(missing_ok=True)

    # Anchor the snapshot in a commit chain for reachability.
    args = ["commit-tree", tree, "-m", "lazysnake snapshot"]
    parent = previous_snapshot or head
    if parent:
        args.extend(["-p", parent])
    worktree_commit = (await git.run(*args)).strip()
    try:
        await git.run("update-ref", SNAPSHOT_REF, worktree_commit)
    except GitError:
        pass  # reachability is best-effort; the snapshot itself is valid

    # Stash list (newest first).
    try:
        stash_out = await git.run("stash", "list", "--format=%H")
        stashes = [line.strip() for line in stash_out.splitlines() if line.strip()]
    except GitError:
        stashes = []

    return Snapshot(
        head=head, index_tree=index_tree, worktree_commit=worktree_commit, stashes=stashes
    )


async def restore(git: Git, snap: Snapshot) -> None:
    """Return the repository to the snapshotted state.

    Untracked files created *after* the snapshot are left alone; everything
    the snapshot knew about is put back exactly, including index vs worktree
    splits, untracked file contents, and the stash list.
    """
    if snap.head:
        await git.run("reset", "--hard", snap.head)
    else:
        await git.run("read-tree", "--empty")

    # Files tracked at HEAD that the snapshot worktree no longer had:
    # `git diff A B --diff-filter=D` lists paths present in A, gone in B.
    snapshot_tree = await git.run("rev-parse", f"{snap.worktree_commit}^{{tree}}")
    snapshot_tree = snapshot_tree.strip()
    if snap.head:
        gone = await git.run(
            "diff", "--name-only", "--diff-filter=D", "-z",
            snapshot_tree, snap.head,
        )
    else:
        gone = ""
    victims = [p for p in gone.split("\0") if p]
    if victims:
        for i in range(0, len(victims), 100):
            chunk = victims[i : i + 100]
            await git.run("rm", "-q", "-f", "--ignore-unmatch", "--", *chunk)

    # Index back to the snapshot (worktree untouched by read-tree).
    if snap.index_tree is not None:
        await git.run("read-tree", snap.index_tree)

    # Worktree back to the snapshot: modifications, and resurrection of
    # files the snapshot knew (verified: restore -W recreates untracked).
    await git.run("restore", "-s", snap.worktree_commit, "-W", "--", ":/")

    await _restore_stashes(git, snap.stashes)


async def _restore_stashes(git: Git, wanted: list[str]) -> None:
    """Make the stash list equal ``wanted`` (newest first)."""

    async def current() -> list[str]:
        out = await git.run("stash", "list", "--format=%H")
        return [line.strip() for line in out.splitlines() if line.strip()]

    have = await current()
    have_set = set(have)

    # Re-add missing stashes oldest-first so the final order matches.
    for sha in reversed(wanted):
        if sha not in have_set:
            await git.run("stash", "store", "-m", "lazysnake undo", sha)
            have_set.add(sha)

    # Drop stashes that did not exist at snapshot time.
    wanted_set = set(wanted)
    while True:
        have = await current()
        extra = next((sha for sha in have if sha not in wanted_set), None)
        if extra is None:
            return
        index = have.index(extra)
        await git.run("stash", "drop", f"stash@{{{index}}}")
