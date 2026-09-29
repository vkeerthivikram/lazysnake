"""Files/global feature extras: ignore, stash-with-message, copy, editor, context."""

from __future__ import annotations

from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp, build_editor_command

SEED = "\n".join(f"line {i}" for i in range(1, 22)) + "\n"


async def test_ignore_file(repo) -> None:
    repo.write("junk.log", "noise\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        junk = next(f for f in app.snapshot.files if f.path == "junk.log")

        await app.ignore_file(junk).wait()
        await pilot.pause()

        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert not any(f.path == "junk.log" for f in snap.files)
        assert "junk.log" in (repo.root / ".gitignore").read_text()


async def test_ignore_appends_to_existing_gitignore(repo) -> None:
    repo.write(".gitignore", "target/\n")
    repo.write("debug.log", "x\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        dbg = next(f for f in app.snapshot.files if f.path == "debug.log")
        await app.ignore_file(dbg).wait()
        await pilot.pause()
        content = (repo.root / ".gitignore").read_text()
        assert content == "target/\ndebug.log\n"


async def test_stash_with_message(repo) -> None:
    repo.write("README.md", "work in progress\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.stash_push(message="checkpoint before refactor").wait()
        await pilot.pause()
        listing = await app.git.run("stash", "list")
        assert "checkpoint before refactor" in listing
        assert len(app.stash) == 1


async def test_copy_path_uses_clipboard(repo) -> None:
    repo.write("README.md", "changed\n")
    app = LazysnakeApp(Git(repo.root))
    copied: list[str] = []
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        # Headless terminals have no clipboard; intercept the call.
        app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
        app.files_panel.action_copy_path()
        await pilot.pause()
        assert copied == ["README.md"]


def test_build_editor_command(monkeypatch) -> None:
    monkeypatch.setenv("EDITOR", '"My Editor" --wait')
    assert build_editor_command() == ["My Editor", "--wait"]

    monkeypatch.setenv("EDITOR", "vim")
    assert build_editor_command() == ["vim"]

    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    assert build_editor_command() is None


async def test_diff_context_changes_hunk_size(repo) -> None:
    repo.write("ctx.txt", SEED)
    repo.commit_all("seed")
    changed = (
        SEED.replace("line 3\n", "line 3 changed\n")
        .replace("line 12\n", "line 12 changed\n")
        .replace("line 20\n", "line 20 changed\n")
    )
    repo.write("ctx.txt", changed)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        app.action_context_down()  # context 3 -> 2
        app.action_context_down()  # -> 1
        await app.refresh_state().wait()
        await pilot.pause()
        rows_at_1 = len(list(app.diff_view.children))

        app.action_context_up()  # -> 2
        app.action_context_up()  # -> 3
        app.action_context_up()  # -> 4 (clamps at 9 later; fine)
        await app.refresh_main().wait()
        await pilot.pause()
        rows_at_4 = len(list(app.diff_view.children))

        assert rows_at_4 > rows_at_1
        assert app.diff_context == 4


async def test_context_clamped(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        for _ in range(20):
            app.action_context_up()
        assert app.diff_context == 9
        for _ in range(20):
            app.action_context_down()
        assert app.diff_context == 1
        await pilot.pause()
