"""Full-fidelity undo/redo: index split, deletions, untracked, stashes."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.git.snapshot import SNAPSHOT_REF, capture, restore
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp


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
