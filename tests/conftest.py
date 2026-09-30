"""Shared fixtures: real throwaway git repositories on disk."""

from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest


class RepoHelper:
    """Synchronous helper for arranging repository state in tests."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        if check and proc.returncode != 0:
            # The live app polls `git status`, which briefly takes
            # index.lock to refresh the stat cache; a sync helper call can
            # collide on slow runners. Retry instead of racing.
            if "index.lock" in (proc.stderr + proc.stdout):
                for _ in range(5):
                    time.sleep(0.2)
                    proc = subprocess.run(
                        ["git", *args],
                        cwd=self.root,
                        capture_output=True,
                        text=True,
                    )
                    if proc.returncode == 0:
                        break
            raise AssertionError(
                f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    def write(self, rel_path: str, content: str) -> Path:
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Byte-exact LF: Windows text mode would translate \n to \r\n,
        # storing CRLF blobs that rebuilt LF patches cannot apply against.
        target.write_text(content, newline="\n")
        return target

    def commit_all(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> RepoHelper:
    """A fresh repository with one initial commit on ``main``."""
    helper = RepoHelper(tmp_path)
    helper.git("init", "-b", "main")
    helper.git("config", "user.email", "test@example.com")
    helper.git("config", "user.name", "Test Snake")
    helper.git("config", "commit.gpgsign", "false")
    # Windows runners default core.autocrlf=true, which rewrites every
    # fixture file to CRLF and turns the tests' LF expectations into
    # line-ending-only diffs. Pin LF semantics everywhere.
    helper.git("config", "core.autocrlf", "false")
    helper.git("config", "core.eol", "lf")
    helper.write("README.md", "initial\n")
    helper.commit_all("initial commit")
    return helper


@pytest.fixture
def remote_repo(tmp_path: Path) -> RepoHelper:
    """A working repo wired to a local bare ``origin`` (real remote flows)."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    work = tmp_path / "work"
    work.mkdir()
    helper = RepoHelper(work)
    helper.git("init", "-b", "main")
    helper.git("config", "user.email", "test@example.com")
    helper.git("config", "user.name", "Test Snake")
    helper.git("config", "commit.gpgsign", "false")
    helper.git("config", "core.autocrlf", "false")
    helper.git("config", "core.eol", "lf")
    helper.write("README.md", "initial\n")
    helper.commit_all("initial commit")
    helper.git("remote", "add", "origin", str(origin))
    return helper


@pytest.fixture
def eventually():
    """Bounded wait for a condition that lands via async workers.

    CI runners are far slower than a dev machine: after a keypress, the
    action worker, its checkpoint subprocesses and the panel refresh can
    all still be in flight when a single ``pilot.pause()`` returns. Tests
    assert through this helper instead of sleeping a fixed amount, so
    fast machines finish immediately and slow ones get a real timeout
    with the last observed state in the failure.
    """

    async def _eventually(condition, timeout: float = 15.0, interval: float = 0.05):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if condition():
                    return True
            except Exception:
                pass
            await asyncio.sleep(interval)
        assert condition(), f"condition not reached within {timeout}s"

    return _eventually
