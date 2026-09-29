"""History surgery: reword, move up/down, fixup, checkout commit, undo."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.rebase import rewrite_todo
from lazysnake.ui.app import LazysnakeApp


def _subjects(app) -> list[str]:
    return [c.subject for c in app.commits]


async def test_reword_head(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.reword_commit(app.commits[0], "better words").wait()
        await pilot.pause()
        assert repo.git("log", "-1", "--pretty=%s").strip() == "better words"
        assert (await app.git.run("rev-list", "--count", "HEAD")).strip() == "1"


async def test_reword_older_commit(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.commit_all("alpha")
    repo.write("b.txt", "b\n")
    repo.commit_all("beta")
    beta_sha = repo.git("log", "--pretty=%H", "--grep=^beta$", "-1").strip()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        target = next(c for c in app.commits if c.sha == beta_sha)
        await app.reword_commit(target, "beta (reworded)").wait()
        await pilot.pause()
        assert repo.git("log", "--pretty=%s", "--grep=^beta", "-1").strip() == "beta (reworded)"
        assert (await app.git.run("rev-list", "--count", "HEAD")).strip() == "3"


async def test_fixup_commit(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.commit_all("alpha")
    repo.write("a.txt", "a\nmore\n")
    repo.git("add", "-A")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        alpha = app.commits[0]
        await app.fixup_commit(alpha).wait()
        await pilot.pause()
        subject = repo.git("log", "-1", "--pretty=%s").strip()
        assert subject == f"fixup! {alpha.subject}"


async def test_move_commit_up_and_down(repo) -> None:
    repo.write("one.txt", "1\n")
    repo.commit_all("commit one")
    repo.write("two.txt", "2\n")
    repo.commit_all("commit two")
    repo.write("three.txt", "3\n")
    repo.commit_all("commit three")
    three_sha = repo.git("rev-parse", "HEAD").strip()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert _subjects(app) == ["commit three", "commit two", "commit one", "initial commit"]

        three = next(c for c in app.commits if c.sha == three_sha)
        await app.move_commit(three, "move-up").wait()
        await pilot.pause()
        assert _subjects(app) == ["commit two", "commit three", "commit one", "initial commit"]

        three = next(c for c in app.commits if c.sha and c.subject == "commit three")
        await app.move_commit(three, "move-down").wait()
        await pilot.pause()
        assert _subjects(app) == ["commit three", "commit two", "commit one", "initial commit"]


async def test_checkout_commit_detached(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.commit_all("alpha")
    alpha_sha = repo.git("rev-parse", "HEAD").strip()
    repo.write("b.txt", "b\n")
    repo.commit_all("beta")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        alpha = next(c for c in app.commits if c.sha == alpha_sha)
        await app.checkout_detached(alpha.sha, label=alpha.short_sha).wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == alpha_sha
        assert not (repo.root / "b.txt").exists()
        # Coming back is one keypress away.
        main = next(b for b in app.branches if b.name == "main")
        await app.checkout_branch(main).wait()
        await pilot.pause()
        assert (repo.root / "b.txt").exists()


async def test_undo_via_reflog(repo) -> None:
    before = repo.git("rev-parse", "HEAD").strip()
    repo.write("a.txt", "a\n")
    repo.git("add", "-A")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.commit("alpha").wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() != before

        await app.undo().wait()
        await pilot.pause()
        assert repo.git("rev-parse", "HEAD").strip() == before
        assert repo.git("log", "-1", "--pretty=%s").strip() == "initial commit"


def test_rewrite_todo_moves() -> None:
    todo = "pick aaa1111 first\npick bbb2222 second\npick ccc3333 third\n"
    up = rewrite_todo(todo, {"bbb2222": "move-up"})
    assert up.splitlines()[0] == "pick bbb2222 second"
    assert up.splitlines()[1] == "pick aaa1111 first"
    assert up.splitlines()[2] == "pick ccc3333 third"

    down = rewrite_todo(todo, {"bbb2222": "move-down"})
    assert down.splitlines()[0] == "pick aaa1111 first"
    assert down.splitlines()[1] == "pick ccc3333 third"
    assert down.splitlines()[2] == "pick bbb2222 second"

    # Moving the first entry up is a no-op, not a crash.
    assert rewrite_todo(todo, {"aaa1111": "move-up"}) == todo
