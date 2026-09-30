"""Streaming remote operations: live output lands in the command log."""

from __future__ import annotations

from lazysnake.git.runner import Git, GitError
from lazysnake.ui.app import LazysnakeApp


def _log_text(app) -> str:
    return "\n".join(line.text for line in app.command_log.lines)


async def test_run_streaming_collects_output(remote_repo) -> None:
    git = Git(remote_repo.root)
    lines: list[tuple[str, bool]] = []
    code, combined = await git.run_streaming(
        "push", "--porcelain", "origin", "main", on_line=lambda t, e: lines.append((t, e))
    )
    assert code == 0
    assert lines, "porcelain push must emit at least one line"
    assert any("refs/heads/main" in text for text, _ in lines)
    assert combined == "\n".join(text for text, _ in lines)


async def test_push_streams_into_command_log(remote_repo) -> None:
    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        code = await app.push(set_upstream=True).wait()
        await pilot.pause()
        assert code == 0
        log = _log_text(app)
        assert "▸ git push --progress" in log
        assert "main -> main" in log  # streamed line, not just the header
        assert "✓ git push --progress" in log  # verdict after completion


async def test_failed_push_marks_failure_in_log(remote_repo) -> None:
    # Diverged remote: push (no force) must fail and the log must say so.
    remote_repo.git("push", "-u", "origin", "main")
    remote_repo.git("commit", "--allow-empty", "-qm", "remote-side")
    remote_repo.git("push", "origin", "main")
    remote_repo.git("reset", "--hard", "HEAD~1")
    remote_repo.write("local.txt", "local\n")
    remote_repo.commit_all("local side")

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        code = await app.push().wait()
        await pilot.pause()
        assert code != 0
        log = _log_text(app)
        assert "✗ git push --progress" in log
        assert "rejected" in log or "fetch first" in log


async def test_pull_streams_and_applies(remote_repo) -> None:
    remote_repo.git("push", "-u", "origin", "main")
    remote_repo.git("commit", "--allow-empty", "-qm", "from origin")
    remote_repo.git("push", "origin", "main")
    remote_repo.git("reset", "--hard", "HEAD~1")

    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        code = await app.pull().wait()
        await pilot.pause()
        assert code == 0
        assert remote_repo.git("log", "-1", "--pretty=%s").strip() == "from origin"
        assert "▸ git pull" in _log_text(app)


async def test_fetch_streams(remote_repo) -> None:
    remote_repo.git("push", "-u", "origin", "main")
    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        code = await app.fetch().wait()
        await pilot.pause()
        assert code == 0
        assert "▸ git fetch --all --progress" in _log_text(app)


async def test_remote_stream_giterror_returns_failure_logs_and_refreshes(
    remote_repo, monkeypatch
) -> None:
    remote_repo.git("push", "-u", "origin", "main")
    app = LazysnakeApp(Git(remote_repo.root))
    async with app.run_test() as pilot:
        await app.refresh_state().wait()
        await pilot.pause()
        original_refresh = app.refresh_state
        refreshes = 0

        def count_refresh(*args, **kwargs):
            nonlocal refreshes
            refreshes += 1
            return original_refresh(*args, **kwargs)

        async def failed_stream(*args, **kwargs):
            raise GitError(list(args), -1, "", "transport timed out")

        monkeypatch.setattr(app, "refresh_state", count_refresh)
        monkeypatch.setattr(app.git, "run_streaming", failed_stream)
        # The 2s poll timer also calls refresh_state; neutralize it so the
        # count below stays exact on any runner speed.
        monkeypatch.setattr(app, "_poll_status", lambda: None)

        for action, expected in (
            (app.fetch, "git fetch --all --progress"),
            (app.pull, "git pull"),
            (app.push, "git push --progress"),
        ):
            code = await action().wait()
            await pilot.pause()
            assert code != 0
            log = _log_text(app)
            assert f"✗ {expected}" in log
            assert "transport timed out" in log

        assert refreshes == 3
