"""Regression tests for the T5 audit-fix batch.

Covers: linked-worktree state detection, non-origin remotes, undo dedupe
and failure handling, render caps, custom-command logging, and the grep
no-match vs real-error distinction.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest
from textual.widgets import Input

import lazysnake.ui.actions.views as views_module
import lazysnake.ui.app as app_module
import lazysnake.ui.diff_render as diff_render_module
import lazysnake.ui.panels as panels_module
from lazysnake.config import Config, CustomCommand
from lazysnake.git.diff import FileDiff, Hunk, LineKind, PatchLine
from lazysnake.git.runner import Git, GitError
from lazysnake.ui.app import LazysnakeApp
from lazysnake.ui.diff_render import MAX_RENDER_LINES, render_file_diffs, render_untracked
from lazysnake.ui.modals import ConfirmScreen, GrepScreen, InputScreen, SubmodulesScreen
from lazysnake.ui.panels import BranchItem, CommitItem, DiffRow


def _log_text(widget) -> str:
    return "\n".join(strip.text for strip in widget.lines)


# ---------------------------------------------------------------- item 1


async def test_linked_worktree_merge_state_and_continue(repo) -> None:
    # A linked worktree: .git is a FILE pointing at the per-worktree git dir.
    wt = repo.root.parent / f"{repo.root.name}-t5-linked"
    repo.git("worktree", "add", str(wt), "-b", "linked")
    assert (wt / ".git").is_file()

    # Diverge both sides of README.md, then merge main inside the worktree.
    (wt / "README.md").write_text("their version\n")
    repo.git("-C", str(wt), "add", "-A")
    repo.git("-C", str(wt), "commit", "-m", "their change")
    repo.write("README.md", "our version\n")
    repo.commit_all("our change")
    repo.git("-C", str(wt), "merge", "--no-edit", "main", check=False)

    app = LazysnakeApp(Git(wt))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        # The status bar is driven by these flags ( StatusBar shows "MERGING").
        assert app.snapshot.merging
        assert app.snapshot.in_conflict

        # R must target the merge, not fall through to `rebase --continue`.
        calls: list[tuple[str, ...]] = []
        inner_run = app.git.run

        async def spy_run(*args, **kwargs):
            calls.append(args)
            return await inner_run(*args, **kwargs)

        app.git.run = spy_run  # type: ignore[method-assign]
        await app._sequencer_continue()
        assert ("merge", "--continue") in calls
        assert ("rebase", "--continue") not in calls


# ---------------------------------------------------------------- item 2


async def test_delete_remote_branch_on_non_origin_remote(remote_repo, tmp_path) -> None:
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(mirror)],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_repo.git("remote", "add", "mirror", str(mirror))
    remote_repo.git("push", "mirror", "main")
    remote_repo.git("switch", "-c", "sentinel")
    remote_repo.write("s.txt", "s\n")
    remote_repo.commit_all("sentinel work")
    remote_repo.git("push", "-u", "mirror", "sentinel")
    remote_repo.git("switch", "main")

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        doomed = next(
            child.branch
            for child in app.branches_panel.children
            if isinstance(child, BranchItem) and child.branch.name == "mirror/sentinel"
        )
        await app.delete_branch(doomed).wait()
        await pilot.pause()

    # The branch must be gone from mirror (by URL) and never have touched origin.
    assert "sentinel" not in remote_repo.git("ls-remote", "--heads", str(mirror))
    assert "sentinel" not in remote_repo.git("ls-remote", "--heads", "origin")


async def test_set_upstream_targets_sole_non_origin_remote(remote_repo, tmp_path) -> None:
    mirror = tmp_path / "only-remote.git"
    mirror.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(mirror)],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_repo.git("remote", "remove", "origin")
    remote_repo.git("remote", "add", "mirror", str(mirror))
    remote_repo.git("push", "mirror", "main")

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        main = next(b for b in app.branches if b.name == "main")
        await app.set_upstream(main).wait()
        await pilot.pause()

    upstream = remote_repo.git("rev-parse", "--abbrev-ref", "main@{upstream}").strip()
    assert upstream == "mirror/main"


async def test_push_set_upstream_targets_sole_non_origin_remote(remote_repo, tmp_path) -> None:
    mirror = tmp_path / "push-target.git"
    mirror.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(mirror)],
        check=True,
        capture_output=True,
        text=True,
    )
    remote_repo.git("remote", "remove", "origin")
    remote_repo.git("remote", "add", "mirror", str(mirror))

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert app.snapshot.upstream is None
        code = await app.push(set_upstream=True).wait()
        await pilot.pause()
        assert code == 0

    upstream = remote_repo.git("rev-parse", "--abbrev-ref", "main@{upstream}").strip()
    assert upstream == "mirror/main"
    heads = remote_repo.git("ls-remote", "--heads", str(mirror))
    assert "main" in heads


# ---------------------------------------------------------------- item 3


async def test_undo_stack_dedupes_identical_state(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.git("add", "-A")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        # Two consecutive actions whose starting state is identical: the
        # second checkpoint must dedupe by content (the chained snapshot
        # commit SHA always differs, so the old check never fired).
        await app.run_git("add", "--", "a.txt")
        assert len(app._undo_stack) == 1
        await app.run_git("add", "--", "a.txt")
        assert len(app._undo_stack) == 1


async def test_undo_failure_keeps_entry(repo, monkeypatch) -> None:
    repo.write("a.txt", "a\n")
    repo.git("add", "-A")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.run_git("stash", "push")
        await pilot.pause()
        assert len(app._undo_stack) == 1

        async def exploding_restore(git, snap):
            raise GitError(["restore"], 128, "", "boom")

        monkeypatch.setattr(app_module, "restore", exploding_restore)
        await app.undo().wait()
        # The entry was pushed back instead of being lost, and nothing
        # was recorded as undone.
        assert len(app._undo_stack) == 1
        assert len(app._redo_stack) == 0
        assert len(app._recovery_snapshots) == 1


# ---------------------------------------------------------------- item 4


async def test_untracked_render_capped_and_responsive(repo) -> None:
    repo.write("big.txt", "".join(f"line {i}\n" for i in range(3000)))

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        started = time.monotonic()
        # set_files auto-highlights the first FileItem (big.txt).
        assert app.files_panel.selected_item is not None
        await app.refresh_main().wait()
        await pilot.pause()
        text = _log_text(app.main_view)
        assert "new file: big.txt" in text
        assert f"... ({3000 - MAX_RENDER_LINES} more lines" in text
        assert "line 1999" in text
        assert "line 2000" not in text
        assert len(text.splitlines()) <= MAX_RENDER_LINES + 3
        # Capped render keeps the loop responsive (would hang without the cap).
        assert time.monotonic() - started < 10


def test_render_file_diffs_caps_total_lines(monkeypatch) -> None:
    lines = [PatchLine(kind=LineKind.ADDITION, content=f"l{i}", new_no=i + 1) for i in range(3000)]
    hunk = Hunk(
        old_start=0,
        old_count=0,
        new_start=1,
        new_count=3000,
        raw_header="@@ -0,0 +1,3000 @@",
        lines=lines,
    )
    fd = FileDiff(old_path="b/big.txt", new_path="b/big.txt", is_new=True, hunks=[hunk])

    def must_render_incrementally(*args, **kwargs):
        pytest.fail("capped file diffs must not materialize a complete file or hunk")

    monkeypatch.setattr(diff_render_module, "render_file_diff", must_render_incrementally)
    plain = render_file_diffs([fd]).plain
    assert len(plain.splitlines()) <= MAX_RENDER_LINES + 1
    assert "more lines not shown" in plain
    assert "l2999" not in plain


async def test_diff_view_caps_rows_and_stages_only_a_visible_complete_line(
    repo, monkeypatch
) -> None:
    original: list[str] = []
    changed: list[str] = []
    for block in range(4):
        original.append(f"target-{block}\n")
        changed.append(f"changed-{block}\n")
        for line in range(10):
            shared = f"context-{block}-{line}\n"
            original.append(shared)
            changed.append(shared)
    repo.write("large.txt", "".join(original))
    repo.commit_all("seed separated hunks")
    repo.write("large.txt", "".join(changed))
    monkeypatch.setattr(panels_module, "MAX_DIFF_ROWS", 12, raising=False)
    monkeypatch.setattr(views_module, "MAX_DIFF_ROWS", 12, raising=False)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        app.main_source = "files"
        await app.refresh_main().wait()
        await pilot.pause()

        rows = list(app.diff_view.children)
        assert len(rows) <= 12
        assert app.diff_view.current_fd is not None
        assert len(app.diff_view.current_fd.hunks) < 4
        addition_index = next(
            i
            for i, row in enumerate(rows)
            if isinstance(row, DiffRow)
            and row.line_index is not None
            and app.diff_view.current_fd.hunks[row.hunk_index].lines[row.line_index].content
            == "changed-0"
        )
        app.diff_view.index = addition_index
        await app.stage_from_diff("line").wait()
        await pilot.pause()

        staged = await app.git.run("diff", "--no-color", "--cached", "--", "large.txt")
        assert "+changed-0" in staged
        assert "+changed-1" not in staged


async def test_giant_tracked_diff_and_show_are_bounded_and_not_stageable(repo, monkeypatch) -> None:
    cap = 1024
    large_line = "changed-" + "x" * (2 * 1024 * 1024) + "\n"
    repo.write("large.txt", "base\n")
    repo.commit_all("seed large diff")
    repo.write("large.txt", large_line)
    monkeypatch.setattr(views_module, "MAX_DIFF_OUTPUT_BYTES", cap, raising=False)

    app = LazysnakeApp(Git(repo.root))
    real_run = app.git.run
    bounded_reads: list[tuple[tuple[str, ...], int, bool]] = []

    async def observe_bounded_run(*args, **kwargs):
        result = await real_run(*args, **kwargs)
        if kwargs.get("max_output_bytes") is not None:
            output, truncated = result
            bounded_reads.append((args, len(output.encode("utf-8", "surrogateescape")), truncated))
        return result

    monkeypatch.setattr(app.git, "run", observe_bounded_run)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        app.main_source = "files"
        await app.refresh_main().wait()
        await pilot.pause()

        assert any(args[0] == "diff" and truncated for args, _, truncated in bounded_reads)
        assert all(size <= cap for _, size, _ in bounded_reads)
        assert "diff truncated" in _log_text(app.main_view).lower()
        assert app.diff_view.current_fd is None
        await app.stage_from_diff("line").wait()
        assert (await real_run("diff", "--cached", "--name-only")).strip() == ""

        repo.commit_all("commit giant line")
        app.main_source = "commits"
        await app.refresh_state(force=True).wait()
        await pilot.pause()
        latest = repo.git("rev-parse", "HEAD").strip()
        app.commits_panel.index = next(
            i
            for i, item in enumerate(app.commits_panel.children)
            if isinstance(item, CommitItem) and item.commit.sha == latest
        )
        await app.refresh_main().wait()
        await pilot.pause()
        assert "diff truncated" in _log_text(app.main_view).lower()

    assert any(args[0] == "show" and truncated for args, _, truncated in bounded_reads)
    assert all(size <= cap for _, size, _ in bounded_reads)


async def test_untracked_preview_retains_bounded_lines_and_counts_in_worker(
    repo, monkeypatch
) -> None:
    long_line_chars = 100_000
    total_lines = MAX_RENDER_LINES + 3
    repo.write("huge.txt", "x" * long_line_chars + "\n" + "short\n" * (total_lines - 1))
    observed: list[tuple[object, dict[str, object]]] = []
    real_render = views_module.render_untracked

    def observe_preview(path, lines, **kwargs):
        observed.append((lines, kwargs))
        return real_render(path, lines, **kwargs)

    monkeypatch.setattr(views_module, "render_untracked", observe_preview)
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        app.main_source = "files"
        await app.refresh_main().wait()
        await pilot.pause()

    lines, options = observed[0]
    assert isinstance(lines, list)
    assert len(lines) == MAX_RENDER_LINES
    assert options["total_lines"] == total_lines
    assert len(lines[0]) <= 4096 + 32


def test_render_untracked_reports_true_total() -> None:
    text = render_untracked("f.txt", "a\nb\n").plain
    assert "@@ -0,0 +1,2 @@" in text
    assert "more lines" not in text


def test_render_cap_note_singular_and_plural() -> None:
    singular = render_untracked("one.txt", "x\n" * (MAX_RENDER_LINES + 1)).plain
    assert "(1 more line not shown)" in singular
    assert "1 more lines" not in singular
    plural = render_untracked("many.txt", "x\n" * (MAX_RENDER_LINES + 2)).plain
    assert "(2 more lines not shown)" in plural


# ---------------------------------------------------------------- item 5


async def test_poll_gated_under_any_modal(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        calls: list[bool] = []
        app.refresh_state = lambda force=False: calls.append(force)  # type: ignore[method-assign]

        # A modal the old four-class allowlist missed.
        app.push_screen(SubmodulesScreen([]))
        await pilot.pause()
        app._poll_status()
        assert calls == []

        app.pop_screen()
        await pilot.pause()
        app._poll_status()
        assert calls == [False]


async def test_custom_key_ignored_under_modal(repo) -> None:
    config = Config(custom_commands=(CustomCommand(key="!", command="touch custom-ran.txt"),))
    app = LazysnakeApp(Git(repo.root), config=config)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        app.push_screen(ConfirmScreen("hold"))
        await pilot.pause()
        await pilot.press("!")
        for _ in range(3):
            await pilot.pause()
        assert not (repo.root / "custom-ran.txt").exists()

        app.pop_screen()
        await pilot.pause()
        await pilot.press("!")
        for _ in range(3):
            await pilot.pause()
        assert (repo.root / "custom-ran.txt").exists()


# ---------------------------------------------------------------- items 6 + 9


async def test_custom_command_logs_once_with_real_outcome(repo) -> None:
    fail = CustomCommand(key="!", command="false")
    good = CustomCommand(key="Y", command="true")
    app = LazysnakeApp(Git(repo.root), config=Config(custom_commands=(fail, good)))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.run_custom(fail).wait()
        await pilot.pause()
        log = _log_text(app.command_log)
        assert log.count("$ false") == 1  # logged once, not twice
        assert "✗ $ false" in log
        assert "✓ $ false" not in log

        await app.run_custom(good).wait()
        await pilot.pause()
        log = _log_text(app.command_log)
        assert "✓ $ true" in log


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/stat").exists(),
    reason="requires POSIX shell process groups and Linux /proc",
)
async def test_custom_command_timeout_kills_child_after_shell_exits(repo, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "CUSTOM_COMMAND_TIMEOUT", 0.5)
    slow = CustomCommand(
        key="Z",
        command="sleep 30 & echo $! > descendant.pid; exit 0",
    )
    app = LazysnakeApp(Git(repo.root), config=Config(custom_commands=(slow,)))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        started = time.monotonic()
        await app.run_custom(slow).wait()
        elapsed = time.monotonic() - started
        # If the child were not killed+reaped, proc.wait() would block ~5s.
        assert elapsed < 3
        log = _log_text(app.command_log)
        assert "✗ $ sleep 30 & echo" in log
        assert "timed out" in log
        # The shell leader has exited while its background child holds the
        # pipes open. A zombie is no longer running; do not assume PID 1
        # reaps it immediately in containers.
        pid_file = repo.root / "descendant.pid"
        deadline = time.monotonic() + 2
        while not pid_file.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text().strip())
        while time.monotonic() < deadline:
            try:
                stat = Path(f"/proc/{pid}/stat").read_text()
            except ProcessLookupError:
                break
            except FileNotFoundError:
                break
            if stat.rsplit(")", 1)[1].strip().split()[0] == "Z":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError(f"descendant {pid} survived the timeout kill")


async def test_custom_command_cleanup_bounds_pipes_that_never_eof(repo, monkeypatch) -> None:
    import lazysnake.git.runner as runner_module

    release = asyncio.Event()

    class NeverEOF:
        async def read(self, _size: int) -> bytes:
            await release.wait()
            return b""

    class FakeTransport:
        closed = False

        def close(self) -> None:
            self.closed = True
            release.set()

    class FakeProcess:
        pid = None
        returncode = None
        stdout = NeverEOF()
        stderr = NeverEOF()

        def __init__(self) -> None:
            self._transport = FakeTransport()
            self.communicate_calls = 0
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def communicate(self):
            self.communicate_calls += 1
            if self.communicate_calls == 1:
                await asyncio.Event().wait()
            await release.wait()
            return b"", b""

        async def wait(self) -> int:
            return self.returncode or 0

    process = FakeProcess()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(app_module, "CUSTOM_COMMAND_TIMEOUT", 0.01)
    monkeypatch.setattr(app_module.asyncio, "create_subprocess_shell", spawn)
    monkeypatch.setattr(runner_module, "_CLEANUP_TIMEOUT", 0.02, raising=False)
    monkeypatch.setattr(runner_module, "_kill_process_group", lambda proc: proc.kill())
    app = LazysnakeApp(Git(repo.root))

    async def release_later() -> None:
        await asyncio.sleep(0.2)
        release.set()

    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        release_task = asyncio.create_task(release_later())
        started = time.monotonic()
        await app.run_custom(CustomCommand(key="X", command="sleep 30")).wait()
        elapsed = time.monotonic() - started
        release_task.cancel()
        await asyncio.gather(release_task, return_exceptions=True)

    assert elapsed < 0.12
    assert process.killed
    assert process._transport.closed


# ---------------------------------------------------------------- item 7


async def test_grep_no_match_vs_real_error(repo) -> None:
    repo.write("f.txt", "hello world\n")
    repo.commit_all("seed")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        notes: list[str] = []
        inner_notify = app.notify

        def spy_notify(message="", **kwargs):
            notes.append(str(message))
            return inner_notify(message, **kwargs)

        app.notify = spy_notify  # type: ignore[method-assign]

        # Exit 1: genuinely no matches → friendly notice, no screen.
        await app._grep_flow("zzz-no-such-thing")
        await pilot.pause()
        assert any("No matches" in n for n in notes)
        assert not isinstance(app.screen, GrepScreen)

        # Any other non-zero code: a real failure, reported as an error.
        async def broken_stream(*args, **kwargs):
            return 128, "fatal: unable to read"

        app.git.run_streaming = broken_stream  # type: ignore[method-assign]
        notes.clear()
        await app._grep_flow("hello")
        await pilot.pause()
        assert any("grep failed" in n for n in notes)
        assert not isinstance(app.screen, GrepScreen)

        # run_streaming *raising* GitError (e.g. the runner's timeout):
        # an error toast, never "No matches" and never an escaping
        # worker exception.
        async def exploding_stream(*args, **kwargs):
            raise GitError(["grep", "-n"], -1, "", "timed out after 60.0s")

        app.git.run_streaming = exploding_stream  # type: ignore[method-assign]
        notes.clear()
        await app._grep_flow("hello")
        await pilot.pause()
        assert any("grep failed" in n for n in notes)
        assert not any("No matches" in n for n in notes)
        assert not isinstance(app.screen, GrepScreen)


# ---------------------------------------------------------------- T7 round 1


async def test_filter_key_opens_prefilled_input_screen(repo) -> None:
    """Regression: `/` used to crash with TypeError — request_filter passed
    a `prefill` kwarg InputScreen did not accept. The screen must open with
    the active filter preloaded into the input (empty when no filter)."""
    repo.write("alpha.txt", "a\n")
    repo.write("beta.txt", "b\n")
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        # Files panel, no filter yet: empty prefill, not a crash.
        app.files_panel.focus()
        await pilot.press("/")
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        assert app.screen.query_one("#input-field", Input).value == ""
        await pilot.press("escape")
        await pilot.pause()

        # Round-trip: with an active filter, `/` preloads it.
        app.set_filter("files", "alpha")
        app.files_panel.focus()
        await pilot.press("/")
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        assert app.screen.query_one("#input-field", Input).value == "alpha"
        await pilot.press("escape")
        await pilot.pause()

        # Commits panel flows through the same screen with its own filter.
        app.commits_panel.focus()
        await pilot.press("/")
        await pilot.pause()
        assert isinstance(app.screen, InputScreen)
        assert app.screen.query_one("#input-field", Input).value == ""


# ------------------------------------------------------- CI stability fixes


async def test_poll_skips_while_refresh_in_flight(repo) -> None:
    """The poll timer must not cancel a running refresh (exclusive group);
    on slow CI machines that killed live actions with WorkerCancelled."""
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        calls: list[bool] = []
        app.refresh_state = lambda force=False: calls.append(force)  # type: ignore[method-assign]
        app._refresh_running = True
        app._poll_status()
        assert calls == []  # refresh busy: this tick is skipped
        app._refresh_running = False
        app._poll_status()
        assert calls == [False]  # idle: poll refreshes again


async def test_refreshed_swallows_superseded_refresh(repo) -> None:
    """_refreshed treats WorkerCancelled as success: a newer refresh
    (poll or newer action) supersedes the awaited one and lands the same
    state — the action worker must not die with WorkerFailed."""
    from textual.worker import WorkerCancelled

    app = LazysnakeApp(Git(repo.root))

    class _CancelledWorker:
        async def wait(self) -> None:
            raise WorkerCancelled("superseded")

    def fake_refresh(force: bool = True) -> _CancelledWorker:
        return _CancelledWorker()

    app.refresh_state = fake_refresh  # type: ignore[method-assign]
    await app._refreshed()  # must not raise


# --------------------------------------------------- git serialization lock


async def test_poll_storm_never_blocks_or_corrupts_actions(repo) -> None:
    """A 20x-faster poll timer overlapping rapid mutations must never
    cancel an action worker nor block a checkpoint with index.lock.

    This is the regression test for the whole CI-storm class: git's index
    allows one writer, `git status` (poll) and write-tree (checkpoint)
    both write it, and only the app-wide git lock keeps them apart.
    """
    config = Config(poll_seconds=0.05)  # direct ctor skips the 0.5s clamp
    app = LazysnakeApp(Git(repo.root), config=config)
    async with app.run_test():
        await app.refresh_state().wait()

        repo.write("storm.txt", "one\n")
        await app.stash_push().wait()  # .wait() raises on WorkerCancelled
        await app.create_branch("storm/x").wait()
        repo.write("storm.txt", "two\n")
        await app.stash_push().wait()
        await app.checkout_branch(next(b for b in app.branches if b.name == "main")).wait()
        repo.write("storm.txt", "three\n")
        await app.stash_push().wait()
        await app.undo().wait()

        log = "\n".join(line.text for line in app.command_log.lines)
        assert "snapshot failed" not in log, log
        assert "index.lock" not in log, log
        # Storm survived: stashes happened and undo worked (exact stash
        # bookkeeping is test_snapshot_undo's territory).
        stash_list = (await app.git.run("stash", "list")).strip()
        assert "stash@" in stash_list
