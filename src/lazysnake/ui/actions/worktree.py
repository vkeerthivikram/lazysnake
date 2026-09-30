"""Repository navigation: worktrees, submodules, recent-repos switching.

``WorktreeActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``mutate``/``confirm``, ``run_git``, ``notify``,
``refresh_state``, ``git`` and ``submodules``.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from textual import work

from lazysnake.git.models import FileEntry
from lazysnake.git.runner import GitError
from lazysnake.git.submodule import submodules
from lazysnake.git.worktree import parse_worktrees
from lazysnake.recent import load_recent, record_recent
from lazysnake.ui.modals import (
    InputScreen,
    RecentReposScreen,
    SubmoduleResult,
    SubmodulesScreen,
    WorktreeResult,
    WorktreeScreen,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from lazysnake.git.runner import Git


class WorktreeActions:
    """Worktree browser, submodule manager, and repo switching."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        git: Git
        submodules: list[str]
        notify: Callable[..., Any]
        push_screen: Callable[..., Any]
        run_worker: Callable[..., Any]
        run_git: Callable[..., Any]
        mutate: Callable[..., Any]
        confirm: Callable[..., Any]
        _notify_error: Callable[..., Any]
        refresh_state: Callable[..., Any]
        _refreshed: Callable[..., Any]

    def switch_repo(self, path: str) -> None:
        """Relaunch lazysnake inside another repository."""
        record_recent(self.git.repo_root)
        os.execv(sys.executable, [sys.executable, "-m", "lazysnake", path])

    # recent repos -------------------------------------------------------------

    def action_recent_repos(self) -> None:
        paths = load_recent()
        if not paths:
            self.notify("No recent repositories recorded yet", severity="warning")
            return
        self.push_screen(
            RecentReposScreen(paths, str(self.git.repo_root)),
            lambda path: self.switch_repo(path) if path else None,
        )

    # worktrees ----------------------------------------------------------------

    def action_worktrees(self) -> None:
        self.run_worker(self._worktree_flow(), exclusive=True, group="action")

    async def _worktree_flow(self) -> None:
        data = await self.git.run("worktree", "list", "--porcelain")
        worktrees = parse_worktrees(data)
        self.push_screen(
            WorktreeScreen(worktrees),
            lambda result: self._worktree_act(result) if result else None,
        )

    def _worktree_act(self, result: WorktreeResult) -> None:
        if result.action == "add":
            self.push_screen(
                InputScreen("New worktree branch name:"),
                lambda name: self.worktree_add(name) if name else None,
            )
        elif result.action == "switch":
            if result.path is not None:
                self.switch_repo(result.path)
        elif result.action == "delete":
            if result.path is not None:
                self.confirm(
                    f"Remove worktree '{result.path}'?",
                    lambda: self.worktree_delete(result.path),
                )

    @work(exclusive=True, group="action")
    async def worktree_add(self, name: str) -> None:
        path = self.git.repo_root.parent / f"{self.git.repo_root.name}-{name}"
        await self.mutate(
            "worktree add",
            "worktree",
            "add",
            "-b",
            name,
            str(path),
            success=f"Worktree created at {path}",
        )

    @work(exclusive=True, group="action")
    async def worktree_delete(self, path: str) -> None:
        await self.mutate("worktree remove", "worktree", "remove", path, success=f"Removed {path}")

    # submodules ---------------------------------------------------------------

    def enter_if_submodule(self, entry: FileEntry) -> None:
        if entry.path.rstrip("/") in self.submodules:
            self.switch_repo(str(self.git.repo_root / entry.path.rstrip("/")))
        else:
            self.notify("Enter only works inside a submodule directory")

    def action_submodules(self) -> None:
        self.run_worker(self._submodule_flow(), exclusive=True, group="action")

    async def _submodule_flow(self) -> None:
        subs = await submodules(self.git)
        self.push_screen(
            SubmodulesScreen(subs),
            lambda result: self._submodule_act(result) if result else None,
        )

    def _submodule_act(self, result: SubmoduleResult) -> None:
        if result.action == "enter":
            if result.path is not None:
                self.switch_repo(str(self.git.repo_root / result.path))
        elif result.action == "update":
            self.submodule_update()
        elif result.action == "add":
            self.push_screen(
                InputScreen("Submodule URL:"),
                lambda url: self._submodule_add_path(url) if url else None,
            )
        elif result.action == "deinit":
            if result.path is None:
                self.notify("No submodule selected", severity="warning")
                return
            self.confirm(
                f"Deinit submodule '{result.path}'?\nIts worktree copy is removed.",
                lambda: self.submodule_deinit(result.path),
            )

    def _submodule_add_path(self, url: str) -> None:
        self.push_screen(
            InputScreen("Submodule path (inside the repo):"),
            lambda path: self.submodule_add(url, path) if path else None,
        )

    @work(exclusive=True, group="action")
    async def submodule_update(self) -> None:
        await self.mutate(
            "submodule update",
            "submodule",
            "update",
            "--init",
            "--recursive",
            success="Submodules updated",
        )

    @work(exclusive=True, group="action")
    async def submodule_add(self, url: str, path: str) -> None:
        args = ["submodule", "add"]
        if url.startswith(("/", "./", "../", "~/")):
            # An explicitly typed local path: opt into the file transport,
            # which git blocks by default (it only blocks file:// URLs).
            args = ["-c", "protocol.file.allow=always", *args]
        args.extend([url, path])
        # Two commands, one error label, one trailing refresh — kept
        # explicit rather than folded into two mutate() calls.
        try:
            await self.run_git(*args)
            await self.run_git("commit", "-m", f"Add submodule {path}", checkpoint=False)
        except GitError as err:
            self._notify_error("submodule add", err)
            return
        self.notify(f"Submodule added at {path}")
        await self._refreshed()

    @work(exclusive=True, group="action")
    async def submodule_deinit(self, path: str) -> None:
        await self.mutate(
            "submodule deinit",
            "submodule",
            "deinit",
            "-f",
            path,
            success=f"Submodule {path} deinitialized",
        )
