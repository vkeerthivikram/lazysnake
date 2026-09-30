"""Commits-panel actions: commit/amend, history surgery (revert, rebase ops,
reword, fixup, move), bisect, patch-apply, the todo editor, and patch mode.

``CommitsActions`` is composed into :class:`lazysnake.ui.app.LazysnakeApp`.
It relies on the app for ``mutate``/``confirm``, ``run_git``,
``_notify_error``, ``notify``, ``refresh_state``, ``snapshot``, ``git``,
``graph``, ``commits_panel`` and the patch-range state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import work

from lazysnake.git.log import FORMAT as LOG_FORMAT
from lazysnake.git.log import Commit, parse_log
from lazysnake.git.runner import GitError
from lazysnake.rebase import TodoEntry, TodoPlan, rebase_with_ops
from lazysnake.ui.modals import CommitScreen, RebaseTodoResult, RebaseTodoScreen

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from lazysnake.git.log import GraphCommit
    from lazysnake.git.models import RepoSnapshot
    from lazysnake.git.runner import Git
    from lazysnake.ui.panels import CommitsPanel


class CommitsActions:
    """Everything the Commits panel triggers."""

    if TYPE_CHECKING:
        # App-provided state and infrastructure; app.py is the owner.
        git: Git
        snapshot: RepoSnapshot
        graph: list[GraphCommit]
        commits_panel: CommitsPanel
        patch_anchor: Commit | None
        patch_range: list[Commit]
        notify: Callable[..., Any]
        push_screen: Callable[..., Any]
        run_worker: Callable[..., Any]
        run_git: Callable[..., Any]
        mutate: Callable[..., Any]
        confirm: Callable[..., Any]
        _checkpoint: Callable[..., Any]
        _notify_error: Callable[..., Any]
        refresh_state: Callable[..., Any]
        _refreshed: Callable[..., Any]

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
        await self.mutate(
            "commit",
            *args,
            success=("Amended" if amend else "Committed") + f": {message.splitlines()[0]}",
        )

    # history surgery ----------------------------------------------------------

    @work(exclusive=True, group="action")
    async def cherry_pick(self, commit: Commit) -> None:
        await self.mutate(
            "cherry-pick",
            "cherry-pick",
            commit.sha,
            success=f"Cherry-picked {commit.short_sha}",
            refresh_on_error=True,
            conflict_hint="Conflicts? Resolve with o/t/b then press R",
        )

    def request_revert(self, commit: Commit) -> None:
        self.confirm(
            f"Revert '{commit.subject}'?\nThis creates a new commit.",
            lambda: self.revert(commit),
        )

    @work(exclusive=True, group="action")
    async def revert(self, commit: Commit) -> None:
        await self.mutate(
            "revert",
            "revert",
            "--no-edit",
            commit.sha,
            success=f"Reverted {commit.short_sha}",
        )

    def request_drop_commit(self, commit: Commit) -> None:
        self.confirm(
            f"Drop commit '{commit.subject}'?\nHistory will be rewritten.",
            lambda: self._do_rebase_op(commit, "drop"),
        )

    def request_squash_commit(self, commit: Commit) -> None:
        self.confirm(
            f"Squash '{commit.subject}' into its parent?\nHistory will be rewritten.",
            lambda: self._do_rebase_op(commit, "squash"),
        )

    @work(exclusive=True, group="action")
    async def _do_rebase_op(self, commit: Commit, op: str) -> None:
        # rebase_with_ops drives git itself (not run_git), so this worker
        # keeps its explicit error/refresh tail instead of mutate().
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
            await self._checkpoint()
            await rebase_with_ops(self.git, base, {commit.sha: op})
        except GitError as err:
            self._notify_error(f"rebase {op}", err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self._refreshed()
            return
        self.notify(f"Rebase done ({op} {commit.short_sha})")
        await self._refreshed()

    def action_rebase_continue(self) -> None:
        """Smart continue: finishes whichever sequencer is in progress
        (cherry-pick, merge, or rebase) based on on-disk state files."""
        self.run_worker(self._sequencer_continue(), exclusive=True, group="action")

    async def _sequencer_continue(self) -> None:
        # Resolve the real git dir: linked worktrees keep .git as a file,
        # so reading state files through repo_root/.git misreports and R
        # would fall through to `rebase --continue`.
        git_dir = await self.git.git_dir()
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
                    await self.run_git("cherry-pick", "--skip", checkpoint=False)
                except GitError as skip_err:
                    self._notify_error("cherry-pick continue", skip_err)
                    return
            else:
                self._notify_error(f"{label} continue", err)
                return
        self.notify(f"{label} continued")
        await self._refreshed()

    def action_rebase_abort(self) -> None:
        self.confirm(
            "Abort the rebase in progress?",
            lambda: self.run_worker(self._rebase_finish("abort"), group="action"),
        )

    async def _rebase_finish(self, how: str) -> None:
        await self.mutate(
            f"rebase {how}",
            "rebase",
            f"--{how}",
            success="Rebase aborted" if how == "abort" else "Rebase continued",
        )

    # history extras -----------------------------------------------------------

    def request_checkout_commit(self, commit: Commit) -> None:
        self.confirm(
            f"Checkout '{commit.subject}' (detached HEAD)?",
            lambda: self.checkout_detached(commit.sha, label=commit.short_sha),
        )

    @work(exclusive=True, group="action")
    async def checkout_detached(self, sha: str, *, label: str) -> None:
        await self.mutate(
            "checkout", "switch", "--detach", sha, success=f"Detached HEAD at {label}"
        )

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
            await self._checkpoint()
            await rebase_with_ops(self.git, base, {commit.sha: "reword"}, message=message)
        except GitError as err:
            self._notify_error("reword", err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self._refreshed()
            return
        self.notify(f"Reworded {commit.short_sha}")
        await self._refreshed()

    @work(exclusive=True, group="action")
    async def fixup_commit(self, commit: Commit) -> None:
        await self.mutate(
            "fixup",
            "commit",
            "--fixup",
            commit.sha,
            success=f"Fixup commit created for {commit.short_sha}",
        )

    @work(exclusive=True, group="action")
    async def move_commit(self, commit: Commit, direction: str) -> None:
        if direction not in ("move-up", "move-down"):
            raise ValueError(f"unknown move direction: {direction}")
        base = await self._anchor_base(commit)
        try:
            await self._checkpoint()
            await rebase_with_ops(self.git, base, {commit.sha: direction})
        except GitError as err:
            self._notify_error(direction, err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self._refreshed()
            return
        self.notify(f"Moved commit {'up' if direction == 'move-up' else 'down'}")
        await self._refreshed()

    async def _anchor_base(self, commit: Commit) -> list[str]:
        """Todo base that includes the commit's parent as an anchor."""
        try:
            await self.git.run("rev-parse", "--verify", "-q", f"{commit.sha}^^")
            return [f"{commit.sha}^^"]
        except GitError:
            return ["--root"]

    # bisect ---------------------------------------------------------------

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
        await self._refreshed()

    def request_bisect_reset(self) -> None:
        if not self.snapshot.bisecting:
            self.notify("No bisect in progress", severity="warning")
            return
        self.confirm("Reset the bisect in progress?", lambda: self.bisect_reset())

    @work(exclusive=True, group="action")
    async def bisect_reset(self) -> None:
        await self.mutate("bisect reset", "bisect", "reset", success="Bisect reset")

    # patch apply ---------------------------------------------------------

    @work(exclusive=True, group="action")
    async def apply_commit_patch(self, commit: Commit) -> None:
        try:
            raw = await self.git.run("show", "--no-color", "--pretty=format:", commit.sha)
        except GitError as err:
            self._notify_error("apply patch", err)
            return
        if not raw.strip():
            self.notify("That commit has no textual diff", severity="warning")
            return
        await self.mutate(
            "apply patch",
            "apply",
            input=raw,
            success=f"Applied patch of {commit.short_sha} to the worktree",
        )

    # rebase todo editor ------------------------------------------------------

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

    def _run_todo(self, base: str, result: RebaseTodoResult) -> None:
        self.run_worker(self._run_todo_worker(base, result), exclusive=True, group="action")

    async def _run_todo_worker(self, base: str, result: RebaseTodoResult) -> None:
        ops = {sha: verb for sha, verb in result.ops.items() if sha}
        try:
            await self._checkpoint()
            await rebase_with_ops(self.git, base, ops, message=result.message, order=result.order)
        except GitError as err:
            self._notify_error("rebase", err)
            self.notify("Fix conflicts or press K to abort the rebase", timeout=10)
            await self._refreshed()
            return
        self.notify("Rebase done")
        await self._refreshed()

    # patch copy mode ---------------------------------------------------------

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
        checkpoint_needed = True
        for commit in reversed(self.patch_range):  # oldest first
            try:
                raw = await self.git.run("show", "--no-color", "--pretty=format:", commit.sha)
                if raw.strip():
                    if checkpoint_needed:
                        await self._checkpoint()
                        checkpoint_needed = False
                    await self.run_git("apply", input=raw, checkpoint=False)
                    applied += 1
            except GitError as err:
                self._notify_error("patch apply", err)
                self.notify(
                    f"Stopped after {applied}/{len(self.patch_range)}",
                    severity="error",
                    timeout=10,
                )
                await self._refreshed()
                return
        self.notify(f"Applied {applied} commit patch(es) to the worktree")
        await self._refreshed()

    @work(exclusive=True, group="action")
    async def patch_cherry(self) -> None:
        """Cherry-pick the marked range onto the current branch."""
        if not self.patch_range:
            self.notify("No patch marked — press m on a commit first", severity="warning")
            return
        oldest = self.patch_range[-1].sha
        newest = self.patch_range[0].sha
        count = len(self.patch_range)
        ok = await self.mutate(
            "cherry-pick range",
            "cherry-pick",
            f"{oldest}^..{newest}",
            refresh_on_error=True,
            conflict_hint="Conflicts? Resolve with o/t/b then press R",
        )
        if ok:
            # mutate() already refreshed; split from mutate() to keep the
            # old notification order (clear notice, then cherry-pick count).
            self.patch_clear()
            self.notify(f"Cherry-picked {count} commit(s)")
