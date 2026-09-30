"""Full-fidelity undo/redo: index split, deletions, untracked, stashes."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path

import pytest
from textual.worker import WorkerCancelled

import lazysnake.git.snapshot as snapshot_module
import lazysnake.ui.app as app_module
from lazysnake.git.models import FileEntry
from lazysnake.git.runner import Git, GitError
from lazysnake.git.snapshot import SNAPSHOT_REF, Snapshot, capture, restore
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp
from lazysnake.ui.modals import RebaseTodoResult


async def _displays(app) -> dict[str, str]:
    data = await app.git.run("status", "--porcelain=v2", "--branch", "-z")
    return {f.path: f.display for f in parse_status(data).files}


async def test_undo_restores_index_vs_worktree_split(repo) -> None:
    repo.write("staged.txt", "base\n")
    repo.write("unstaged.txt", "base\n")
    repo.commit_all("seed")
    repo.write("staged.txt", "staged change\n")
    repo.git("add", "staged.txt")
    repo.write("unstaged.txt", "unstaged change\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        before_head = repo.git("rev-parse", "HEAD").strip()

        await app.commit("takes only staged").wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() != before_head

        await app.undo().wait()
        await pilot.pause()

        assert repo.git("rev-parse", "HEAD").strip() == before_head
        displays = await _displays(app)
        assert displays["staged.txt"] == "M "  # still staged
        assert displays["unstaged.txt"] == " M"  # still unstaged


async def test_undo_restores_discard_including_staged_state(repo) -> None:
    repo.write("f.txt", "precious\n")
    repo.git("add", "f.txt")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        entry = next(f for f in app.snapshot.files if f.path == "f.txt")

        await app.discard(entry).wait()
        await pilot.pause()
        assert not (repo.root / "f.txt").exists()

        await app.undo().wait()
        await pilot.pause()
        assert (repo.root / "f.txt").read_text() == "precious\n"
        displays = await _displays(app)
        assert displays["f.txt"] == "A "  # staged addition, exactly as before


async def test_undo_resurrects_untracked_file_removed_by_clean(repo) -> None:
    repo.write("junk.txt", "untracked but wanted back\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        entry = next(f for f in app.snapshot.files if f.path == "junk.txt")

        await app.discard(entry).wait()  # runs git clean -f
        await pilot.pause()
        assert not (repo.root / "junk.txt").exists()

        await app.undo().wait()
        await pilot.pause()
        assert (repo.root / "junk.txt").read_text() == "untracked but wanted back\n"
        displays = await _displays(app)
        assert displays["junk.txt"] == "??"


async def test_undo_preserves_worktree_deletion(repo) -> None:
    """A file deleted in the worktree must STAY deleted after undo."""
    repo.write("victim.txt", "to be deleted\n")
    repo.commit_all("seed victim")
    repo.write("other.txt", "commit me\n")
    repo.git("add", "other.txt")
    repo.root.joinpath("victim.txt").unlink()  # unstaged deletion

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        head_before = repo.git("rev-parse", "HEAD").strip()

        await app.commit("unrelated commit").wait()
        await pilot.pause()
        assert not (repo.root / "victim.txt").exists()

        await app.undo().wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == head_before
        assert not (repo.root / "victim.txt").exists()  # deletion survived
        displays = await _displays(app)
        assert displays["victim.txt"] == " D"  # unstaged deletion state back


async def test_undo_restores_stash_list(repo) -> None:
    repo.write("wip.txt", "wip content\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.stash_push().wait()
        await pilot.pause()
        assert len(app.stash) == 1
        assert not (repo.root / "wip.txt").exists()  # stashed away

        await app.undo().wait()
        await pilot.pause()
        # Worktree change is back…
        assert (repo.root / "wip.txt").read_text() == "wip content\n"
        # …and the stash list is empty again, as before the stash action.
        stash_list = (await app.git.run("stash", "list")).strip()
        assert stash_list == ""


async def test_redo_round_trip(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.git("add", "-A")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        before = repo.git("rev-parse", "HEAD").strip()

        await app.commit("action").wait()
        await pilot.pause()
        after = repo.git("rev-parse", "HEAD").strip()

        await app.undo().wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == before

        await app.redo().wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == after
        displays = await _displays(app)
        assert displays == {}  # clean tree, exactly as after the commit


async def test_snapshot_chain_reachable(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.git("add", "-A")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.commit("x").wait()
        await pilot.pause()
        ref = (await app.git.run("rev-parse", "--verify", "-q", SNAPSHOT_REF)).strip()
        assert len(ref) == 40
        # Chain: the snapshot commit's history contains the repo head.
        heads = await app.git.run("log", "--pretty=%s", ref)
        assert "lazysnake snapshot" in heads


async def test_capture_restore_roundtrip_standalone(repo) -> None:
    """Library-level: capture, wreck everything, restore, verify bytes."""
    git = Git(repo.root)
    repo.write("tracked.txt", "original tracked\n")
    repo.git("add", "tracked.txt")
    repo.write("untracked.txt", "original untracked\n")

    snap = await capture(git, None)
    assert snap is not None

    # Wreck: commit the tracked file, delete the untracked one, modify more.
    repo.git("commit", "-m", "wreck")
    repo.root.joinpath("untracked.txt").unlink()
    repo.write("tracked.txt", "wrecked\n")
    repo.write("extra.txt", "appeared\n")
    repo.git("add", "extra.txt")

    await restore(git, snap)

    assert (repo.root / "tracked.txt").read_text() == "original tracked\n"
    assert (repo.root / "untracked.txt").read_text() == "original untracked\n"
    assert not (repo.root / "extra.txt").exists()
    # Index: tracked.txt staged as a new addition, nothing else.
    staged = await git.run("diff", "--cached", "--name-only")
    assert staged.strip() == "tracked.txt"


async def test_capture_fails_closed_when_head_read_fails(repo, monkeypatch) -> None:
    git = Git(repo.root)
    run = git.run

    async def broken_head(*args, **kwargs):
        if args == ("rev-parse", "--verify", "-q", "HEAD"):
            from lazysnake.git.runner import GitError

            raise GitError(list(args), 128, "", "object database unavailable")
        return await run(*args, **kwargs)

    monkeypatch.setattr(git, "run", broken_head)
    with pytest.raises(GitError, match="object database unavailable"):
        await capture(git, None)


async def test_capture_fails_closed_when_index_or_stash_cannot_be_read(repo, monkeypatch) -> None:
    git = Git(repo.root)
    run = git.run

    async def broken_index(*args, **kwargs):
        if args == ("write-tree",) and "env_extra" not in kwargs:
            from lazysnake.git.runner import GitError

            raise GitError(list(args), 128, "", "index unreadable")
        return await run(*args, **kwargs)

    monkeypatch.setattr(git, "run", broken_index)
    with pytest.raises(GitError, match="index unreadable"):
        await capture(git, None)

    monkeypatch.setattr(git, "run", run)

    async def broken_stash(*args, **kwargs):
        if args[:2] == ("stash", "list"):
            from lazysnake.git.runner import GitError

            raise GitError(list(args), 128, "", "stash list unavailable")
        return await run(*args, **kwargs)

    monkeypatch.setattr(git, "run", broken_stash)
    with pytest.raises(GitError, match="stash list unavailable"):
        await capture(git, None)


async def test_restore_returns_to_saved_branch_without_rewinding_other_branch(repo) -> None:
    main_tip = repo.git("rev-parse", "HEAD").strip()
    git = Git(repo.root)
    snap = await capture(git, None)
    assert snap is not None

    repo.git("switch", "-c", "feature")
    repo.write("feature.txt", "advanced feature\n")
    repo.commit_all("advance feature")
    feature_tip = repo.git("rev-parse", "HEAD").strip()

    await restore(git, snap)

    assert repo.git("symbolic-ref", "--short", "HEAD").strip() == "main"
    assert repo.git("rev-parse", "main").strip() == main_tip
    assert repo.git("rev-parse", "feature").strip() == feature_tip


async def test_restore_does_not_rewind_an_unrelated_ref_advanced_after_capture(repo) -> None:
    repo.git("switch", "-c", "other")
    repo.write("other.txt", "first\n")
    repo.commit_all("other first")
    repo.git("switch", "main")
    git = Git(repo.root)
    snap = await capture(git, None)
    assert snap is not None

    repo.git("switch", "other")
    repo.write("other.txt", "second\n")
    repo.commit_all("other second")
    other_tip = repo.git("rev-parse", "other").strip()
    repo.git("switch", "main")
    await restore(git, snap)

    assert repo.git("rev-parse", "other").strip() == other_tip
    assert repo.git("symbolic-ref", "--short", "HEAD").strip() == "main"


async def test_restore_keeps_a_path_deleted_in_the_snapshot_deleted(repo) -> None:
    repo.write("removed.txt", "snapshot deletion\n")
    repo.commit_all("add removable file")
    repo.root.joinpath("removed.txt").unlink()
    git = Git(repo.root)
    snap = await capture(git, None)
    assert snap is not None
    diff_calls: list[tuple[str, ...]] = []
    run = git.run

    async def record_deletion_diff(*args, **kwargs):
        if args and args[0] == "diff" and "--diff-filter=D" in args:
            diff_calls.append(args)
        return await run(*args, **kwargs)

    git.run = record_deletion_diff  # type: ignore[method-assign]

    repo.write("removed.txt", "must not survive restore\n")
    await restore(git, snap)

    assert not (repo.root / "removed.txt").exists()
    assert diff_calls and diff_calls[0][-2:] == (snap.head, snap.worktree_tree)


async def test_restore_preserves_detached_head(repo) -> None:
    repo.git("switch", "--detach", "HEAD")
    detached_tip = repo.git("rev-parse", "HEAD").strip()
    git = Git(repo.root)
    snap = await capture(git, None)
    assert snap is not None

    repo.git("switch", "-c", "temporary")
    repo.write("temporary.txt", "temporary\n")
    repo.commit_all("temporary commit")
    await restore(git, snap)

    assert repo.git("rev-parse", "HEAD").strip() == detached_tip
    assert repo.git("symbolic-ref", "-q", "HEAD", check=False).strip() == ""


async def test_restore_unborn_empty_snapshot_without_root_pathspec(tmp_path) -> None:
    root = tmp_path / "empty-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "empty", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test Snake"], cwd=root, check=True)
    git = Git(root)
    snap = await capture(git, None)
    assert snap is not None

    (root / "after-snapshot.txt").write_text("new commit\n")
    await git.run("add", "after-snapshot.txt")
    await git.run("commit", "-m", "first commit after snapshot")
    calls: list[tuple[str, ...]] = []
    run = git.run

    async def record_restore(*args, **kwargs):
        if args and args[0] == "restore":
            calls.append(args)
        return await run(*args, **kwargs)

    git.run = record_restore  # type: ignore[method-assign]
    await restore(git, snap)

    assert not calls
    assert (await git.run("symbolic-ref", "--short", "HEAD")).strip() == "empty"
    assert not (root / "after-snapshot.txt").exists()
    show_ref = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/empty"], cwd=root
    )
    assert show_ref.returncode == 1


async def test_restore_unborn_branch_after_switching_away_preserves_other_ref(tmp_path) -> None:
    root = tmp_path / "unborn-repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test Snake"], cwd=root, check=True)
    git = Git(root)
    snap = await capture(git, None)
    assert snap.head is None
    assert snap.head_ref == "refs/heads/main"

    (root / "first.txt").write_text("first commit\n")
    await git.run("add", "first.txt")
    await git.run("commit", "-m", "first commit on main")
    repo_first_commit = (await git.run("rev-parse", "HEAD")).strip()
    await git.run("switch", "-c", "feature")
    feature_tip = (await git.run("rev-parse", "HEAD")).strip()

    await restore(git, snap)

    assert (await git.run("symbolic-ref", "--short", "HEAD")).strip() == "main"
    assert not (root / "first.txt").exists()
    show_main = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/main"], cwd=root
    )
    assert show_main.returncode == 1
    assert (await git.run("rev-parse", "feature")).strip() == feature_tip == repo_first_commit


async def test_capture_rejects_unreadable_nested_operation_state(repo, monkeypatch) -> None:
    git = Git(repo.root)
    git_dir = Path((await git.run("rev-parse", "--absolute-git-dir")).strip())
    nested = git_dir / "rebase-merge" / "nested"
    nested.mkdir(parents=True)
    (nested / "todo").write_text("pick deadbeef subject\n")
    real_scandir = os.scandir
    failed = False

    def denied_scandir(path):
        nonlocal failed
        if Path(path) == nested:
            failed = True
            raise PermissionError("nested operation state is unreadable")
        return real_scandir(path)

    monkeypatch.setattr(snapshot_module.os, "scandir", denied_scandir)
    with pytest.raises(GitError, match="nested operation state is unreadable"):
        await capture(git, None)
    assert failed


async def test_undo_restores_rebase_conflict_state_and_rebase_head(repo) -> None:
    repo.write("conflict.txt", "base\n")
    repo.commit_all("rebase base")
    repo.git("switch", "-c", "feature")
    repo.write("conflict.txt", "feature change\n")
    repo.commit_all("feature change")
    repo.git("switch", "main")
    repo.write("conflict.txt", "main change\n")
    repo.commit_all("main change")
    repo.git("switch", "feature")
    subprocess.run(
        ["git", "rebase", "--merge", "main"], cwd=repo.root, check=False, capture_output=True
    )

    git = Git(repo.root)
    git_dir = Path((await git.run("rev-parse", "--absolute-git-dir")).strip())
    rebase_dir = git_dir / "rebase-merge"
    rebase_head = git_dir / "REBASE_HEAD"
    assert rebase_dir.is_dir()
    expected_head = rebase_head.read_bytes()
    expected_state = {
        path.relative_to(rebase_dir).as_posix(): path.read_bytes()
        for path in rebase_dir.rglob("*")
        if path.is_file()
    }

    app = LazysnakeApp(git)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert app.snapshot.in_conflict
        entry = next(file for file in app.snapshot.conflict_files if file.path == "conflict.txt")
        await app.resolve_conflict_union(entry).wait()
        await pilot.pause()

        repo.git("rebase", "--abort")
        assert not rebase_dir.exists()
        assert not rebase_head.exists()
        await app.undo().wait()
        await pilot.pause()

    assert rebase_head.read_bytes() == expected_head
    restored_state = {
        path.relative_to(rebase_dir).as_posix(): path.read_bytes()
        for path in rebase_dir.rglob("*")
        if path.is_file()
    }
    assert restored_state == expected_state
    assert (await git.run("rev-parse", "REBASE_HEAD")).strip()
    restored = parse_status(await git.run("status", "--porcelain=v2", "--branch", "-z"))
    assert restored.in_conflict


async def test_undo_restores_deleted_branch_tip(repo) -> None:
    repo.git("switch", "-c", "keep-me")
    repo.write("feature.txt", "feature\n")
    repo.commit_all("feature commit")
    tip = repo.git("rev-parse", "HEAD").strip()
    repo.git("switch", "main")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        branch = next(branch for branch in app.branches if branch.name == "keep-me")

        await app.delete_branch(branch, force=True).wait()
        await pilot.pause()
        assert "keep-me" not in repo.git("branch", "--format=%(refname:short)")

        await app.undo().wait()
        await pilot.pause()

    assert repo.git("rev-parse", "keep-me").strip() == tip


async def test_union_resolution_undo_restores_conflict_index_and_merge_state(
    repo, monkeypatch
) -> None:
    repo.write("conflict.txt", "base\n")
    repo.commit_all("conflict base")
    repo.git("switch", "-c", "feature")
    repo.write("conflict.txt", "feature\n")
    repo.commit_all("feature side")
    repo.git("switch", "main")
    repo.write("conflict.txt", "main\n")
    repo.commit_all("main side")
    repo.git("merge", "feature", check=False)

    git = Git(repo.root)
    index_path = Path((await git.run("rev-parse", "--git-path", "index")).strip())
    if not index_path.is_absolute():
        index_path = repo.root / index_path
    merge_head = await git.state_file("MERGE_HEAD")
    before_merge_head = merge_head.read_bytes()
    before_file = (repo.root / "conflict.txt").read_bytes()
    restored_index: list[bytes] = []
    original_restore = app_module.restore

    async def observe_restored_index(git, snap):
        await original_restore(git, snap)
        restored_index.append(index_path.read_bytes())

    monkeypatch.setattr(app_module, "restore", observe_restored_index)

    app = LazysnakeApp(git)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        before_index = index_path.read_bytes()
        entry = next(file for file in app.snapshot.conflict_files if file.path == "conflict.txt")

        await app.resolve_conflict_union(entry).wait()
        await pilot.pause()
        assert not app.snapshot.in_conflict

        await app.undo().wait()
        await pilot.pause()

    assert restored_index == [before_index]
    assert merge_head.read_bytes() == before_merge_head
    assert (repo.root / "conflict.txt").read_bytes() == before_file
    restored = parse_status(await git.run("status", "--porcelain=v2", "--branch", "-z"))
    assert restored.in_conflict


async def test_undo_recovers_partial_restore_failure_and_keeps_retry_target(
    repo, monkeypatch
) -> None:
    repo.write("state.txt", "before\n")
    repo.git("add", "state.txt")
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.commit("action commit").wait()
        await pilot.pause()
        committed_tip = repo.git("rev-parse", "HEAD").strip()
        target: Snapshot = app._undo_stack[-1]
        original_restore = app_module.restore
        calls = 0

        async def partial_then_restore(git, snap):
            nonlocal calls
            calls += 1
            if calls == 1:
                (repo.root / "state.txt").write_text("partially restored\n")
                from lazysnake.git.runner import GitError

                raise GitError(["restore"], 128, "", "injected after mutation")
            await original_restore(git, snap)

        monkeypatch.setattr("lazysnake.ui.app.restore", partial_then_restore)
        await app.undo().wait()
        await pilot.pause()

        assert calls == 2  # failed target restore followed by current-state rollback
        assert repo.git("rev-parse", "HEAD").strip() == committed_tip
        assert (repo.root / "state.txt").read_text() == "before\n"
        assert app._undo_stack[-1] is target
        assert app._redo_stack == []


async def test_cancelled_undo_rolls_back_partial_restore(repo, monkeypatch) -> None:
    repo.write("state.txt", "before\n")
    repo.git("add", "state.txt")
    app = LazysnakeApp(Git(repo.root))
    entered_restore = asyncio.Event()

    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.commit("action commit").wait()
        await pilot.pause()
        current_tip = repo.git("rev-parse", "HEAD").strip()
        target = app._undo_stack[-1]
        original_restore = app_module.restore
        calls = 0

        async def pause_after_partial_restore(git, snap):
            nonlocal calls
            calls += 1
            if calls == 1:
                (repo.root / "state.txt").write_text("partially restored\n")
                entered_restore.set()
                await asyncio.Event().wait()
            await original_restore(git, snap)

        monkeypatch.setattr(app_module, "restore", pause_after_partial_restore)
        worker = app.undo()
        await entered_restore.wait()
        worker.cancel()
        try:
            await worker.wait()
        except WorkerCancelled:
            pass

        assert calls == 2
        assert repo.git("rev-parse", "HEAD").strip() == current_tip
        assert (repo.root / "state.txt").read_text() == "before\n"
        assert app._undo_stack[-1] is target
        assert app._redo_stack == []
        assert app._recovery_snapshots == []


async def test_checkpoint_failure_prevents_destructive_action(repo, monkeypatch) -> None:
    repo.write("staged.txt", "staged\n")
    repo.git("add", "staged.txt")
    before = repo.git("rev-parse", "HEAD").strip()
    app = LazysnakeApp(Git(repo.root))

    async def failed_capture(*args, **kwargs):
        raise GitError(["snapshot"], 128, "", "snapshot index could not be captured")

    monkeypatch.setattr(app_module, "capture", failed_capture)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.commit("must not run").wait()
        await pilot.pause()

    assert repo.git("rev-parse", "HEAD").strip() == before
    assert "staged.txt" in repo.git("diff", "--cached", "--name-only")


async def test_rebase_workers_checkpoint_before_history_mutation(repo) -> None:
    repo.write("one.txt", "one\n")
    repo.commit_all("first feature commit")
    repo.write("two.txt", "two\n")
    repo.commit_all("second feature commit")
    original_tip = repo.git("rev-parse", "HEAD").strip()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        target = app.commits[0]
        actions = (
            lambda: app._do_rebase_op(target, "drop").wait(),
            lambda: app.reword_commit(target, "rewritten subject").wait(),
            lambda: app.move_commit(target, "move-up").wait(),
            lambda: app.run_worker(
                app._run_todo_worker(
                    f"{target.sha}^",
                    RebaseTodoResult(ops={target.sha: "drop"}, order=[target.sha]),
                ),
                exclusive=True,
                group="action",
            ).wait(),
        )

        for run_action in actions:
            await run_action()
            await pilot.pause()
            assert app._undo_stack
            await app.undo().wait()
            await pilot.pause()
            assert repo.git("rev-parse", "HEAD").strip() == original_tip
            app._undo_stack.clear()
            app._redo_stack.clear()


async def test_ignore_file_is_checkpointed_and_undo_removes_new_file(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.ignore_file(FileEntry(path="ignored.txt", index_status="?")).wait()
        await pilot.pause()
        assert (repo.root / ".gitignore").read_text() == "ignored.txt\n"
        assert app._undo_stack

        await app.undo().wait()
        await pilot.pause()

    assert not (repo.root / ".gitignore").exists()


async def test_cancelled_ignore_write_finishes_before_next_write(repo, monkeypatch) -> None:
    import lazysnake.ui.actions.files as files_module

    started = threading.Event()
    release = threading.Event()
    calls = 0
    real_write = files_module._atomic_write_text

    def blocked_write(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            if not release.wait(5):
                raise TimeoutError("test write was not released")
        real_write(path, text)

    monkeypatch.setattr(files_module, "_atomic_write_text", blocked_write)
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        first = app.ignore_file(FileEntry(path="first.txt", index_status="?"))
        assert await asyncio.to_thread(started.wait, 2)

        first.cancel()
        second = app.ignore_file(FileEntry(path="second.txt", index_status="?"))
        release.set()
        await second.wait()
        await pilot.pause()

    assert (repo.root / ".gitignore").read_text().splitlines() == ["first.txt", "second.txt"]


async def test_cancelled_union_write_finishes_before_following_action(repo, monkeypatch) -> None:
    repo.write("conflict.txt", "base\n")
    repo.commit_all("conflict base")
    repo.git("switch", "-c", "feature")
    repo.write("conflict.txt", "feature\n")
    repo.commit_all("feature side")
    repo.git("switch", "main")
    repo.write("conflict.txt", "main\n")
    repo.commit_all("main side")
    repo.git("merge", "feature", check=False)

    import lazysnake.ui.actions.files as files_module

    started = threading.Event()
    release = threading.Event()
    calls = 0
    real_write = files_module._atomic_write_text

    def blocked_write(path: Path, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            if not release.wait(5):
                raise TimeoutError("test write was not released")
        real_write(path, text)

    monkeypatch.setattr(files_module, "_atomic_write_text", blocked_write)
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        entry = next(file for file in app.snapshot.conflict_files if file.path == "conflict.txt")
        first = app.resolve_conflict_union(entry)
        assert await asyncio.to_thread(started.wait, 2)

        first.cancel()
        second = app.ignore_file(FileEntry(path="ignored.txt", index_status="?"))
        release.set()
        try:
            await first.wait()
        except WorkerCancelled:
            pass
        await second.wait()
        await pilot.pause()

    assert (repo.root / "conflict.txt").read_text() == "main\nfeature\n"
    assert (repo.root / ".gitignore").read_text() == "ignored.txt\n"
