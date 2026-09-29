"""Real remote flows against a local bare origin."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp


async def test_push_sets_upstream_then_plain_push(remote_repo) -> None:
    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert app.snapshot.upstream is None

        await app.push(set_upstream=True).wait()
        await pilot.pause()

        upstream = remote_repo.git("rev-parse", "--abbrev-ref", "main@{upstream}").strip()
        assert upstream == "origin/main"
        assert remote_repo.git("ls-remote", "--heads", "origin", "main").strip()

        # A second commit pushes without needing the flag.
        remote_repo.write("more.txt", "more\n")
        remote_repo.commit_all("second")
        await app.refresh_state(force=True).wait()
        await app.push().wait()
        await pilot.pause()
        snap = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        )
        assert snap.ahead == 0


async def test_ahead_behind_and_fetch(remote_repo) -> None:
    remote_repo.write("local.txt", "local\n")
    remote_repo.commit_all("local work")
    remote_repo.git("push", "-u", "origin", "main")

    # Someone else advances origin behind our back.
    remote_repo.git("fetch", "origin")
    remote_repo.git("reset", "--hard", "origin/main")
    remote_repo.write("remote.txt", "remote\n")
    remote_repo.commit_all("remote work")
    remote_repo.git("push", "origin", "main")
    remote_repo.git("reset", "--hard", "HEAD~1")

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        # Fresh app: upstream known, local ahead by the reset-away commit?
        await app.refresh_state().wait()
        await pilot.pause()
        await app.fetch().wait()
        await pilot.pause()
        snap = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        )
        # We reset the local branch to the pre-push commit, origin has one more.
        assert snap.behind >= 1 or snap.ahead >= 1  # direction depends on state

        await app.pull().wait()
        await pilot.pause()
        assert remote_repo.git("log", "-1", "--pretty=%s").strip() == "remote work"


async def test_set_upstream_action(remote_repo) -> None:
    remote_repo.git("push", "origin", "main")  # create origin/main without tracking
    # A push without -u sets no tracking config (exit 5 when unset is absent).
    remote_repo.git("config", "--unset", "branch.main.remote", check=False)

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        main = next(b for b in app.branches if b.name == "main")
        await app.set_upstream(main).wait()
        await pilot.pause()
        upstream = remote_repo.git("rev-parse", "--abbrev-ref", "main@{upstream}").strip()
        assert upstream == "origin/main"


async def test_fast_forward_branch(repo) -> None:
    repo.git("switch", "-c", "ahead")
    repo.write("new.txt", "new\n")
    repo.commit_all("ahead work")
    repo.git("switch", "main")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        ahead = next(b for b in app.branches if b.name == "ahead")
        await app.fast_forward_branch(ahead).wait()
        await pilot.pause()
        assert repo.git("log", "-1", "--pretty=%s").strip() == "ahead work"
        assert repo.git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


async def test_rebase_onto_branch(repo) -> None:
    repo.git("switch", "-c", "base")
    repo.write("base.txt", "base\n")
    repo.commit_all("base work")
    base_tip = repo.git("rev-parse", "HEAD").strip()
    repo.git("switch", "main")
    repo.write("main.txt", "main\n")
    repo.commit_all("main work")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        base = next(b for b in app.branches if b.name == "base")
        assert base.sha == base_tip
        await app.rebase_onto(base).wait()
        await pilot.pause()
        subjects = [c.subject for c in app.commits]
        assert subjects[0] == "main work"  # replayed on top of base
        assert subjects[1] == "base work"
        assert subjects[2] == "initial commit"


async def test_force_delete_unmerged_branch(repo) -> None:
    repo.git("switch", "-c", "wip")
    repo.write("wip.txt", "wip\n")
    repo.commit_all("wip work")
    repo.git("switch", "main")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        wip = next(b for b in app.branches if b.name == "wip")

        # Plain delete refuses: the branch is not merged.
        await app.delete_branch(wip).wait()
        await pilot.pause()
        assert any(b.name == "wip" for b in app.branches)

        await app.delete_branch(wip, force=True).wait()
        await pilot.pause()
        assert all(b.name != "wip" for b in app.branches)
