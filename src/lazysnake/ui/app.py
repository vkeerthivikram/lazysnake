"""The lazysnake Textual application.

This module is the composition root: it owns the app class statement
(composed from the action mixins in :mod:`lazysnake.ui.actions`), layout,
keymap wiring, refresh orchestration, and the shared helpers every mixin
builds on (``run_git``, ``mutate``, ``confirm``, undo/redo, custom
commands). Panel actions live in the mixins; Textual resolves their
``action_*`` methods and ``@work`` workers through the MRO unchanged.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any, ClassVar

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer

from lazysnake.config import Config, CustomCommand, load_config
from lazysnake.git.branch import FORMAT as BRANCH_FORMAT
from lazysnake.git.branch import Branch, parse_branches
from lazysnake.git.log import GRAPH_FORMAT, Commit, GraphCommit, parse_log_graph
from lazysnake.git.merge import bisect_in_progress
from lazysnake.git.models import RepoSnapshot
from lazysnake.git.runner import Git, GitError, terminate_process
from lazysnake.git.snapshot import Snapshot, capture, restore
from lazysnake.git.stash import FORMAT as STASH_FORMAT
from lazysnake.git.stash import StashEntry, parse_stash
from lazysnake.git.status import merge_in_progress, parse_status
from lazysnake.git.submodule import submodule_paths
from lazysnake.git.tags import FORMAT as TAG_FORMAT
from lazysnake.git.tags import Tag, parse_tags
from lazysnake.keymap import Keymap, bind, bind_keys
from lazysnake.ui.actions.branch import BranchActions
from lazysnake.ui.actions.commits import CommitsActions
from lazysnake.ui.actions.files import FilesActions
from lazysnake.ui.actions.files import build_editor_command as build_editor_command
from lazysnake.ui.actions.remote import RemoteActions
from lazysnake.ui.actions.search import SearchActions
from lazysnake.ui.actions.stash import StashActions
from lazysnake.ui.actions.tags import TagActions
from lazysnake.ui.actions.views import MainViewMixin
from lazysnake.ui.actions.worktree import WorktreeActions
from lazysnake.ui.modals import ConfirmScreen
from lazysnake.ui.panels import (
    DEFAULT_KEYMAP,
    BranchesPanel,
    CommandLog,
    CommitsPanel,
    DiffView,
    FilesPanel,
    MainView,
    StashPanel,
    StatusBar,
    TagsPanel,
)

POLL_SECONDS = 2.0
CUSTOM_COMMAND_TIMEOUT = 300.0  # user commands can be slow (builds, tests)


@bind("global", DEFAULT_KEYMAP)
class LazysnakeApp(
    FilesActions,
    CommitsActions,
    BranchActions,
    StashActions,
    TagActions,
    RemoteActions,
    WorktreeActions,
    SearchActions,
    MainViewMixin,
    App[None],
):
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
        **kwargs: Any,
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
        self.graph: list[GraphCommit] = []
        self.stash: list[StashEntry] = []
        self.tags: list[Tag] = []
        self.diff_context = 3
        self.files_filter: str | None = None
        self.commits_filter: str | None = None
        self.submodules: list[str] = []
        self.patch_anchor = None
        self.patch_range: list[Commit] = []
        self._undo_stack: list[Snapshot] = []
        self._redo_stack: list[Snapshot] = []
        self._recovery_snapshots: list[Snapshot] = []
        self._last_snapshot_commit: str | None = None
        self._file_write_lock = asyncio.Lock()
        self._last_status_raw: str | None = None
        self.main_source = "files"
        self._editor_task = None

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
        # Porcelain v2 has no merge/bisect headers; on-disk state is the
        # truth. Resolve the real git dir via the runner: in a linked
        # worktree .git is a FILE, so repo_root/.git/<name> never exists.
        self.snapshot.merging = await merge_in_progress(self.git)
        self.snapshot.bisecting = await bisect_in_progress(self.git)
        self.snapshot.cherry_picking = (await self.git.state_file("CHERRY_PICK_HEAD")).exists()
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
        # Any modal owns the screen (whatever its class); never mutate the
        # panels underneath it. isinstance keeps this true for modals
        # added after this check was written.
        if not isinstance(self.screen, ModalScreen):
            self.refresh_state()

    # ------------------------------------------------------------ logged git

    async def run_git(self, *args: str, checkpoint: bool = True, **kwargs: Any) -> str:
        """Run git, log the command, and surface errors as toasts.

        Snapshots full state before the command so ``z`` can undo the
        actions lazysnake itself performs (reads dedupe to nothing).
        """
        if checkpoint:
            await self._checkpoint()
        if kwargs.get("max_output_bytes") is not None:
            raise ValueError("bounded output is only supported by Git.run, not logged mutations")
        display = "git " + " ".join(args)
        try:
            out = await self.git.run(*args, **kwargs)
        except GitError as err:
            self.command_log.write_command(display, ok=False, detail=err.stderr.strip()[:300])
            raise
        self.command_log.write_command(display, ok=True)
        if not isinstance(out, str):
            raise TypeError("run_git expects a string result")
        return out

    async def _checkpoint(self, *, remove_paths: tuple[str, ...] = ()) -> Snapshot:
        """Snapshot full repo state before a mutating action; clears redo."""
        snap = await capture(self.git, self._last_snapshot_commit, remove_paths=remove_paths)
        self._last_snapshot_commit = snap.worktree_commit
        # Dedupe on content, not the chained snapshot commit SHA (see
        # Snapshot.content_key) — two no-op actions from identical state
        # must not flood the undo stack.
        if not self._undo_stack or self._undo_stack[-1].content_key != snap.content_key:
            self._undo_stack.append(snap)
            if len(self._undo_stack) > 50:
                self._undo_stack.pop(0)
            self._redo_stack.clear()
            return snap
        return self._undo_stack[-1]

    def _notify_error(self, action: str, err: GitError) -> None:
        self.notify(f"{action} failed: {err}", severity="error", timeout=10)

    async def mutate(
        self,
        action: str,
        *args: str,
        input: str | None = None,
        success: str | None = None,
        refresh_on_error: bool = False,
        conflict_hint: str | Callable[[], str | None] | None = None,
        checkpoint: bool = True,
    ) -> bool:
        """Run one mutating git command with the uniform worker tail.

        Implements the pattern every action worker used to spell out:
        ``run_git`` → on :class:`GitError` an error toast (labelled
        ``action``) plus an optional conflict hint and optional
        error-refresh; on success an optional toast and a forced refresh.
        Returns True when the command succeeded. Multi-command or
        non-run_git workers keep their explicit tails.
        """
        try:
            await self.run_git(*args, input=input, checkpoint=checkpoint)
        except GitError as err:
            self._notify_error(action, err)
            hint = conflict_hint() if callable(conflict_hint) else conflict_hint
            if hint:
                self.notify(hint, timeout=10)
            if refresh_on_error:
                await self.refresh_state(force=True).wait()
            return False
        if success is not None:
            self.notify(success)
        await self.refresh_state(force=True).wait()
        return True

    def confirm(self, prompt: str, on_ok: Callable[[], object]) -> None:
        """Danger-confirm ``on_ok`` behind a modal (the push_screen pattern).

        Every legacy call site used ``danger=True`` styling, so the
        wrapper keeps it. ``on_ok`` may be a ``@work`` method (its
        ``Worker`` return value is ignored), hence the loose return type.
        """
        self.push_screen(ConfirmScreen(prompt, danger=True), lambda ok: on_ok() if ok else None)

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

    # undo/redo live here (not in a mixin): tests patch
    # ``lazysnake.ui.app.restore`` and ``capture``/``restore`` must be
    # resolved through this module's namespace.

    def action_undo(self) -> None:
        self.confirm(
            "Undo the last lazysnake action?\n"
            "Restores HEAD, index, worktree and stash state exactly —\n"
            "uncommitted changes made since are lost.",
            self.undo,
        )

    @work(exclusive=True, group="action")
    async def undo(self) -> None:
        if not self._undo_stack:
            self.notify("Nothing left to undo", severity="warning")
            return
        target = self._undo_stack[-1]
        try:
            current = await capture(self.git, self._last_snapshot_commit)
        except GitError as err:
            self._notify_error("undo checkpoint", err)
            return
        self._last_snapshot_commit = current.worktree_commit
        try:
            await restore(self.git, target)
        except asyncio.CancelledError:
            rollback_error = await self._rollback_snapshot(current)
            if rollback_error is None:
                self.notify("Undo cancelled; repository state recovered", severity="warning")
            else:
                self._keep_recovery_snapshot(current, "undo", rollback_error)
            raise
        except Exception as err:
            rollback_error = await self._rollback_snapshot(current)
            if rollback_error is not None:
                self._keep_recovery_snapshot(current, "undo", rollback_error)
            self._report_restore_error("undo", err)
            return
        self._undo_stack.pop()
        self._redo_stack.append(current)
        self.notify("Undone — state restored")
        await self.refresh_state(force=True).wait()

    def action_redo(self) -> None:
        if not self._redo_stack:
            self.notify("Nothing to redo", severity="warning")
            return
        self.confirm("Redo the last undone action?", self.redo)

    @work(exclusive=True, group="action")
    async def redo(self) -> None:
        if not self._redo_stack:
            return
        target = self._redo_stack[-1]
        try:
            current = await capture(self.git, self._last_snapshot_commit)
        except GitError as err:
            self._notify_error("redo checkpoint", err)
            return
        self._last_snapshot_commit = current.worktree_commit
        try:
            await restore(self.git, target)
        except asyncio.CancelledError:
            rollback_error = await self._rollback_snapshot(current)
            if rollback_error is None:
                self.notify("Redo cancelled; repository state recovered", severity="warning")
            else:
                self._keep_recovery_snapshot(current, "redo", rollback_error)
            raise
        except Exception as err:
            rollback_error = await self._rollback_snapshot(current)
            if rollback_error is not None:
                self._keep_recovery_snapshot(current, "redo", rollback_error)
            self._report_restore_error("redo", err)
            return
        self._redo_stack.pop()
        self._undo_stack.append(current)
        self.notify("Redone — state restored")
        await self.refresh_state(force=True).wait()

    async def _rollback_snapshot(self, snapshot: Snapshot) -> Exception | None:
        """Finish restoring a recovery snapshot even if this worker is cancelled."""
        task = asyncio.create_task(restore(self.git, snapshot))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                await task
            except Exception as err:
                return err
        except Exception as err:
            return err
        return None

    def _keep_recovery_snapshot(self, snapshot: Snapshot, action: str, err: Exception) -> None:
        self._recovery_snapshots.append(snapshot)
        self.notify(
            f"{action} rollback failed: {err}. Recovery snapshot retained.",
            severity="error",
            timeout=15,
        )

    def _report_restore_error(self, action: str, err: Exception) -> None:
        if isinstance(err, GitError):
            self._notify_error(action, err)
        else:
            self.notify(f"{action} failed: {err}", severity="error", timeout=10)

    # custom commands ---------------------------------------------------------
    # run_custom stays in this module: tests patch
    # ``lazysnake.ui.app.CUSTOM_COMMAND_TIMEOUT``, which only affects
    # functions that resolve it through this namespace.

    def on_key(self, event: events.Key) -> None:
        """Run a user custom command when its key is otherwise unbound."""
        # Only the base screen dispatches custom commands; keys bubbling
        # out of an open modal must not fire them.
        if self.screen is not self.default_screen:
            return
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
            self.confirm(f"Run `{custom.command}`?", lambda: self.run_custom(custom))
        else:
            self.run_custom(custom)

    @work(exclusive=True, group="action")
    async def run_custom(self, custom: CustomCommand) -> None:
        display = f"$ {custom.command}"
        spawn_options: Any = {"start_new_session": True} if os.name == "posix" else {}
        try:
            proc = await asyncio.create_subprocess_shell(
                custom.command,
                cwd=str(self.git.repo_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_options,
            )
        except OSError as err:
            self.command_log.write_command(display, ok=False, detail=str(err)[:300])
            self.notify(f"custom command failed: {err}", severity="error", timeout=10)
            return
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=CUSTOM_COMMAND_TIMEOUT
            )
        except TimeoutError:
            # wait_for cancels communicate() but leaves the child running;
            # kill + reap it the way the git runner does — the whole group,
            # or grandchildren (a && b, make, npm) outlive the timeout.
            await terminate_process(proc)
            self.command_log.write_command(
                display, ok=False, detail=f"timed out after {CUSTOM_COMMAND_TIMEOUT:.0f}s"
            )
            self.notify(
                f"custom command timed out after {CUSTOM_COMMAND_TIMEOUT:.0f}s",
                severity="error",
                timeout=10,
            )
            return
        except asyncio.CancelledError:
            await terminate_process(proc)
            raise
        out = (stdout_b + stderr_b).decode(errors="replace").strip()
        ok = proc.returncode == 0
        # Log once, after completion, with the real outcome (run_git's
        # pattern) — never a ✓ for a command that then fails.
        self.command_log.write_command(display, ok=ok, detail=out[-300:] if not ok else "")
        if out:
            self.notify(
                out.splitlines()[-1][:200],
                severity="information" if ok else "error",
                timeout=8,
            )
        self._last_status_raw = None
        await self.refresh_state(force=True).wait()


# The app class above declares ACTIONS; keys come from the keymap, and the
# @bind decorator applies the default table at class-creation time so the
# public name stays a usable class. `cli` rebuilds this class with a user
# keymap when [keys] config is present.
