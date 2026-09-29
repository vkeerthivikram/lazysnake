"""Shared fixtures: real throwaway git repositories on disk."""

from __future__ import annotations

import subprocess
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
            raise AssertionError(
                f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    def write(self, rel_path: str, content: str) -> Path:
        target = self.root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
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
    helper.write("README.md", "initial\n")
    helper.commit_all("initial commit")
    helper.git("remote", "add", "origin", str(origin))
    return helper
