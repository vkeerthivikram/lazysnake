"""The lazysnake Textual application."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from typing import ClassVar

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, ListView

from lazysnake.config import Config, CustomCommand, load_config
from lazysnake.git.branch import FORMAT as BRANCH_FORMAT
from lazysnake.git.branch import Branch, parse_branches
from lazysnake.git.diff import LineKind, parse_diff
from lazysnake.git.log import FORMAT as LOG_FORMAT
from lazysnake.git.log import GRAPH_FORMAT, Commit, parse_log, parse_log_graph
from lazysnake.git.merge import bisect_in_progress, union_resolve
from lazysnake.git.models import FileEntry, RepoSnapshot
from lazysnake.git.runner import Git, GitError
from lazysnake.git.snapshot import Snapshot, capture, restore
from lazysnake.git.stash import FORMAT as STASH_FORMAT
from lazysnake.git.stash import StashEntry, parse_stash
from lazysnake.git.status import merge_in_progress, parse_status
from lazysnake.git.submodule import submodule_paths, submodules
from lazysnake.git.tags import FORMAT as TAG_FORMAT
from lazysnake.git.tags import Tag, parse_tags
from lazysnake.git.worktree import parse_worktrees
from lazysnake.keymap import Keymap, bind_keys
from lazysnake.rebase import TodoEntry, TodoPlan, rebase_with_ops
from lazysnake.recent import load_recent, record_recent
from lazysnake.staging import hunk_patch, line_patch
from lazysnake.ui.diff_render import (
    render_branch_view,
    render_commit,
    render_file_diffs,
    render_message,
    render_stash_view,
    render_tag_view,
    render_untracked,
)
from lazysnake.ui.modals import (
    CommitScreen,
    ConfirmScreen,
    GrepScreen,
    InputScreen,
    RebaseTodoScreen,
    RecentReposScreen,
    SubmodulesScreen,
    WorktreeScreen,
)
from lazysnake.ui.panels import (
    DEFAULT_KEYMAP,
    BranchesPanel,
    CommandLog,
    CommitsPanel,
    DiffRow,
    DiffView,
    FilesPanel,
    MainView,
    StashPanel,
    StatusBar,
    TagsPanel,
)

POLL_SECONDS = 2.0
_MODAL_SCREENS = (ConfirmScreen, CommitScreen, InputScreen, RebaseTodoScreen)


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


class LazysnakeApp(App):
    """lazygit-style git client."""

    CSS_PATH = "theme.tcss"
    TITLE = "lazysnake"

    ACTIONS: ClassVar[list[tuple[str, str, str]]] = [
        ("quit", "Quit", "quit"),
        ("refresh", "Refresh", "refresh"),
        ("focus_files", "Files", "focus_panel('files')"),
        ("focus_branches", "Branches", "focus_panel('branches')"),
        ("focus_commits", "Commits", "focus_panel('commits')"),
        ("focus_stash", "Stash", "focus_panel('stash')"),
        ("focus_tags", "Tags", "focus_panel('tags')"),
        ("focus_diff", "Diff", "focus_diff"),
        ("fetch", "fetch", "fetch"),
        ("pull", "pull", "pull"),
        ("push", "push", "push"),
        ("rebase_continue", "rebase/merge continue", "rebase_continue"),
        ("rebase_abort", "abort rebase", "rebase_abort"),
        ("undo", "undo", "undo"),
        ("redo", "redo", "redo"),
        ("context_down", "less context", "context_down"),
        ("context_up", "more context", "context_up"),
        ("recent_repos", "recent repos", "recent_repos"),
        ("worktrees", "worktrees", "worktrees"),
        ("submodules", "submodules", "submodules"),
        ("grep", "grep", "grep"),
    ]

    def __init__(
        self,
        git: Git,
        config: Config | None = None,
        keymap: Keymap | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.git = git
        self.config = config or load_config()
        self.keymap = keymap or DEFAULT_KEYMAP
        for warning in self.keymap.warnings:
            self.notify(f"keymap: {warning}", severity="warning", timeout=10)
        self.snapshot = RepoSnapshot()
        self.branches: list[Branch] = []
        self.commits: list[Commit] = []
        self.graph: list = []  # list[GraphCommit]
        self.stash: list[StashEntry] = []
        self.tags: list[Tag] = []
        self.diff_context = 3
        self.files_filter: str | None = None
        self.commits_filter: str | None = None
        self.submodules: list[str] = []
        self.patch_anchor: Commit | None = None
        self.patch_range: list[Commit] = []
        self._undo_stack: list[Snapshot] = []
        self._redo_stack: list[Snapshot] = []
        self._last_snapshot_commit: str | None = None
        self._last_status_raw: str | None = None
        self.main_source = "files"
        self._editor_task: asyncio.Task | None = None

        self.status_bar = StatusBar()
        self.files_panel = bind_keys(FilesPanel, "files", self.keymap)()
        self.branches_panel = bind_keys(BranchesPanel, "branches", self.keymap)()
        self.commits_panel = bind_keys(CommitsPanel, "commits", self.keymap)()
        self.stash_panel = bind_keys(StashPanel, "stash", self.keymap)()
        self.tags_panel = bind_keys(TagsPanel, "tags", self.keymap)()
        self.diff_view = bind_keys(DiffView, "diff", self.keymap)()
        self.main_view = MainView()
        self.command_log = CommandLog()

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield self.status_bar
                yield self.files_panel
                yield self.branches_panel
                yield self.commits_panel
                yield self.stash_panel
                yield self.tags_panel
            with Vertical(id="main"):
                yield self.diff_view
                yield self.main_view
                yield self.command_log
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#sidebar").styles.width = self.config.sidebar_width
        self.files_panel.focus()
        self.set_interval(self.config.poll_seconds, self._poll_status)
        self.refresh_state()

    # ----------------------------------------------------------------- refresh

    @work(exclusive=True, group="refresh")
    async def refresh_state(self, force: bool = False) -> None:
        """Re-read repository state and repopulate every panel.

        ``force`` bypasses the status-unchanged fast path (needed when
        branches, the log, or the stash changed while status did not).
        """
        try:
            data = await self.git.run("status", "--porcelain=v2", "--branch", "-z")
        except GitError as err:
            self.notify(f"git status failed: {err}", severity="error", timeout=10)
            return
        if not force and data == self._last_status_raw:
            return
        self._last_status_raw = data
        self.snapshot = parse_status(data)
        # Porcelain v2 has no merge/bisect headers; on-disk state is the truth.
        self.snapshot.merging = merge_in_progress(self.git.repo_root)
        self.snapshot.bisecting = bisect_in_progress(self.git.repo_root)
        self.snapshot.cherry_picking = (
            self.git.repo_root / ".git" / "CHERRY_PICK_HEAD"
        ).exists()
        self.status_bar.update_snapshot(self.snapshot)
        self.files_panel.set_files(self.snapshot.files, filter_text=self.files_filter)
        self._sync_commits_title()

        # Secondary panels; each failure is non-fatal.
        for feeder in (
            self._refresh_branches,
            self._refresh_commits,
            self._refresh_stash,
            self._refresh_tags,
            self._refresh_submodules,
        ):
            try:
                await feeder()
            except GitError as err:
                self.notify(f"{err}", severity="error", timeout=5)
        self.refresh_main()

    async def _refresh_submodules(self) -> None:
        self.submodules = await submodule_paths(self.git)

    async def _refresh_branches(self) -> None:
        local = parse_branches(
            await self.git.run("for-each-ref", "refs/heads", f"--format={BRANCH_FORMAT}")
        )
        remote = parse_branches(
            await self.git.run("for-each-ref", "refs/remotes", f"--format={BRANCH_FORMAT}"),
            remote=True,
        )
        self.branches = local
        self.branches_panel.set_branches(local, remote)

    async def _refresh_commits(self) -> None:
        data = await self.git.run(
            "log", "--graph", f"--pretty=format:{GRAPH_FORMAT}", "-n", str(self.config.log_limit)
        )
        self.graph = parse_log_graph(data)
        self.commits = [g.commit for g in self.graph]
        self.commits_panel.set_commits(self.graph, filter_text=self.commits_filter)

    async def _refresh_stash(self) -> None:
        data = await self.git.run("stash", "list", f"--format={STASH_FORMAT}")
        self.stash = parse_stash(data)
        self.stash_panel.set_stash(self.stash)

    async def _refresh_tags(self) -> None:
        data = await self.git.run("for-each-ref", "refs/tags", f"--format={TAG_FORMAT}")
        self.tags = parse_tags(data)
        self.tags_panel.set_tags(self.tags)

    def _poll_status(self) -> None:
        if not isinstance(self.screen, _MODAL_SCREENS):
            self.refresh_state()

    # -------------------------------------------------------------- main view

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
                raw = await self.git.run("diff", "--no-color", context_flag, "--", entry.path)
                self.diff_view.display = False
                self.main_view.display = True
                self.main_view.show(
                    render_file_diffs(
                        parse_diff(raw), note="conflicted file (resolution UI lands in phase 5)"
                    )
                )
            elif entry.untracked:
                try:
                    content = (self.git.repo_root / entry.path).read_text(errors="replace")
                except OSError:
                    content = "(unreadable or binary)"
                self.diff_view.display = False
                self.main_view.display = True
                self.main_view.show(render_untracked(entry.path, content))
            else:
                if staged_view:
                    raw = await self.git.run(
                        "diff", "--no-color", "--cached", context_flag, "--", entry.path
                    )
                    note = "staged changes — space unstages"
                else:
                    raw = await self.git.run(
                        "diff", "--no-color", context_flag, "--", entry.path
                    )
                    note = None
                files = parse_diff(raw)
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
            raw = await self.git.run(
                "show", "--no-color", "--pretty=format:", commit.sha
            )
            self.main_view.show(render_commit(commit, parse_diff(raw)))
        except GitError as err:
            self.main_view.show(render_message(f"show failed: {err}"))

    async def _main_stash(self) -> None:
        item = self.stash_panel.selected_item
        if item is None:
            self.main_view.show(render_message("Stash is empty."))
            return
        entry = item.entry
        try:
            raw = await self.git.run("stash", "show", "--no-color", "-p", entry.name)
            self.main_view.show(render_stash_view(entry, parse_diff(raw)))
        except GitError as err:
            self.main_view.show(render_message(f"stash show failed: {err}"))

    async def _main_tags(self) -> None:
        item = self.tags_panel.selected_item
        if item is None:
            self.main_view.show(render_message("No tags."))
            return
        tag = item.tag
        try:
            data = await self.git.run(
                "log", f"--pretty=format:{LOG_FORMAT}", "-n", "30", tag.sha
            )
            self.main_view.show(render_tag_view(tag, parse_log(data)))
        except GitError as err:
            self.main_view.show(render_message(f"log failed: {err}"))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        # The main view follows the *focused* panel; background repopulation
        # (e.g. refreshing the commit log) must not steal it.
        if event.list_view is not self.focused or event.item is None:
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

    # ------------------------------------------------------------ logged git

    async def run_git(self, *args: str, **kwargs) -> str:
        """Run git, log the command, and surface errors as toasts.

        Snapshots full state before the command so ``z`` can undo the
        actions lazysnake itself performs (reads dedupe to nothing).
        """
        await self._checkpoint()
        display = "git " + " ".join(args)
        try:
            out = await self.git.run(*args, **kwargs)
        except GitError as err:
            self.command_log.write_command(display, ok=False, detail=err.stderr.strip()[:300])
            raise
        self.command_log.write_command(display, ok=True)
        return out

    async def _checkpoint(self) -> None:
        """Snapshot full repo state before a mutating action; clears redo."""
        snap = await capture(self.git, self._last_snapshot_commit)
        if snap is None:
            return
        self._last_snapshot_commit = snap.worktree_commit
        if not self._undo_stack or self._undo_stack[-1] != snap:
            self._undo_stack.append(snap)
            if len(self._undo_stack) > 50:
                self._undo_stack.pop(0)
            self._redo_stack.clear()

    def _notify_error(self, action: str, err: GitError) -> None:
        self.notify(f"{action} failed: {err}", severity="error", timeout=10)

    # ---------------------------------------------------------------- actions

    def action_refresh(self) -> None:
        self._last_status_raw = None
        self.refresh_state()

    def action_focus_panel(self, panel: str) -> None:
        widget = {
            "files": self.files_panel,
            "branches": self.branches_panel,
            "commits": self.commits_panel,
            "stash": self.stash_panel,
            "tags": self.tags_panel,
        }[panel]
        widget.focus()
        self.main_source = panel
        self.refresh_main()

    def action_focus_diff(self) -> None:
        if self.diff_view.display:
            self.diff_view.focus()

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
            args = ["apply", "--cached"]
            if dv.reverse:
                args.append("--reverse")
            await self.run_git(*args, input=patch)
        except (GitError, ValueError) as err:
            self._notify_error("partial stage", err)
            return
        self.notify("Staged" if not dv.reverse else "Unstaged")
        await self.refresh_state(force=True).wait()

    # staging -------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def toggle_stage(self, entry: FileEntry, *, staged_view: bool) -> None:
        if entry.unmerged:
            self.notify("Resolve the conflict first (phase 5)", severity="warning")
            return
        try:
            if staged_view:
                await self.run_git("restore", "--staged", "--", entry.path)
            else:
                await self.run_git("add", "--", entry.path)
        except GitError as err:
            self._notify_error("stage", err)
            return
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def toggle_stage_all(self) -> None:
        has_unstaged = bool(self.snapshot.unstaged_files)
        try:
            if has_unstaged:
                await self.run_git("add", "-A")
            else:
                await self.run_git("restore", "--staged", ":/")
        except GitError as err:
            self._notify_error("stage all", err)
            return
        await self.refresh_state(force=True).wait()

    # discard ---------------------------------------------------------------

    def request_discard(self, entry: FileEntry) -> None:
        if entry.unmerged:
            self.notify("Resolve the conflict first (phase 5)", severity="warning")
            return
        if entry.untracked:
            prompt = f"Delete untracked file '{entry.path}'?"
        else:
            prompt = f"Discard ALL changes in '{entry.path}'?\nThis cannot be undone."
        self.push_screen(
            ConfirmScreen(prompt, danger=True),
            lambda ok: self.discard(entry) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def discard(self, entry: FileEntry) -> None:
        try:
            if entry.untracked:
                await self.run_git("clean", "-f", "--", entry.path)
            else:
                await self.run_git(
                    "restore", "--source=HEAD", "--staged", "--worktree", "--", entry.path
                )
        except GitError as err:
            self._notify_error("discard", err)
            return
        self.notify(f"Discarded changes in {entry.path}")
        await self.refresh_state(force=True).wait()

    # commit ----------------------------------------------------------------

    def request_commit(self) -> None:
        if not self.snapshot.staged_files:
            self.notify("Nothing staged — stage files with space first", severity="warning")
            return
        self.push_screen(CommitScreen(), lambda result: self.commit(result) if result else None)

    def request_amend(self) -> None:
        if self.snapshot.oid is None:
            self.notify("No commits yet — nothing to amend", severity="warning")
            return
        self.run_worker(self._amend_flow(), exclusive=True, group="action")

    async def _amend_flow(self) -> None:
        try:
            last_message = (await self.git.run("log", "-1", "--pretty=%B")).strip()
        except GitError:
            last_message = ""
        self.push_screen(
            CommitScreen(prefill=last_message),
            lambda result: self.commit(result, amend=True) if result else None,
        )

    @work(exclusive=True, group="action")
    async def commit(self, message: str, *, amend: bool = False) -> None:
        message = message.strip()
        if not message:
            return
        args = ["commit"]
        if amend:
            args.append("--amend")
        args.extend(["-m", message])
        try:
            await self.run_git(*args)
        except GitError as err:
            self._notify_error("commit", err)
            return
        self.notify(("Amended" if amend else "Committed") + f": {message.splitlines()[0]}")
        await self.refresh_state(force=True).wait()

    # branches ---------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def checkout_branch(self, branch: Branch) -> None:
        if branch.is_head:
            self.notify(f"Already on {branch.name}")
            return
        try:
            if branch.is_remote:
                await self.run_git("switch", "--track", branch.name)
            else:
                await self.run_git("switch", branch.name)
        except GitError as err:
            self._notify_error("checkout", err)
            return
        self.notify(f"Switched to {branch.short_name if branch.is_remote else branch.name}")
        await self.refresh_state(force=True).wait()

    def request_create_branch(self) -> None:
        self.push_screen(
            InputScreen("New branch name:", placeholder="feature/…"),
            lambda name: self.create_branch(name) if name else None,
        )

    @work(exclusive=True, group="action")
    async def create_branch(self, name: str) -> None:
        try:
            await self.run_git("switch", "-c", name)
        except GitError as err:
            self._notify_error("create branch", err)
            return
        self.notify(f"Created and switched to {name}")
        await self.refresh_state(force=True).wait()

    def request_delete_branch(self, branch: Branch, *, force: bool = False) -> None:
        if branch.is_remote:
            prompt = (
                f"Delete remote branch '{branch.name}'?\nRuns git push origin --delete."
            )
        elif branch.is_head:
            self.notify("Cannot delete the branch you are on", severity="warning")
            return
        elif force:
            prompt = f"FORCE delete branch '{branch.name}'?\nUnmerged commits become unreachable."
        else:
            prompt = f"Delete branch '{branch.name}'?"
        self.push_screen(
            ConfirmScreen(prompt, danger=True),
            lambda ok: self.delete_branch(branch, force=force) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def delete_branch(self, branch: Branch, *, force: bool = False) -> None:
        try:
            if branch.is_remote:
                await self.run_git("push", "origin", "--delete", branch.short_name)
            else:
                await self.run_git("branch", "-D" if force else "-d", branch.name)
        except GitError as err:
            self._notify_error("delete branch", err)
            return
        self.notify(f"Deleted {branch.name}")
        await self.refresh_state(force=True).wait()

    # stash ------------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def stash_push(self, message: str | None = None) -> None:
        args = ["stash", "push"]
        if self.snapshot.untracked_files:
            args.append("-u")
        if message:
            args.extend(["-m", message])
        try:
            await self.run_git(*args)
        except GitError as err:
            self._notify_error("stash", err)
            return
        self.notify("Stashed")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def apply_stash(self, entry: StashEntry) -> None:
        try:
            await self.run_git("stash", "apply", entry.name)
        except GitError as err:
            self._notify_error("stash apply", err)
            return
        self.notify(f"Applied {entry.name}")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def pop_stash(self, entry: StashEntry) -> None:
        try:
            await self.run_git("stash", "pop", entry.name)
        except GitError as err:
            self._notify_error("stash pop", err)
            return
        self.notify(f"Popped {entry.name}")
        await self.refresh_state(force=True).wait()

    def request_drop_stash(self, entry: StashEntry) -> None:
        self.push_screen(
            ConfirmScreen(f"Drop {entry.name}?", danger=True),
            lambda ok: self.drop_stash(entry) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def drop_stash(self, entry: StashEntry) -> None:
        try:
            await self.run_git("stash", "drop", entry.name)
        except GitError as err:
            self._notify_error("stash drop", err)
            return
        self.notify(f"Dropped {entry.name}")
        await self.refresh_state(force=True).wait()

    # conflicts ----------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def resolve_conflict_union(self, entry: FileEntry) -> None:
        """Resolve a conflicted file by keeping both sides (union merge)."""
        target = self.git.repo_root / entry.path
        try:
            content = target.read_text(errors="replace")
        except OSError as err:
            self.notify(f"cannot read {entry.path}: {err}", severity="error")
            return
        merged, count = union_resolve(content)
        if count == 0:
            self.notify("No conflict markers found in that file", severity="warning")
            return
        target.write_text(merged)
        try:
            await self.run_git("add", "--", entry.path)
        except GitError as err:
            self._notify_error("resolve (union)", err)
            return
        self.notify(f"Union-merged {entry.path} ({count} conflict block(s))")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def resolve_conflict(self, entry: FileEntry, side: str) -> None:
        """Resolve a conflicted file by taking ours/theirs, then stage it."""
        if side not in ("ours", "theirs"):
            raise ValueError(f"unknown conflict side: {side}")
        flag = "--ours" if side == "ours" else "--theirs"
        try:
            await self.run_git("checkout", flag, "--", entry.path)
            await self.run_git("add", "--", entry.path)
        except GitError as err:
            self._notify_error(f"resolve ({side})", err)
            return
        self.notify(f"Resolved {entry.path} with {side}")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def merge_branch(self, branch: Branch) -> None:
        if branch.is_head:
            self.notify("Already on that branch — nothing to merge", severity="warning")
            return
        try:
            await self.run_git("merge", "--no-edit", branch.name)
        except GitError as err:
            self._notify_error("merge", err)
            if self.snapshot.in_conflict:
                self.notify("Conflicts! Resolve with o/t in the Files panel", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        self.notify(f"Merged {branch.name}")
        await self.refresh_state(force=True).wait()

    # history surgery ----------------------------------------------------------

    @work(exclusive=True, group="action")
    async def cherry_pick(self, commit: Commit) -> None:
        try:
            await self.run_git("cherry-pick", commit.sha)
        except GitError as err:
            self._notify_error("cherry-pick", err)
            self.notify("Conflicts? Resolve with o/t/b then press R", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        self.notify(f"Cherry-picked {commit.short_sha}")
        await self.refresh_state(force=True).wait()

    def request_revert(self, commit: Commit) -> None:
        self.push_screen(
            ConfirmScreen(f"Revert '{commit.subject}'?\nThis creates a new commit."),
            lambda ok: self.revert(commit) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def revert(self, commit: Commit) -> None:
        try:
            await self.run_git("revert", "--no-edit", commit.sha)
        except GitError as err:
            self._notify_error("revert", err)
            return
        self.notify(f"Reverted {commit.short_sha}")
        await self.refresh_state(force=True).wait()

    def request_drop_commit(self, commit: Commit) -> None:
        self.push_screen(
            ConfirmScreen(f"Drop commit '{commit.subject}'?\nHistory will be rewritten."),
            lambda ok: self._do_rebase_op(commit, "drop") if ok else None,
        )

    def request_squash_commit(self, commit: Commit) -> None:
        self.push_screen(
            ConfirmScreen(
                f"Squash '{commit.subject}' into its parent?\nHistory will be rewritten."
            ),
            lambda ok: self._do_rebase_op(commit, "squash") if ok else None,
        )

    @work(exclusive=True, group="action")
    async def _do_rebase_op(self, commit: Commit, op: str) -> None:
        try:
            if op in ("squash", "fixup"):
                # The parent must be in the todo as the squash anchor.
                try:
                    await self.git.run("rev-parse", "--verify", "-q", f"{commit.sha}^^")
                    base: str | list[str] = [f"{commit.sha}^^"]
                except GitError:
                    base = ["--root"]
            else:
                base = [f"{commit.sha}^"]
            await rebase_with_ops(self.git, base, {commit.sha: op})
        except GitError as err:
            self._notify_error(f"rebase {op}", err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        self.notify(f"Rebase done ({op} {commit.short_sha})")
        await self.refresh_state(force=True).wait()

    def action_rebase_continue(self) -> None:
        """Smart continue: finishes whichever sequencer is in progress
        (cherry-pick, merge, or rebase) based on on-disk state files."""
        self.run_worker(self._sequencer_continue(), exclusive=True, group="action")

    async def _sequencer_continue(self) -> None:
        git_dir = self.git.repo_root / ".git"
        if (git_dir / "CHERRY_PICK_HEAD").exists():
            command, label = ("cherry-pick", "--continue"), "cherry-pick"
        elif (git_dir / "MERGE_HEAD").exists():
            command, label = ("merge", "--continue"), "merge"
        else:
            command, label = ("rebase", "--continue"), "rebase"
        try:
            await self.run_git(*command)
        except GitError as err:
            # A fully-ours (or fully-theirs) resolution can make the picked
            # commit empty; git refuses to commit it — skip instead, the way
            # lazygit offers.
            if label == "cherry-pick" and "empty" in (err.stderr or ""):
                try:
                    await self.run_git("cherry-pick", "--skip")
                except GitError as skip_err:
                    self._notify_error("cherry-pick continue", skip_err)
                    return
            else:
                self._notify_error(f"{label} continue", err)
                return
        self.notify(f"{label} continued")
        await self.refresh_state(force=True).wait()

    def action_rebase_abort(self) -> None:
        self.push_screen(
            ConfirmScreen("Abort the rebase in progress?", danger=True),
            lambda ok: (
                self.run_worker(self._rebase_finish("abort"), group="action") if ok else None
            ),
        )

    async def _rebase_finish(self, how: str) -> None:
        try:
            await self.run_git("rebase", f"--{how}")
        except GitError as err:
            self._notify_error(f"rebase {how}", err)
            return
        self.notify("Rebase aborted" if how == "abort" else "Rebase continued")
        await self.refresh_state(force=True).wait()

    # branch extras --------------------------------------------------------

    @work(exclusive=True, group="action")
    async def fast_forward_branch(self, branch: Branch) -> None:
        try:
            await self.run_git("merge", "--ff-only", branch.name)
        except GitError as err:
            self._notify_error("fast-forward", err)
            return
        self.notify(f"Fast-forwarded to {branch.name}")
        await self.refresh_state(force=True).wait()

    def request_rebase_onto(self, branch: Branch) -> None:
        self.push_screen(
            ConfirmScreen(f"Rebase current branch onto '{branch.name}'?"),
            lambda ok: self.rebase_onto(branch) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def rebase_onto(self, branch: Branch) -> None:
        try:
            await self.run_git("rebase", branch.name)
        except GitError as err:
            self._notify_error("rebase", err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        self.notify(f"Rebased onto {branch.name}")
        await self.refresh_state(force=True).wait()

    def request_set_upstream(self, branch: Branch) -> None:
        upstream = f"origin/{branch.name}"
        self.push_screen(
            ConfirmScreen(f"Set upstream of '{branch.name}' to '{upstream}'?"),
            lambda ok: self.set_upstream(branch) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def set_upstream(self, branch: Branch) -> None:
        try:
            await self.run_git("branch", "--set-upstream-to", f"origin/{branch.name}", branch.name)
        except GitError as err:
            self._notify_error("set upstream", err)
            return
        self.notify(f"Upstream set to origin/{branch.name}")
        await self.refresh_state(force=True).wait()

    # tags ------------------------------------------------------------------

    def request_create_tag(self) -> None:
        self.push_screen(
            InputScreen("New tag name:", placeholder="v1.0.0"),
            lambda name: self.create_tag(name) if name else None,
        )

    @work(exclusive=True, group="action")
    async def create_tag(self, name: str) -> None:
        try:
            await self.run_git("tag", name)
        except GitError as err:
            self._notify_error("create tag", err)
            return
        self.notify(f"Tagged {name}")
        await self.refresh_state(force=True).wait()

    def request_delete_tag(self, tag: Tag) -> None:
        self.push_screen(
            ConfirmScreen(f"Delete tag '{tag.name}'?", danger=True),
            lambda ok: self.delete_tag(tag) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def delete_tag(self, tag: Tag) -> None:
        try:
            await self.run_git("tag", "-d", tag.name)
        except GitError as err:
            self._notify_error("delete tag", err)
            return
        self.notify(f"Deleted tag {tag.name}")
        await self.refresh_state(force=True).wait()

    def request_checkout_tag(self, tag: Tag) -> None:
        self.push_screen(
            ConfirmScreen(f"Checkout tag '{tag.name}' (detached HEAD)?"),
            lambda ok: self.checkout_detached(tag.sha, label=tag.name) if ok else None,
        )

    # history extras -----------------------------------------------------------

    def request_checkout_commit(self, commit: Commit) -> None:
        self.push_screen(
            ConfirmScreen(f"Checkout '{commit.subject}' (detached HEAD)?"),
            lambda ok: self.checkout_detached(commit.sha, label=commit.short_sha) if ok else None,
        )

    @work(exclusive=True, group="action")
    async def checkout_detached(self, sha: str, *, label: str) -> None:
        try:
            await self.run_git("switch", "--detach", sha)
        except GitError as err:
            self._notify_error("checkout", err)
            return
        self.notify(f"Detached HEAD at {label}")
        await self.refresh_state(force=True).wait()

    def request_reword_commit(self, commit: Commit) -> None:
        self.push_screen(
            CommitScreen(prefill=commit.subject),
            lambda msg: self.reword_commit(commit, msg) if msg else None,
        )

    @work(exclusive=True, group="action")
    async def reword_commit(self, commit: Commit, message: str) -> None:
        message = message.strip()
        if not message:
            return
        base = await self._anchor_base(commit)
        try:
            await rebase_with_ops(
                self.git, base, {commit.sha: "reword"}, message=message
            )
        except GitError as err:
            self._notify_error("reword", err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        self.notify(f"Reworded {commit.short_sha}")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def fixup_commit(self, commit: Commit) -> None:
        try:
            await self.run_git("commit", "--fixup", commit.sha)
        except GitError as err:
            self._notify_error("fixup", err)
            return
        self.notify(f"Fixup commit created for {commit.short_sha}")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def move_commit(self, commit: Commit, direction: str) -> None:
        if direction not in ("move-up", "move-down"):
            raise ValueError(f"unknown move direction: {direction}")
        base = await self._anchor_base(commit)
        try:
            await rebase_with_ops(self.git, base, {commit.sha: direction})
        except GitError as err:
            self._notify_error(direction, err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        self.notify(f"Moved commit {'up' if direction == 'move-up' else 'down'}")
        await self.refresh_state(force=True).wait()

    async def _anchor_base(self, commit: Commit) -> list[str]:
        """Todo base that includes the commit's parent as an anchor."""
        try:
            await self.git.run("rev-parse", "--verify", "-q", f"{commit.sha}^^")
            return [f"{commit.sha}^^"]
        except GitError:
            return ["--root"]

    # files extras --------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def ignore_file(self, entry: FileEntry) -> None:
        gitignore = self.git.repo_root / ".gitignore"
        try:
            existing = gitignore.read_text() if gitignore.exists() else ""
            gitignore.write_text(
                existing + ("" if existing.endswith("\n") or not existing else "\n")
                + entry.path
                + "\n"
            )
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
            proc = await asyncio.create_subprocess_exec(
                *command, str(self.git.repo_root / path)
            )
        except OSError as err:
            self.notify(f"could not start editor: {err}", severity="error")
            return
        self.notify(f"Opened {path} ({' '.join(command)})")
        # Do not block the UI on the editor; keep a handle so the task
        # is not garbage-collected mid-flight.
        self._editor_task = asyncio.create_task(proc.wait())

    def request_stash_with_message(self) -> None:
        self.push_screen(
            InputScreen("Stash message:"),
            lambda msg: self.stash_push(message=msg) if msg else None,
        )

    # global extras ---------------------------------------------------------------

    def action_undo(self) -> None:
        self.push_screen(
            ConfirmScreen(
                "Undo the last lazysnake action?\n"
                "Restores HEAD, index, worktree and stash state exactly —\n"
                "uncommitted changes made since are lost.",
                danger=True,
            ),
            lambda ok: self.undo() if ok else None,
        )

    @work(exclusive=True, group="action")
    async def undo(self) -> None:
        if not self._undo_stack:
            self.notify("Nothing left to undo", severity="warning")
            return
        target = self._undo_stack.pop()
        # Snapshot the present so redo can come back here.
        current = await capture(self.git, self._last_snapshot_commit)
        if current is not None:
            self._last_snapshot_commit = current.worktree_commit
            self._redo_stack.append(current)
        try:
            await restore(self.git, target)
        except GitError as err:
            self._notify_error("undo", err)
            return
        self.notify("Undone — state restored")
        await self.refresh_state(force=True).wait()

    def action_redo(self) -> None:
        if not self._redo_stack:
            self.notify("Nothing to redo", severity="warning")
            return
        self.push_screen(
            ConfirmScreen("Redo the last undone action?"),
            lambda ok: self.redo() if ok else None,
        )

    @work(exclusive=True, group="action")
    async def redo(self) -> None:
        if not self._redo_stack:
            return
        target = self._redo_stack.pop()
        current = await capture(self.git, self._last_snapshot_commit)
        if current is not None:
            self._last_snapshot_commit = current.worktree_commit
            self._undo_stack.append(current)
        try:
            await restore(self.git, target)
        except GitError as err:
            self._notify_error("redo", err)
            return
        self.notify("Redone — state restored")
        await self.refresh_state(force=True).wait()

    # global extras ---------------------------------------------------------------

    # --- filters -----------------------------------------------------------

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

    # --- bisect ------------------------------------------------------------

    @work(exclusive=True, group="action")
    async def bisect_mark(self, commit: Commit, verdict: str) -> None:
        if verdict not in ("good", "bad"):
            raise ValueError(f"unknown bisect verdict: {verdict}")
        if not self.snapshot.bisecting and verdict == "bad":
            args = ["bisect", "start", commit.sha]
        else:
            args = ["bisect", verdict, commit.sha]
        try:
            out = await self.run_git(*args)
        except GitError as err:
            self._notify_error(f"bisect {verdict}", err)
            return
        for line in out.splitlines():
            found_bad = line.startswith(("# first bad commit", f"{verdict} commit"))
            if found_bad or "is the first" in line:
                self.notify(line.strip(), timeout=10)
                break
        else:
            self.notify(f"Marked {commit.short_sha} {verdict}")
        await self.refresh_state(force=True).wait()

    def request_bisect_reset(self) -> None:
        if not self.snapshot.bisecting:
            self.notify("No bisect in progress", severity="warning")
            return
        self.push_screen(
            ConfirmScreen("Reset the bisect in progress?"),
            lambda ok: self.bisect_reset() if ok else None,
        )

    @work(exclusive=True, group="action")
    async def bisect_reset(self) -> None:
        try:
            await self.run_git("bisect", "reset")
        except GitError as err:
            self._notify_error("bisect reset", err)
            return
        self.notify("Bisect reset")
        await self.refresh_state(force=True).wait()

    # --- patch apply ---------------------------------------------------------

    @work(exclusive=True, group="action")
    async def apply_commit_patch(self, commit: Commit) -> None:
        try:
            raw = await self.git.run("show", "--no-color", "--pretty=format:", commit.sha)
            if not raw.strip():
                self.notify("That commit has no textual diff", severity="warning")
                return
            await self.run_git("apply", input=raw)
        except GitError as err:
            self._notify_error("apply patch", err)
            return
        self.notify(f"Applied patch of {commit.short_sha} to the worktree")
        await self.refresh_state(force=True).wait()

    # --- rebase todo editor ------------------------------------------------------

    def request_todo_editor(self, commit: Commit) -> None:
        self.run_worker(self._todo_editor_flow(commit), exclusive=True, group="action")

    async def _todo_editor_flow(self, commit: Commit) -> None:
        base = f"{commit.sha}^"
        try:
            data = await self.git.run(
                "log", "--reverse", f"--pretty=format:{LOG_FORMAT}", f"{base}..HEAD"
            )
        except GitError as err:
            self._notify_error("log for todo", err)
            return
        entries = [
            TodoEntry(sha=c.sha, subject=c.subject, original_pos=i)
            for i, c in enumerate(parse_log(data))
        ]
        if not entries:
            self.notify("No commits to rebase from there", severity="warning")
            return
        plan = TodoPlan(entries=entries)
        self.push_screen(
            RebaseTodoScreen(plan),
            lambda result: self._run_todo(base, result) if result else None,
        )

    def _run_todo(self, base: str, result: dict) -> None:
        self.run_worker(self._run_todo_worker(base, result), exclusive=True, group="action")

    async def _run_todo_worker(self, base: str, result: dict) -> None:
        message = result.get("message")
        ops = {sha: verb for sha, verb in result["ops"].items() if sha}
        try:
            await rebase_with_ops(
                self.git,
                base,
                ops,
                message=message,
                order=result.get("order"),
            )
        except GitError as err:
            self._notify_error("rebase", err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        self.notify("Rebase done")
        await self.refresh_state(force=True).wait()

    # --- patch copy mode ---------------------------------------------------------

    def _sync_commits_title(self) -> None:
        count = len(self.patch_range)
        self.commits_panel.patch_suffix = f"  ⎇ patch({count})" if count else ""

    def patch_mark(self, commit: Commit) -> None:
        """First mark sets the anchor; the second selects the range between."""
        if self.patch_anchor is None:
            self.patch_anchor = commit
            self.patch_range = [commit]
            self.notify(f"Patch anchor {commit.short_sha} — mark the other end next")
        else:
            shas = [g.commit.sha for g in self.graph]
            if self.patch_anchor.sha not in shas or commit.sha not in shas:
                self.notify(
                    "Marked commit left the log — clear (x) and re-mark",
                    severity="warning",
                )
                return
            i1 = shas.index(self.patch_anchor.sha)
            i2 = shas.index(commit.sha)
            lo, hi = sorted((i1, i2))
            self.patch_range = [self.graph[i].commit for i in range(lo, hi + 1)]
            self.notify(
                f"Patch: {len(self.patch_range)} commit(s) — a apply · c cherry-pick · x clear"
            )
        self._sync_commits_title()

    def patch_clear(self) -> None:
        self.patch_anchor = None
        self.patch_range = []
        self._sync_commits_title()
        self.notify("Patch cleared")

    @work(exclusive=True, group="action")
    async def patch_apply(self) -> None:
        """Apply the marked range as patches onto the current worktree."""
        if not self.patch_range:
            self.notify("No patch marked — press m on a commit first", severity="warning")
            return
        applied = 0
        for commit in reversed(self.patch_range):  # oldest first
            try:
                raw = await self.git.run("show", "--no-color", "--pretty=format:", commit.sha)
                if raw.strip():
                    await self.run_git("apply", input=raw)
                    applied += 1
            except GitError as err:
                self._notify_error("patch apply", err)
                self.notify(
                    f"Stopped after {applied}/{len(self.patch_range)}",
                    severity="error",
                    timeout=10,
                )
                await self.refresh_state(force=True).wait()
                return
        self.notify(f"Applied {applied} commit patch(es) to the worktree")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def patch_cherry(self) -> None:
        """Cherry-pick the marked range onto the current branch."""
        if not self.patch_range:
            self.notify("No patch marked — press m on a commit first", severity="warning")
            return
        oldest = self.patch_range[-1].sha
        newest = self.patch_range[0].sha
        try:
            await self.run_git("cherry-pick", f"{oldest}^..{newest}")
        except GitError as err:
            self._notify_error("cherry-pick range", err)
            self.notify("Conflicts? Resolve with o/t/b then press R", timeout=10)
            await self.refresh_state(force=True).wait()
            return
        count = len(self.patch_range)
        self.patch_clear()
        self.notify(f"Cherry-picked {count} commit(s)")
        await self.refresh_state(force=True).wait()

    # --- custom commands ---------------------------------------------------------

    def on_key(self, event) -> None:
        """Run a user custom command when its key is otherwise unbound."""
        # Printable keys arrive as names ("exclamation_mark"); match the
        # character too so config can simply say key = "!".
        pressed = getattr(event, "character", None) or event.key
        for custom in self.config.custom_commands:
            if pressed == custom.key or event.key == custom.key:
                event.stop()
                event.prevent_default()
                self.request_custom(custom)
                return

    def request_custom(self, custom: CustomCommand) -> None:
        if custom.confirm:
            self.push_screen(
                ConfirmScreen(f"Run `{custom.command}`?"),
                lambda ok: self.run_custom(custom) if ok else None,
            )
        else:
            self.run_custom(custom)

    @work(exclusive=True, group="action")
    async def run_custom(self, custom: CustomCommand) -> None:
        self.command_log.write_command(f"$ {custom.command}", ok=True)
        try:
            proc = await asyncio.create_subprocess_shell(
                custom.command,
                cwd=str(self.git.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except (OSError, TimeoutError) as err:
            self.notify(f"custom command failed: {err}", severity="error", timeout=10)
            return
        out = (stdout + stderr).decode(errors="replace").strip()
        ok = proc.returncode == 0
        if out:
            self.notify(out.splitlines()[-1][:200], severity=None if ok else "error", timeout=8)
        if not ok:
            self.command_log.write_command(
                f"$ {custom.command}", ok=False, detail=out[-300:]
            )
        self._last_status_raw = None
        await self.refresh_state(force=True).wait()

    # --- recent repos -------------------------------------------------------------

    def action_recent_repos(self) -> None:
        paths = load_recent()
        if not paths:
            self.notify("No recent repositories recorded yet", severity="warning")
            return
        self.push_screen(
            RecentReposScreen(paths, str(self.git.repo_root)),
            lambda path: self.switch_repo(path) if path else None,
        )

    def switch_repo(self, path: str) -> None:
        """Relaunch lazysnake inside another repository."""
        record_recent(self.git.repo_root)
        os.execv(sys.executable, [sys.executable, "-m", "lazysnake", path])

    # --- worktrees ----------------------------------------------------------------

    def action_worktrees(self) -> None:
        self.run_worker(self._worktree_flow(), exclusive=True, group="action")

    async def _worktree_flow(self) -> None:
        data = await self.git.run("worktree", "list", "--porcelain")
        worktrees = parse_worktrees(data)
        self.push_screen(
            WorktreeScreen(worktrees),
            lambda result: self._worktree_act(result) if result else None,
        )

    def _worktree_act(self, result: dict) -> None:
        action = result["action"]
        if action == "add":
            self.push_screen(
                InputScreen("New worktree branch name:"),
                lambda name: self.worktree_add(name) if name else None,
            )
        elif action == "switch":
            self.switch_repo(result["path"])
        elif action == "delete":
            self.push_screen(
                ConfirmScreen(f"Remove worktree '{result['path']}'?", danger=True),
                lambda ok: self.worktree_delete(result["path"]) if ok else None,
            )

    @work(exclusive=True, group="action")
    async def worktree_add(self, name: str) -> None:
        path = self.git.repo_root.parent / f"{self.git.repo_root.name}-{name}"
        try:
            await self.run_git("worktree", "add", "-b", name, str(path))
        except GitError as err:
            self._notify_error("worktree add", err)
            return
        self.notify(f"Worktree created at {path}")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def worktree_delete(self, path: str) -> None:
        try:
            await self.run_git("worktree", "remove", path)
        except GitError as err:
            self._notify_error("worktree remove", err)
            return
        self.notify(f"Removed {path}")
        await self.refresh_state(force=True).wait()

    # --- submodules ---------------------------------------------------------------

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

    def _submodule_act(self, result: dict) -> None:
        action = result["action"]
        if action == "enter":
            self.switch_repo(str(self.git.repo_root / result["path"]))
        elif action == "update":
            self.submodule_update()
        elif action == "add":
            self.push_screen(
                InputScreen("Submodule URL:"),
                lambda url: self._submodule_add_path(url) if url else None,
            )
        elif action == "deinit":
            self.push_screen(
                ConfirmScreen(
                    f"Deinit submodule '{result['path']}'?\nIts worktree copy is removed.",
                    danger=True,
                ),
                lambda ok: self.submodule_deinit(result["path"]) if ok else None,
            )

    def _submodule_add_path(self, url: str) -> None:
        self.push_screen(
            InputScreen("Submodule path (inside the repo):"),
            lambda path: self.submodule_add(url, path) if path else None,
        )

    @work(exclusive=True, group="action")
    async def submodule_update(self) -> None:
        try:
            await self.run_git("submodule", "update", "--init", "--recursive")
        except GitError as err:
            self._notify_error("submodule update", err)
            return
        self.notify("Submodules updated")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def submodule_add(self, url: str, path: str) -> None:
        args = ["submodule", "add"]
        if url.startswith(("/", "./", "../", "~/")):
            # An explicitly typed local path: opt into the file transport,
            # which git blocks by default (it only blocks file:// URLs).
            args = ["-c", "protocol.file.allow=always", *args]
        args.extend([url, path])
        try:
            await self.run_git(*args)
            await self.run_git("commit", "-m", f"Add submodule {path}")
        except GitError as err:
            self._notify_error("submodule add", err)
            return
        self.notify(f"Submodule added at {path}")
        await self.refresh_state(force=True).wait()

    @work(exclusive=True, group="action")
    async def submodule_deinit(self, path: str) -> None:
        try:
            await self.run_git("submodule", "deinit", "-f", path)
        except GitError as err:
            self._notify_error("submodule deinit", err)
            return
        self.notify(f"Submodule {path} deinitialized")
        await self.refresh_state(force=True).wait()

    # --- grep ------------------------------------------------------------------

    def action_grep(self) -> None:
        self.push_screen(
            InputScreen("grep tracked files (fixed substring):"),
            lambda term: self.run_worker(self._grep_flow(term), group="action")
            if term
            else None,
        )

    async def _grep_flow(self, term: str) -> None:
        try:
            out = await self.git.run("grep", "-n", "-I", "--fixed-strings", "--", term)
        except GitError:
            self.notify(f"No matches for '{term}'")
            return
        matches: list[tuple[str, int, str]] = []
        for raw in out.splitlines():
            head, _, text = raw.partition(":")
            path, _, line_s = head.partition(":")
            if line_s.isdigit():
                matches.append((path, int(line_s), text))
        if not matches:
            self.notify(f"No matches for '{term}'")
            return
        self.push_screen(
            GrepScreen(matches, term),
            lambda sel: self._jump_to_match(sel) if sel else None,
        )

    def _jump_to_match(self, sel: dict) -> None:
        # Own group: jump awaits toggle_stage/refresh workers that live in
        # other exclusive groups; sharing "action" would get it cancelled.
        self.run_worker(self._jump_worker(sel), group="jump")

    async def _jump_worker(self, sel: dict) -> None:
        path, line_no = sel["path"], int(sel["line"])
        if sel.get("stage"):
            entry = next((f for f in self.snapshot.files if f.path == path), None)
            if entry is not None:
                await self.toggle_stage(entry, staged_view=False).wait()
        # Focus the Files panel on just this file; `/` with empty text clears.
        self.files_filter = path
        await self.refresh_state(force=True).wait()
        self.action_focus_panel("files")
        await self._land_cursor(path, line_no)

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
        for i, child in enumerate(dv.children):
            if (
                isinstance(child, DiffRow)
                and child.line_index is not None
                and dv.current_fd.hunks[child.hunk_index].lines[child.line_index].new_no
                == line_no
            ):
                dv.index = i
                return

    def action_context_down(self) -> None:
        self.diff_context = max(1, self.diff_context - 1)
        self.notify(f"Diff context: {self.diff_context}")
        self.refresh_main()

    def action_context_up(self) -> None:
        self.diff_context = min(9, self.diff_context + 1)
        self.notify(f"Diff context: {self.diff_context}")
        self.refresh_main()

    # remote operations -------------------------------------------------------

    async def run_git_stream(self, *args: str) -> int:
        """Run git with live output streaming into the command log.

        Snapshots state first (like run_git), streams each output line as
        it arrives, and returns the exit code without raising.
        """
        await self._checkpoint()
        display = "git " + " ".join(args)
        self.command_log.write_output(f"▸ {display}")
        code, _combined = await self.git.run_streaming(
            *args,
            on_line=lambda text, is_err: self.command_log.write_output(
                text, is_stderr=is_err
            ),
        )
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
            return code
        self.notify("Fetched")
        self._last_status_raw = None
        await self.refresh_state(force=True).wait()
        return 0

    @work(exclusive=True, group="action")
    async def pull(self) -> int:
        if not self.snapshot.upstream:
            self.notify("No upstream configured for this branch", severity="warning")
            return 1
        code = await self.run_git_stream("pull")
        if code != 0:
            self.notify("Pull failed — see command log", severity="error", timeout=10)
            await self.refresh_state(force=True).wait()
            return code
        self.notify("Pulled")
        self._last_status_raw = None
        await self.refresh_state(force=True).wait()
        return 0

    def action_fetch(self) -> None:
        self.fetch()

    def action_pull(self) -> None:
        self.pull()

    def action_push(self) -> None:
        if self.snapshot.upstream:
            self.push()
        else:
            branch = self.snapshot.branch
            self.push_screen(
                ConfirmScreen(
                    f"No upstream for '{branch}'.\nPush and set upstream to 'origin/{branch}'?"
                ),
                lambda ok: self.push(set_upstream=True) if ok else None,
            )

    @work(exclusive=True, group="action")
    async def push(self, *, set_upstream: bool = False) -> int:
        args = ["push", "--progress"]
        if set_upstream:
            args.extend(["--set-upstream", "origin", self.snapshot.branch])
        code = await self.run_git_stream(*args)
        if code != 0:
            self.notify("Push failed — see command log", severity="error", timeout=10)
            await self.refresh_state(force=True).wait()
            return code
        self.notify("Pushed")
        self._last_status_raw = None
        await self.refresh_state(force=True).wait()
        return 0


# The app class above declares ACTIONS; keys come from the keymap. Module
# name stays `LazysnakeApp` so imports and CSS keep working. `cli` rebuilds
# this class with a user keymap when [keys] config is present.
LazysnakeApp = bind_keys(LazysnakeApp, "global", DEFAULT_KEYMAP)
