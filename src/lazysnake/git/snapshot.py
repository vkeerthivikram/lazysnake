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

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from lazysnake.git.runner import Git, GitError

SNAPSHOT_REF = "refs/lazysnake/snapshots"

_STATE_FILES = (
    "AUTO_MERGE",
    "BISECT_EXPECTED_REV",
    "BISECT_LOG",
    "BISECT_NAMES",
    "BISECT_START",
    "BISECT_TERMS",
    "CHERRY_PICK_HEAD",
    "FETCH_HEAD",
    "MERGE_HEAD",
    "MERGE_MODE",
    "MERGE_MSG",
    "MERGE_RR",
    "ORIG_HEAD",
    "REBASE_HEAD",
    "REVERT_HEAD",
    "SQUASH_MSG",
)
_STATE_DIRS = ("bisect", "rebase-apply", "rebase-merge", "sequencer")


@dataclass
class Snapshot:
    head: str | None  # HEAD sha, None when the symbolic branch was unborn
    index_tree: str | None  # None when the index was conflicted
    worktree_commit: str
    stashes: list[str] = field(default_factory=list)  # newest first, as git lists
    # Tree behind ``worktree_commit``, recorded by ``capture`` for free —
    # the content identity used to dedupe checkpoints.
    worktree_tree: str = ""
    head_ref: str | None = None  # symbolic HEAD target; None means detached
    refs: dict[str, str] = field(default_factory=dict)
    index_data: bytes | None = None  # exact index, including conflict stages
    git_state: dict[str, bytes | None] = field(default_factory=dict)
    remove_paths: tuple[str, ...] = ()  # app-created untracked files owned by this action

    @property
    def content_key(self) -> tuple[object, ...]:
        """Content identity of the snapshot, ignoring the snapshot chain.

        ``capture`` parents every snapshot commit on the previous one, so
        identical states produce different ``worktree_commit`` SHAs and
        plain equality never dedupes. The tree each commit points at is
        the actual content, so the key is (head, index, worktree tree,
        stashes). Computed from values ``capture`` already has — no extra
        git invocation at compare time.
        """
        return (
            self.head,
            self.head_ref,
            self.index_tree,
            self.index_data if self.index_tree is None else None,
            self.worktree_tree,
            tuple(self.stashes),
            tuple(sorted(self.refs.items())),
            tuple(sorted(self.git_state.items())),
            self.remove_paths,
        )


async def capture(
    git: Git,
    previous_snapshot: str | None,
    *,
    remove_paths: tuple[str, ...] = (),
) -> Snapshot:
    """Snapshot the repository state.

    ``previous_snapshot`` is the commit of the prior snapshot in the chain
    (parent of the new one) — pass the app's last snapshot commit. Any
    incomplete capture raises :class:`GitError`; callers must not mutate
    until the snapshot is complete.
    """
    head, head_ref = await _head_state(git)

    refs = await _read_refs(git)

    git_dir = Path((await git.run("rev-parse", "--absolute-git-dir")).strip())
    index_path = await _index_path(git)
    try:
        index_data = index_path.read_bytes() if index_path.exists() else None
    except OSError as err:
        raise _snapshot_error("read index", str(err)) from err

    # An unmerged index has no tree representation; retain its exact bytes.
    unmerged = await git.run("ls-files", "-u")
    index_tree = None if unmerged else (await git.run("write-tree")).strip()
    git_state = _capture_git_state(git_dir)
    stash_out = await git.run("stash", "list", "--format=%H")
    stashes = [line.strip() for line in stash_out.splitlines() if line.strip()]

    # Worktree content via a temporary index (real index untouched):
    # seed from HEAD, then absorb worktree changes incl. untracked files.
    try:
        with tempfile.NamedTemporaryFile(
            prefix="lazysnake-snap-", suffix=".index", delete=False
        ) as fh:
            tmp_index = Path(fh.name)
    except OSError as err:
        raise _snapshot_error("create temporary index", str(err)) from err

    try:
        env = {"GIT_INDEX_FILE": str(tmp_index)}
        if head:
            await git.run("read-tree", "HEAD", env_extra=env)
        else:
            await git.run("read-tree", "--empty", env_extra=env)
        await git.run("add", "-A", "--", ":/", env_extra=env)
        tree = (await git.run("write-tree", env_extra=env)).strip()
    finally:
        tmp_index.unlink(missing_ok=True)

    # Anchor the snapshot in a commit chain for reachability.
    args = ["commit-tree", tree, "-m", "lazysnake snapshot"]
    parent = previous_snapshot or head
    if parent:
        args.extend(["-p", parent])
    worktree_commit = (await git.run(*args)).strip()
    await git.run("update-ref", SNAPSHOT_REF, worktree_commit)

    return Snapshot(
        head=head,
        index_tree=index_tree,
        worktree_commit=worktree_commit,
        stashes=stashes,
        worktree_tree=tree,
        head_ref=head_ref,
        refs=refs,
        index_data=index_data,
        git_state=git_state,
        remove_paths=remove_paths,
    )


