"""Phase-3 app flows: branches, commits, stash panels wired end to end."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp


async def test_branch_checkout_flow(repo) -> None:
    repo.git("switch", "-c", "feature/x")
    repo.write("README.md", "changed on feature\n")
    repo.commit_all("feature commit")
    repo.git("switch", "main")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        branches = {b.name: b for b in app.branches}
        assert set(branches) == {"main", "feature/x"}
        assert app.branches_panel.selected_item is not None
        assert app.branches_panel.selected_item.branch.is_head

        # Select feature/x by name (headers make positional guesses fragile).
        from lazysnake.ui.panels import BranchItem

        rows = [
            (i, child)
            for i, child in enumerate(app.branches_panel.children)
            if isinstance(child, BranchItem)
        ]
        feature_row = next((i, c) for i, c in rows if c.branch.name == "feature/x")
        app.branches_panel.index = feature_row[0]
        await pilot.pause()
        target = app.branches_panel.selected_item.branch.name
        assert target == "feature/x"
        await app.checkout_branch(branches["feature/x"]).wait()
        await pilot.pause()

        head = (await app.git.run("rev-parse", "--abbrev-ref", "HEAD")).strip()
        assert head == "feature/x"


async def test_create_and_delete_branch(repo, eventually) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.create_branch("topic/y").wait()
        await eventually(lambda: any(b.name == "topic/y" and b.is_head for b in app.branches))
        head = (await app.git.run("rev-parse", "--abbrev-ref", "HEAD")).strip()
        assert head == "topic/y"

        # Switch back to main, then delete topic/y.
        await app.checkout_branch(next(b for b in app.branches if b.name == "main")).wait()
        await eventually(lambda: app.snapshot.branch == "main")
        topic = next(b for b in app.branches if b.name == "topic/y")
        await app.delete_branch(topic).wait()
        await eventually(lambda: all(b.name != "topic/y" for b in app.branches))


async def test_commits_panel_and_main_view(repo) -> None:
    repo.write("a.txt", "a\n")
    repo.commit_all("second commit")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        assert [c.subject for c in app.commits] == ["second commit", "initial commit"]
        assert app.commits[0].refs  # HEAD -> main decoration present

        # Focus the commits panel (like pressing "3"), then select a commit:
        # the main view must route to commit detail.
        app.action_focus_panel("commits")
        app.commits_panel.index = 0
        await pilot.pause()
        await app.refresh_main().wait()
        content = app.main_view.lines
        joined = "".join(seg.text for line in content for seg in line)
        assert "second commit" in joined
        assert "commit " in joined


async def test_stash_flow(repo) -> None:
    repo.write("README.md", "stash me\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.stash_push().wait()
        await pilot.pause()
        assert len(app.stash) == 1
        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert not any(f.path == "README.md" for f in snap.files)

        await app.pop_stash(app.stash[0]).wait()
        await pilot.pause()
        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        by_path = {f.path: f for f in snap.files}
        assert by_path["README.md"].display == " M"
        assert app.stash == []


async def test_pull_without_upstream_warns(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        # No upstream configured: pull must not run git and must not raise.
        await app.pull().wait()
        await pilot.pause()
        # Status bar still healthy.
        assert app.snapshot.branch == "main"
