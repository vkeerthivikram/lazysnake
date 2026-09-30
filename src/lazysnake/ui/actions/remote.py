"""Remote-synchronization actions: fetch, pull, push (streamed, with the
command log fed live).

``RemoteActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``confirm``, ``_checkpoint``, ``notify``,
``refresh_state``, ``snapshot``, ``git``, ``command_log`` and
``_last_status_raw``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work

from lazysnake.git.runner import GitError

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from lazysnake.git.models import RepoSnapshot
    from lazysnake.git.runner import Git
    from lazysnake.ui.panels import CommandLog


class RemoteActions:
    """Fetch/pull/push and the streaming runner they share."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        git: Git
        snapshot: RepoSnapshot
        command_log: CommandLog
        _last_status_raw: str | None
        _git_lock: Any
        notify: Callable[..., Any]
        confirm: Callable[..., Any]
        run_worker: Callable[..., Any]
        _checkpoint: Callable[..., Any]
        _remote_for: Callable[..., Any]
        refresh_state: Callable[..., Any]
        _refreshed: Callable[..., Any]

    async def run_git_stream(self, *args: str) -> int:
        """Run git with live output streaming into the command log.

        Snapshots state first (like run_git), streams each output line as
        it arrives, and returns the exit code without raising. Checkpoint
        and stream both touch the index, so both run under the app's git
        lock (the poll timer's status refresh must not collide).
        """
        display = "git " + " ".join(args)
        try:
            async with self._git_lock:
                await self._checkpoint()
                self.command_log.write_output(f"▸ {display}")
                code, _combined = await self.git.run_streaming(
                    *args,
                    on_line=lambda text, is_err: self.command_log.write_output(
                        text, is_stderr=is_err
                    ),
                )
        except GitError as err:
            self.command_log.write_command(display, ok=False, detail=str(err)[:500])
            return err.returncode or 1
        if code != 0:
            self.command_log.write_output(f"✗ {display}: exit code {code}", is_stderr=True)
        else:
            self.command_log.write_command(display, ok=True)
        return code

    @work(exclusive=True, group="action")
    async def fetch(self) -> int:
        code = await self.run_git_stream("fetch", "--all", "--progress")
        if code != 0:
            self.notify("Fetch failed — see command log", severity="error", timeout=10)
            self._last_status_raw = None
            await self._refreshed()
            return code
        self.notify("Fetched")
        self._last_status_raw = None
        await self._refreshed()
        return 0

    @work(exclusive=True, group="action")
    async def pull(self) -> int:
        if not self.snapshot.upstream:
            self.notify("No upstream configured for this branch", severity="warning")
            return 1
        code = await self.run_git_stream("pull")
        if code != 0:
            self.notify("Pull failed — see command log", severity="error", timeout=10)
            self._last_status_raw = None
            await self._refreshed()
            return code
        self.notify("Pulled")
        self._last_status_raw = None
        await self._refreshed()
        return 0

    def action_fetch(self) -> None:
        self.fetch()

    def action_pull(self) -> None:
        self.pull()

    def action_push(self) -> None:
        if self.snapshot.upstream:
            self.push()
            return
        self.run_worker(self._push_setup_flow(), group="action")

    async def _push_setup_flow(self) -> None:
        branch = self.snapshot.branch
        remote = await self._remote_for()
        self.confirm(
            f"No upstream for '{branch}'.\nPush and set upstream to '{remote}/{branch}'?",
            lambda: self.push(set_upstream=True, remote=remote),
        )

    @work(exclusive=True, group="action")
    async def push(self, *, set_upstream: bool = False, remote: str | None = None) -> int:
        args = ["push", "--progress"]
        if set_upstream:
            if remote is None:
                remote = await self._remote_for()
            args.extend(["--set-upstream", remote, self.snapshot.branch])
        code = await self.run_git_stream(*args)
        if code != 0:
            self.notify("Push failed — see command log", severity="error", timeout=10)
            self._last_status_raw = None
            await self._refreshed()
            return code
        self.notify("Pushed")
        self._last_status_raw = None
        await self._refreshed()
        return 0
