"""Runner mechanics: oversized stream lines, cancellation reaping, timeouts."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest import mock

import pytest

import lazysnake.git.runner as runner_module
from lazysnake.git.runner import Git, GitError

_SPAWN = "lazysnake.git.runner.asyncio.create_subprocess_exec"


class FakeProcess:
    """Subprocess stand-in that hangs until killed and records kill()/wait()."""

    def __init__(self) -> None:
        self.killed = False
        self.wait_calls = 0
        self.returncode = 0
        self.pid = 4242
        self.stdout = None
        self.stderr = None
        self._gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def communicate(self, stdin: bytes | None = None) -> tuple[bytes, bytes]:
        await self._gate
        return b"", b""

    async def wait(self) -> int:
        self.wait_calls += 1
        if not self.killed:
            await self._gate
        return self.returncode


def _fake_spawn(proc: FakeProcess, calls: list[dict[str, object]] | None = None):
    async def spawn(*args: object, **kwargs: object) -> FakeProcess:
        if calls is not None:
            calls.append(kwargs)
        return proc

    return mock.patch(_SPAWN, spawn)


async def test_run_returns_stdout_and_check_false_returns_stdout(repo) -> None:
    git = Git(repo.root)
    assert (await git.run("rev-parse", "--is-inside-work-tree")).strip() == "true"
    with pytest.raises(GitError) as exc_info:
        await git.run("rev-parse", "--verify", "does-not-exist")
    assert "does-not-exist" in str(exc_info.value)
    stdout = await git.run("rev-parse", "--verify", "does-not-exist", check=False)
    assert stdout == ""


async def test_run_timeout_kills_child(tmp_path: Path) -> None:
    git = Git(tmp_path, timeout=0.05)
    proc = FakeProcess()
    with _fake_spawn(proc):
        with pytest.raises(GitError, match="timed out"):
            await git.run("fetch")
    assert proc.killed
    assert proc.wait_calls >= 1


async def test_run_bounded_output_retains_prefix_and_reports_truncation(repo) -> None:
    payload = "START" + "x" * 200_000 + "END\n"
    repo.write("large.txt", payload)
    repo.commit_all("large output")
    git = Git(repo.root)

    output, truncated = await git.run("show", "HEAD:large.txt", max_output_bytes=1024)

    assert truncated
    assert len(output.encode("utf-8", "surrogateescape")) <= 1024
    assert output.startswith("START")


class NeverEOF:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release

    async def read(self, _size: int) -> bytes:
        await self.release.wait()
        return b""


class CloseableTransport:
    def __init__(self, release: asyncio.Event) -> None:
        self.release = release
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.release.set()


class HungPipeProcess(FakeProcess):
    def __init__(self, release: asyncio.Event) -> None:
        super().__init__()
        self.stdout = NeverEOF(release)
        self.stderr = NeverEOF(release)
        self._transport = CloseableTransport(release)
        self.wait_calls = 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        return self.returncode


async def test_stop_and_reap_bounds_never_eof_pipe_cleanup(monkeypatch) -> None:
    release = asyncio.Event()
    proc = HungPipeProcess(release)
    monkeypatch.setattr(runner_module, "_CLEANUP_TIMEOUT", 0.02, raising=False)

    async def release_eventually() -> None:
        await asyncio.sleep(0.2)
        release.set()

    releaser = asyncio.create_task(release_eventually())
    started = time.monotonic()
    await runner_module._stop_and_reap(proc)  # type: ignore[arg-type]
    elapsed = time.monotonic() - started
    releaser.cancel()
    await asyncio.gather(releaser, return_exceptions=True)

    assert elapsed < 0.12
    assert proc.killed
    assert proc._transport.closed


async def test_run_cancellation_kills_child_and_reraises(tmp_path: Path) -> None:
    git = Git(tmp_path)
    proc = FakeProcess()
    with _fake_spawn(proc):
        task = asyncio.create_task(git.run("fetch"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert proc.killed
    assert proc.wait_calls >= 1


async def test_run_streaming_timeout_kills_child(tmp_path: Path) -> None:
    git = Git(tmp_path, timeout=0.05)
    proc = FakeProcess()
    with _fake_spawn(proc):
        with pytest.raises(GitError, match="timed out"):
            await git.run_streaming("fetch")
    assert proc.killed
    assert proc.wait_calls >= 2  # gather's inner wait + the handler's reap


async def test_run_streaming_cancellation_kills_child_and_reraises(tmp_path: Path) -> None:
    git = Git(tmp_path)
    proc = FakeProcess()
    with _fake_spawn(proc):
        task = asyncio.create_task(git.run_streaming("fetch"))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert proc.killed
    assert proc.wait_calls >= 2  # gather's inner wait + the handler's reap


async def test_run_streaming_delivers_oversized_line_whole(repo) -> None:
    payload = "BEGIN:" + "x" * (120 * 1024) + ":END"
    repo.write("big.txt", payload + "\n")
    repo.commit_all("one very long line")
    git = Git(repo.root)
    seen: list[tuple[str, bool]] = []
    code, combined = await git.run_streaming(
        "show", "HEAD:big.txt", on_line=lambda text, is_err: seen.append((text, is_err))
    )
    assert code == 0
    assert seen == [(payload, False)]
    assert combined == payload


async def test_run_streaming_delivers_unterminated_tail(repo) -> None:
    repo.write("noeol.txt", "first\nunterminated-tail")
    repo.commit_all("no trailing newline")
    git = Git(repo.root)
    seen: list[str] = []
    code, combined = await git.run_streaming(
        "show", "HEAD:noeol.txt", on_line=lambda text, is_err: seen.append(text)
    )
    assert code == 0
    assert seen == ["first", "unterminated-tail"]
    assert combined == "first\nunterminated-tail"


async def test_run_streaming_empty_output(repo) -> None:
    git = Git(repo.root)
    seen: list[str] = []
    code, combined = await git.run_streaming(
        "status", "--porcelain", on_line=lambda text, is_err: seen.append(text)
    )
    assert code == 0
    assert seen == []
    assert combined == ""


async def test_discover_finds_repo_root(repo) -> None:
    sub = repo.root / "sub"
    sub.mkdir()
    git = await Git.discover(sub)
    assert git.repo_root == repo.root.resolve()


async def test_discover_fails_on_plain_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError):
        await Git.discover(plain)


async def test_discover_timeout_kills_child(tmp_path: Path) -> None:
    proc = FakeProcess()
    with _fake_spawn(proc):
        with pytest.raises(GitError, match="timed out"):
            await Git.discover(tmp_path, timeout=0.05)
    assert proc.killed
    assert proc.wait_calls >= 1


async def test_discover_cancellation_kills_child_and_reraises(tmp_path: Path) -> None:
    proc = FakeProcess()
    with _fake_spawn(proc):
        task = asyncio.create_task(Git.discover(tmp_path))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert proc.killed
    assert proc.wait_calls >= 1


@pytest.mark.parametrize("method", ["discover", "run", "streaming"])
async def test_git_process_launch_uses_platform_safe_group_options(
    tmp_path: Path, method: str
) -> None:
    proc = FakeProcess()
    spawn_calls: list[dict[str, object]] = []
    git = Git(tmp_path, timeout=0.02)
    with _fake_spawn(proc, spawn_calls):
        with pytest.raises(GitError):
            if method == "discover":
                await Git.discover(tmp_path, timeout=0.02)
            elif method == "run":
                await git.run("fetch")
            else:
                await git.run_streaming("fetch")

    assert len(spawn_calls) == 1
    assert spawn_calls[0].get("start_new_session", False) is (os.name == "posix")


async def test_kill_process_group_windows_taskkills_whole_tree(monkeypatch) -> None:
    """Without POSIX process groups, cleanup must terminate the whole
    descendant tree via ``taskkill /T /F`` — not only the direct child."""
    calls: list[list[str]] = []
    monkeypatch.setattr(runner_module.os, "name", "nt")
    monkeypatch.setattr(
        runner_module.subprocess, "run", lambda args, **kw: calls.append(list(args))
    )
    proc = FakeProcess()
    runner_module._kill_process_group(proc)
    assert calls == [["taskkill", "/T", "/F", "/PID", str(proc.pid)]]
    assert proc.killed  # belt-and-braces direct kill follows the tree kill


async def test_kill_process_group_windows_survives_taskkill_failure(monkeypatch) -> None:
    """A missing/broken taskkill must not skip the direct-kill fallback."""

    def broken(args: object, **kw: object) -> None:
        raise OSError("taskkill unavailable")

    monkeypatch.setattr(runner_module.os, "name", "nt")
    monkeypatch.setattr(runner_module.subprocess, "run", broken)
    proc = FakeProcess()
    runner_module._kill_process_group(proc)  # must not raise
    assert proc.killed


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/stat").exists(),
    reason="requires POSIX process groups and Linux /proc",
)
async def test_run_streaming_timeout_kills_ssh_helper_descendant(repo, tmp_path: Path) -> None:
    git = Git(repo.root, timeout=0.6)
    await git.run("remote", "add", "ssh-test", "ssh://example.invalid/repo.git")
    script = tmp_path / "fake-ssh"
    pid_file = tmp_path / "sleeper.pid"
    script.write_text('#!/bin/sh\nsleep 30 &\nprintf \'%s\' "$!" > "$SLEEPER_PID_FILE"\nwait\n')
    script.chmod(0o755)

    with pytest.raises(GitError, match="timed out"):
        await git.run_streaming(
            "ls-remote",
            "ssh-test",
            env_extra={"GIT_SSH_COMMAND": str(script), "SLEEPER_PID_FILE": str(pid_file)},
        )

    deadline = time.monotonic() + 2
    while not pid_file.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert pid_file.exists(), "fake SSH helper never started"
    pid = int(pid_file.read_text())
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            break
        if stat.rsplit(")", 1)[1].strip().split()[0] == "Z":
            break
        await asyncio.sleep(0.02)
    else:
        raise AssertionError(f"SSH helper descendant {pid} survived timeout")
