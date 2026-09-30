"""Files-panel actions: staging, discarding, conflict resolution, ignore.

``FilesActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``mutate``/``confirm``/``_notify_error``,
``run_git``, ``refresh_state``, ``notify``, ``snapshot``, ``diff_view``,
``git`` and ``_editor_task``.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from textual import work

from lazysnake.git.diff import LineKind
from lazysnake.git.merge import union_resolve
from lazysnake.git.models import FileEntry
from lazysnake.git.runner import GitError
from lazysnake.staging import hunk_patch, line_patch

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from lazysnake.git.models import RepoSnapshot
    from lazysnake.git.runner import Git
    from lazysnake.ui.panels import DiffView


def build_editor_command() -> list[str] | None:
    """Split ``$EDITOR`` into an argv we can exec; None when unset."""
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL")
    if not editor:
        return None
    try:
        parts = shlex.split(editor)
    except ValueError:
        return None
    return parts or None


class FilesActions:
    """Stage/unstage, discard, resolve conflicts, ignore, open in editor."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        git: Git
        snapshot: RepoSnapshot
        diff_view: DiffView
        _editor_task: asyncio.Task[None] | None
        notify: Callable[..., Any]
        _notify_error: Callable[..., Any]
        run_git: Callable[..., Any]
        mutate: Callable[..., Any]
        _checkpoint: Callable[..., Any]
        _file_write_lock: asyncio.Lock
        confirm: Callable[..., Any]
        refresh_state: Callable[..., Any]

    # partial staging ---------------------------------------------------------

    @work(exclusive=True, group="action")
    async def stage_from_diff(self, mode: str) -> None:
        """Stage (or unstage, when viewing the staged side) hunks/lines."""
        dv = self.diff_view
        fd = dv.current_fd
        row = dv.selected_row
        if fd is None or row is None:
            self.notify("Nothing selected in the diff", severity="warning")
            return
        hunk = fd.hunks[row.hunk_index]
        try:
            if mode == "hunk":
                patch = hunk_patch(fd, hunk)
            elif row.line_index is not None and row.row_kind in (
                LineKind.ADDITION,
                LineKind.DELETION,
            ):
                patch = line_patch(fd, hunk, {row.line_index})
            else:
                self.notify("Move to a +/- line to stage a single line", severity="warning")
                return
        except ValueError as err:
            self._notify_error("partial stage", err)
            return
        args = ["apply", "--cached"]
        if dv.reverse:
            args.append("--reverse")
        await self.mutate(
            "partial stage",
            *args,
            input=patch,
            success="Staged" if not dv.reverse else "Unstaged",
        )

    # staging -------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def toggle_stage(self, entry: FileEntry, *, staged_view: bool) -> None:
        if entry.unmerged:
            self.notify("Resolve the conflict first (phase 5)", severity="warning")
            return
        if staged_view:
            await self.mutate("stage", "restore", "--staged", "--", entry.path)
        else:
            await self.mutate("stage", "add", "--", entry.path)

    @work(exclusive=True, group="action")
    async def toggle_stage_all(self) -> None:
        if self.snapshot.unstaged_files:
            await self.mutate("stage all", "add", "-A")
        else:
            await self.mutate("stage all", "restore", "--staged", ":/")

    # discard ---------------------------------------------------------------

    def request_discard(self, entry: FileEntry) -> None:
        if entry.unmerged:
            self.notify("Resolve the conflict first (phase 5)", severity="warning")
            return
        if entry.untracked:
            prompt = f"Delete untracked file '{entry.path}'?"
        else:
            prompt = f"Discard ALL changes in '{entry.path}'?\nThis cannot be undone."
        self.confirm(prompt, lambda: self.discard(entry))

    @work(exclusive=True, group="action")
    async def discard(self, entry: FileEntry) -> None:
        if entry.untracked:
            await self.mutate(
                "discard",
                "clean",
                "-f",
                "--",
                entry.path,
                success=f"Discarded changes in {entry.path}",
            )
        else:
            await self.mutate(
                "discard",
                "restore",
                "--source=HEAD",
                "--staged",
                "--worktree",
                "--",
                entry.path,
                success=f"Discarded changes in {entry.path}",
            )

    # conflicts ----------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def resolve_conflict_union(self, entry: FileEntry) -> None:
        """Resolve a conflicted file by keeping both sides (union merge)."""
        target = self.git.repo_root / entry.path
        try:
            async with self._file_write_lock:
                content = await asyncio.to_thread(target.read_text, errors="replace")
                merged, count = union_resolve(content)
                if count == 0:
                    self.notify("No conflict markers found in that file", severity="warning")
                    return
                await self._checkpoint()
                await _atomic_write_async(target, merged)
        except GitError as err:
            self._notify_error("resolve (union)", err)
            return
        except OSError as err:
            self.notify(f"cannot update {entry.path}: {err}", severity="error")
            return
        await self.mutate(
            "resolve (union)",
            "add",
            "--",
            entry.path,
            success=f"Union-merged {entry.path} ({count} conflict block(s))",
            checkpoint=False,
        )

    @work(exclusive=True, group="action")
    async def resolve_conflict(self, entry: FileEntry, side: str) -> None:
        """Resolve a conflicted file by taking ours/theirs, then stage it."""
        if side not in ("ours", "theirs"):
            raise ValueError(f"unknown conflict side: {side}")
        flag = "--ours" if side == "ours" else "--theirs"
        # Two commands with one error label and a single trailing refresh —
        # kept explicit rather than folded into two mutate() calls.
        try:
            await self.run_git("checkout", flag, "--", entry.path)
            await self.run_git("add", "--", entry.path, checkpoint=False)
        except GitError as err:
            self._notify_error(f"resolve ({side})", err)
            return
        self.notify(f"Resolved {entry.path} with {side}")
        await self.refresh_state(force=True).wait()

    # files extras --------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def ignore_file(self, entry: FileEntry) -> None:
        gitignore = self.git.repo_root / ".gitignore"
        try:
            async with self._file_write_lock:
                existed, existing = await asyncio.to_thread(_read_optional_text, gitignore)
                await self._checkpoint(remove_paths=() if existed else (".gitignore",))
                content = (
                    existing
                    + ("" if existing.endswith("\n") or not existing else "\n")
                    + entry.path
                    + "\n"
                )
                await _atomic_write_async(gitignore, content)
        except GitError as err:
            self._notify_error("ignore", err)
            return
        except OSError as err:
            self.notify(f"could not update .gitignore: {err}", severity="error")
            return
        self.notify(f"Ignored {entry.path}")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def open_in_editor(self, path: str) -> None:
        command = build_editor_command()
        if command is None:
            self.notify("$EDITOR is not set — configure it to open files", severity="warning")
            return
        try:
            proc = await asyncio.create_subprocess_exec(*command, str(self.git.repo_root / path))
        except OSError as err:
            self.notify(f"could not start editor: {err}", severity="error")
            return
        self.notify(f"Opened {path} ({' '.join(command)})")

        async def _reap() -> None:
            try:
                await proc.wait()
            except asyncio.CancelledError:
                # Same contract as the git runner: a cancelled wait kills
                # and reaps the child so no orphan editor is left behind.
                proc.kill()
                await proc.wait()
                raise

        # No timeout by design: $EDITOR follows interactive-editor
        # semantics, so a timeout would kill a legitimate editing session
        # mid-flight. Cancellation is still cleaned up above. The UI is
        # not blocked: the wait lives in its own task.
        self._editor_task = asyncio.create_task(_reap())


def _read_optional_text(path: Path) -> tuple[bool, str]:
    try:
        return True, path.read_text()
    except FileNotFoundError:
        return False, ""


async def _atomic_write_async(path: Path, text: str) -> None:
    task = asyncio.create_task(asyncio.to_thread(_atomic_write_text, path, text))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def _atomic_write_text(path: Path, text: str) -> None:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else None
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", dir=target.parent, prefix=f".{target.name}.", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(text)
        if mode is not None:
            temporary.chmod(mode)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
