"""Worktrees, remote-branch panel, recent screen, submodule enter, H/L scroll."""

from __future__ import annotations

from pathlib import Path

from lazysnake.git.runner import Git
from lazysnake.ui.app import LazysnakeApp
from lazysnake.ui.modals import RecentReposScreen
from lazysnake.ui.panels import BranchItem


async def test_branches_panel_shows_remotes(remote_repo) -> None:
    remote_repo.git("push", "-u", "origin", "main")
    remote_repo.git("switch", "-c", "feature")
    remote_repo.write("f.txt", "f\n")
    remote_repo.commit_all("feature work")
    remote_repo.git("push", "-u", "origin", "feature")
    remote_repo.git("switch", "main")

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        items = [
            child.branch for child in app.branches_panel.children if isinstance(child, BranchItem)
        ]
        names = {b.name: b for b in items}
        assert "origin/main" in names
        assert "origin/feature" in names
        assert names["origin/main"].is_remote
        assert names["origin/main"].short_name == "main"
        assert "origin/HEAD" not in names  # symbolic ref filtered


async def test_checkout_remote_branch(remote_repo) -> None:
    remote_repo.git("push", "-u", "origin", "main")
    remote_repo.git("switch", "-c", "feature")
    remote_repo.write("f.txt", "f\n")
    remote_repo.commit_all("feature work")
    remote_repo.git("push", "-u", "origin", "feature")
    remote_repo.git("switch", "main")
    remote_repo.git("branch", "-D", "feature")  # remote-only now

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        item = next(
            child.branch
            for child in app.branches_panel.children
            if isinstance(child, BranchItem)
            and child.branch.is_remote
            and child.branch.name == "origin/feature"
        )
        await app.checkout_branch(item).wait()
        await pilot.pause()
        assert remote_repo.git("rev-parse", "--abbrev-ref", "HEAD").strip() == "feature"
        upstream = remote_repo.git("rev-parse", "--abbrev-ref", "feature@{upstream}").strip()
        assert upstream == "origin/feature"


async def test_delete_remote_branch(remote_repo) -> None:
    remote_repo.git("push", "-u", "origin", "main")
    remote_repo.git("switch", "-c", "doomed")
    remote_repo.write("d.txt", "d\n")
    remote_repo.commit_all("doomed work")
    remote_repo.git("push", "-u", "origin", "doomed")
    remote_repo.git("switch", "main")

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        doomed = next(
            child.branch
            for child in app.branches_panel.children
            if isinstance(child, BranchItem) and child.branch.name == "origin/doomed"
        )
        await app.delete_branch(doomed).wait()
        await pilot.pause()
        heads = remote_repo.git("ls-remote", "--heads", "origin")
        assert "doomed" not in heads


async def test_worktree_app_flow(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.worktree_add("topic").wait()
        await pilot.pause()

        wt_path = repo.root.parent / f"{repo.root.name}-topic"
        assert wt_path.exists()
        assert (wt_path / ".git").exists()
        # The main repo's status stays clean of the sibling worktree.
        # git prints forward slashes on every platform; Path str() does not.
        listing = repo.git("worktree", "list", "--porcelain")
        assert wt_path.as_posix() in listing.replace("\\", "/")

        await app.worktree_delete(str(wt_path)).wait()
        await pilot.pause()
        assert not wt_path.exists()


async def test_recent_screen_callback(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    picked: list[str] = []
    async with app.run_test() as pilot:
        screen = RecentReposScreen(["/a", "/b"], str(repo.root))
        app.push_screen(screen, lambda path: picked.append(path) if path else None)
        await pilot.pause()
        rows = [c for c in screen.query_one("#recent-list").children]
        assert len(rows) == 2
        screen.query_one("#recent-list").index = 1
        await pilot.pause()
        screen.action_choose()
        await pilot.pause()
        assert picked == ["/b"]


async def test_enter_submodule_switches(repo, tmp_path: Path) -> None:
    # Build a real submodule at a local path.
    sub = tmp_path / "sublib"
    sub.mkdir()
    import subprocess

    def sg(*args: str) -> None:
        subprocess.run(["git", "-C", str(sub), *args], check=True, capture_output=True, text=True)

    sg("init", "-q", "-b", "main")
    sg("config", "user.email", "t@e")
    sg("config", "user.name", "T")
    (sub / "lib.txt").write_text("lib\n")
    sg("add", "-A")
    sg("commit", "-qm", "lib init")

    repo.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/lib")
    repo.git("commit", "-qm", "add submodule")

    app = LazysnakeApp(Git(repo.root))
    switched: list[str] = []
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert "vendor/lib" in app.submodules

        app.switch_repo = lambda path: switched.append(path)  # type: ignore[method-assign]
        from lazysnake.git.models import FileEntry

        entry = FileEntry(path="vendor/lib")
        app.enter_if_submodule(entry)
        assert switched == [str(repo.root / "vendor/lib")]

        # Non-submodule paths get a notice instead.
        app.enter_if_submodule(FileEntry(path="README.md"))
        assert len(switched) == 1


async def test_diff_view_horizontal_scroll(repo) -> None:
    long_line = "x" * 400 + "\n"
    repo.write("wide.txt", "short\n")
    repo.commit_all("seed wide")
    repo.write("wide.txt", "short\n" + long_line)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test(size=(80, 24)) as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        app.action_focus_diff()
        await pilot.pause()
        before = app.diff_view.scroll_x
        app.diff_view.action_scroll_right()
        await pilot.pause()
        after = app.diff_view.scroll_x
        assert after > before  # the 400-char line must be horizontally scrollable
        app.diff_view.action_scroll_left()
        await pilot.pause()
        assert app.diff_view.scroll_x < after
