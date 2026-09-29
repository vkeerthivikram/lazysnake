"""Headless UI smoke tests driving the real app with Textual's pilot."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp
from lazysnake.ui.panels import FileItem


def _file_items(app: LazysnakeApp) -> list[tuple[str, bool]]:
    return [
        (item.entry.path, item.staged_view)
        for item in app.files_panel.children
        if isinstance(item, FileItem)
    ]


async def test_app_boots_and_lists_files(repo) -> None:
    repo.write("README.md", "changed\n")
    repo.write("todo.txt", "brand new\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        assert app.snapshot.branch == "main"
        items = _file_items(app)
        assert ("README.md", False) in items
        assert ("todo.txt", False) in items
        assert app.files_panel.selected_item is not None
        # The interactive diff view is populated for the selection (README.md
        # is a tracked modification, so it renders as a navigable diff).
        assert app.diff_view.current_fd is not None
        assert len(list(app.diff_view.children)) > 0


async def test_space_stages_selected_file(repo) -> None:
    repo.write("README.md", "changed\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        assert app.files_panel.selected_item is not None
        await pilot.press("space")
        await pilot.pause()

        snap = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        )
        by_path = {f.path: f for f in snap.files}
        assert by_path["README.md"].display == "M "


async def test_toggle_stage_unstages(repo) -> None:
    repo.write("README.md", "changed\n")
    repo.git("add", "README.md")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.toggle_stage(app.files_panel.selected_item.entry, staged_view=True).wait()
        await pilot.pause()

        snap = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        )
        by_path = {f.path: f for f in snap.files}
        assert by_path["README.md"].display == " M"


async def test_commit_flow(repo) -> None:
    repo.write("README.md", "changed\n")
    repo.git("add", "README.md")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.commit("smoke commit message").wait()
        await pilot.pause()

        subject = (await app.git.run("log", "-1", "--pretty=%s")).strip()
        assert subject == "smoke commit message"


async def test_stage_all_and_unstage_all(repo) -> None:
    repo.write("README.md", "changed\n")
    repo.write("another.txt", "also changed\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.toggle_stage_all().wait()
        await pilot.pause()
        snap = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        )
        assert all(f.staged for f in snap.files)

        await app.toggle_stage_all().wait()
        await pilot.pause()
        snap = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        )
        assert not any(f.staged for f in snap.files)


async def test_discard_untracked(repo) -> None:
    repo.write("junk.txt", "throwaway\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        await app.discard(app.files_panel.selected_item.entry).wait()
        await pilot.pause()

        snap = parse_status(
            await app.git.run("status", "--porcelain=v2", "--branch", "-z")
        )
        assert not any(f.path == "junk.txt" for f in snap.files)
