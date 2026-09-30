"""Tags-panel actions: create, delete, checkout.

``TagActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``mutate``/``confirm`` and ``refresh_state``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work

from lazysnake.git.tags import Tag
from lazysnake.ui.modals import InputScreen

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


class TagActions:
    """Everything the Tags panel triggers."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        push_screen: Callable[..., Any]
        mutate: Callable[..., Any]
        confirm: Callable[..., Any]
        checkout_detached: Callable[..., Any]

    def request_create_tag(self) -> None:
        self.push_screen(
            InputScreen("New tag name:", placeholder="v1.0.0"),
            lambda name: self.create_tag(name) if name else None,
        )

    @work(exclusive=True, group="action")
    async def create_tag(self, name: str) -> None:
        await self.mutate("create tag", "tag", name, success=f"Tagged {name}")

    def request_delete_tag(self, tag: Tag) -> None:
        self.confirm(f"Delete tag '{tag.name}'?", lambda: self.delete_tag(tag))

    @work(exclusive=True, group="action")
    async def delete_tag(self, tag: Tag) -> None:
        await self.mutate("delete tag", "tag", "-d", tag.name, success=f"Deleted tag {tag.name}")

    def request_checkout_tag(self, tag: Tag) -> None:
        self.confirm(
            f"Checkout tag '{tag.name}' (detached HEAD)?",
            lambda: self.checkout_detached(tag.sha, label=tag.name),
        )
