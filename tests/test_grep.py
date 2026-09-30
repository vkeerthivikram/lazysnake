"""Grep mode: results screen, jump-to-hunk, jump+stage."""

from __future__ import annotations

from textual.widgets import Label

from lazysnake.git.runner import Git
from lazysnake.git.status import parse_status
from lazysnake.ui.app import LazysnakeApp
from lazysnake.ui.modals import GrepMatch, GrepScreen
from lazysnake.ui.panels import DiffRow


def _row_lines(app) -> list[tuple[str, int]]:
    out = []
    for child in app.diff_view.children:
        if isinstance(child, DiffRow) and child.line_index is not None:
            pl = app.diff_view.current_fd.hunks[child.hunk_index].lines[child.line_index]
            if pl.new_no is not None:
                out.append((pl.content, pl.new_no))
    return out


async def test_grep_jump_lands_cursor_on_matched_line(repo) -> None:
    repo.write("needle.py", "unrelated top\nfind me here\nbottom\n")
    repo.commit_all("seed")
    repo.write("needle.py", "unrelated top\nfind me HERE now\nbottom\nmore\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        picked: list[GrepMatch] = []
        screen = GrepScreen([("needle.py", 2, "find me HERE now")], "HERE")
        app.push_screen(
            screen, lambda sel: (picked.append(sel), app._jump_to_match(sel)) if sel else None
        )
        await pilot.pause()
        screen.query_one("#grep-list").index = 0
        await pilot.pause()
        screen.action_jump()
        for _ in range(20):
            await pilot.pause()

        assert picked and picked[0].path == "needle.py"
        assert picked[0].line == 2
        # Files panel filtered to the match, diff cursor on the changed line.
        assert app.files_filter == "needle.py"
        rows = _row_lines(app)
        assert any("find me HERE now" in content and no == 2 for content, no in rows)


async def test_grep_jump_stage_stages_file(repo) -> None:
    repo.write("target.py", "value = 1\n")
    repo.commit_all("seed")
    repo.write("target.py", "value = 42\n")

    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()

        screen = GrepScreen([("target.py", 1, "value = 42")], "42")
        app.push_screen(screen, lambda sel: app._jump_to_match(sel) if sel else None)
        await pilot.pause()
        screen.action_jump_stage()
        for _ in range(20):
            await pilot.pause()

        snap = parse_status(await app.git.run("status", "--porcelain=v2", "--branch", "-z"))
        by_path = {f.path: f for f in snap.files}
        assert by_path["target.py"].display == "M "


async def test_grep_no_match_notifies(repo) -> None:
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        # Must not raise; screen simply doesn't open.
        await app._grep_flow("zzz-no-such-thing")
        await pilot.pause()


async def test_grep_drains_all_output_without_retaining_more_than_200_matches(
    repo, monkeypatch
) -> None:
    count = 750
    repo.write("many.txt", "".join(f"needle {line}\n" for line in range(count)))
    repo.commit_all("many grep matches")
    app = LazysnakeApp(Git(repo.root))
    real_stream = app.git.run_streaming
    observed = 0
    combined: list[str] = []

    async def observe_stream(*args, **kwargs):
        nonlocal observed
        assert kwargs.get("capture_output") is False
        on_line = kwargs["on_line"]

        def count_lines(text: str, is_stderr: bool) -> None:
            nonlocal observed
            observed += 1
            on_line(text, is_stderr)

        kwargs["on_line"] = count_lines
        code, output = await real_stream(*args, **kwargs)
        combined.append(output)
        return code, output

    monkeypatch.setattr(app.git, "run_streaming", observe_stream)
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app._grep_flow("needle")
        await pilot.pause()

        assert isinstance(app.screen, GrepScreen)
        assert len(app.screen.matches) == 200
        assert app.screen.truncated
        heading = app.screen.query_one(Label)
        assert "truncated" in str(heading.render())

    assert observed == count
    assert combined == [""]


async def test_grep_preserves_colon_laden_match_content(repo) -> None:
    repo.write("colon.txt", "needle:42:tail\n")
    repo.commit_all("colon-laden grep content")
    app = LazysnakeApp(Git(repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        await app._grep_flow("needle")
        await pilot.pause()

        assert isinstance(app.screen, GrepScreen)
        assert app.screen.matches == [("colon.txt", 1, "needle:42:tail")]
