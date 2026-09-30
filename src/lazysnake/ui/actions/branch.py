"""Branches-panel actions: checkout, create/delete, merge, fast-forward,
rebase-onto, upstream wiring.

``BranchActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``mutate``/``confirm``, ``run_git``, ``notify``,
``refresh_state``, ``snapshot`` and ``git``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work

from lazysnake.git.branch import Branch
from lazysnake.git.runner import GitError
from lazysnake.ui.modals import InputScreen

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from lazysnake.git.models import RepoSnapshot
    from lazysnake.git.runner import Git


class BranchActions:
    """Everything the Branches panel triggers."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        git: Git
        snapshot: RepoSnapshot
        notify: Callable[..., Any]
        push_screen: Callable[..., Any]
        run_worker: Callable[..., Any]
        mutate: Callable[..., Any]
        confirm: Callable[..., Any]
        refresh_state: Callable[..., Any]
        _refreshed: Callable[..., Any]

    @work(exclusive=True, group="action")
    async def checkout_branch(self, branch: Branch) -> None:
        if branch.is_head:
            self.notify(f"Already on {branch.name}")
            return
        target = branch.short_name if branch.is_remote else branch.name
        if branch.is_remote:
            await self.mutate(
                "checkout", "switch", "--track", branch.name, success=f"Switched to {target}"
            )
        else:
            await self.mutate("checkout", "switch", branch.name, success=f"Switched to {target}")

    def request_create_branch(self) -> None:
        self.push_screen(
            InputScreen("New branch name:", placeholder="feature/…"),
            lambda name: self.create_branch(name) if name else None,
        )

    @work(exclusive=True, group="action")
    async def create_branch(self, name: str) -> None:
        await self.mutate(
            "create branch", "switch", "-c", name, success=f"Created and switched to {name}"
        )

    def request_delete_branch(self, branch: Branch, *, force: bool = False) -> None:
        if branch.is_remote:
            prompt = (
                f"Delete remote branch '{branch.name}'?\n"
                f"Runs git push {branch.name.split('/', 1)[0]} --delete."
            )
        elif branch.is_head:
            self.notify("Cannot delete the branch you are on", severity="warning")
            return
        elif force:
            prompt = f"FORCE delete branch '{branch.name}'?\nUnmerged commits become unreachable."
        else:
            prompt = f"Delete branch '{branch.name}'?"
        self.confirm(prompt, lambda: self.delete_branch(branch, force=force))

    @work(exclusive=True, group="action")
    async def delete_branch(self, branch: Branch, *, force: bool = False) -> None:
        if branch.is_remote:
            # The ref prefix IS the remote (origin/feat → origin,
            # mirror/feat → mirror); never assume it is "origin".
            remote = branch.name.split("/", 1)[0]
            await self.mutate(
                "delete branch",
                "push",
                remote,
                "--delete",
                branch.short_name,
                success=f"Deleted {branch.name}",
            )
        else:
            await self.mutate(
                "delete branch",
                "branch",
                "-D" if force else "-d",
                branch.name,
                success=f"Deleted {branch.name}",
            )

    # conflicts ---------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def merge_branch(self, branch: Branch) -> None:
        if branch.is_head:
            self.notify("Already on that branch — nothing to merge", severity="warning")
            return
        await self.mutate(
            "merge",
            "merge",
            "--no-edit",
            branch.name,
            success=f"Merged {branch.name}",
            refresh_on_error=True,
            # Evaluated only after a failure (a mid-merge poll can flip
            # in_conflict), exactly like the old inline check.
            conflict_hint=lambda: (
                "Conflicts! Resolve with o/t in the Files panel"
                if self.snapshot.in_conflict
                else None
            ),
        )

    # branch extras --------------------------------------------------------

    @work(exclusive=True, group="action")
    async def fast_forward_branch(self, branch: Branch) -> None:
        await self.mutate(
            "fast-forward",
            "merge",
            "--ff-only",
            branch.name,
            success=f"Fast-forwarded to {branch.name}",
        )

    def request_rebase_onto(self, branch: Branch) -> None:
        self.confirm(
            f"Rebase current branch onto '{branch.name}'?",
            lambda: self.rebase_onto(branch),
        )

    @work(exclusive=True, group="action")
    async def rebase_onto(self, branch: Branch) -> None:
        await self.mutate(
            "rebase",
            "rebase",
            branch.name,
            success=f"Rebased onto {branch.name}",
            refresh_on_error=True,
            conflict_hint="Fix conflicts or press K to abort the rebase",
        )

    async def _remote_for(self, branch: Branch | None = None) -> str:
        """The remote a remote-facing action should target.

        Prefers the branch's own ref prefix (``origin/feat`` →
        ``origin``), then the branch's configured upstream, then asks
        git: the sole remote when there is exactly one, else ``origin``
        if it exists, else the first remote alphabetically. Falls back
        to ``origin`` only when git cannot be asked at all.
        """
        if branch is not None:
            if branch.is_remote and "/" in branch.name:
                return branch.name.split("/", 1)[0]
            if branch.upstream and "/" in branch.upstream:
                return branch.upstream.split("/", 1)[0]
        try:
            remotes = (await self.git.run("remote")).split()
        except GitError:
            remotes = []
        if len(remotes) == 1:
            return remotes[0]
        if "origin" in remotes:
            return "origin"
        return remotes[0] if remotes else "origin"

    def request_set_upstream(self, branch: Branch) -> None:
        self.run_worker(self._set_upstream_flow(branch), exclusive=True, group="action")

    async def _set_upstream_flow(self, branch: Branch) -> None:
        name = branch.short_name if branch.is_remote else branch.name
        upstream = f"{await self._remote_for(branch)}/{name}"
        self.confirm(
            f"Set upstream of '{name}' to '{upstream}'?",
            lambda: self.set_upstream(branch),
        )

    @work(exclusive=True, group="action")
    async def set_upstream(self, branch: Branch) -> None:
        name = branch.short_name if branch.is_remote else branch.name
        upstream = f"{await self._remote_for(branch)}/{name}"
        await self.mutate(
            "set upstream",
            "branch",
            "--set-upstream-to",
            upstream,
            name,
            success=f"Upstream set to {upstream}",
        )
