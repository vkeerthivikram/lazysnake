"""Search and filtering: the grep flow (screen, jump, land) and the
files/commits substring filters.

``SearchActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``run_worker``, ``notify``, ``refresh_state``,
``snapshot``, ``files_panel``, ``commits_panel``, ``diff_view``,
``action_focus_panel``, ``files_filter``/``commits_filter`` and ``graph``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

from lazysnake.git.runner import GitError
from lazysnake.ui.modals import MAX_GREP_MATCHES, GrepMatch, GrepScreen, InputScreen
from lazysnake.ui.panels import DiffRow

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from lazysnake.git.log import GraphCommit
    from lazysnake.git.models import RepoSnapshot
    from lazysnake.git.runner import Git
    from lazysnake.ui.panels import CommitsPanel, DiffView, FilesPanel

MAX_GREP_LINE_BYTES = 4096
MAX_GREP_DIAGNOSTIC_CHARS = 4096


class SearchActions:
    """grep mode plus the panel filters."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        git: Git
        snapshot: RepoSnapshot
        graph: list[GraphCommit]
        files_filter: str | None
        commits_filter: str | None
        files_panel: FilesPanel
        commits_panel: CommitsPanel
        diff_view: DiffView
        notify: Callable[..., Any]
        push_screen: Callable[..., Any]
        run_worker: Callable[..., Any]
        toggle_stage: Callable[..., Any]
        action_focus_panel: Callable[..., Any]
        refresh_state: Callable[..., Any]
        _refreshed: Callable[..., Any]

    # filters -----------------------------------------------------------

    def request_filter(self, source: str) -> None:
        current = self.files_filter if source == "files" else self.commits_filter
        self.push_screen(
            InputScreen(f"Filter {source} by substring (empty clears):", prefill=current or ""),
            lambda text: self.set_filter(source, text),
        )

    def set_filter(self, source: str, text: str | None) -> None:
        needle = (text or "").strip() or None
        if source == "files":
            self.files_filter = needle
            self.files_panel.set_files(self.snapshot.files, filter_text=needle)
        else:
            self.commits_filter = needle
            self.commits_panel.set_commits(self.graph, filter_text=needle)

    # grep ------------------------------------------------------------------

    def action_grep(self) -> None:
        self.push_screen(
            InputScreen("grep tracked files (fixed substring):"),
            lambda term: self.run_worker(self._grep_flow(term), group="action") if term else None,
        )

    async def _grep_flow(self, term: str) -> None:
        # git grep exits 1 for "no matches" — only that is a clean
        # empty result. Any other non-zero code is a real failure and
        # must not be reported as "no matches". run_streaming can also
        # *raise* GitError (e.g. the runner's timeout) — same treatment.
        matches: list[tuple[str, int, str]] = []
        diagnostics: deque[str] = deque(maxlen=8)
        truncated = False

        def collect_line(raw: str, is_stderr: bool) -> None:
            nonlocal truncated
            path, separator, remainder = raw.partition("\0")
            line_number, separator2, text = remainder.partition("\0")
            if is_stderr or not separator or not separator2 or not line_number.isdigit():
                diagnostics.append(raw)
            elif len(matches) < MAX_GREP_MATCHES:
                matches.append((path, int(line_number), text))
            else:
                truncated = True

        try:
            code, _ = await self.git.run_streaming(
                "grep",
                "-n",
                "-z",
                "-I",
                "--fixed-strings",
                "--",
                term,
                on_line=collect_line,
                capture_output=False,
                max_line_bytes=MAX_GREP_LINE_BYTES,
            )
        except GitError as err:
            self.notify(f"grep failed: {err}", severity="error", timeout=10)
            return
        if code == 1:
            self.notify(f"No matches for '{term}'")
            return
        if code != 0:
            detail = "\n".join(diagnostics)[-MAX_GREP_DIAGNOSTIC_CHARS:]
            self.notify(
                f"grep failed: {detail or f'exit code {code}'}",
                severity="error",
                timeout=10,
            )
            return
        if not matches:
            self.notify(f"No matches for '{term}'")
            return
        self.push_screen(
            GrepScreen(matches, term, truncated=truncated),
            lambda sel: self._jump_to_match(sel) if sel else None,
        )

    def _jump_to_match(self, sel: GrepMatch) -> None:
        # Own group: jump awaits toggle_stage/refresh workers that live in
        # other exclusive groups; sharing "action" would get it cancelled.
        self.run_worker(self._jump_worker(sel), group="jump")

    async def _jump_worker(self, sel: GrepMatch) -> None:
        if sel.stage:
            entry = next((f for f in self.snapshot.files if f.path == sel.path), None)
            if entry is not None:
                await self.toggle_stage(entry, staged_view=False).wait()
        # Focus the Files panel on just this file; `/` with empty text clears.
        self.files_filter = sel.path
        await self._refreshed()
        self.action_focus_panel("files")
        await self._land_cursor(sel.path, sel.line)

    async def _land_cursor(self, path: str, line_no: int) -> None:
        """Move the diff cursor to the row holding a matched line, once the
        diff for ``path`` is on screen (matches on unchanged lines keep the
        cursor at the top — there is nothing to highlight in the diff)."""
        for _ in range(40):
            await asyncio.sleep(0.05)
            dv = self.diff_view
            if dv.display and dv.current_fd is not None and dv.current_fd.path == path:
                break
        else:
            return
        dv = self.diff_view
        fd = dv.current_fd
        if fd is None:
            return
        for i, child in enumerate(dv.children):
            if (
                isinstance(child, DiffRow)
                and child.line_index is not None
                and fd.hunks[child.hunk_index].lines[child.line_index].new_no == line_no
            ):
                dv.index = i
                return
