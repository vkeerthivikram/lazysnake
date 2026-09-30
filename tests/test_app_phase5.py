"""Phase-5 flows: merge conflicts and user config."""

from __future__ import annotations

from pathlib import Path

from lazysnake.config import Config, load_config
from lazysnake.git.runner import Git
from lazysnake.git.status import merge_in_progress, parse_status
from lazysnake.ui.app import LazysnakeApp


async def _make_conflict(repo) -> None:
    repo.git("switch", "-c", "other")
    repo.write("README.md", "their version\n")
    repo.commit_all("their change")
    repo.git("switch", "main")
    repo.write("README.md", "our version\n")
    repo.commit_all("our change")


async def test_merge_conflict_and_resolve(repo) -> None:
    await _make_conflict(repo)

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        # Merge `other` into main — conflicts expected.
        other = next(b for b in app.branches if b.name == "other")
        await app.merge_branch(other).wait()
        await pilot.pause()

        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert snap.in_conflict
        assert await merge_in_progress(app.git)
        conflicted = snap.conflict_files[0]
        assert conflicted.path == "README.md"

        # Resolve keeping our version, then conclude the merge with a commit.
        await app.resolve_conflict(conflicted, "ours").wait()
        await pilot.pause()
        await app.commit("merge done").wait()
        await pilot.pause()

        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        assert not snap.in_conflict
        assert not await merge_in_progress(app.git)
        assert (repo.root / "README.md").read_text() == "our version\n"
        subjects = [c.subject for c in app.commits]
        assert subjects[0] == "merge done"


async def test_merge_clean(repo) -> None:
    repo.git("switch", "-c", "feature")
    repo.write("extra.txt", "no conflict\n")
    repo.commit_all("feature work")
    repo.git("switch", "main")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        feature = next(b for b in app.branches if b.name == "feature")
        await app.merge_branch(feature).wait()
        await pilot.pause()
        subjects = [c.subject for c in app.commits]
        # main had no divergent commits, so git fast-forwarded.
        assert subjects[0] == "feature work"


def test_config_defaults_when_missing(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg == Config()


def test_config_reads_values(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("sidebar_width = 60\npoll_seconds = 5\nlog_limit = 100\n")
    cfg = load_config(target)
    assert cfg.sidebar_width == 60
    assert cfg.poll_seconds == 5.0
    assert cfg.log_limit == 100


def test_config_tolerates_garbage(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("sidebar_width = <<not toml>>\n")
    cfg = load_config(target)
    assert cfg == Config()

    target.write_text('sidebar_width = "wide"\npoll_seconds = -3\n')
    cfg = load_config(target)
    # bad type falls back; out-of-range is clamped, not fatal
    assert cfg.sidebar_width == 46
    assert cfg.poll_seconds == 0.5
