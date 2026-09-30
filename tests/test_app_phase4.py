"""Phase-4 flows: partial staging UI, amend, cherry-pick, rebase ops."""

from __future__ import annotations

from lazysnake.git.log import Commit
from lazysnake.git.runner import Git
from lazysnake.rebase import rewrite_todo
from lazysnake.ui.app import LazysnakeApp
from lazysnake.ui.panels import DiffRow

SEED = "\n".join(f"line {i}" for i in range(1, 16)) + "\n"


async def _two_hunks(repo) -> None:
    repo.write("file.txt", SEED)
    repo.commit_all("seed")
    repo.write(
        "file.txt",
        SEED.replace("line 2\n", "line 2 changed\n").replace("line 14\n", "line 14 changed\n"),
    )


async def test_stage_hunk_through_diff_view(repo, eventually) -> None:
    await _two_hunks(repo)
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        # Focus the interactive diff (key "5") and land on the first hunk;
        # on slow CI the diff render lands slightly after the refresh.
        app.action_focus_diff()

        def hunks_ready() -> int:
            fd = app.diff_view.current_fd
            return len(fd.hunks) if fd is not None else 0

        await eventually(lambda: hunks_ready() == 2)

        app.diff_view.index = 1  # first content row of hunk 0
        await pilot.pause()
        row = app.diff_view.selected_row
        assert isinstance(row, DiffRow) and row.hunk_index == 0

        await app.stage_from_diff("hunk").wait()
        await pilot.pause()

        staged = await app.git.run("diff", "--no-color", "--cached")
        assert "+line 2 changed" in staged
        assert "line 14 changed" not in staged
        # Worktree still carries both changes.
        worktree = await app.git.run("diff", "--no-color")
        assert "line 14 changed" in worktree


async def test_stage_single_line_through_diff_view(repo, eventually) -> None:
    await _two_hunks(repo)
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        app.action_focus_diff()
        await eventually(lambda: app.diff_view.current_fd is not None)

        # Move to the "+line 2 changed" row.
        for i, child in enumerate(app.diff_view.children):
            if isinstance(child, DiffRow) and child.line_index is not None:
                pl = app.diff_view.current_fd.hunks[child.hunk_index].lines[child.line_index]
                if pl.kind.name == "ADDITION" and pl.content == "line 2 changed":
                    app.diff_view.index = i
                    break
        await pilot.pause()

        await app.stage_from_diff("line").wait()
        await pilot.pause()
        staged = await app.git.run("diff", "--no-color", "--cached")
        assert "+line 2 changed" in staged
        assert "line 14 changed" not in staged


async def test_amend_flow(repo) -> None:
    repo.write("README.md", "changed\n")
    repo.git("add", "-A")
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.commit("amended message", amend=True).wait()
        await pilot.pause()
        subject = (await app.git.run("log", "-1", "--pretty=%s")).strip()
        assert subject == "amended message"
        assert (await app.git.run("rev-list", "--count", "HEAD")).strip() == "1"


async def test_cherry_pick_flow(repo, eventually) -> None:
    repo.git("switch", "-c", "source")
    repo.write("gift.txt", "from source branch\n")
    repo.commit_all("gift commit")
    gift_sha = repo.git("rev-parse", "HEAD").strip()
    repo.git("switch", "main")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        # The gift commit is only reachable from `source`, so build the model
        # directly instead of looking it up in main's log.
        gift = Commit(
            sha=gift_sha,
            short_sha=gift_sha[:7],
            author="Test Snake",
            timestamp=0,
            subject="gift commit",
            refs="",
        )
        await app.cherry_pick(gift).wait()
        # The worker's panel refresh can land a beat later on slow CI.
        await eventually(lambda: bool(app.commits) and app.commits[0].subject == "gift commit")
        subjects = [c.subject for c in app.commits]
        assert subjects[0] == "gift commit"


async def test_rebase_drop_commit(repo) -> None:
    repo.write("one.txt", "1\n")
    repo.commit_all("commit one")
    repo.write("two.txt", "2\n")
    repo.commit_all("commit two")
    repo.write("three.txt", "3\n")
    repo.commit_all("commit three")
    two_sha = repo.git("log", "--pretty=%H", "--grep=^commit two$", "-1").strip()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        commit = next(c for c in app.commits if c.sha == two_sha)
        await app._do_rebase_op(commit, "drop").wait()
        await pilot.pause()
        subjects = [c.subject for c in app.commits]
        assert subjects == ["commit three", "commit one", "initial commit"]
        assert (repo.root / "three.txt").exists()
        assert (repo.root / "one.txt").exists()
        assert not (repo.root / "two.txt").exists()


async def test_rebase_squash_commit(repo) -> None:
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
        await app._do_rebase_op(commit, "squash").wait()
        await pilot.pause()
        subjects = [c.subject for c in app.commits]
        assert subjects == ["commit one", "initial commit"]
        assert (await app.git.run("rev-list", "--count", "HEAD")).strip() == "2"
        assert (repo.root / "two.txt").exists()


def test_rewrite_todo_prefix_matching() -> None:
    todo = "pick abc1234 first\npick def5678 second\npick 901abcd third\n"
    out = rewrite_todo(todo, {"def56789abcdef": "drop", "901abcd": "squash"})
    lines = out.splitlines()
    assert lines[0] == "pick abc1234 first"
    assert lines[1].startswith("drop def5678")
    assert lines[2].startswith("squash 901abcd")
