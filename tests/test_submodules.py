"""Submodule manager: status parse, update, add, deinit, enter."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lazysnake.git.runner import Git
from lazysnake.git.submodule import submodules
from lazysnake.ui.app import LazysnakeApp


def _sg(sub: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(sub), *args], check=True, capture_output=True, text=True)


def _make_sub(sub: Path) -> None:
    sub.mkdir(parents=True)
    _sg(sub, "init", "-q", "-b", "main")
    _sg(sub, "config", "user.email", "t@e")
    _sg(sub, "config", "user.name", "T")
    (sub / "lib.txt").write_text("lib\n")
    _sg(sub, "add", "-A")
    _sg(sub, "commit", "-qm", "lib init")


def _add_submodule(repo, sub: Path) -> None:
    repo.git("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor/lib")
    repo.git("commit", "-qm", "add submodule")


async def test_submodule_state_parse(repo, tmp_path: Path) -> None:
    sub = tmp_path / "sublib"
    _make_sub(sub)
    _add_submodule(repo, sub)
    git = Git(repo.root)

    subs = await submodules(git)
    assert len(subs) == 1
    info = subs[0]
    assert info.path == "vendor/lib"
    assert info.state == "ok"

    # Deinitialized shows the '-' flag.
    repo.git("submodule", "deinit", "-f", "vendor/lib")
    subs = await submodules(git)
    assert subs[0].state == "not initialized"
    repo.git("submodule", "update", "--init", "vendor/lib")


async def test_submodule_update_via_app(repo, tmp_path: Path) -> None:
    sub = tmp_path / "sublib"
    _make_sub(sub)
    _add_submodule(repo, sub)
    repo.git("submodule", "deinit", "-f", "vendor/lib")
    assert not (repo.root / "vendor" / "lib" / "lib.txt").exists()

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.submodule_update().wait()
        await pilot.pause()
        assert (repo.root / "vendor" / "lib" / "lib.txt").read_text() == "lib\n"


async def test_submodule_add_via_app(repo, tmp_path: Path, eventually) -> None:
    sub = tmp_path / "fresh"
    _make_sub(sub)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.submodule_add(str(sub), "libs/fresh").wait()
        await eventually(lambda: (repo.root / "libs" / "fresh" / "lib.txt").exists())
        # The add commits itself.
        assert repo.git("log", "-1", "--pretty=%s").strip() == "Add submodule libs/fresh"
        await eventually(lambda: "libs/fresh" in app.submodules)


async def test_submodule_deinit_via_app(repo, tmp_path: Path) -> None:
    sub = tmp_path / "sublib"
    _make_sub(sub)
    _add_submodule(repo, sub)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app.submodule_deinit("vendor/lib").wait()
        # Worktree copy gone, .gitmodules still references it. Bounded
        # inline wait; on failure dump the real git state for diagnosis.
        import asyncio as _asyncio

        target = repo.root / "vendor" / "lib" / "lib.txt"
        for _ in range(300):
            if not target.exists():
                break
            await _asyncio.sleep(0.05)
        else:
            log = "\n".join(line.text for line in app.command_log.lines)
            status = subprocess.run(
                ["git", "-C", str(repo.root), "submodule", "status"],
                capture_output=True,
                text=True,
            )
            raise AssertionError(
                "deinit did not remove the worktree;\n"
                f"submodule status: {status.stdout}\ncommand log:\n{log}"
            )
        assert (repo.root / ".gitmodules").exists()


async def test_enter_submodule_uses_switch(repo, tmp_path: Path) -> None:
    sub = tmp_path / "sublib"
    _make_sub(sub)
    _add_submodule(repo, sub)

    app = LazysnakeApp(Git(repo.root))
    switched: list[str] = []
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        assert "vendor/lib" in app.submodules
        app.switch_repo = lambda p: switched.append(p)  # type: ignore[method-assign]
        from lazysnake.git.models import FileEntry

        app.enter_if_submodule(FileEntry(path="vendor/lib/"))
        assert switched == [str(repo.root / "vendor/lib")]
