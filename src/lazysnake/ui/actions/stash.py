"""Stash-panel actions: push, apply, pop, drop.

``StashActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``mutate``/``confirm``, ``notify``,
``refresh_state``, ``snapshot`` and the stash panel.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work

from lazysnake.git.stash import StashEntry
from lazysnake.ui.modals import InputScreen

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from lazysnake.git.models import RepoSnapshot


class StashActions:
    """Everything the Stash panel triggers."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        snapshot: RepoSnapshot
        notify: Callable[..., Any]
        push_screen: Callable[..., Any]
        mutate: Callable[..., Any]
        confirm: Callable[..., Any]

    @work(exclusive=True, group="action")
    async def stash_push(self, message: str | None = None) -> None:
        args = ["stash", "push"]
        if self.snapshot.untracked_files:
            args.append("-u")
        if message:
            args.extend(["-m", message])
        await self.mutate("stash", *args, success="Stashed")

    @work(exclusive=True, group="action")
    async def apply_stash(self, entry: StashEntry) -> None:
        await self.mutate(
            "stash apply", "stash", "apply", entry.name, success=f"Applied {entry.name}"
        )

    @work(exclusive=True, group="action")
    async def pop_stash(self, entry: StashEntry) -> None:
        await self.mutate("stash pop", "stash", "pop", entry.name, success=f"Popped {entry.name}")

    def request_drop_stash(self, entry: StashEntry) -> None:
        self.confirm(f"Drop {entry.name}?", lambda: self.drop_stash(entry))

    @work(exclusive=True, group="action")
    async def drop_stash(self, entry: StashEntry) -> None:
        await self.mutate(
            "stash drop", "stash", "drop", entry.name, success=f"Dropped {entry.name}"
        )

    def request_stash_with_message(self) -> None:
        self.push_screen(
            InputScreen("Stash message:"),
            lambda msg: self.stash_push(message=msg) if msg else None,
        )
