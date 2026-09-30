"""Main-view rendering: the right-hand pane follows the focused panel.

``MainViewMixin`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It reads ``main_source``, the five panels, ``diff_view``/``main_view``,
``diff_context`` and ``git`` from the app.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from textual import work
from textual.app import ScreenStackError
from textual.widgets import ListView

from lazysnake.git.diff import parse_diff
from lazysnake.git.log import FORMAT as LOG_FORMAT
from lazysnake.git.log import parse_log
from lazysnake.git.runner import GitError
from lazysnake.ui.diff_render import (
    MAX_RENDER_LINES,
    render_branch_view,
    render_commit,
    render_file_diffs,
    render_message,
    render_stash_view,
    render_tag_view,
    render_untracked,
)
from lazysnake.ui.panels import MAX_DIFF_ROWS

MAX_DIFF_OUTPUT_BYTES = 8 * 1024 * 1024

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from textual.widget import Widget

    from lazysnake.git.runner import Git
    from lazysnake.ui.panels import (
        BranchesPanel,
        CommitsPanel,
        DiffView,
        FilesPanel,
        MainView,
        StashPanel,
        TagsPanel,
    )


class MainViewMixin:
    """Renders the main pane for the current panel selection."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        git: Git
        main_source: str
        diff_context: int
        focused: Widget | None
        diff_view: DiffView
        main_view: MainView
        files_panel: FilesPanel
        branches_panel: BranchesPanel
        commits_panel: CommitsPanel
        stash_panel: StashPanel
        tags_panel: TagsPanel
        notify: Callable[..., Any]

    @work(exclusive=True, group="diff")
    async def refresh_main(self) -> None:
        """Render the main view for the current panel selection."""
        use_diff_view = self.main_source == "files"
        self.diff_view.display = use_diff_view
        self.main_view.display = not use_diff_view
        handler = {
            "files": self._main_files,
            "branches": self._main_branches,
            "commits": self._main_commits,
            "stash": self._main_stash,
            "tags": self._main_tags,
        }[self.main_source]
        await handler()

    async def _main_files(self) -> None:
        item = self.files_panel.selected_item
        if item is None:
            self.main_view.display = True
            self.diff_view.display = False
            self.main_view.show(render_message("Nothing selected — stage something."))
            return
        entry, staged_view = item.entry, item.staged_view
        context_flag = f"-U{self.diff_context}"
        try:
            if entry.unmerged:
                raw, was_truncated = await self.git.run(
                    "diff",
                    "--no-color",
                    context_flag,
                    "--",
                    entry.path,
                    max_output_bytes=MAX_DIFF_OUTPUT_BYTES,
                )
                self.diff_view.display = False
                self.main_view.display = True
                self.main_view.show(
                    render_file_diffs(
                        parse_diff(
                            raw,
                            max_rows=MAX_RENDER_LINES,
                            max_files=MAX_RENDER_LINES,
                            truncated=was_truncated,
                        ),
                        note="conflicted file (resolution UI lands in phase 5)",
                    )
                )
            elif entry.untracked:
                try:
                    content, total_lines = await asyncio.to_thread(
                        _read_untracked_preview, self.git.repo_root / entry.path
                    )
                except OSError:
                    content, total_lines = ["(unreadable or binary)"], 1
                self.diff_view.display = False
                self.main_view.display = True
                self.main_view.show(render_untracked(entry.path, content, total_lines=total_lines))
            else:
                if staged_view:
                    raw, was_truncated = await self.git.run(
                        "diff",
                        "--no-color",
                        "--cached",
                        context_flag,
                        "--",
                        entry.path,
                        max_output_bytes=MAX_DIFF_OUTPUT_BYTES,
                    )
                    note = "staged changes — space unstages"
                else:
                    raw, was_truncated = await self.git.run(
                        "diff",
                        "--no-color",
                        context_flag,
                        "--",
                        entry.path,
                        max_output_bytes=MAX_DIFF_OUTPUT_BYTES,
                    )
                    note = None
                files = parse_diff(
                    raw,
                    max_rows=MAX_DIFF_ROWS,
                    max_files=1,
                    truncated=was_truncated,
                )
                if files and files[0].hunks:
                    self.main_view.display = False
                    self.diff_view.display = True
                    self.diff_view.set_diff(files[0], reverse=staged_view, note=note)
                else:
                    self.diff_view.display = False
                    self.main_view.display = True
                    self.main_view.show(
                        render_file_diffs(files, note="no hunks (rename/mode/binary change)")
                    )
        except GitError as err:
            self.main_view.show(render_message(f"diff failed: {err}"))

    async def _main_branches(self) -> None:
        item = self.branches_panel.selected_item
        if item is None:
            self.main_view.show(render_message("No branch selected."))
            return
        branch = item.branch
        try:
            data = await self.git.run(
                "log", f"--pretty=format:{LOG_FORMAT}", "-n", "30", branch.name
            )
            self.main_view.show(render_branch_view(branch, parse_log(data)))
        except GitError as err:
            self.main_view.show(render_message(f"log failed: {err}"))

    async def _main_commits(self) -> None:
        item = self.commits_panel.selected_item
        if item is None:
            self.main_view.show(render_message("No commit selected."))
            return
        commit = item.commit
        try:
            raw, was_truncated = await self.git.run(
                "show",
                "--no-color",
                "--pretty=format:",
                commit.sha,
                max_output_bytes=MAX_DIFF_OUTPUT_BYTES,
            )
            self.main_view.show(
                render_commit(
                    commit,
                    parse_diff(
                        raw,
                        max_rows=MAX_RENDER_LINES,
                        max_files=MAX_RENDER_LINES,
                        truncated=was_truncated,
                    ),
                )
            )
        except GitError as err:
            self.main_view.show(render_message(f"show failed: {err}"))

    async def _main_stash(self) -> None:
        item = self.stash_panel.selected_item
        if item is None:
            self.main_view.show(render_message("Stash is empty."))
            return
        entry = item.entry
        try:
            raw, was_truncated = await self.git.run(
                "stash",
                "show",
                "--no-color",
                "-p",
                entry.name,
                max_output_bytes=MAX_DIFF_OUTPUT_BYTES,
            )
            self.main_view.show(
                render_stash_view(
                    entry,
                    parse_diff(
                        raw,
                        max_rows=MAX_RENDER_LINES,
                        max_files=MAX_RENDER_LINES,
                        truncated=was_truncated,
                    ),
                )
            )
        except GitError as err:
            self.main_view.show(render_message(f"stash show failed: {err}"))

    async def _main_tags(self) -> None:
        item = self.tags_panel.selected_item
        if item is None:
            self.main_view.show(render_message("No tags."))
            return
        tag = item.tag
        try:
            data = await self.git.run("log", f"--pretty=format:{LOG_FORMAT}", "-n", "30", tag.sha)
            self.main_view.show(render_tag_view(tag, parse_log(data)))
        except GitError as err:
            self.main_view.show(render_message(f"log failed: {err}"))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # The main view follows the *focused* panel; background repopulation
        # (e.g. refreshing the commit log) must not steal it.
        try:
            focused = self.focused
        except ScreenStackError:
            # Teardown window: the screen stack is gone but queued
            # highlight events still arrive. Nothing to follow then.
            return
        if event.list_view is not focused or event.item is None:
            return
        sources = {
            id(self.files_panel): "files",
            id(self.branches_panel): "branches",
            id(self.commits_panel): "commits",
            id(self.stash_panel): "stash",
            id(self.tags_panel): "tags",
        }
        source = sources.get(id(event.list_view))
        if source is not None:
            self.main_source = source
            self.refresh_main()

    def action_context_down(self) -> None:
        self.diff_context = max(1, self.diff_context - 1)
        self.notify(f"Diff context: {self.diff_context}")
        self.refresh_main()

    def action_context_up(self) -> None:
        self.diff_context = min(9, self.diff_context + 1)
        self.notify(f"Diff context: {self.diff_context}")
        self.refresh_main()


MAX_UNTRACKED_LINE_CHARS = 4096


def _read_untracked_preview(path: Path) -> tuple[list[str], int]:
    """Retain bounded preview data while scanning for the exact file line count."""
    lines: list[str] = []
    total = 0
    with path.open("r", errors="replace") as stream:
        while first := stream.readline(MAX_UNTRACKED_LINE_CHARS + 1):
            total += 1
            overlong = len(first) > MAX_UNTRACKED_LINE_CHARS and not first.endswith("\n")
            ended = first.endswith("\n")
            while not ended:
                chunk = stream.readline(MAX_UNTRACKED_LINE_CHARS + 1)
                if not chunk:
                    break
                overlong = True
                ended = chunk.endswith("\n")
            if len(lines) < MAX_RENDER_LINES:
                line = first[:MAX_UNTRACKED_LINE_CHARS] if overlong else first.rstrip("\r\n")
                if overlong:
                    line += " ... [line truncated]"
                lines.append(line)
    return lines, total