async def restore(git: Git, snap: Snapshot) -> None:
    """Return the repository to the snapshotted state.

    Untracked files created *after* the snapshot are left alone; everything
    the snapshot knew about is put back exactly, including index vs worktree
    splits, untracked file contents, and the stash list.
    """
    head_before, _ = await _head_state(git)
    git_dir = Path((await git.run("rev-parse", "--absolute-git-dir")).strip())
    snapshot_tree = (
        snap.worktree_tree
        or (await git.run("rev-parse", f"{snap.worktree_commit}^{{tree}}")).strip()
    )

    current_refs = await _read_refs(git)
    # Restore the active branch tip and recreate refs deleted since capture.
    # Leave every other existing tip alone: it may have advanced outside this
    # action, and undo must not silently rewind unrelated refs.
    for ref, sha in snap.refs.items():
        current = current_refs.get(ref)
        if current is None or (ref == snap.head_ref and snap.head is not None):
            if current != sha:
                await git.run("update-ref", ref, sha)

    _clear_git_state(git_dir)
    compare_head = snap.head or head_before
    if snap.head is not None:
        if snap.head_ref:
            await git.run("symbolic-ref", "HEAD", snap.head_ref)
        else:
            await git.run("update-ref", "--no-deref", "HEAD", snap.head)
        await git.run("reset", "--hard", snap.head)

    # `git diff A B --diff-filter=D` names paths present in A and absent
    # from B. Compare the restored HEAD (or the pre-restore HEAD when the
    # snapshot was unborn) to the snapshot worktree tree.
    if compare_head:
        gone = await git.run(
            "diff", "--name-only", "--diff-filter=D", "-z", compare_head, snapshot_tree
        )
    else:
        gone = ""
    victims = [p for p in gone.split("\0") if p]
    if victims:
        for i in range(0, len(victims), 100):
            chunk = victims[i : i + 100]
            await git.run("rm", "-q", "-f", "--ignore-unmatch", "--", *chunk)

    if snap.head is None:
        # The branch may no longer be checked out, but a commit created on
        # its formerly-unborn ref still belongs to this snapshot's state.
        # Compare-and-delete only that one ref; leave every other ref alone.
        if snap.head_ref and snap.head_ref not in snap.refs:
            ref_tip = current_refs.get(snap.head_ref)
            if ref_tip is not None:
                await git.run("update-ref", "-d", snap.head_ref, ref_tip)
        if snap.head_ref:
            await git.run("symbolic-ref", "HEAD", snap.head_ref)
        await git.run("read-tree", "--empty")

    # Worktree back to the snapshot: modifications, and resurrection of
    # files the snapshot knew (verified: restore -W recreates untracked).
    if (await git.run("ls-tree", snapshot_tree)).strip():
        await git.run("restore", "-s", snap.worktree_commit, "-W", "--", ":/")

    # Put the exact index back after reset/restore, including conflict stages.
    index_path = await _index_path(git)
    if snap.index_data is None:
        index_path.unlink(missing_ok=True)
    else:
        _atomic_write(index_path, snap.index_data)

    _restore_git_state(git_dir, snap.git_state)

    for rel_path in snap.remove_paths:
        path = Path(rel_path)
        if path.is_absolute() or ".." in path.parts:
            raise _snapshot_error("remove app-created path", f"unsafe path: {rel_path}")
        (git.repo_root / path).unlink(missing_ok=True)

    await _restore_stashes(git, snap.stashes)


