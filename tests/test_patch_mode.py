"""Patch mode: mark range, apply to worktree, cherry-pick range, continue."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp


def _seed_feature(repo) -> str:
    """Two commits on `feature`; returns to main. Returns feature tip sha."""
    repo.git("switch", "-c", "feature")
    repo.write("f1.txt", "feature one\n")
    repo.commit_all("feature commit one")
    repo.write("f2.txt", "feature two\n")
    repo.commit_all("feature commit two")
    tip = repo.git("rev-parse", "HEAD").strip()
    repo.git("switch", "main")
    return tip


async def _mark_range_on_feature(app) -> None:
    """Switch app to feature, mark the two feature commits as a range."""
    feature = next(b for b in app.branches if b.name == "feature")
    await app.checkout_branch(feature).wait()
    await app.refresh_state(force=True).wait()
    app.patch_mark(app.commits[1])  # older feature commit (anchor)
    app.patch_mark(app.commits[0])  # tip -> completes the range
    # Range survives a branch switch (shas are global).
    main = next(b for b in app.branches if b.name == "main")
    await app.checkout_branch(main).wait()


async def test_patch_apply_range_to_worktree(repo) -> None:
    _seed_feature(repo)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await _mark_range_on_feature(app)

        assert len(app.patch_range) == 2
        assert "patch(2)" in app.commits_panel.border_title

        await app.patch_apply().wait()
        await pilot.pause()

        # Both feature file contents now sit unstaged on main.
        assert (repo.root / "f1.txt").read_text() == "feature one\n"
        assert (repo.root / "f2.txt").read_text() == "feature two\n"
        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        unstaged = {f.path for f in snap.files if f.unstaged and not f.staged}
        assert unstaged == {"f1.txt", "f2.txt"}
        # main history untouched.
        subjects = [c.subject for c in app.commits]
        assert "feature commit two" not in subjects


async def test_patch_cherry_range_commits(repo) -> None:
    _seed_feature(repo)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await _mark_range_on_feature(app)

        await app.patch_cherry().wait()
        await pilot.pause()

        subjects = [c.subject for c in app.commits]
        assert subjects[0] == "feature commit two"
        assert subjects[1] == "feature commit one"
        # Range cleared after a successful cherry-pick.
        assert app.patch_range == []
        assert "patch" not in app.commits_panel.border_title
        assert (repo.root / "f1.txt").exists()


async def test_patch_clear(repo) -> None:
    _seed_feature(repo)
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        app.patch_mark(app.commits[0])
        assert len(app.patch_range) == 1
        app.patch_clear()
        assert app.patch_range == []
        assert app.patch_anchor is None
        assert "patch" not in app.commits_panel.border_title


async def test_cherry_pick_conflict_then_smart_continue(repo) -> None:
    """R must finish a conflicted cherry-pick, not just rebases."""
    repo.git("switch", "-c", "side")
    repo.write("README.md", "their line\n")
    repo.commit_all("their change")
    their_sha = repo.git("rev-parse", "HEAD").strip()
    repo.git("switch", "main")
    repo.write("README.md", "our line\n")
    repo.commit_all("our change")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        from lazysnake.git.log import Commit

        theirs = Commit(
            sha=their_sha,
            short_sha=their_sha[:7],
            author="T",
            timestamp=0,
            subject="their change",
            refs="",
        )
        try:
            await app.cherry_pick(theirs).wait()
        except Exception:
            pass  # cherry-pick exits non-zero on conflict; state is what matters
        await pilot.pause()

        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert snap.in_conflict
        assert app.snapshot.cherry_picking

        await app.resolve_conflict(snap.conflict_files[0], "ours").wait()
        await pilot.pause()
        await app._sequencer_continue()  # plain coroutine, not a worker
        await pilot.pause()

        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert not snap.in_conflict
        assert not app.snapshot.cherry_picking
        # The ours-resolution made the pick empty; the sequencer skipped it,
        # so our own commit stays on top.
        assert repo.git("log", "-1", "--pretty=%s").strip() == "our change"


async def test_rebase_continue_still_works(repo) -> None:
    """The smart continue must not break plain rebase flows."""
    repo.write("one.txt", "1\n")
    repo.commit_all("commit one")
    repo.write("two.txt", "2\n")
    repo.commit_all("commit two")
    two_sha = repo.git("log", "--pretty=%H", "--grep=^commit two$", "-1").strip()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        commit = next(c for c in app.commits if c.sha == two_sha)
        await app._do_rebase_op(commit, "drop").wait()
        await pilot.pause()
        subjects = [c.subject for c in app.commits]
        assert subjects == ["commit one", "initial commit"]