async def _head_state(git: Git) -> tuple[str | None, str | None]:
    """Return ``(HEAD sha, symbolic ref)``; only a missing branch tip is unborn."""
    try:
        head_ref = (await git.run("symbolic-ref", "-q", "HEAD")).strip()
    except GitError as err:
        if err.returncode != 1:
            raise
        head_ref = None

    try:
        head = (await git.run("rev-parse", "--verify", "-q", "HEAD")).strip()
    except GitError as head_error:
        if head_ref is None:
            raise
        try:
            await git.run("show-ref", "--verify", "--quiet", head_ref)
        except GitError as ref_error:
            if ref_error.returncode == 1:
                return None, head_ref
        raise head_error
    return head, head_ref


async def _read_refs(git: Git) -> dict[str, str]:
    output = await git.run(
        "for-each-ref",
        "--format=%(refname)%00%(objectname)%00%(symref)",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
    )
    refs: dict[str, str] = {}
    for row in output.splitlines():
        ref, separator, rest = row.partition("\0")
        sha, separator2, symref = rest.partition("\0")
        if separator and separator2 and ref and sha and not symref:
            refs[ref] = sha
    return refs


async def _index_path(git: Git) -> Path:
    raw = (await git.run("rev-parse", "--git-path", "index")).strip()
    path = Path(raw)
    return path if path.is_absolute() else git.repo_root / path


def _capture_git_state(git_dir: Path) -> dict[str, bytes | None]:
    state: dict[str, bytes | None] = {}
    for name in _STATE_FILES:
        path = git_dir / name
        try:
            mode = os.lstat(path).st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as err:
            raise _snapshot_error("capture git state", str(err)) from err
        if not stat.S_ISREG(mode):
            raise _snapshot_error("capture git state", f"unexpected state file: {path}")
        try:
            state[name] = path.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as err:
            raise _snapshot_error("capture git state", str(err)) from err

    for name in _STATE_DIRS:
        root = git_dir / name
        try:
            mode = os.lstat(root).st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as err:
            raise _snapshot_error("capture git state", str(err)) from err
        if not stat.S_ISDIR(mode):
            raise _snapshot_error("capture git state", f"unexpected state directory: {root}")
        _scan_state_directory(root, root.name, state)
    return state


def _scan_state_directory(path: Path, rel_dir: str, state: dict[str, bytes | None]) -> None:
    try:
        with os.scandir(path) as entries:
            state[rel_dir] = None
            for entry in entries:
                child = Path(entry.path)
                rel = f"{rel_dir}/{entry.name}"
                try:
                    if entry.is_symlink():
                        raise _snapshot_error("capture git state", f"unexpected symlink: {child}")
                    if entry.is_dir(follow_symlinks=False):
                        _scan_state_directory(child, rel, state)
                    elif entry.is_file(follow_symlinks=False):
                        state[rel] = child.read_bytes()
                    else:
                        raise _snapshot_error(
                            "capture git state", f"unexpected state entry: {child}"
                        )
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as err:
                    raise _snapshot_error("capture git state", str(err)) from err
    except (FileNotFoundError, NotADirectoryError):
        state.pop(rel_dir, None)
    except OSError as err:
        raise _snapshot_error("capture git state", str(err)) from err


def _clear_git_state(git_dir: Path) -> None:
    for name in (*_STATE_FILES, *_STATE_DIRS):
        path = git_dir / name
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError as err:
            raise _snapshot_error("clear git operation state", str(err)) from err


def _restore_git_state(git_dir: Path, state: dict[str, bytes | None]) -> None:
    for rel, data in sorted(state.items(), key=lambda item: (item[0].count("/"), item[0])):
        path = git_dir / rel
        if data is None:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as err:
                raise _snapshot_error("restore git operation state", str(err)) from err
        else:
            _atomic_write(path, data)


def _atomic_write(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode & 0o777 if path.exists() else None
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as fh:
            temporary = Path(fh.name)
            fh.write(data)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, path)
    except OSError as err:
        raise _snapshot_error("write repository state", str(err)) from err
    finally:
        if "temporary" in locals():
            temporary.unlink(missing_ok=True)


def _snapshot_error(action: str, detail: str) -> GitError:
    return GitError(["snapshot", action], 128, "", detail)


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
